-- Learner inference is consumed from the source-only learner:<learner_id>
-- stream. Derived learner.model.updated and learner.projection.failed events
-- are written to learner-model:<learner_id> so projected_through_sequence can
-- remain a contiguous checkpoint over immutable inference inputs.

ALTER TABLE yaya_events
    ADD CONSTRAINT yaya_events_id_stream_sequence_key
    UNIQUE (tenant_id, event_id, stream_id, sequence);

ALTER TABLE yaya_evidence
    ADD CONSTRAINT yaya_evidence_authority_hash_key
    UNIQUE (
        tenant_id, evidence_id, actor_id, content_hash, payload_sha256
    );

ALTER TABLE yaya_agent_sessions
    ADD CONSTRAINT yaya_agent_sessions_task_authority_key
    UNIQUE (tenant_id, session_id, task_id, actor_id, content_hash);

ALTER TABLE yaya_learner_models
    ADD COLUMN request_context_json JSONB,
    ADD COLUMN projection_policy_version TEXT,
    ADD COLUMN snapshot_sha256 CHAR(64),
    ADD CONSTRAINT yaya_learner_models_authority_key
        UNIQUE (tenant_id, learner_id, actor_id, content_hash),
    ADD CONSTRAINT yaya_learner_models_projection_provenance_check
        CHECK (
            (
                request_context_json IS NULL
                AND projection_policy_version IS NULL
                AND snapshot_sha256 IS NULL
            )
            OR (
                request_context_json IS NOT NULL
                AND projection_policy_version IS NOT NULL
                AND length(projection_policy_version) BETWEEN 1 AND 128
                AND snapshot_sha256 IS NOT NULL
                AND snapshot_sha256 ~ '^[a-f0-9]{64}$'
                AND revision = projected_through_sequence
            )
        );

