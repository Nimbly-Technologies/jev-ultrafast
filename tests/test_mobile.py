"""Offline contracts for the Android Jev adapter."""

import json

import pytest

from jev_ultrafast import mobile
from jev_ultrafast.mobile import MobileHealingRefused


def candidates(count=2):
    return [
        {
            "label": "Sign in",
            "role": "android.widget.Button",
            "resourceId": "com.example:id/sign_in",
            "enabled": True,
            "visible": True,
        },
        {
            "label": "auto-user@example.com",
            "role": "android.widget.EditText",
            "value": "nimbly123",
            "enabled": True,
            "visible": True,
            "sensitive": True,
        },
    ] * (count // 2) + ([
        {
            "label": "Continue",
            "role": "android.widget.Button",
            "enabled": True,
            "visible": True,
        }
    ] if count % 2 else [])


def decision(ids, selected="c1", confidence=0.95, operation_confidence=None):
    probabilities = {candidate_id: 0.0 for candidate_id in ids}
    probabilities[selected] = confidence
    remaining = [candidate_id for candidate_id in ids if candidate_id != selected]
    if remaining:
        probabilities[remaining[0]] = 1 - confidence
    return {
        "choice": selected,
        "operation": "CLICK",
        "confidence": confidence,
        "operation_confidence": confidence if operation_confidence is None else operation_confidence,
        "target_confidence": confidence,
        "probabilities": probabilities,
        "model": "offline-test",
        "latency_ms": 1,
    }


def test_mobile_policy_reuses_jev_action_selection_and_redacts_secrets():
    seen = {}

    def chooser(state, goal, history):
        seen.update(state=state, goal=goal, history=history)
        ids = [action["id"] for action in state["actions"]]
        return decision(ids)

    selected = mobile.choose_mobile_candidate(
        {"textRegex": "auto-user@example.com", "idRegex": "password_token"},
        candidates(),
        chooser=chooser,
    )

    assert selected["candidateId"] == "c1"
    assert seen["history"] == []
    assert "auto-user@example.com" not in json.dumps(seen)
    assert "password_token" not in json.dumps(seen)
    assert "nimbly123" not in json.dumps(seen)
    assert "[redacted-email]" in seen["goal"]


def test_mobile_policy_bounds_candidate_list():
    seen = {}

    def chooser(state, _goal, _history):
        seen["count"] = len(state["actions"])
        return decision([action["id"] for action in state["actions"]])

    mobile.choose_mobile_candidate("Login", candidates(40), chooser=chooser)
    assert seen["count"] == mobile.MAX_MOBILE_CANDIDATES


def test_mobile_policy_refuses_ambiguous_target():
    def chooser(state, _goal, _history):
        ids = [action["id"] for action in state["actions"]]
        return decision(ids, confidence=0.56)

    with pytest.raises(MobileHealingRefused, match="ambiguous"):
        mobile.choose_mobile_candidate("Login", candidates(), chooser=chooser)


def test_http_payload_does_not_return_model_request(monkeypatch):
    monkeypatch.setattr(
        "jev_ultrafast.mobile_adapter.choose_mobile_candidate",
        lambda _selector, _candidates: {
            "candidateId": "c1",
            "confidence": 0.9,
            "operationConfidence": 0.9,
            "probability": 0.9,
            "margin": 0.8,
        },
    )
    from jev_ultrafast.mobile_adapter import heal_payload

    result = heal_payload({"selector": "Login", "candidates": candidates()})
    assert result == {
        "ok": True,
        "candidateId": "c1",
        "confidence": 0.9,
        "operationConfidence": 0.9,
        "probability": 0.9,
        "margin": 0.8,
    }


def test_mobile_policy_ignores_disabled_and_invisible_candidates():
    seen = {}

    def chooser(state, _goal, _history):
        seen["ids"] = [action["id"] for action in state["actions"]]
        return decision(seen["ids"], selected=seen["ids"][0])

    selected = mobile.choose_mobile_candidate(
        "Login",
        [
            {"id": "c1", "label": "Hidden", "visible": False},
            {"id": "c2", "label": "Disabled", "enabled": False},
            {"id": "c3", "label": "Sign in", "visible": True, "enabled": True},
        ],
        chooser=chooser,
    )

    assert selected["candidateId"] == "c3"
    assert seen["ids"] == ["c3"]


def test_mobile_policy_uses_one_bounded_jev_request(monkeypatch):
    seen = {}

    def choose(state, _goal, _history, **options):
        seen["options"] = options
        return decision([action["id"] for action in state["actions"]])

    monkeypatch.setattr(mobile.model, "choose", choose)
    mobile.choose_mobile_candidate("Login", candidates())

    assert seen["options"] == {
        "request_timeout": mobile.MOBILE_MODEL_TIMEOUT_SECONDS,
        "retries": mobile.MOBILE_MODEL_RETRIES,
    }


def test_mobile_policy_refuses_low_operation_confidence_even_for_strong_target():
    def chooser(state, _goal, _history):
        ids = [action["id"] for action in state["actions"]]
        return decision(ids, confidence=0.95, operation_confidence=0.45)

    with pytest.raises(MobileHealingRefused, match="confidence"):
        mobile.choose_mobile_candidate("Login", candidates(), chooser=chooser)


def test_mobile_policy_keeps_password_semantic_label_but_redacts_value():
    seen = {}

    def chooser(state, _goal, _history):
        seen["state"] = state
        ids = [action["id"] for action in state["actions"]]
        return decision(ids, selected="c2")

    mobile.choose_mobile_candidate(
        "Login",
        [
            {
                "id": "c1",
                "label": "Enter password",
                "role": "android.widget.EditText",
                "value": "correct-horse-battery-staple",
                "enabled": True,
                "visible": True,
            },
            {
                "id": "c2",
                "label": "Continue",
                "role": "android.widget.Button",
                "enabled": True,
                "visible": True,
            },
        ],
        chooser=chooser,
    )

    encoded = json.dumps(seen["state"])
    assert "Enter password" in encoded
    assert "correct-horse-battery-staple" not in encoded
