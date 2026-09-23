# Nimbly fork of jev-ultrafast

`nimbly` is this fork's default branch. `main` mirrors
[browser-use/jev-ultrafast](https://github.com/browser-use/jev-ultrafast) and is never committed to, so a
rebase is `git fetch upstream && git rebase upstream/main nimbly`. Upstream's `AGENTS.md` rules still apply:
no site-specific plans, no hardcoded field values, never retry a browser mutation, tests stay offline.

The fork exists to drive the Nimbly web-admin QA suite (`Nimbly-Technologies/qa`, `web-admin-jev/`).
Everything site-specific lives there, not here.

## What diverges, and why

Each change has an offline unit test or a browser guard check (`scripts/check_guards.py`, 31 checks).

### Offered upstream

| Change | Why | Upstream |
| --- | --- | --- |
| Password fields observable, values masked everywhere the page is serialised; `JEV_SECRETS` vault matched on the exact label (ignoring case and a trailing `*` or `:`); a password field with no stored entry takes its value from the goal; a field with no value anywhere stops the run as blocked, naming the likely cause | Stock jev cannot log in anywhere; the secret never reaches a model, trace or history, and `Password` never fills `Confirm password` | [#123](https://github.com/browser-use/jev-ultrafast/pull/123) |
| `WAIT` returns once the page has changed and the network is quiet (`JEV_WAIT_TIMEOUT` 3 s of quiet, `JEV_WAIT_MAX` 15 s cap); in-flight fetch/XHR and a started navigation count as busy; limits read when a WAIT runs | A 100 ms tick burned one model call per tick on a slow redirect, then the policy wandered | [#124](https://github.com/browser-use/jev-ultrafast/pull/124) |

### Not offered (a design choice upstream may not want, or already proposed there by others)

| Change | Why |
| --- | --- |
| Raw CDP client (`cdp.py`) replaces browser-harness; the agent owns a window, not a background tab | One WebSocket, no daemon, optional SSH tunnel (`CDP_SSH`); an occluded tab gets ~2 animation frames/s (0 headless), freezing menus mid-fade |
| `Agent(browser=...)`, `pursue(goal)`, `act(instruction)`; `act()` frames its instruction with single-step rules (`STEP`) | A test interleaves goals with its own checks on one page; under the multi-step rules the policy refused a plainly visible Save button because other required fields "looked empty" |
| Step, model-call and wall-clock budgets; stop reasons; `diagnose()` | A wandering run must not spend unbounded money, and a flake must say why it stopped |
| A step that quotes a value for a password field types that value, not the stored secret | Creating a user must never hand it the QA account's own password |
| Retries: an invalid TypeSafe answer or unparseable text-helper value once; 408, 429, every 5xx and dropped connections up to 4 times with backoff | Nothing has executed while a model call is in flight; a Cloudflare 520 failed two CI steps (upstream [#73](https://github.com/browser-use/jev-ultrafast/pull/73) covers some of this) |
| A refused target (covered by a toast, moved, page navigating) waits for the page to change before the next model call | The same refused choice burned a 6-call budget in 2-3 s |
| Scripted clickable elements: the outermost `cursor: pointer` element, and anything with an `onclick` property (React sets one for every `onClick`), observed as buttons; a scripted menu nested in its trigger split into its items | Cards, tiles, avatars, section headers and hover-menu items were invisible (upstream [#22](https://github.com/browser-use/jev-ultrafast/pull/22)/[#24](https://github.com/browser-use/jev-ultrafast/pull/24) cover part of this) |
| Off-screen controls offered (marked `offscreen`, at most 80, nearest first) and scrolled to the middle before input; pages that scroll a container, not the window, are scrollable | Stagehand reaches any target; a Save button above the fold was BLOCKED |
| A select-style combobox (react-select) reports the choice it displays; a pixel-wide input is pressed through its visible container | The model saw every chosen dropdown as empty, and the 2 px input could not be hit |
| Open dialogs' text is read first | A long table behind a modal crowded the dialog out of the 6000-character budget |
| `Browser.goto/run/click_at/press/insert_text/screenshot`, `isolated_context()`, `JEV_VIEWPORT` | The deterministic escape hatch a test uses for steps it already knows, per-test isolation, the suite's 1280x800 viewport |

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

Upstream has not merged an outside pull request yet (only maintainer commits through #30), so everything
above is carried here until it does.