CREATE TABLE yaya_learner_projection_jobs (
    tenant_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    learner_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    content_hash CHAR(64) NOT NULL,
    task_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    command_id TEXT NOT NULL,
    run_id TEXT,
    source_stream_id TEXT NOT NULL,
    source_stream_sequence BIGINT NOT NULL CHECK (source_stream_sequence >= 1),
    event_sha256 CHAR(64) NOT NULL,
    source_event_sha256 CHAR(64) NOT NULL,
    turn_commit_sha256 CHAR(64) NOT NULL,
    inference_sha256 CHAR(64) NOT NULL,
    teaching_spec_version TEXT NOT NULL,
    role TEXT NOT NULL
        CHECK (role IN ('teaching_agent', 'bug_agent', 'book_agent')),
    event_json JSONB NOT NULL,
    operation_context_json JSONB NOT NULL,
    state TEXT NOT NULL DEFAULT 'READY'
        CHECK (state IN ('READY', 'LEASED', 'SUCCEEDED', 'FAILED')),
    attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    fencing_token BIGINT NOT NULL DEFAULT 0 CHECK (fencing_token >= 0),
    worker_id TEXT,
    lease_id TEXT,
    claimed_at TIMESTAMPTZ,
    heartbeat_at TIMESTAMPTZ,
    lease_expires_at TIMESTAMPTZ,
    available_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    last_error_code TEXT,
    last_error_json JSONB,
    succeeded_at TIMESTAMPTZ,
    failed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, job_id),
    UNIQUE (tenant_id, event_id),
    UNIQUE (tenant_id, source_stream_id, source_stream_sequence),
    UNIQUE (
        tenant_id, job_id, event_id, source_event_id, learner_id, actor_id,
        content_hash, source_stream_id, source_stream_sequence
    ),
    CONSTRAINT yaya_learner_projection_jobs_terminal_generation_key
        UNIQUE (tenant_id, job_id, state, attempt, fencing_token),
    CONSTRAINT yaya_learner_projection_jobs_source_stream_event_fkey
    FOREIGN KEY (
        tenant_id, event_id, source_stream_id, source_stream_sequence
    ) REFERENCES yaya_events (
        tenant_id, event_id, stream_id, sequence
    ),
    FOREIGN KEY (tenant_id, source_event_id)
        REFERENCES yaya_agent_turns (tenant_id, event_id),
    FOREIGN KEY (
        tenant_id, command_id, actor_id, content_hash, session_id, turn_id
    ) REFERENCES yaya_commands (
        tenant_id, command_id, actor_id, content_hash, session_id, turn_id
    ),
    FOREIGN KEY (tenant_id, session_id, task_id, actor_id, content_hash)
        REFERENCES yaya_agent_sessions (
            tenant_id, session_id, task_id, actor_id, content_hash
        ),
    FOREIGN KEY (tenant_id, task_id, actor_id, content_hash)
        REFERENCES yaya_tasks (tenant_id, task_id, actor_id, content_hash),
    FOREIGN KEY (tenant_id, run_id, actor_id, content_hash)
        REFERENCES yaya_runs (tenant_id, run_id, actor_id, content_hash),
    CHECK (learner_id = actor_id),
    CHECK (source_stream_id = 'learner:' || learner_id),
    CHECK (length(teaching_spec_version) BETWEEN 1 AND 96),
    CHECK (fencing_token = attempt),
    CHECK (
        attempt > 0
        OR (last_error_code IS NULL AND last_error_json IS NULL)
    ),
    CHECK (
        (last_error_code IS NULL) = (last_error_json IS NULL)
    ),
    CHECK (
        (state = 'READY'
            AND worker_id IS NULL AND lease_id IS NULL
            AND claimed_at IS NULL AND heartbeat_at IS NULL
            AND lease_expires_at IS NULL
            AND succeeded_at IS NULL AND failed_at IS NULL)
        OR
        (state = 'LEASED'
            AND attempt >= 1
            AND worker_id IS NOT NULL AND lease_id IS NOT NULL
            AND claimed_at IS NOT NULL AND heartbeat_at IS NOT NULL
            AND lease_expires_at IS NOT NULL
            AND lease_expires_at > heartbeat_at
            AND succeeded_at IS NULL AND failed_at IS NULL)
        OR
        (state = 'SUCCEEDED'
            AND attempt >= 1
            AND worker_id IS NULL AND lease_id IS NULL
            AND claimed_at IS NULL AND heartbeat_at IS NULL
            AND lease_expires_at IS NULL
            AND last_error_code IS NULL AND last_error_json IS NULL
            AND succeeded_at IS NOT NULL AND failed_at IS NULL)
        OR
        (state = 'FAILED'
            AND worker_id IS NULL AND lease_id IS NULL
            AND claimed_at IS NULL AND heartbeat_at IS NULL
            AND lease_expires_at IS NULL
            AND last_error_code IS NOT NULL AND last_error_json IS NOT NULL
            AND succeeded_at IS NULL AND failed_at IS NOT NULL)
    )
);

COMMENT ON COLUMN yaya_learner_projection_jobs.source_stream_id IS
    'Source-only learner:<learner_id> inference stream; derived model events use learner-model:<learner_id>.';
COMMENT ON COLUMN yaya_learner_projection_jobs.fencing_token IS
    'Monotonic claim generation. Every projecting transaction must verify job_id, worker_id, lease_id, fencing_token and an unexpired DB-clock lease.';

CREATE INDEX yaya_learner_projection_jobs_ready_idx
    ON yaya_learner_projection_jobs (
        available_at, tenant_id, learner_id, source_stream_sequence, job_id
    )
    WHERE state = 'READY';
CREATE INDEX yaya_learner_projection_jobs_takeover_idx
    ON yaya_learner_projection_jobs (
        lease_expires_at, tenant_id, learner_id, source_stream_sequence, job_id
    )
    WHERE state = 'LEASED';
CREATE INDEX yaya_learner_projection_jobs_learner_state_idx
    ON yaya_learner_projection_jobs (
        tenant_id, learner_id, state, source_stream_sequence
    );

