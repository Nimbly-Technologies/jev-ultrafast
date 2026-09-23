"""Bounded Android candidate selection through Jev's existing action policy.

The mobile adapter deliberately turns an accessibility hierarchy into the same
indexed action space used by the browser agent. It does not add a second
model call or allow model output to become a selector or a coordinate.
"""

from __future__ import annotations

import math
import re
from typing import Any, Callable

from . import model

MAX_MOBILE_CANDIDATES = 32
MAX_LABEL_LENGTH = 120
MIN_TARGET_CONFIDENCE = 0.70
MIN_TARGET_MARGIN = 0.15
MOBILE_MODEL_TIMEOUT_SECONDS = 3.0
MOBILE_MODEL_RETRIES = 0

_EMAIL = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
_BEARER = re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+")
_SECRET_KEY = re.compile(r"(?i)(?:password|passwd|secret|token|api[_ -]?key|authorization)")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(?:password|passwd|secret|token|api[_ -]?key|authorization)\b\s*[:=]\s*[^\s,;]+"
)
_SECRET_VALUE = re.compile(
    r"(?i)^(?:password|passwd|secret|token|api[_ -]?key|authorization)[A-Za-z0-9_.:/=-]{2,}$"
)
_SAFE_SECRET_LABEL = re.compile(
    r"(?i)^(?:(?:enter|type|input|confirm|current|new|your|forgot|show|hide)\s+)?"
    r"(?:password|passcode|secret|token|api(?:\s+key)?|authorization)"
    r"(?:\s+(?:field|input|button|here))?$"
)
_LONG_SECRET = re.compile(r"\b[A-Za-z0-9_-]{32,}\b")


class MobileHealingRefused(ValueError):
    """The policy response is valid JSON but is unsafe to execute."""


def redact(value: Any, *, sensitive: bool = False, limit: int = MAX_LABEL_LENGTH) -> str:
    """Return short display text while retaining safe semantic field labels."""

    text = "" if value is None else str(value)
    normalized = " ".join(text.split())
    if sensitive and not _SAFE_SECRET_LABEL.fullmatch(normalized):
        return "[redacted]"
    if _SECRET_VALUE.fullmatch(normalized):
        return "[redacted]"
    text = _SECRET_ASSIGNMENT.sub("[redacted]", normalized)
    text = _EMAIL.sub("[redacted-email]", text)
    text = _BEARER.sub("[redacted-token]", text)
    text = _LONG_SECRET.sub("[redacted-token]", text)
    if len(text) > limit:
        return text[: limit - 1] + "..."
    return text


def safe_selector(selector: Any) -> str:
    """Describe the failed selector without copying secret values into the prompt."""

    if isinstance(selector, dict):
        parts = []
        for key in ("text", "textRegex", "id", "idRegex", "description"):
            if key in selector and selector[key] is not None:
                is_id = key.lower() in {"id", "idregex"}
                parts.append(f"{key}={redact(selector[key], sensitive=is_id)}")
        return redact(", ".join(parts) or "element selector")
    return redact(selector or "element selector")


def _candidate_label(candidate: dict[str, Any]) -> str:
    role = str(candidate.get("role") or "")
    is_text_input = any(marker in role.lower() for marker in ("edittext", "textfield", "textbox", "input"))
    sensitive = bool(candidate.get("sensitive")) or bool(_SECRET_KEY.search(role)) or is_text_input
    for key in ("label", "text", "contentDescription", "accessibilityText", "hintText", "resourceId", "className"):
        value = candidate.get(key)
        if value:
            is_password_hint = key == "hintText" and "password" in role.lower()
            return redact(value, sensitive=sensitive or is_password_hint)
    return "[unlabeled control]"


def _action(candidate: dict[str, Any], index: int) -> dict[str, Any]:
    candidate_id = str(candidate.get("id") or f"c{index + 1}")
    role = str(candidate.get("role") or "")
    value_is_text_input = any(marker in role.lower() for marker in ("edittext", "textfield", "textbox", "input"))
    value_is_sensitive = bool(candidate.get("sensitive")) or bool(_SECRET_KEY.search(role)) or value_is_text_input
    return {
        "id": candidate_id,
        "kind": "click",
        "label": _candidate_label(candidate),
        "role": redact(role, limit=60),
        "value": redact(candidate.get("value"), sensitive=value_is_sensitive, limit=80),
        "bounds": redact(candidate.get("bounds"), limit=80),
        "node": candidate_id,
        "enabled": True,
        "visible": True,
    }


