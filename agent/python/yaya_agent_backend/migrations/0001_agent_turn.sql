CREATE TABLE yaya_tasks (
    tenant_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    content_hash CHAR(64) NOT NULL,
    snapshot_json JSONB NOT NULL,
    PRIMARY KEY (tenant_id, task_id),
    UNIQUE (tenant_id, task_id, actor_id, content_hash)
);

CREATE TABLE yaya_worlds (
    tenant_id TEXT NOT NULL,
    world_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    content_hash CHAR(64) NOT NULL,
    stream_id TEXT NOT NULL,
    revision BIGINT NOT NULL CHECK (revision >= 0),
    last_event_sequence BIGINT NOT NULL CHECK (last_event_sequence >= 0),
    state_hash CHAR(64) NOT NULL,
    world_rules_version TEXT NOT NULL,
    state_json JSONB NOT NULL,
    request_context_json JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, world_id),
    UNIQUE (tenant_id, stream_id),
    UNIQUE (tenant_id, world_id, actor_id, content_hash)
);

CREATE TABLE yaya_agent_sessions (
    tenant_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    world_id TEXT NOT NULL,
    content_hash CHAR(64) NOT NULL,
    snapshot_json JSONB NOT NULL,
    client_turn_sequence BIGINT NOT NULL DEFAULT 0 CHECK (client_turn_sequence >= 0),
    PRIMARY KEY (tenant_id, session_id),
    UNIQUE (tenant_id, session_id, actor_id, content_hash),
    UNIQUE (tenant_id, session_id, world_id, actor_id, content_hash),
    FOREIGN KEY (tenant_id, task_id, actor_id, content_hash)
        REFERENCES yaya_tasks (tenant_id, task_id, actor_id, content_hash),
    FOREIGN KEY (tenant_id, world_id, actor_id, content_hash)
        REFERENCES yaya_worlds (tenant_id, world_id, actor_id, content_hash)
);

CREATE TABLE yaya_skills (
    tenant_id TEXT NOT NULL,
    skill_id TEXT NOT NULL,
    skill_version_id TEXT NOT NULL,
    certification_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    content_hash CHAR(64) NOT NULL,
    artifact_sha256 CHAR(64) NOT NULL,
    snapshot_json JSONB NOT NULL,
    active BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, skill_version_id),
    UNIQUE (tenant_id, certification_id),
    UNIQUE (tenant_id, skill_version_id, actor_id, content_hash),
    FOREIGN KEY (tenant_id, session_id, actor_id, content_hash)
        REFERENCES yaya_agent_sessions (tenant_id, session_id, actor_id, content_hash)
);
CREATE INDEX yaya_skills_active_idx
    ON yaya_skills (tenant_id, actor_id, active, created_at);

CREATE TABLE yaya_compile_results (
    tenant_id TEXT NOT NULL,
    build_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    content_hash CHAR(64) NOT NULL,
    snapshot_json JSONB NOT NULL,
    PRIMARY KEY (tenant_id, build_id)
);

CREATE TABLE yaya_evidence (
    tenant_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    content_hash CHAR(64) NOT NULL,
    evidence_type TEXT NOT NULL,
    payload_sha256 CHAR(64) NOT NULL,
    evidence_json JSONB NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, evidence_id)
);

CREATE TABLE yaya_runs (
    tenant_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    content_hash CHAR(64) NOT NULL,
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    command_id TEXT NOT NULL,
    world_id TEXT NOT NULL,
    skill_version_id TEXT NOT NULL,
    failure_key TEXT,
    task_success BOOLEAN NOT NULL,
    snapshot_json JSONB NOT NULL,
    wire_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, run_id),
    UNIQUE (tenant_id, run_id, actor_id, content_hash),
    UNIQUE (tenant_id, session_id, turn_id),
    UNIQUE (tenant_id, command_id),
    FOREIGN KEY (tenant_id, session_id, actor_id, content_hash)
        REFERENCES yaya_agent_sessions (tenant_id, session_id, actor_id, content_hash),
    FOREIGN KEY (tenant_id, session_id, world_id, actor_id, content_hash)
        REFERENCES yaya_agent_sessions (
            tenant_id, session_id, world_id, actor_id, content_hash
        ),
    FOREIGN KEY (tenant_id, world_id, actor_id, content_hash)
        REFERENCES yaya_worlds (tenant_id, world_id, actor_id, content_hash),
    FOREIGN KEY (tenant_id, skill_version_id, actor_id, content_hash)
        REFERENCES yaya_skills (tenant_id, skill_version_id, actor_id, content_hash)
);
CREATE INDEX yaya_runs_failure_idx
    ON yaya_runs (tenant_id, session_id, failure_key, created_at, run_id);

