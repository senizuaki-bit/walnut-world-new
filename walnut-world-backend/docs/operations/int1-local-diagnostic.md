# INT1 deterministic local diagnostic

`scripts/run-int1-local-diagnostic.ps1` is a reproducible wiring diagnostic for the three sibling
repositories. It is intentionally classified as
`DETERMINISTIC_LOCAL_RELAY_ONLY_NOT_REAL_PROVIDER`; a PASS from this script is never evidence for
the separate real-Provider acceptance gate.

The harness starts only private localhost processes, creates a fresh disposable PostgreSQL 16.9
container from an exact digest-pinned image and a run-unique Docker volume, migrates and seeds the
fixed INT1 authority, starts the one Gateway,
combined workflow worker, and dedicated learner worker, and runs the official Godot 4.5.2
real-Gateway runner. It uses
`scripts/int1_recoverable_relay.py` as an in-memory fixture for
`YAYA_RECOVERABLE_LLM_V1`.

The fixture deliberately materializes the first stable dispatch and then drops its PUT
acknowledgement. It also makes the first reconciliation temporarily unavailable. The workflow job
must release its claim, retry with a higher attempt/fencing token, reconcile the same dispatch, and
finish with `generation_count=1`. The final structured fingerprint includes the stable identities,
Draft and World revisions, last event sequence, content hashes, Sandbox receipt count, database
side-effect counts, and a SHA-256 over that authority tuple.

With `-EnableSkillPatch`, Command acceptance is an exact closed equation rather than a lower
bound: one Session + two Builds + two Activations + six Turns = 11 terminal Commands. Four failed
Turns are `REJECTED`, so the other seven Commands are `APPLIED`, and all 11 Commands have one
durable command receipt each. Without Skill Patch the same equation remains one + two + two +
four = 9 terminal Commands, partitioned as six `APPLIED` and three `REJECTED`, with nine receipts.

The deterministic harness also exercises a real temporary database outage before that process
boundary. After all business work and learner projections are terminal, it stops the exact
PostgreSQL container and proves the published database port is closed while the Gateway,
workflow worker, and learner worker remain alive. A database-backed Gateway GET must fail closed.
The harness then starts the same container identity, waits for PostgreSQL health, requires an
exact World Snapshot GET, and observes a fresh PostgreSQL TCP connection from each of the three
service processes. The database, fixture-relay, Sandbox, and Artifact fingerprints must remain
byte-equivalent. This stop/start gate is intentionally excluded from billable real-Provider mode;
its structured classification is `DETERMINISTIC_POSTGRES_STOP_START_RECOVERY`.

The current harness also contains an explicit two-process recovery acceptance. After the first
formal Godot process reaches the terminal Interaction, the harness fingerprints the complete
PostgreSQL authority, private-relay generation set, and every persistent Sandbox/Artifact file.
The PostgreSQL fingerprint uses deterministic full-row JSON for every Command, workflow and
step receipt, Interaction, LearnerProfile, and durable learner-projection job. It also closes over
every domain event and event-stream head, including the non-World event streams used for
Agent feedback and `LEARNER_MODEL_UPDATED`. The learner job material therefore includes its exact
identity, immutable objective, result/error, attempt/fence, lease, and timestamps rather than only
its terminal status. Relay statistics remain the Provider-generation authority; in real-Provider
mode the sanitized relay-table material additionally covers every scalar, stored hash, timestamp,
failure field, and body length without printing prompt or response bytes.
It then force-stops the Gateway, combined workflow worker, and dedicated learner worker, proves
that port 8790 has no listener or Backend Gateway process, and restarts all three under new PIDs
and new worker identities. A second Godot OS process opens the same hash-scoped `user://` state
and runs AppRoot in recovery-only mode. It may recover Session, Workspace, Draft, active Skill
tuple, World, Interaction, and formal UI state, but it never creates a Session or invokes Draft,
Build, Activation, or Turn writes. This is verified from the restarted Gateway access log: at
least the eight canonical recovery GETs must be present, while any method other than GET, HEAD, or
OPTIONS fails the run. The before/after database, Provider, Sandbox, and Artifact fingerprints must
be byte-equivalent. After those comparisons, all three restarted services must still be alive and
the Gateway must still be the sole process owning the single loopback listener.