CREATE TABLE yaya_learner_projection_job_evidence (
    tenant_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    learner_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    content_hash CHAR(64) NOT NULL,
    source_stream_id TEXT NOT NULL,
    source_stream_sequence BIGINT NOT NULL CHECK (source_stream_sequence >= 1),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0 AND ordinal < 64),
    evidence_id TEXT NOT NULL,
    evidence_sha256 CHAR(64) NOT NULL,
    PRIMARY KEY (tenant_id, job_id, evidence_id),
    UNIQUE (tenant_id, job_id, ordinal),
    FOREIGN KEY (
        tenant_id, job_id, event_id, source_event_id, learner_id, actor_id,
        content_hash, source_stream_id, source_stream_sequence
    ) REFERENCES yaya_learner_projection_jobs (
        tenant_id, job_id, event_id, source_event_id, learner_id, actor_id,
        content_hash, source_stream_id, source_stream_sequence
    ) ON DELETE RESTRICT,
    FOREIGN KEY (
        tenant_id, evidence_id, actor_id, content_hash, evidence_sha256
    ) REFERENCES yaya_evidence (
        tenant_id, evidence_id, actor_id, content_hash, payload_sha256
    )
);

CREATE INDEX yaya_learner_projection_job_evidence_event_idx
    ON yaya_learner_projection_job_evidence (tenant_id, event_id, ordinal);

CREATE TABLE yaya_learner_projection_receipts (
    tenant_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    learner_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    content_hash CHAR(64) NOT NULL,
    source_stream_id TEXT NOT NULL,
    source_stream_sequence BIGINT NOT NULL CHECK (source_stream_sequence >= 1),
    event_sha256 CHAR(64) NOT NULL,
    inference_sha256 CHAR(64) NOT NULL,
    previous_learner_revision BIGINT NOT NULL
        CHECK (previous_learner_revision >= 0),
    learner_revision BIGINT NOT NULL CHECK (learner_revision >= 1),
    model_version TEXT NOT NULL,
    snapshot_sha256 CHAR(64) NOT NULL,
    model_updated_event_id TEXT NOT NULL,
    outbox_message_id TEXT NOT NULL,
    update_json JSONB NOT NULL,
    receipt_sha256 CHAR(64) NOT NULL,
    projected_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, event_id),
    UNIQUE (tenant_id, job_id),
    UNIQUE (tenant_id, learner_id, source_stream_sequence),
    UNIQUE (tenant_id, model_updated_event_id),
    UNIQUE (tenant_id, outbox_message_id),
    FOREIGN KEY (
        tenant_id, job_id, event_id, source_event_id, learner_id, actor_id,
        content_hash, source_stream_id, source_stream_sequence
    ) REFERENCES yaya_learner_projection_jobs (
        tenant_id, job_id, event_id, source_event_id, learner_id, actor_id,
        content_hash, source_stream_id, source_stream_sequence
    ),
    FOREIGN KEY (tenant_id, model_updated_event_id)
        REFERENCES yaya_events (tenant_id, event_id),
    FOREIGN KEY (tenant_id, outbox_message_id)
        REFERENCES yaya_outbox (tenant_id, message_id),
    CHECK (learner_revision = previous_learner_revision + 1)
);

CREATE INDEX yaya_learner_projection_receipts_checkpoint_idx
    ON yaya_learner_projection_receipts (
        tenant_id, learner_id, source_stream_sequence DESC
    );

CREATE TABLE yaya_learner_projection_failures (
    tenant_id TEXT NOT NULL,
    failure_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    learner_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    content_hash CHAR(64) NOT NULL,
    source_stream_id TEXT NOT NULL,
    source_stream_sequence BIGINT NOT NULL CHECK (source_stream_sequence >= 1),
    attempt INTEGER NOT NULL CHECK (attempt >= 1),
    fencing_token BIGINT NOT NULL CHECK (fencing_token >= 1),
    classification TEXT NOT NULL
        CHECK (classification IN ('RETRYABLE', 'PERMANENT', 'QUARANTINED')),
    error_code TEXT NOT NULL,
    error_json JSONB NOT NULL,
    error_sha256 CHAR(64) NOT NULL,
    failure_event_id TEXT,
    outbox_message_id TEXT,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    resolved_at TIMESTAMPTZ,
    resolution TEXT
        CHECK (resolution IN ('RETRIED', 'REBUILT', 'DISMISSED')),
    PRIMARY KEY (tenant_id, failure_id),
    UNIQUE (tenant_id, job_id, attempt),
    FOREIGN KEY (
        tenant_id, job_id, event_id, source_event_id, learner_id, actor_id,
        content_hash, source_stream_id, source_stream_sequence
    ) REFERENCES yaya_learner_projection_jobs (
        tenant_id, job_id, event_id, source_event_id, learner_id, actor_id,
        content_hash, source_stream_id, source_stream_sequence
    ),
    FOREIGN KEY (tenant_id, failure_event_id)
        REFERENCES yaya_events (tenant_id, event_id),
    FOREIGN KEY (tenant_id, outbox_message_id)
        REFERENCES yaya_outbox (tenant_id, message_id),
    CHECK (fencing_token = attempt),
    CHECK (
        (classification = 'PERMANENT'
            AND failure_event_id IS NOT NULL AND outbox_message_id IS NOT NULL)
        OR
        (classification IN ('RETRYABLE', 'QUARANTINED')
            AND failure_event_id IS NULL AND outbox_message_id IS NULL)
    ),
    CHECK ((resolved_at IS NULL) = (resolution IS NULL))
);

