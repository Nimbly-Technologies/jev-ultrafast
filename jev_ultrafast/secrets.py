"""Values for secret fields come from the local environment, never from a model.

JEV_SECRETS is a JSON object mapping a case-insensitive substring of the field's label to its value:

    JEV_SECRETS='{"password": "..."}'

The value is read in-process and typed straight into the page. It is never placed in a prompt, a
model request, a decision trace, or the run's history.
"""

import json
import os

MASK = "(secret)"


def secret_for(label):
    try:
        mapping = json.loads(os.environ.get("JEV_SECRETS") or "{}")
    except ValueError:
        raise ValueError("JEV_SECRETS is not valid JSON") from None
    if not isinstance(mapping, dict):
        raise ValueError("JEV_SECRETS must be a JSON object of {field label: value}")
    for key, value in mapping.items():
        if key.lower() in label.lower() and isinstance(value, str) and value:
            return value
    raise ValueError(f"No secret configured for the field {label!r}; set JEV_SECRETS.")