CREATE TABLE yaya_skill_invocations (
    tenant_id TEXT NOT NULL,
    invocation_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    content_hash CHAR(64) NOT NULL,
    run_id TEXT NOT NULL,
    request_sha256 CHAR(64) NOT NULL,
    result_json JSONB NOT NULL,
    committed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, invocation_id),
    UNIQUE (tenant_id, run_id),
    FOREIGN KEY (tenant_id, run_id, actor_id, content_hash)
        REFERENCES yaya_runs (tenant_id, run_id, actor_id, content_hash)
);

CREATE TABLE yaya_counterexamples (
    tenant_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    failure_key TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    content_hash CHAR(64) NOT NULL,
    snapshot_json JSONB NOT NULL,
    PRIMARY KEY (tenant_id, case_id),
    FOREIGN KEY (tenant_id, task_id, actor_id, content_hash)
        REFERENCES yaya_tasks (tenant_id, task_id, actor_id, content_hash)
);

CREATE TABLE yaya_learner_models (
    tenant_id TEXT NOT NULL,
    learner_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    content_hash CHAR(64) NOT NULL,
    revision BIGINT NOT NULL CHECK (revision >= 0),
    projected_through_sequence BIGINT NOT NULL CHECK (projected_through_sequence >= 0),
    snapshot_json JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, learner_id)
);

CREATE TABLE yaya_agent_messages (
    tenant_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    content_hash CHAR(64) NOT NULL,
    session_id TEXT NOT NULL,
    snapshot_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, message_id),
    FOREIGN KEY (tenant_id, session_id, actor_id, content_hash)
        REFERENCES yaya_agent_sessions (tenant_id, session_id, actor_id, content_hash)
);
CREATE INDEX yaya_agent_messages_recent_idx
    ON yaya_agent_messages (tenant_id, session_id, created_at DESC, message_id DESC);

CREATE TABLE yaya_commands (
    tenant_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    command_id TEXT NOT NULL,
    session_id TEXT,
    turn_id TEXT,
    client_turn_sequence BIGINT CHECK (client_turn_sequence >= 1),
    request_sha256 CHAR(64) NOT NULL,
    content_hash CHAR(64) NOT NULL,
    revision BIGINT NOT NULL CHECK (revision >= 1),
    status TEXT NOT NULL CHECK (
        status IN (
            'ACCEPTED', 'VALIDATING', 'RUNNING_SANDBOX', 'APPLYING_WORLD',
            'APPLIED', 'REJECTED', 'FAILED', 'UNKNOWN', 'CANCELLED'
        )
    ),
    updated_at TIMESTAMPTZ NOT NULL,
    record_json JSONB NOT NULL,
    PRIMARY KEY (tenant_id, command_id),
    UNIQUE (tenant_id, actor_id, operation, idempotency_key),
    UNIQUE (tenant_id, command_id, actor_id, content_hash),
    UNIQUE (tenant_id, command_id, actor_id, content_hash, session_id, turn_id),
    UNIQUE (
        tenant_id, command_id, actor_id, content_hash,
        session_id, turn_id, client_turn_sequence
    ),
    CHECK (
        (session_id IS NULL AND turn_id IS NULL AND client_turn_sequence IS NULL)
        OR (session_id IS NOT NULL AND turn_id IS NOT NULL AND client_turn_sequence IS NOT NULL)
    ),
    FOREIGN KEY (tenant_id, session_id, actor_id, content_hash)
        REFERENCES yaya_agent_sessions (tenant_id, session_id, actor_id, content_hash)
);
CREATE UNIQUE INDEX yaya_commands_session_turn_idx
    ON yaya_commands (tenant_id, session_id, turn_id)
    WHERE session_id IS NOT NULL;
CREATE UNIQUE INDEX yaya_commands_client_turn_sequence_idx
    ON yaya_commands (tenant_id, session_id, client_turn_sequence)
    WHERE session_id IS NOT NULL;
CREATE INDEX yaya_commands_nonterminal_idx
    ON yaya_commands (tenant_id, updated_at, command_id)
    WHERE status IN ('ACCEPTED', 'VALIDATING', 'RUNNING_SANDBOX', 'APPLYING_WORLD');