CREATE UNIQUE INDEX yaya_learner_projection_failures_active_idx
    ON yaya_learner_projection_failures (tenant_id, event_id)
    WHERE resolved_at IS NULL AND classification IN ('PERMANENT', 'QUARANTINED');
CREATE INDEX yaya_learner_projection_failures_learner_idx
    ON yaya_learner_projection_failures (
        tenant_id, learner_id, recorded_at DESC, failure_id
    );

-- A terminal projection transaction must leave behind a durable obligation for
-- a separate read-only graph audit.  This prevents a transient PostgreSQL
-- interruption during immediate reconciliation from turning FenceLost into an
-- implicit acknowledgement of an unaudited SUCCEEDED/FAILED graph.
CREATE TABLE yaya_learner_projection_terminal_audits (
    tenant_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    terminal_state TEXT NOT NULL
        CHECK (terminal_state IN ('SUCCEEDED', 'FAILED')),
    terminal_kind TEXT NOT NULL
        CHECK (terminal_kind IN ('SUCCESS', 'PERMANENT_FAILURE', 'QUARANTINE')),
    attempt INTEGER NOT NULL CHECK (attempt >= 1),
    fencing_token BIGINT NOT NULL CHECK (fencing_token >= 1),
    terminal_at TIMESTAMPTZ NOT NULL,
    verified_at TIMESTAMPTZ,
    verified_by TEXT,
    PRIMARY KEY (tenant_id, job_id),
    CONSTRAINT yaya_learner_projection_terminal_audits_job_generation_fkey
    FOREIGN KEY (
        tenant_id, job_id, terminal_state, attempt, fencing_token
    ) REFERENCES yaya_learner_projection_jobs (
        tenant_id, job_id, state, attempt, fencing_token
    )
        ON DELETE RESTRICT,
    CHECK (fencing_token = attempt),
    CHECK (
        (terminal_state = 'SUCCEEDED' AND terminal_kind = 'SUCCESS')
        OR
        (terminal_state = 'FAILED'
            AND terminal_kind IN ('PERMANENT_FAILURE','QUARANTINE'))
    ),
    CHECK ((verified_at IS NULL) = (verified_by IS NULL)),
    CHECK (verified_by IS NULL OR length(verified_by) BETWEEN 1 AND 128),
    CHECK (verified_at IS NULL OR verified_at >= terminal_at)
);

CREATE INDEX yaya_learner_projection_terminal_audits_pending_idx
    ON yaya_learner_projection_terminal_audits (
        terminal_at, tenant_id, job_id
    )
    WHERE verified_at IS NULL;

INSERT INTO yaya_learner_projection_terminal_audits(
    tenant_id,job_id,terminal_state,terminal_kind,
    attempt,fencing_token,terminal_at
)
SELECT j.tenant_id,j.job_id,j.state,
       CASE
           WHEN j.state='SUCCEEDED' THEN 'SUCCESS'
           WHEN f.classification='PERMANENT' THEN 'PERMANENT_FAILURE'
           ELSE 'QUARANTINE'
       END,
       j.attempt,j.fencing_token,
       COALESCE(j.succeeded_at,j.failed_at)
