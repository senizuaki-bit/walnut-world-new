# INT1 cross-repository E2E authority seed

`walnut_backend.int1_e2e_authority` is a one-shot, explicitly enabled test seeder. Run it only against a PostgreSQL database that has just completed `alembic upgrade head`, before starting the API or any worker.

This is the only allowed INT1/INT2 formal-E2E precondition writer. It belongs to `walnut-world-backend`, uses the production HS256 authenticator, and does not create a second Gateway or database authority. The current Alembic head is `019_int2_skill_patch_authority`, whose parent is `018_world_presentation_events`.

The seeder first runs the Backend-owned `scripts/verify_contract_release.py` against `contract-release.json` and the configured Agent repository. It refuses an Agent manifest or any manifested file that differs from the Backend pin.

It inserts exactly seven preconditions:

- one published `ProductContentUnit` containing the canonical Task and compilable C++20 starter Skill;
- one revision/sequence-zero `WorldSnapshot` with eight mature, harvest-ready tilled plots;
- one `LearnerProfile` and one `AgentProfile` whose provider/model/prompt are non-secret environment identifiers;
- one `BuildPolicy` with the digest-pinned GCC image and PUBLIC/HIDDEN exact-output tests;
- one active `LaunchAuthority`;
- one revision-zero `RegistryHead`.

The starter accepts the harvest loop length as its only process argument, reads no stdin, and emits at most eight closed HARVEST intents. The default INT1 Turn uses length `8`, actor entity `avatar_0001`, expected World revision `0`, and unique intents for `plot_0001` through `plot_0008`. Before writing the database, the seeder applies the seven- and eight-intent boundary through the Backend `WorldEngine` and pinned `WorldRules`; it requires score `7` with `success=false`, then score `8` with `success=true`, at the pinned success threshold `8`.

The seeder creates no Command, Session, Draft, Build, Artifact, Certification, Activation, Run, Evidence, Interaction, Event, Outbox, or worker Job. Its artifact root must be empty before, during, and after the transaction. Any existing business row, migration drift, canonical-hash drift, contract-release drift, or non-empty artifact root causes a refusal; existing state is never overwritten or repaired.

## Run

Every variable below must be set explicitly. `WALNUT_LLM_PROVIDER`, `WALNUT_LLM_MODEL`, and `WALNUT_PROMPT_VERSION` are identifiers, not credentials. The seeder never reads `WALNUT_LLM_RELAY_API_KEY`.

```powershell
$env:WALNUT_INT1_E2E_SEED = 'true'
$env:WALNUT_DATABASE_URL = 'postgresql://postgres:postgres@127.0.0.1:55432/walnut_int1'
$agentRepo = if (Test-Path '.\agent\contracts\manifest.json') { '.\agent' } else { '..\agent' }
$env:WALNUT_CONTRACT_PATH = (Resolve-Path $agentRepo).Path
$env:WALNUT_RUNTIME_ROOT = [IO.Path]::GetFullPath((Join-Path (Get-Location) 'tmp\int1-runtime'))
$env:WALNUT_DEVELOPMENT_AUTH = 'false'
$env:WALNUT_AUTH_HMAC_SECRET = '<32-or-more-character-test-secret>'
$env:WALNUT_AUTH_ISSUER = 'walnut-int1'
$env:WALNUT_AUTH_AUDIENCE = 'walnut-game-client'
$env:WALNUT_SANDBOX_IMAGE = 'gcc@sha256:b99b86a28812b1e6453a231a947dc43d76fe192788a12f344a9b568bf9f5d24c'
$env:WALNUT_LLM_PROVIDER = 'deepseek'
$env:WALNUT_LLM_MODEL = 'deepseek-v4-flash'
$env:WALNUT_PROMPT_VERSION = 'int1-prompt-v1'
$env:WALNUT_TEACHING_SPEC_VERSION = 'agent-teaching-v1'
$env:WALNUT_WORLD_RULES_VERSION = 'farm-rules-1'
$env:WALNUT_WORLD_SUCCESS_SCORE = '8'

python -m alembic upgrade head
python -m walnut_backend.int1_e2e_authority
```

The command emits one JSON line. Its `authorization` field is an exact 30-minute student HS256 bearer JWT accepted by the same production `JwtAuthenticator` used by Compose. This lifetime is confined to the explicitly enabled INT1 E2E seed; the seeder refuses to issue a shorter token when `WALNUT_AUTH_MAXIMUM_LIFETIME_SECONDS` is below `1800`. The local harness additionally requires `2 * TotalDeadlineSeconds + 300 <= 1800`, covering both client phases plus the outage/restart transition without changing the general JWT defaults. The HMAC secret is never persisted, printed, included in a subprocess argument, or exposed by configuration/result representations. Pass the emitted authorization value only to the cross-process client; rerun from a fresh database if it expires.

Start the API and workers with the same database, contract path, runtime root, sandbox image, JWT issuer/audience/secret, recoverable relay, provider/model/prompt, TeachingSpec, and WorldRules identifiers used for the seed.

The seeder does not by itself prove a live Provider path. If the real recoverable relay endpoint/key or Provider/model identifiers are absent, that particular **real-Provider** cross-process Godot run remains **NOT RUN / NOT PASS**; this does not invalidate the separately classified deterministic cross-process gate. Current INT2 live authority instead comes from 2026-08-15 run `868a`, which supplied the real Provider configuration and passed. A plain fake/scripted OpenAI-compatible chat endpoint does not implement response-loss reconciliation and is rejected. A local fixture relay may prove deterministic capability/PUT/GET, outage, and restart behavior, but it must not be recorded as live `source=provider, degraded=false` acceptance evidence.

## Verification

Run the focused tests with:

```powershell
python -m pytest -q tests/unit/test_int1_e2e_authority.py
$env:WALNUT_TEST_DATABASE_URL = 'postgresql://postgres:postgres@127.0.0.1:55432/postgres'
python -m pytest -q tests/integration/test_int1_e2e_authority_seed.py
```

The integration test creates and drops only a randomly named `walnut_int1_<20 hex>` scratch database. It migrates from zero, seeds once, verifies every SQLAlchemy business-table count and the empty artifact root, proves the production JWT through `JwtAuthenticator`, reads the public student bootstrap, introduces drift, and proves a second seed is refused without mutation.
