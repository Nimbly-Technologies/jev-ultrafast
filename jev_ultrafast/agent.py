"""The complete agent loop. Typed choices, observable state, bounded execution."""

import base64
import time
from pathlib import Path

from .browser import Browser, StalePage
from .model import action_space, choose, field_context, field_text
from .questions import MAX_STEPS, NEXT_ACTION, STEP
from .secrets import MASK, secret_for

# Operations that change nothing a user asked for: taking one never completes a single-action step.
PASSIVE = {"wait", "scroll"}


class BudgetExceeded(ValueError):
    """A step, model-call or wall-clock budget stopped the run. The run's status is already blocked."""


class Agent:
    """One browser page, driven toward natural-language goals.

    ``Agent(url, goal)`` opens its own window, as upstream. ``Agent(browser=b)`` drives a page someone else
    opened and leaves it open on close, so a caller can interleave goals with its own checks on one page.
    """

    def __init__(self, url=None, goals="", *, browser=None, record_dir=None, screenshots=False,
                 max_steps=MAX_STEPS, max_model_calls=None, timeout=None):
        task = goals.strip() if isinstance(goals, str) else "\n".join(goals).strip()
        if not task and browser is None:
            raise ValueError("Supply a task")
        if (url is None) == (browser is None):
            raise ValueError("Supply either a url or a browser")
        self.pending_text = None
        self.owns_browser = browser is None
        self.browser = browser or Browser(url)
        self.record_dir = Path(record_dir) if record_dir else None
        self.screenshots = screenshots or bool(record_dir)
        self.default_budget = {"max_steps": max_steps, "max_model_calls": max_model_calls, "timeout": timeout}
        self.budget = dict(self.default_budget)
        # Every goal this agent pursued, oldest first; the current goal's state is also in self.state.
        self.log = []
        try:
            page = self.browser.observe(screenshot=self.screenshots)
        except Exception:
            self.close()
            raise
        self.state = self._goal_state(task, page)
        if self.record_dir:
            self.record_dir.mkdir(parents=True, exist_ok=True)
            (self.record_dir / "000000.jpg").write_bytes(base64.b64decode(page["screenshot"]))

    def _goal_state(self, goal, page):
        return dict(
            browser=self.browser,
            goal=goal,
            page=page,
            decision=None,
            history=[],
            status="ready",
            reason=None,
            plan=[goal],
            plan_index=0,
            decisions=[],
            text_calls=[],
            elapsed_ms=0,
            started_at=None,
            record=bool(self.record_dir),
            rules=NEXT_ACTION,
        )

    def snapshot(self):
        return {
            **{k: v for k, v in self.state.items() if k != "browser"},
            "elements": action_space(self.state["page"]["actions"])[0],
        }

    def _stop(self, reason):
        self.state["status"] = "blocked"
        self.state["reason"] = reason
        raise BudgetExceeded(reason)

    def command(self, name, body=None):
        body = body or {}
        state = self.state
        budget = getattr(self, "budget", {"max_steps": MAX_STEPS})
        max_steps = budget.get("max_steps") or MAX_STEPS
        if name == "tick":
            try:
                self.command("predict", {})
                return self.command("act", {"fingerprint": state["page"]["fingerprint"]})
            except StalePage:
                state["decision"] = None
                state["status"] = "ready"
                state["page"] = state["browser"].observe(screenshot=self.screenshots)
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                return self.snapshot()
        elif name == "predict":
            if not state["browser"]:
                raise ValueError("Start a demo first")
            if state["started_at"] is None:
                state["started_at"] = time.perf_counter()
            if not state["browser"].fresh(state["page"]):
                state["page"] = state["browser"].observe(screenshot=self.screenshots)
            state["decision"] = None
            if state["status"] in {"done", "blocked"}:
                raise ValueError("This run has stopped. Start a fresh demo.")
            max_calls = budget.get("max_model_calls") or max_steps * 2
            if len(state["decisions"]) >= max_calls:
                self._stop(f"Reached the {max_calls}-call model budget")
            timeout = budget.get("timeout")
            if timeout and time.perf_counter() - state["started_at"] > timeout:
                self._stop(f"Reached the {timeout:g} s time budget")
            state["decision"] = choose(state["page"], state["goal"], state["history"], state.get("rules", NEXT_ACTION))
            state["decisions"].append(
                {
                    **state["decision"],
                    "fingerprint": state["page"]["fingerprint"],
                    "elapsed_ms": round((time.perf_counter() - state["started_at"]) * 1000),
                }
            )
            state["status"] = "predicted"
        elif name == "act":
            decision, page = state["decision"], state["page"]
            if not decision or body.get("fingerprint") != page["fingerprint"]:
                raise ValueError("Observe and choose before acting")
            # Consume once, before any mutation or model call. A retry cannot double-click.
            state["decision"] = None
            selected = decision["choice"]
            if selected in {"DONE", "BLOCKED"}:
                if not state["browser"].fresh(page):
                    state["status"] = "ready"
                    raise StalePage("Page changed since the decision. Choose again.")
                state["status"] = "done" if selected == "DONE" else "blocked"
                state["reason"] = f"Model chose {selected} (p={decision['probabilities'][selected]:.2f})"
                state["plan_index"] = int(selected == "DONE")
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                return self.snapshot()
            action = next(a for a in page["actions"] if a["id"] == selected)
            if len(state["history"]) >= max_steps:
                self._stop(f"Stopped at the {max_steps}-action budget")
            text, helper = None, None
            if action["kind"] == "fill" and action.get("secret"):
                # A secret is read locally and typed. No model sees it, and the trace records only a mask.
                text = secret_for(action["label"])
            elif action["kind"] == "fill":
                if not state["browser"].fresh(page):
                    raise StalePage("Page changed before text generation. Choose again.")
                context = field_context(state["goal"], action, page, state["history"])
                if self.pending_text and self.pending_text[0] == context:
                    _, text, helper = self.pending_text
                else:
                    text, helper = field_text(context)
                    self.pending_text = (context, text, helper)
                    state["text_calls"].append({**helper, "field": action["label"], "value": text})
            # Browser.act checks freshness immediately before input, including after text generation.
            state["browser"].act(action, page, text=text)
            self.pending_text = None
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
            # Record execution before observing. A stale post-action observation must not erase the action.
            state["history"].append(
                {
                    "step": len(state["history"]) + 1,
                    "action": action["label"],
                    "kind": action["kind"],
                    "choice": selected,
                    "probability": decision["probabilities"][selected],
                    "confidence": decision["confidence"],
                    "latency_ms": decision["latency_ms"],
                    "text": MASK if action.get("secret") else text,
                    "text_helper": helper["model"] if helper else None,
                    "text_latency_ms": helper["latency_ms"] if helper else 0,
                    "operation": decision["operation"],
                    "target": decision["target"],
                    "page_changed": None,
                    "url": page["url"],
                    "usage": decision["usage"],
                    "executed_ms": round((time.perf_counter() - state["started_at"]) * 1000),
                    "elapsed_ms": state["elapsed_ms"],
                }
            )
            state["page"] = state["browser"].observe(screenshot=self.screenshots)
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
            state["history"][-1].update(
                page_changed=state["page"]["fingerprint"] != page["fingerprint"],
                url=state["page"]["url"],
                elapsed_ms=state["elapsed_ms"],
            )
            if state["record"]:
                (self.record_dir / f"{state['elapsed_ms']:06d}.jpg").write_bytes(
                    base64.b64decode(state["page"]["screenshot"])
                )
            repeated = state["history"][-3:]
            if len(repeated) == 3 and all(h["page_changed"] is False and h["kind"] != "wait" for h in repeated):
                state["status"] = "blocked"
                state["reason"] = "Three actions in a row changed nothing"
            else:
                state["status"] = "ready"
        else:
            raise ValueError("Unknown command")
        return self.snapshot()

    def run(self):
        while self.state["status"] not in {"done", "blocked"}:
            yield self.command("tick")

    def _begin(self, goal, budget):
        goal = goal.strip()
        if not goal:
            raise ValueError("Supply a goal")
        if self.state["started_at"] is not None or self.state["history"]:
            self.log.append(self.outcome())
        page = self.browser.observe(screenshot=self.screenshots)
        self.pending_text = None
        self.state = self._goal_state(goal, page)
        self.budget = {**self.default_budget, **{k: v for k, v in budget.items() if v is not None}}

    def pursue(self, goal, *, max_steps=None, max_model_calls=None, timeout=None):
        """Work toward one goal on the current page until DONE, BLOCKED or a budget stops it.

        Returns the outcome; never raises for a blocked run. The page is left as the goal left it, so the
        caller can check it independently. A DONE choice is not proof of success.
        """
        self._begin(goal, {"max_steps": max_steps, "max_model_calls": max_model_calls, "timeout": timeout})
        try:
            for _ in self.run():
                pass
        except BudgetExceeded:
            pass
        return self.outcome()

    def act(self, instruction, *, max_model_calls=4, timeout=None):
        """Execute exactly one action that the instruction asks for, then return.

        Waiting and scrolling do not count as that action; they are taken and the choice is made again.
        A DONE choice executes nothing: the model judged the instruction already satisfied. Nothing is
        ever repeated, so a click cannot be issued twice.
        """
        self._begin(instruction, {"max_steps": max_model_calls, "max_model_calls": max_model_calls,
                                  "timeout": timeout})
        # A caller-chosen step is framed as one: the multi-step rules (fill every required field before
        # submitting, BLOCKED when the goal cannot progress) made the policy refuse a plainly visible target.
        self.state["rules"] = STEP
        state = self.state
        try:
            while state["status"] not in {"done", "blocked"}:
                self.command("tick")
                state = self.state
                last = state["history"][-1] if state["history"] else None
                if state["status"] == "ready" and last and last["kind"] not in PASSIVE:
                    state["status"] = "done"
                    state["reason"] = f"Executed {last['operation']} on {last['action']!r}"
        except BudgetExceeded:
            pass
        return self.outcome()

    def outcome(self):
        state = self.state
        history = state["history"]
        return {
            "goal": state["goal"],
            "status": state["status"],
            "reason": state["reason"],
            "url": state["page"]["url"],
            "elapsed_ms": state["elapsed_ms"],
            "actions": len(history),
            "model_calls": len(state["decisions"]),
            "text_calls": len(state["text_calls"]),
            "usage": usage(state),
            "diagnostics": diagnose(history),
            "history": history,
            "decisions": [
                {k: d.get(k) for k in ("choice", "operation", "target", "confidence", "probabilities",
                                       "latency_ms", "elapsed_ms", "usage")}
                for d in state["decisions"]
            ],
            "text_helper": [{k: v for k, v in c.items() if k != "value"} for c in state["text_calls"]],
        }

    def close(self):
        if self.owns_browser:
            self.browser.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def usage(state):
    typesafe = [d.get("usage") or {} for d in state["decisions"]]
    helper = [c.get("usage") or {} for c in state["text_calls"]]
    return {
        "typesafe_calls": len(typesafe),
        "typesafe_input_tokens": sum(u.get("input_tokens", 0) for u in typesafe),
        "typesafe_output_tokens": sum(u.get("output_tokens", 0) for u in typesafe),
        "text_calls": len(helper),
        "text_prompt_tokens": sum(u.get("prompt_tokens", 0) for u in helper),
        "text_completion_tokens": sum(u.get("completion_tokens", 0) for u in helper),
        # OpenRouter reports each call's own cost; other providers leave it out.
        "text_cost_usd": round(sum(u.get("cost", 0) or 0 for u in helper), 8),
    }


def diagnose(history):
    """Signs of wandering, so a flaky run is diagnosable rather than mysterious. Reported, never enforced."""
    seen, repeats, revisits, urls = {}, [], [], []
    for h in history:
        if h["kind"] in PASSIVE:
            continue
        key = (h["kind"], h["action"])
        seen[key] = seen.get(key, 0) + 1
        if seen[key] == 2:
            repeats.append({"action": h["action"], "kind": h["kind"], "first_repeat_step": h["step"]})
        url = h.get("url")
        if url and urls and url != urls[-1] and url in urls:
            revisits.append({"url": url, "step": h["step"]})
        if url and (not urls or url != urls[-1]):
            urls.append(url)
    return {
        "repeated_actions": repeats,
        "url_revisits": revisits,
        "no_change_actions": [h["step"] for h in history if h.get("page_changed") is False
                              and h["kind"] not in PASSIVE],
        "waits": sum(h["kind"] == "wait" for h in history),
    }