def mobile_state(selector: Any, candidates: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Build the policy state from enabled visible controls in bounded order."""

    bounded = [
        candidate
        for candidate in candidates
        if bool(candidate.get("enabled", True)) and bool(candidate.get("visible", True))
    ][:MAX_MOBILE_CANDIDATES]
    actions = [_action(candidate, index) for index, candidate in enumerate(bounded)]
    return (
        {
            "url": "android://accessibility",
            "title": "Android accessibility hierarchy",
            "text": "Visible Android accessibility controls from the current screen.",
            "actions": actions,
        },
        actions,
    )


def _choose_mobile_with_bound(state: dict[str, Any], goal: str, history: list[dict[str, Any]]) -> dict[str, Any]:
    """Reuse Jev's normal policy with a single bounded provider attempt."""

    return model.choose(
        state,
        goal,
        history,
        request_timeout=MOBILE_MODEL_TIMEOUT_SECONDS,
        retries=MOBILE_MODEL_RETRIES,
    )


def choose_mobile_candidate(
    selector: Any,
    candidates: list[dict[str, Any]],
    *,
    chooser: Callable[[dict[str, Any], str, list[dict[str, Any]]], dict[str, Any]] | None = None,
    min_confidence: float = MIN_TARGET_CONFIDENCE,
    min_margin: float = MIN_TARGET_MARGIN,
) -> dict[str, Any]:
    """Use Jev's normal operation/target policy to choose one safe candidate."""

    if not candidates:
        raise MobileHealingRefused("No visible candidates were available")
    if not 0 <= min_confidence <= 1 or not 0 <= min_margin <= 1:
        raise ValueError("Invalid mobile healing thresholds")

    state, actions = mobile_state(selector, candidates)
    if not actions:
        raise MobileHealingRefused("No enabled visible candidates were available")
    goal = (
        f"Recover a failed Android tap for {safe_selector(selector)}. "
        "Choose the single visible control that best matches the original selector. "
        "Use CLICK only; a text field may be clicked when the failed step intended to focus it; "
        "never choose a disabled control."
    )
    decision = (chooser or _choose_mobile_with_bound)(state, goal, [])
    selected = decision.get("choice")
    if decision.get("operation") != "CLICK" or selected not in {action["id"] for action in actions}:
        raise MobileHealingRefused("Jev did not return one offered CLICK candidate")

    probabilities = decision.get("probabilities") or {}
    selected_probability = probabilities.get(selected)
    try:
        scores = sorted((float(value) for value in probabilities.values()), reverse=True)
    except (TypeError, ValueError):
        raise MobileHealingRefused("Jev returned invalid candidate confidence") from None
    if selected_probability is None or len(scores) < 1:
        raise MobileHealingRefused("Jev returned no candidate confidence")
    try:
        selected_probability = float(selected_probability)
        confidence = float(decision.get("target_confidence", decision.get("confidence", 0.0)))
        operation_confidence = float(
            decision.get("operation_confidence", decision.get("operationConfidence", decision.get("confidence", 0.0)))
        )
    except (TypeError, ValueError):
        raise MobileHealingRefused("Jev returned invalid candidate confidence") from None
    if not all(
        math.isfinite(value) and 0 <= value <= 1
        for value in (selected_probability, confidence, operation_confidence)
    ):
        raise MobileHealingRefused("Jev returned non-finite candidate confidence")
    margin = selected_probability - (scores[1] if len(scores) > 1 else 0.0)
    if (
        operation_confidence < min_confidence
        or confidence < min_confidence
        or selected_probability < min_confidence
        or margin < min_margin
    ):
        raise MobileHealingRefused("Jev candidate was ambiguous or below the confidence bound")

    return {
        "candidateId": selected,
        "confidence": confidence,
        "operationConfidence": operation_confidence,
        "probability": selected_probability,
        "margin": margin,
        "model": decision.get("model"),
        "latency_ms": decision.get("latency_ms"),
    }