ALTER TABLE yaya_runs
    ADD CONSTRAINT yaya_runs_command_identity_fk
    FOREIGN KEY (tenant_id, command_id, actor_id, content_hash, session_id, turn_id)
    REFERENCES yaya_commands (
        tenant_id, command_id, actor_id, content_hash, session_id, turn_id
    );

CREATE TABLE yaya_command_jobs (
    tenant_id TEXT NOT NULL,
    command_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    content_hash CHAR(64) NOT NULL,
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    client_turn_sequence BIGINT NOT NULL CHECK (client_turn_sequence >= 1),
    event_json JSONB NOT NULL,
    operation_context_json JSONB NOT NULL,
    request_body BYTEA NOT NULL CHECK (
        octet_length(request_body) BETWEEN 2 AND 8388608
    ),
    accepted_receipt_json JSONB NOT NULL,
    state TEXT NOT NULL DEFAULT 'READY' CHECK (state IN ('READY', 'LEASED', 'DONE')),
    attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    worker_id TEXT,
    lease_id TEXT,
    lease_expires_at TIMESTAMPTZ,
    available_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    last_error_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, command_id),
    UNIQUE (tenant_id, job_id),
    FOREIGN KEY (
        tenant_id, command_id, actor_id, content_hash,
        session_id, turn_id, client_turn_sequence
    ) REFERENCES yaya_commands (
        tenant_id, command_id, actor_id, content_hash,
        session_id, turn_id, client_turn_sequence
    ),
    FOREIGN KEY (tenant_id, session_id, actor_id, content_hash)
        REFERENCES yaya_agent_sessions (tenant_id, session_id, actor_id, content_hash),
    CHECK (
        (state = 'LEASED' AND worker_id IS NOT NULL
            AND lease_id IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR (state <> 'LEASED' AND worker_id IS NULL
            AND lease_id IS NULL AND lease_expires_at IS NULL)
    )
);
CREATE INDEX yaya_command_jobs_ready_idx
    ON yaya_command_jobs (available_at, command_id)
    WHERE state <> 'DONE';

CREATE TABLE yaya_agent_turns (
    tenant_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    content_hash CHAR(64) NOT NULL,
    event_sha256 CHAR(64) NOT NULL,
    claim_id TEXT,
    claim_expires_at TIMESTAMPTZ,
    record_json JSONB,
    committed_at TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, event_id),
    CHECK (
        (record_json IS NULL AND committed_at IS NULL)
        OR (record_json IS NOT NULL AND committed_at IS NOT NULL AND claim_id IS NULL AND claim_expires_at IS NULL)
    ),
    CHECK (
        (record_json IS NOT NULL AND claim_id IS NULL AND claim_expires_at IS NULL)
        OR (record_json IS NULL AND (claim_id IS NULL) = (claim_expires_at IS NULL))
    )
);

CREATE TABLE yaya_agent_interactions (
    tenant_id TEXT NOT NULL,
    interaction_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    content_hash CHAR(64) NOT NULL,
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    command_id TEXT NOT NULL,
    run_id TEXT,
    sequence BIGINT NOT NULL CHECK (sequence >= 1),
    projection_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, interaction_id),
    UNIQUE (tenant_id, session_id, sequence),
    UNIQUE (tenant_id, session_id, turn_id),
    FOREIGN KEY (tenant_id, session_id, actor_id, content_hash)
        REFERENCES yaya_agent_sessions (tenant_id, session_id, actor_id, content_hash),
    FOREIGN KEY (tenant_id, command_id, actor_id, content_hash, session_id, turn_id)
        REFERENCES yaya_commands (
            tenant_id, command_id, actor_id, content_hash, session_id, turn_id
        ),
    FOREIGN KEY (tenant_id, run_id, actor_id, content_hash)
        REFERENCES yaya_runs (tenant_id, run_id, actor_id, content_hash)
);

