# INT2 Agent full non-live red diagnostic

Date: 2026-08-14 (Asia/Shanghai)

Evidence class: current full non-live red diagnostic. This is not a PASS and
is not real Provider evidence.

Command:

```powershell
.\.venv\Scripts\python.exe scripts\run-non-live-python-tests.py
```

The runner discovered the two explicitly billable live Provider tests and
reported both as `EXCLUDED_NOT_RUN`; neither test was executed or counted as a
skip. The non-live suite then ran 595 tests and ended with 31 errors and 5
failures.

The common root cause is the production startup verifier
`python/yaya_agent_backend/composition.py::verify_contract_manifest`. It still
derives the frozen v0.3 entry count directly from the current manifest after
excluding only the v0.4 additions. The additive v0.5/v0.6 files therefore make
the valid 147-entry v0.6 candidate fail with
`v0.3 frozen manifest entry count drifted` before the service or worker can
start. The five startup timeouts are downstream consequences of that same
fail-fast startup rejection, not five independent timeout defects.

The verifier must be repaired without changing any manifest or historical
release-lock bytes. It must strictly self-verify the current manifest and
independently validate the frozen v0.3, v0.4, and v0.5 release locks, their
inventories, counts, byte identities, and entry digests. Focused tests must
also prove that regenerating the current manifest cannot legitimize drift in
an older release or its lock.

After the focused repair is green, the complete non-live gate must be rerun.
Until that rerun passes with zero errors, failures, or skips and the same two
live tests remain `EXCLUDED_NOT_RUN`, Agent full non-live status is
`NOT_PASS / RED`.
