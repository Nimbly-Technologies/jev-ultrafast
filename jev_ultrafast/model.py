"""TypeSafe makes choices; an optional small OpenAI-compatible model writes field values."""

import json
import math
import os
import time

import httpx

from .questions import NEXT_ACTION, TARGET, TEXT_VALUE

CLIENT = httpx.Client(http2=True, timeout=25)


# Worth asking again: nothing has executed while a model call is in flight, so a retry cannot repeat an action.
RETRY_STATUSES = {408, 429} | set(range(500, 600))
ATTEMPTS = 4


class NoFieldValue(ValueError):
    """The text helper found no value for the field in the goal. Nothing was typed."""


def post_json(url, key, body):
    for attempt in range(ATTEMPTS):
        last = attempt == ATTEMPTS - 1
        try:
            response = CLIENT.post(url, json=body, headers={"Authorization": f"Bearer {key}"})
        except httpx.HTTPError:
            if last:
                raise RuntimeError("Model connection failed; no action executed.") from None
            time.sleep(0.5 * 2**attempt)
            continue
        if response.status_code in RETRY_STATUSES and not last:
            time.sleep(0.5 * 2**attempt)
            continue
        if response.is_error:
            raise RuntimeError(f"Model provider returned HTTP {response.status_code}; no action executed.")
        return response.json()
    raise RuntimeError("Model unavailable")


def validate_choice(answer, ids):
    try:
        probabilities = answer["probabilities"]
        numbers = [*probabilities.values(), answer["confidence"]]
        valid = (
            answer["choice"] in ids
            and set(probabilities) == set(ids)
            and all(type(n) in (int, float) and math.isfinite(n) and 0 <= n <= 1 for n in numbers)
            and abs(sum(probabilities.values()) - 1) < 0.02
            and probabilities[answer["choice"]] >= max(probabilities.values()) - 1e-6
        )
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("Invalid TypeSafe response; no action executed.")
    return answer


def action_space(actions):
    """One index per observed element; each operation has its own valid target choices."""
    elements, indices, targets, controls = [], {}, {}, {}
    operations = {"click": "CLICK", "fill": "TYPE_TEXT", "select": "SELECT"}
    for action in actions:
        kind = action["kind"]
        if kind not in operations:
            controls[action["id"].upper()] = action
            continue
        node = action["node"]
        if node not in indices:
            index = str(len(elements) + 1)
            indices[node] = index
            keys = ("role", "value", "checked", "selected", "expanded", "secret", "offscreen")
            element = {k: action[k] for k in keys if k in action}
            element.update(index=index, label=action["label"].split(" → ")[0], operations=[])
            if kind == "select":
                element["value"] = action.get("current_value", "")
                element["options"] = []
            elements.append(element)
        index = indices[node]
        operation = operations[kind]
        group = targets.setdefault(operation, {})
        element = elements[int(index) - 1]
        if operation not in element["operations"]:
            element["operations"].append(operation)
        target = index
        if kind == "select":
            target = f"{index}:{len(element['options']) + 1}"
            element["options"].append({"index": target, "label": action["label"], "value": action["value"]})
        group[target] = action
    return elements, targets, controls


def read_answers(result, operations, targets):
    """Validate the operation head, then only the target head that operation selects."""
    answers = result.get("answers") if isinstance(result, dict) else None
    if not isinstance(answers, dict):
        raise ValueError("Invalid TypeSafe response; no action executed.")
    operation_answer = validate_choice(answers.get("operation", {}), operations)
    operation = operation_answer["choice"]
    if operation not in targets:
        return operation_answer, None
    # Unused target heads cannot cause an action. Validate the head selected by the operation.
    return operation_answer, validate_choice(answers.get(operation.lower() + "_target", {}), targets[operation])