FROM yaya_learner_projection_jobs j
LEFT JOIN yaya_learner_projection_failures f
  ON f.tenant_id=j.tenant_id AND f.job_id=j.job_id AND f.attempt=j.attempt
WHERE state = 'SUCCEEDED'
   OR (
       state = 'FAILED'
       AND f.classification IN ('PERMANENT','QUARANTINED')
   );

CREATE FUNCTION yaya_enqueue_learner_projection_terminal_audit()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.state = 'SUCCEEDED' THEN
        IF TG_OP = 'INSERT' OR OLD.state IS DISTINCT FROM NEW.state THEN
            INSERT INTO yaya_learner_projection_terminal_audits(
                tenant_id,job_id,terminal_state,terminal_kind,
                attempt,fencing_token,terminal_at
            ) VALUES (
                NEW.tenant_id,NEW.job_id,NEW.state,'SUCCESS',
                NEW.attempt,NEW.fencing_token,
                COALESCE(NEW.succeeded_at,NEW.failed_at)
            );
        END IF;
    ELSIF NEW.state = 'FAILED'
          AND (TG_OP = 'INSERT' OR OLD.state IS DISTINCT FROM NEW.state) THEN
        IF EXISTS (
            SELECT 1
            FROM yaya_learner_projection_failures f
            WHERE f.tenant_id=NEW.tenant_id AND f.job_id=NEW.job_id
              AND f.attempt=NEW.attempt AND f.classification='PERMANENT'
        ) THEN
            INSERT INTO yaya_learner_projection_terminal_audits(
                tenant_id,job_id,terminal_state,terminal_kind,
                attempt,fencing_token,terminal_at
            ) VALUES (
                NEW.tenant_id,NEW.job_id,NEW.state,'PERMANENT_FAILURE',
                NEW.attempt,NEW.fencing_token,
                COALESCE(NEW.succeeded_at,NEW.failed_at)
            );
        ELSIF EXISTS (
            SELECT 1
            FROM yaya_learner_projection_failures f
            WHERE f.tenant_id=NEW.tenant_id AND f.job_id=NEW.job_id
              AND f.attempt=NEW.attempt AND f.classification='QUARANTINED'
        ) THEN
            INSERT INTO yaya_learner_projection_terminal_audits(
                tenant_id,job_id,terminal_state,terminal_kind,
                attempt,fencing_token,terminal_at
            ) VALUES (
                NEW.tenant_id,NEW.job_id,NEW.state,'QUARANTINE',
                NEW.attempt,NEW.fencing_token,
                COALESCE(NEW.succeeded_at,NEW.failed_at)
            );
        ELSE
            RAISE EXCEPTION 'FAILED learner projection Job lacks a terminal failure record';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER yaya_learner_projection_terminal_audit_enqueue
AFTER INSERT OR UPDATE ON yaya_learner_projection_jobs
FOR EACH ROW
EXECUTE FUNCTION yaya_enqueue_learner_projection_terminal_audit();

CREATE FUNCTION yaya_guard_learner_projection_terminal_audit()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'learner projection terminal audit obligations are immutable';
    END IF;
    IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
       OR NEW.job_id IS DISTINCT FROM OLD.job_id
       OR NEW.terminal_state IS DISTINCT FROM OLD.terminal_state
       OR NEW.terminal_kind IS DISTINCT FROM OLD.terminal_kind
       OR NEW.attempt IS DISTINCT FROM OLD.attempt
       OR NEW.fencing_token IS DISTINCT FROM OLD.fencing_token
       OR NEW.terminal_at IS DISTINCT FROM OLD.terminal_at
       OR OLD.verified_at IS NOT NULL
       OR OLD.verified_by IS NOT NULL
       OR NEW.verified_at IS NULL
       OR NEW.verified_by IS NULL THEN
        RAISE EXCEPTION 'learner projection terminal audit identity is immutable';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER yaya_learner_projection_terminal_audit_immutable
BEFORE UPDATE OR DELETE ON yaya_learner_projection_terminal_audits
FOR EACH ROW
EXECUTE FUNCTION yaya_guard_learner_projection_terminal_audit();
