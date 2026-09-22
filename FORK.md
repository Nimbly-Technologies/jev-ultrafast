# Nimbly fork of jev-ultrafast

`nimbly` is this fork's default branch. `main` mirrors
[browser-use/jev-ultrafast](https://github.com/browser-use/jev-ultrafast) and is never committed to, so a
rebase is `git fetch upstream && git rebase upstream/main nimbly`. Upstream's `AGENTS.md` rules still apply:
no site-specific plans, no hardcoded field values, never retry a browser mutation, tests stay offline.

The fork exists to drive the Nimbly web-admin QA suite (`Nimbly-Technologies/qa`, `web-admin-jev/`).
Everything site-specific lives there, not here.

## What diverges, and why

| Change | Why | Offered upstream |
| --- | --- | --- |
| Raw CDP client (`cdp.py`) replaces browser-harness | One WebSocket, no daemon; optional SSH tunnel (`CDP_SSH`) to a remote Chrome | no, a design choice upstream may not want |
| Agent owns a window, not a background tab | An occluded tab gets ~2 animation frames/s (0 when headless), freezing menus mid-fade | with the CDP change |
| Password fields observable, values masked; `JEV_SECRETS` vault | Stock jev cannot log in anywhere; the secret never reaches a model, trace or history | yes |
| `WAIT` waits for a real page change (`JEV_WAIT_TIMEOUT`, 3 s) | A 100 ms tick burned one model call per tick on a slow redirect, then the policy wandered | yes |
| One retry for an unparseable text-helper value | Aborted whole runs; nothing is typed yet | yes |
| One retry for an invalid TypeSafe answer | Same failure on the choice head; nothing has executed yet | yes |
| `Agent(browser=...)`, `pursue(goal)`, `act(instruction)` | A test interleaves goals with its own assertions on one page; `act` executes exactly one action, like Stagehand's `act()` | maybe |
| Step, model-call and wall-clock budgets; stop reasons; `diagnose()` | A wandering run must not spend unbounded money, and a flake must say why it stopped | maybe |
| `Browser.goto/run/click_at/press/insert_text/screenshot`, `isolated_context()` | The deterministic escape hatch a test uses for steps it already knows, and per-test isolation | no |
| `JEV_VIEWPORT` | The QA suite runs at 1280x800, as its Stagehand predecessor did | no |

## API added for tests

```python
from jev_ultrafast import Agent
from jev_ultrafast.browser import Browser, isolated_context

page = Browser("https://example.com")            # own window, default (shared) context
agent = Agent(browser=page)                       # attaches; close() leaves the page open
agent.act("Click the Add Site button")            # exactly one action; waits/scrolls do not count
outcome = agent.pursue("Open the newest report")  # until DONE / BLOCKED / budget; never raises for blocked
outcome["status"], outcome["reason"], outcome["usage"], outcome["diagnostics"]
page.run("(sel) => document.querySelector(sel).textContent", "h1")  # deterministic JS
page.close()                                      # also closes any window the page opened
```

`act()` returning `status == "done"` with `actions == 0` means the model judged the instruction already
satisfied and executed nothing. `diagnose()` reports repeated actions, URL revisits and no-change actions;
it never changes a run's outcome.
