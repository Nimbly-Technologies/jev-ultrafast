# Android Maestro healing adapter

`jev-mobile-adapter` exposes Jev's existing TypeSafe operation and target policy through a loopback HTTP endpoint for the Nimbly Maestro fork. It is a small local bridge: Maestro remains responsible for Android hierarchy reads, candidate validation, and the eventual tap. Jev only selects one offered candidate.

Start it from this worktree:

```bash
uv sync
uv run jev-mobile-adapter
```

The default endpoint is `http://127.0.0.1:8767/v1/mobile/heal`; `--host` accepts only loopback addresses and the server rejects request bodies over 64 KiB or candidate lists over 32 entries. Each mobile policy request makes one provider attempt with a 3 s model timeout and no retry; Maestro bounds the complete local request to 4 s. The adapter does not log request bodies, hierarchy labels, selectors, or model responses. Model credentials are read from the normal Jev environment and never belong in a Maestro flow.

The Maestro flow must opt in with a top-level `jevHealing` block. A failed plain Android element `tapOn` sends the redacted original selector and current enabled, visible candidates with sensitive values removed. Operation or target confidence below 0.70, a target probability margin below 0.15, a non-click operation, an unknown candidate, or a provider failure is refused. Maestro then keeps the original lookup error. A successful decision is executed as exactly one regular element tap and is recorded as sanitized command metadata.

This pilot deliberately excludes assertions, text entry, point or relative taps, long presses, repeats, retry-on-no-change, wait-until-visible, and any action that could already have changed app state. The adapter tests are offline; a live Jev model call is not part of the test suite.
