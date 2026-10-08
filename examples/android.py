"""Drive an Android phone: uv run --extra android python examples/android.py SERIAL "goal" ["next goal" ...]"""

import json
import sys
import time

from jev_ultrafast import Agent
from jev_ultrafast.android import AndroidDevice

serial, goals = sys.argv[1], sys.argv[2:]
phone = AndroidDevice(serial)
started = time.perf_counter()
page = phone.observe()
print(f"observe: {(time.perf_counter() - started) * 1000:.0f} ms, {len(page['actions'])} actions", flush=True)
agent = Agent(browser=phone)
for goal in goals:
    out = agent.pursue(goal, max_steps=20, max_model_calls=30)
    print(json.dumps({"goal": goal, "status": out["status"], "reason": out["reason"], "elapsed_ms": out["elapsed_ms"],
                      "model_calls": out["model_calls"],
                      "steps": [(h["action"], h["text"], h["executed_ms"]) for h in out["history"]]}), flush=True)
    if out["status"] != "done":
        sys.exit(1)