CREATE TABLE yaya_projection_outbox (
    tenant_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    destination TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    payload_sha256 CHAR(64) NOT NULL,
    payload_json JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'SENDING', 'SENT', 'RETRYING', 'DEAD_LETTER')),
    attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    lease_id TEXT,
    lease_expires_at TIMESTAMPTZ,
    next_attempt_at TIMESTAMPTZ,
    last_error_json JSONB,
    receipt_json JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, message_id),
    UNIQUE (tenant_id, destination, idempotency_key),
    CHECK (
        (status = 'PENDING' AND attempt = 0 AND lease_id IS NULL
            AND lease_expires_at IS NULL AND next_attempt_at IS NULL
            AND last_error_json IS NULL AND receipt_json IS NULL)
        OR (status = 'SENDING' AND attempt >= 1 AND lease_id IS NOT NULL
            AND lease_expires_at IS NOT NULL AND next_attempt_at IS NULL
            AND last_error_json IS NULL AND receipt_json IS NULL)
        OR (status = 'SENT' AND attempt >= 1 AND lease_id IS NULL
            AND lease_expires_at IS NULL AND next_attempt_at IS NULL
            AND last_error_json IS NULL AND receipt_json IS NOT NULL)
        OR (status = 'RETRYING' AND attempt >= 1 AND lease_id IS NULL
            AND lease_expires_at IS NULL AND next_attempt_at IS NOT NULL
            AND last_error_json IS NOT NULL AND receipt_json IS NULL)
        OR (status = 'DEAD_LETTER' AND attempt >= 1 AND lease_id IS NULL
            AND lease_expires_at IS NULL AND next_attempt_at IS NULL
            AND last_error_json IS NOT NULL AND receipt_json IS NULL)
    )
);

CREATE TABLE yaya_agent_traces (
    tenant_id TEXT NOT NULL,
    trace_record_id BIGSERIAL,
    actor_id TEXT NOT NULL,
    content_hash CHAR(64) NOT NULL,
    turn_id TEXT NOT NULL,
    trace_json JSONB NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, trace_record_id)
);

CREATE TABLE yaya_events (
    tenant_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    stream_id TEXT NOT NULL,
    sequence BIGINT NOT NULL CHECK (sequence >= 1),
    event_type TEXT NOT NULL,
    event_json JSONB NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (tenant_id, event_id),
    UNIQUE (tenant_id, stream_id, sequence)
);

CREATE TABLE yaya_outbox (
    tenant_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    destination TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    payload_sha256 CHAR(64) NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'SENDING', 'SENT', 'RETRYING', 'DEAD_LETTER')),
    attempt INTEGER NOT NULL CHECK (attempt >= 0),
    lease_id TEXT,
    lease_expires_at TIMESTAMPTZ,
    next_attempt_at TIMESTAMPTZ,
    last_error_json JSONB,
    receipt_json JSONB,
    message_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, message_id),
    UNIQUE (tenant_id, destination, idempotency_key),
    CHECK (
        (status = 'PENDING' AND attempt = 0 AND lease_id IS NULL
            AND lease_expires_at IS NULL AND next_attempt_at IS NULL
            AND last_error_json IS NULL AND receipt_json IS NULL)
        OR (status = 'SENDING' AND attempt >= 1 AND lease_id IS NOT NULL
            AND lease_expires_at IS NOT NULL AND next_attempt_at IS NULL
            AND last_error_json IS NULL AND receipt_json IS NULL)
        OR (status = 'SENT' AND attempt >= 1 AND lease_id IS NULL
            AND lease_expires_at IS NULL AND next_attempt_at IS NULL
            AND last_error_json IS NULL AND receipt_json IS NOT NULL)
        OR (status = 'RETRYING' AND attempt >= 1 AND lease_id IS NULL
            AND lease_expires_at IS NULL AND next_attempt_at IS NOT NULL
            AND last_error_json IS NOT NULL AND receipt_json IS NULL)
        OR (status = 'DEAD_LETTER' AND attempt >= 1 AND lease_id IS NULL
            AND lease_expires_at IS NULL AND next_attempt_at IS NULL
            AND last_error_json IS NOT NULL AND receipt_json IS NULL)
    )
);
CREATE INDEX yaya_outbox_ready_idx
    ON yaya_outbox (tenant_id, status, next_attempt_at, created_at);

CREATE TABLE yaya_audit (
    tenant_id TEXT NOT NULL,
    audit_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    outcome TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    record_json JSONB NOT NULL,
    PRIMARY KEY (tenant_id, audit_id)
);

CREATE TABLE yaya_registry_certifications (
    tenant_id TEXT NOT NULL,
    certification_id TEXT NOT NULL,
    skill_id TEXT NOT NULL,
    skill_version_id TEXT NOT NULL,
    artifact_sha256 CHAR(64) NOT NULL,
    record_json JSONB NOT NULL,
    rejected BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (tenant_id, certification_id),
    UNIQUE (tenant_id, skill_version_id)
);

CREATE TABLE yaya_registry_active (
    tenant_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    skill_id TEXT NOT NULL,
    record_json JSONB NOT NULL,
    revision BIGINT NOT NULL CHECK (revision >= 1),
    PRIMARY KEY (tenant_id, actor_id, skill_id)
);