def choose(state, goal, history, rules=NEXT_ACTION):
    """One operation and its target. `rules` frames the goal: NEXT_ACTION for a multi-step goal, STEP for a
    single explicit instruction."""
    elements, targets, controls = action_space(state["actions"])
    labels = {
        "CLICK": "Click an element, button, menu option, autocomplete suggestion, or calendar day.",
        "TYPE_TEXT": (
            "Enter or replace text in an editable field. A small LLM will supply the value from the goal. "
            "An element marked secret is filled from local secure storage when it has an entry there, so "
            "choose it even though the goal never states its value."
        ),
        "SELECT": "Select an observed dropdown value.",
    }
    operations = {key: labels[key] for key in targets}
    operations.update({key: value["label"] for key, value in controls.items()})
    operations.update(DONE="Every requirement is visibly satisfied.", BLOCKED="No supported operation can progress.")
    questions = {
        "operation": {"type": "choice", "criteria": operations, "instructions": {"goal": goal, "rules": rules}}
    }
    for operation, candidates in targets.items():
        questions[operation.lower() + "_target"] = {
            "type": "choice",
            "criteria": {
                index: {
                    "element": f"[{index}] {a['label']}",
                    "current_value": a.get("current_value", a.get("value", "")),
                    **{k: a[k] for k in ("role", "checked", "selected", "expanded", "secret", "offscreen") if k in a},
                }
                for index, a in candidates.items()
            },
            "instructions": {"goal": goal, "operation": operation, "rules": [rules, TARGET]},
        }
    body = {
        "model": os.environ.get("TYPESAFE_MODEL", "jev-latest"),
        "state": {
            "page": {k: state[k] for k in ("url", "title", "text")},
            "elements": elements,
            "recent_actions": [
                {k: h.get(k) for k in ("action", "kind", "text", "page_changed")} for h in history[-10:]
            ],
        },
        "questions": questions,
    }
    started = time.perf_counter()
    spent = []
    for attempt in range(2):
        result = post_json("https://api.typesafe.ai/v1/systemone", os.environ["TYPESAFE_API_KEY"], body)
        spent.append(result.get("usage", {}) if isinstance(result, dict) else {})
        try:
            operation_answer, target_answer = read_answers(result, operations, targets)
            break
        except ValueError:
            # One retry: nothing has executed, so asking again cannot repeat a browser mutation.
            if attempt:
                raise
    operation = operation_answer["choice"]
    target = None
    probabilities = {}
    if target_answer:
        target = target_answer["choice"]
        choice = targets[operation][target]["id"]
        probabilities = {a["id"]: target_answer["probabilities"][index] for index, a in targets[operation].items()}
    else:
        choice = controls[operation]["id"] if operation in controls else operation
        probabilities[choice] = operation_answer["probabilities"][operation]
    return {
        "choice": choice,
        "operation": operation,
        "target": target,
        "confidence": operation_answer["confidence"],
        "probabilities": probabilities,
        "operation_probabilities": operation_answer["probabilities"],
        "target_probabilities": target_answer["probabilities"] if target_answer else {},
        "target_confidence": target_answer["confidence"] if target_answer else None,
        "raw_answers": result["answers"],
        "model": result["model"],
        # A retried request is billed too, so usage covers every attempt.
        "usage": {k: sum(u.get(k, 0) for u in spent) for k in {k for u in spent for k in u}},
        "attempts": len(spent),
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "request": body,
    }


def field_context(goal, action, page, history):
    return {
        "goal": goal,
        "field": {k: action.get(k) for k in ("label", "role", "value")},
        "page": {"title": page["title"], "text": page["text"][:6000]},
        "recent_actions": [{k: h.get(k) for k in ("action", "text")} for h in history[-6:]],
    }


def parse_field_value(result):
    """A valid helper answer is a JSON object with exactly one string key, text."""
    try:
        output = json.loads(result["choices"][0]["message"]["content"])
        value = output["text"]
        if set(output) != {"text"} or not isinstance(value, str) or not value.strip() or len(value) > 2000:
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise NoFieldValue("Text helper returned no valid field value; nothing typed.") from None
    return value


def field_text(context):
    key = os.environ.get("TEXT_MODEL_API_KEY")
    if not key:
        raise ValueError("TYPE_TEXT needs TEXT_MODEL_API_KEY; no text is hardcoded or guessed by the executor.")
    base = os.environ.get("TEXT_MODEL_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
    model = os.environ.get("TEXT_MODEL", "deepseek-chat")
    reasoning = {"thinking": {"type": "disabled"}} if "api.deepseek.com/" in base else {"reasoning": {"effort": "low"}}
    if os.environ.get("TEXT_MODEL_REASONING") == "none":
        reasoning = {"reasoning": {"enabled": False}}
    request = {
        "model": model,
        "max_tokens": 1024,
        "response_format": {"type": "json_object"},
        **reasoning,
        # OpenRouter reports each call's own cost only when asked.
        **({"usage": {"include": True}} if "openrouter.ai" in base else {}),
        "messages": [
            {"role": "system", "content": TEXT_VALUE},
            {"role": "user", "content": json.dumps(context)},
        ],
    }
    started = time.perf_counter()
    result = post_json(base + "/chat/completions", key, request)
    try:
        value = parse_field_value(result)
    except ValueError:
        # One retry: nothing was typed, so no browser state depends on the discarded answer.
        result = post_json(base + "/chat/completions", key, request)
        value = parse_field_value(result)
    return value, {
        "model": model,
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "usage": result.get("usage", {}),
    }