The test state lifecycle is intentionally narrow: phase 1 removes only
`user://real_gateway_chain_<base-url-hash>.json`, not the application user-data directory; phase 2
removes that exact file only after all recovery assertions and fingerprint capture. The file is
retained between the two Godot processes. A failed run may leave that one file for inspection, and
the next phase-1 run removes it before accepting any result.

Run from the Backend repository:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\run-int1-local-diagnostic.ps1
```

The script requires these local inputs and never downloads them implicitly:

- the sibling `agent` and `walnut-world-frontend` workspaces;
- the Backend `.venv`;
- the pinned Godot 4.5.2 console executable under the shared `tools` directory (or `-GodotExe`);
- the exact local images
  `postgres:16.9-alpine@sha256:7c688148e5e156d0e86df7ba8ae5a05a2386aaec1e2ad8e6d11bdf10504b1fb7`
  and
  `gcc@sha256:b99b86a28812b1e6453a231a947dc43d76fe192788a12f344a9b568bf9f5d24c`;
- enough free physical memory and disk for PostgreSQL, Docker compilation, the Gateway, worker,
  and headless Godot.

Before any container is started, the harness requires both image arguments to match the closed
`name@sha256:<64 lowercase hex>` form and emits `INT1_LOCAL_DIAGNOSTIC_PREFLIGHT`. That object
retains the exact `postgres_image` and `sandbox_image` strings, their digest-format verdicts, and
their local-presence verdicts. A floating/malformed image, missing local image, or failed resource
threshold emits `INT1_LOCAL_DIAGNOSTIC_NOT_LIVE` and exits 2 without pulling or starting a
container. It also requires the contract-declared plaintext loopback Gateway port
`127.0.0.1:8790` to be free and zero existing `walnut_backend.main:app` host processes. Existing
unrelated Docker containers
may remain running only when none conflicts on the selected ports, owned name, or ownership label,
but the harness captures their full-ID/running-state/canonical-inspect baseline before it creates
anything and must restore that baseline byte-for-byte. Its disposable PostgreSQL is the only new
container used by the harness, and task-owned resources are labelled with the run identity. The
production Godot transport deliberately rejects plaintext HTTP on any other port.
It does not label a partial or fixture-only check as a live PASS. A successful full run emits
exactly one `INT1_LOCAL_DIAGNOSTIC_PASS` JSON object. The run log directory is retained in the
system temp directory for inspection; the exact disposable PostgreSQL container is removed first,
then its run-unique data volume is removed, and the pre-existing Docker baseline must still match
exactly.

## Current formal deterministic M2 evidence

On 2026-08-15, the current-tree run with `-EnableWorldPresentation -EnableSkillPatch` completed in
**270.638 seconds** and emitted exactly one `INT1_LOCAL_DIAGNOSTIC_PASS` with classification
`DETERMINISTIC_LOCAL_RELAY_ONLY_NOT_REAL_PROVIDER`. The outer wrapper exited 0; its stdout SHA-256
is `90442f1f1171a6014f4025241bb71d3c7afc1d5b3e64499eccb30460dd3640dc`.

The formal chain closed six Turns, five Runs, six Product Interactions, five learner projections,
and 13 Evidence rows. The fixture relay and durable Provider receipts closed 16 unique dispatches
and 16 generations, with one generation per dispatch. Command acceptance matched the exact
equation one Session + two Builds + two Activations + six Turns = 11 terminal Commands: seven were
`APPLIED`, four were `REJECTED`, and all 11 had durable command receipts. The deterministic
full-row PostgreSQL authority SHA-256 was
`a37d5c503d136396d0e4fe0f0f7f13594e6dc632c9095d2ae20b6a101b14e13a`.

World authority closed at revision 1 with one committed World domain event and
`last_event_sequence=1`; that is intentionally separate from the eight authoritative presentation
events at `presentation_high_watermark=8`. The student-visible Patch chain closed with status
`PUBLIC_UI_CHAIN_CLOSED` and public-chain SHA-256
`102dcec526ca0ffd088cf5f465b3bcaab0af1e97fe0b60980f4833084fe63fff`.

The same disposable PostgreSQL container was unavailable on its published port for 3,785 ms. The
Gateway stayed listening, its database-backed GET failed closed with HTTP 500, and the exact World
Snapshot recovered after the same container restarted. Relay, database, Sandbox, and Artifact
fingerprints remained unchanged across that outage. The later three-service PID restart had no
Gateway listener between phases; the second Godot process performed exactly 17 GETs and 0
mutations, and the same four fingerprints remained unchanged. Cleanup removed the task-owned
container, volume, and persisted Godot state and restored the three-container pre-existing Docker
baseline exactly, including canonical bytes with SHA-256
`e8c9e96d0995266d9852e85dff86a77cf60fc70300fdf430f9fcf09f0156a84f`.

This is authoritative deterministic fixture-relay/host-Docker evidence. It used no real Provider
credential and remains independently reproducible from the live gate. It does not prove production
private DinD or public-Gateway pending write response-loss.

## Current formal real-Provider M2 evidence

On 2026-08-15, current-tree real-Provider run `868a` completed in **301.012 seconds** and passed the
same six-Turn M2 public chain. The private relay recorded 18 unique Provider dispatches, 18 total
generations, and a maximum of one generation per dispatch. The injected response-loss recovered the
same dispatch at generation one. The outer stdout SHA-256 is
`2A7D2C057EF66D54F4DBFA828166DAC0A688704471618E6FA2940CE4F95B2425`; the database SHA-256 is
`b8bb2b568ac6978d938a98d041f9c5b74ef108167f53790c4bbeecbb6c051e30`.

The student-visible Patch chain closed as `PUBLIC_UI_CHAIN_CLOSED`. World authority recorded one
commit and eight distinct presentation events. The exact 11 terminal Commands comprised seven
`APPLIED` and four `REJECTED`; phase 2 performed 17 GETs and 0 mutations after restart. Cleanup
removed every task-owned resource and restored the exact three-container pre-existing Docker
baseline.

This is the current controlled real-Provider M2 PASS. It uses digest-pinned host Docker and the
private-relay fault proxy; it is not production private-DinD live evidence. The proxy proves the
private Provider relay PUT/GET response-loss path, not public-Gateway pending write response-loss,
which remains `NOT_PROVEN`.

## Earlier deterministic records (historical)

The following 2026-08-12 UTC / 2026-08-13 CST execution was a historical
**`DETERMINISTIC_LOCAL_RELAY_ONLY_NOT_REAL_PROVIDER` PASS**. Its successful inner preflight recorded:

> This historical 79.764-second PASS predates the PostgreSQL stop/start gate described above. It
> remains valid for the older restart and response-loss scope. The current evidence is the later
> 106.867-second run described below.

- `free_memory_bytes=2221518848`;
- `free_disk_bytes=47646638080`;
- zero running containers and no residual Backend/Godot process;
- the requested PostgreSQL and GCC images present (this historical preflight retained presence
  booleans, not their exact image strings);
- `127.0.0.1:8790` available;
- Provider secrets cleared from the deterministic child environment.

The earlier full diagnostic completed in **79.764 seconds**. It started from a fresh authority seed and
closed Draft revisions `1 -> 2 -> 3`, Workspace revisions `1 -> 2 -> 9 -> 12`, two Builds,
Certifications and Activations, and four exact-version Turns. Command status was
`REJECTED/REJECTED/REJECTED/APPLIED`; Run status was
`REJECTED/REJECTED/REJECTED/SUCCEEDED`; the formal Agent role sequence was
`teaching_agent/teaching_agent/bug_agent/book_agent`. Each Turn produced exactly one Run and one
Product Interaction (`run_count=4`, `interaction_count=4`); Backend also closed 11 Evidence rows,
four Learner projections with learner revision 4, HTTP Events/Snapshot authority, and the
TaskWorkspace, DialoguePanel, and WorldViewport UI panels.

The injected response-loss fingerprint recorded `put_ack_drops=1`,
`forced_reconcile_unavailable=1`, `reconcile_gets=2`, `turn_job_attempt=2`,
`turn_job_fencing_token=2`, `worker_reconcile_receipts=1`, `worker_failure_receipts=0`, and
`generation_count_max=1`. The relay recorded `relay_unique_dispatches=12` and
`relay_total_generations=12`; all 12 Provider dispatch/result receipts closed without a repeated
generation. The authority tuple has
`side_effect_sha256=86e4c940b2fbff564c970ea87830be00a9c5942696194b205939405181726469`.

On 2026-08-13 the extended deterministic diagnostic completed in **106.867 seconds** and emitted
one `INT1_LOCAL_DIAGNOSTIC_PASS`. In addition to the same four-Turn and restart closure, it stopped
and restarted the exact disposable PostgreSQL container while retaining its run-unique volume.
The published database port closed for 3,608 ms; the Gateway remained listening and returned the
expected fail-closed 500 during the outage; Gateway, workflow worker, and learner worker each
established a new PostgreSQL connection after restart. The DB, relay, Sandbox, and Artifact
fingerprints were byte-identical before and after the outage, and the later process-restart phase
again performed 8 GET / 0 mutation with all four fingerprints unchanged. This remains classified
`DETERMINISTIC_LOCAL_RELAY_ONLY_NOT_REAL_PROVIDER`; it is not a real-Provider PASS.

After phase1, the harness force-stopped Gateway, workflow worker and learner worker, verified no
Gateway listener between phases, then started all three with new PIDs and worker identities. An
independent recovery-only Godot process issued exactly 8 GET requests and 0 mutations. The stable
before/after fingerprints were byte-equivalent:

- relay side effects: `dd565a3ad002a0c11db2a098dd0d074f7e5cd93996af5d3580bbbe926ac77661`;
- database authority: `45f85c322f1bbb9cc95e97219af579a83176ce2d6deacbdb559b82879c28956a`;
- Sandbox authority: `5b1692ff07f3256730e10545f1f195d490c17cff313645948dc165fcacfa9b19`;
- Artifact authority: `8e534551a9d79904e983b1a2784f5e5804efc33fcaa72457d4dc3ecb8e6df43e`.

The relay capability counter changed from 1 to 2 by the expected single phase2 startup probe; it
is excluded from the stable side-effect fingerprint and did not create a Provider generation.
These are deterministic fixture-relay results and do not satisfy or alter the separate real-Provider
acceptance gate. The earlier 33.159-second single-Turn record remains only a historical
pre-full-restart diagnostic; it is not evidence
that the new two-process restart gate has executed and is not the current result.

The relay credential is randomly generated in memory, passed only through child-process
environment variables, never accepted on the command line, never included in relay statistics,
and never printed or written by the fixture. The authority Bearer token is likewise kept out of
the structured output and logs produced by the harness.

The separate billable gate, `scripts/run-int1-real-provider-e2e.ps1`, accepts the upstream
Provider credential from exactly one process variable:
`WALNUT_LLM_UPSTREAM_API_KEY` or `WALNUT_LLM_UPSTREAM_API_KEY_FILE`. On Windows, the file source
must resolve to a regular, non-reparse file no larger than 4098 bytes. Its DACL must not grant
`ReadData` to Everyone (`S-1-1-0`), Authenticated Users (`S-1-5-11`), or the built-in Users group
(`S-1-5-32-545`). The DACL must be present and non-null; an absent/null DACL or any inability to
read and prove the descriptor fails the gate closed. The file is decoded
as strict UTF-8 and may contain only one bounded, non-whitespace secret with an optional final
line ending. Keep the file outside the repositories and configure its DACL before running the
gate. The harness resolves the source once, injects the value only while starting the private
relay, immediately removes both upstream-key variables from the parent process environment, and
repeats that narrow injection only for the deliberate relay restart. Gateway, workflow worker,
learner worker, Godot, process arguments, logs, statistics, and structured results never receive
the upstream credential. The independently generated relay bearer remains a separate secret.

The billable wrapper alone sets `WALNUT_LLM_RELAY_MAX_TOTAL_GENERATIONS` for the private relay: 24
for the four-Turn M1 chain and 32 when `-EnableSkillPatch` selects the six-Turn M2 chain. The
real-mode harness refuses any other or missing value. PostgreSQL serializes both new dispatch
reservations and final generation claims under that explicit budget, so it survives the deliberate
relay restart. The first dispatch beyond the selected limit returns `GENERATION_LIMIT_EXCEEDED`
before any Provider POST, and the claim boundary independently fails closed if an over-budget
pending row already exists. With the variable absent, the general relay and production Compose
behavior retain their uncapped default.

Real-Provider mode places `scripts/int1_real_provider_fault_proxy.py` on a separate loopback port
between the workflow client and that private relay. The proxy receives only the independent relay
bearer and refuses to start if either upstream Provider-key variable is present. For the first
dispatch it forwards the immutable PUT, polls the private relay until the same dispatch is durably
terminal with `generation_count=1`, and only then closes the client socket without a response. It
returns 503 for exactly the first external reconciliation GET and forwards the next GET to the same
terminal resource. Its authenticated statistics contain only the fault dispatch ID, bounded
counters, terminal state/generation, and same-dispatch recovery booleans—never request, prompt,
response, or credential bytes. The private relay is deliberately restarted behind the still-running
proxy. The real gate requires one acknowledgement drop, one forced unavailable reconciliation,
same-dispatch generation-one recovery, at least one workflow reconciliation receipt, zero
`WORKER_FAILURE` receipts, and byte-identical private-relay/proxy authority across phase 2. This
fault was proven by both the historical 2026-08-13 INT1 result below and current INT2 run `868a`;
the current M2 evidence is recorded above.

## Historical INT1 real-Provider evidence

The historical unique successful INT1 billable run completed in **194.12 seconds** with classification
`REAL_PROVIDER_PRIVATE_DURABLE_RELAY`. DeepSeek V4 Flash returned `source=provider` and
`degraded=false`; the relay recorded 13 unique dispatches, 13 total generations, and a maximum of
one generation per dispatch. The controlled PUT acknowledgement loss occurred once, the forced
reconciliation-unavailable fault was attempted once and delivered once, and recovery used the same
dispatch with generation count one.

The formal Godot chain closed four Turns/Runs/Interactions/Learner projections (three rejected and
one succeeded), two Builds/Certifications/Activations, nine terminal Commands, eleven Evidence
rows, eight Sandbox receipts, and two Artifact files. Gateway, workflow worker, and learner worker
restarted with new PIDs; recovery-only traffic was 8 GETs and 0 mutations. Relay, database,
Sandbox, Artifact, and response-loss-proxy fingerprints were unchanged. The outer evidence root is
the versioned wrapper output identified by the sanitized stdout hash in the INT1 report;
its `stdout.log` SHA-256 is
`2ea58a686b641820855ba4424994d5fbecd783f969cadfbd8c5c6c7d815bdbda`, and `stderr.log` is empty.
Cleanup left zero running Docker containers and no listener on `127.0.0.1:8790`.

This was digest-pinned host-Docker historical INT1 live evidence, not current INT2 or production
private-DinD live evidence. Its formal Godot fingerprint still records public-Gateway pending write
response loss as `NOT_PROVEN`; the historical live fault was the private Provider relay PUT/GET
recovery path.

Validate the fixture protocol and script contract without running the heavy chain:

```powershell
.\.venv\Scripts\python.exe -m pytest -q `
  tests\unit\test_int1_recoverable_relay_fixture.py `
  tests\unit\test_int1_real_provider_fault_proxy.py `
  tests\contract\test_int1_local_diagnostic_harness.py
```

These tests cover the actual recoverable relay adapter, lost-PUT-ack reconciliation, exact replay,
conflict handling, one-generation bounds, credential hygiene, PowerShell syntax, fresh PostgreSQL
topology, persistent Sandbox result authority, official Godot runner selection, and explicit
not-real-Provider labelling.

## Historical deterministic evidence

After the binding, database-classification, response-loss observability, and Windows Docker
authority-probe closures, the recorded deterministic diagnostic completed in **169.836 seconds**.
It closed four formal Godot Turns (`REJECTED/REJECTED/REJECTED/APPLIED` Commands,
`REJECTED/REJECTED/REJECTED/SUCCEEDED` Runs, teaching/teaching/bug/book), one terminal PUT ACK loss,
one unavailable reconciliation, and one generation per each of 12 dispatches. The same disposable
PostgreSQL container was stopped and restarted with its published port unavailable for 4,784 ms;
Gateway stayed listening and failed closed, and Gateway/workflow/learner each established a new
database connection. The later three-service PID restart and recovery-only Godot issued 8 GETs and
0 mutations. DB, relay, Sandbox, and Artifact fingerprints were unchanged across both recovery
boundaries. The recorded `side_effect_sha256` was
`9d9e770a6bf8f9f03fc351c50a3fba2dd3d57971df91237d46f9e49c3335ab05`. Its classification was
`DETERMINISTIC_LOCAL_RELAY_ONLY_NOT_REAL_PROVIDER`; this was not real-Provider acceptance.
This 169.836-second record predates the exact image strings and digest-format verdicts now added to
the preflight object, as well as the real-only 12-generation pre-billing fuse and its PostgreSQL
integration coverage. It is historical evidence only: it must not be cited as a current-tree PASS or
as evidence that either new gate has run.
