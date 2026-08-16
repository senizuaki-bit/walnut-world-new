-- Public student Skill lifecycle. This is additive: legacy Agent Turn rows remain valid.

ALTER TABLE yaya_skills ALTER COLUMN session_id DROP NOT NULL;

CREATE OR REPLACE FUNCTION yaya_reject_immutable_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is immutable', TG_TABLE_NAME USING ERRCODE = '55000';
END;
$$;

CREATE OR REPLACE FUNCTION yaya_guard_public_agent_session_scope()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' OR ROW(
        NEW.tenant_id, NEW.session_id, NEW.authority_id, NEW.actor_id,
        NEW.content_hash, NEW.task_id, NEW.world_id, NEW.learner_id,
        NEW.agent_profile_id, NEW.created_at
    ) IS DISTINCT FROM ROW(
        OLD.tenant_id, OLD.session_id, OLD.authority_id, OLD.actor_id,
        OLD.content_hash, OLD.task_id, OLD.world_id, OLD.learner_id,
        OLD.agent_profile_id, OLD.created_at
    ) THEN
        RAISE EXCEPTION 'yaya_public_agent_sessions scope is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
        (OLD.status = 'ACTIVE' AND NEW.status IN ('CLOSING', 'CLOSED', 'FAILED'))
        OR (OLD.status = 'CLOSING' AND NEW.status IN ('CLOSED', 'FAILED'))
    ) THEN
        RAISE EXCEPTION 'yaya_public_agent_sessions status transition is invalid'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION yaya_allow_only_active_retirement()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE'
       AND OLD.active IS TRUE
       AND NEW.active IS FALSE
       AND (to_jsonb(NEW) - 'active') = (to_jsonb(OLD) - 'active') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION '% permits only an active TRUE to FALSE retirement', TG_TABLE_NAME
        USING ERRCODE = '55000';
END;
$$;

CREATE OR REPLACE FUNCTION yaya_guard_skill_build_head_transition()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    resource_created_at TIMESTAMPTZ;
    resource_updated_at TIMESTAMPTZ;
    legal_transition BOOLEAN;
BEGIN
    IF TG_OP = 'DELETE' OR OLD.terminal IS TRUE THEN
        RAISE EXCEPTION 'yaya_skill_builds terminal and delete mutations are forbidden'
            USING ERRCODE = '55000';
    END IF;

    IF (to_jsonb(NEW) - ARRAY[
            'status', 'terminal', 'resource_sha256', 'resource_json', 'updated_at'
        ]) IS DISTINCT FROM
       (to_jsonb(OLD) - ARRAY[
            'status', 'terminal', 'resource_sha256', 'resource_json', 'updated_at'
        ]) THEN
        RAISE EXCEPTION 'yaya_skill_builds authority columns are immutable'
            USING ERRCODE = '55000';
    END IF;

    legal_transition :=
        (OLD.status = 'ACCEPTED' AND OLD.terminal IS FALSE
         AND NEW.status = 'COMPILING' AND NEW.terminal IS FALSE)
        OR
        (OLD.status = 'COMPILING' AND OLD.terminal IS FALSE
         AND NEW.status IN ('CERTIFIED', 'REJECTED', 'FAILED')
         AND NEW.terminal IS TRUE)
        OR
        (OLD.status = 'ACCEPTED' AND OLD.terminal IS FALSE
         AND NEW.status = 'FAILED' AND NEW.terminal IS TRUE
         AND NEW.resource_json #>> '{failure,stage}' = 'VALIDATE_SOURCE');
    IF legal_transition IS NOT TRUE THEN
        RAISE EXCEPTION 'yaya_skill_builds state transition is forbidden'
            USING ERRCODE = '55000';
    END IF;

    IF NEW.resource_json IS NOT DISTINCT FROM OLD.resource_json
       OR NEW.resource_sha256 IS NOT DISTINCT FROM OLD.resource_sha256
       OR NEW.updated_at <= OLD.updated_at
       OR NEW.resource_json ->> 'build_id' IS DISTINCT FROM NEW.build_id
       OR NEW.resource_json ->> 'skill_id' IS DISTINCT FROM NEW.skill_id
       OR NEW.resource_json ->> 'status' IS DISTINCT FROM NEW.status
       OR NEW.resource_json -> 'terminal' IS DISTINCT FROM to_jsonb(NEW.terminal)
       OR NEW.resource_json -> 'request_context'
            IS DISTINCT FROM OLD.resource_json -> 'request_context'
       OR NEW.resource_json ->> 'created_at'
            IS DISTINCT FROM OLD.resource_json ->> 'created_at' THEN
        RAISE EXCEPTION 'yaya_skill_builds resource head is inconsistent'
            USING ERRCODE = '55000';
    END IF;

    BEGIN
        resource_created_at := (NEW.resource_json ->> 'created_at')::TIMESTAMPTZ;
        resource_updated_at := (NEW.resource_json ->> 'updated_at')::TIMESTAMPTZ;
    EXCEPTION WHEN OTHERS THEN
        RAISE EXCEPTION 'yaya_skill_builds resource timestamps are invalid'
            USING ERRCODE = '55000';
    END;
    IF resource_created_at IS DISTINCT FROM NEW.created_at
       OR resource_updated_at IS DISTINCT FROM NEW.updated_at
       OR NEW.resource_json #>> '{request_context,actor,tenant_id}'
            IS DISTINCT FROM NEW.tenant_id
       OR NEW.resource_json #>> '{request_context,actor,actor_id}'
            IS DISTINCT FROM NEW.actor_id
       OR NEW.resource_json #>> '{request_context,content_ref,content_hash}'
            IS DISTINCT FROM NEW.content_hash THEN
        RAISE EXCEPTION 'yaya_skill_builds resource authority is inconsistent'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION yaya_guard_a8_skill_mirror()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM yaya_skill_certifications certification
        WHERE certification.tenant_id = OLD.tenant_id
          AND certification.certification_id = OLD.certification_id
    ) THEN
        RAISE EXCEPTION 'A8 yaya_skills mirror is immutable' USING ERRCODE = '55000';
    END IF;
    IF TG_OP = 'UPDATE' AND EXISTS (
            SELECT 1 FROM yaya_skill_certifications certification
            WHERE certification.tenant_id = NEW.tenant_id
              AND certification.certification_id = NEW.certification_id
    ) THEN
        RAISE EXCEPTION 'A8 yaya_skills mirror is immutable' USING ERRCODE = '55000';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION yaya_guard_a8_registry_certification_mirror()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM yaya_skill_certifications certification
        WHERE certification.tenant_id = OLD.tenant_id
          AND certification.certification_id = OLD.certification_id
          AND certification.skill_id = OLD.skill_id
          AND certification.skill_version_id = OLD.skill_version_id
          AND certification.artifact_sha256 = OLD.artifact_sha256
    ) THEN
        RAISE EXCEPTION 'A8 registry Certification mirror is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF TG_OP = 'UPDATE' AND EXISTS (
            SELECT 1 FROM yaya_skill_certifications certification
            WHERE certification.tenant_id = NEW.tenant_id
              AND certification.certification_id = NEW.certification_id
              AND certification.skill_id = NEW.skill_id
              AND certification.skill_version_id = NEW.skill_version_id
              AND certification.artifact_sha256 = NEW.artifact_sha256
    ) THEN
        RAISE EXCEPTION 'A8 registry Certification mirror is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION yaya_guard_a8_compile_result_mirror()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM yaya_skill_builds build
        WHERE build.tenant_id = OLD.tenant_id
          AND build.build_id = OLD.build_id
          AND build.terminal IS TRUE
    ) THEN
        RAISE EXCEPTION 'A8 CompileResult mirror is immutable' USING ERRCODE = '55000';
    END IF;
    IF TG_OP = 'UPDATE' AND EXISTS (
            SELECT 1 FROM yaya_skill_builds build
            WHERE build.tenant_id = NEW.tenant_id
              AND build.build_id = NEW.build_id
              AND build.terminal IS TRUE
    ) THEN
        RAISE EXCEPTION 'A8 CompileResult mirror is immutable' USING ERRCODE = '55000';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION yaya_guard_a8_evidence_mirror()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (
        OLD.evidence_json #>> '{source,source_type}' = 'SKILL_BUILD'
        AND EXISTS (
            SELECT 1 FROM yaya_skill_builds build
            WHERE build.tenant_id = OLD.tenant_id
              AND build.build_id = (OLD.evidence_json #>> '{source,source_id}')
        )
    ) THEN
        RAISE EXCEPTION 'A8 Build Evidence mirror is immutable' USING ERRCODE = '55000';
    END IF;
    IF TG_OP = 'UPDATE'
       AND NEW.evidence_json #>> '{source,source_type}' = 'SKILL_BUILD'
       AND EXISTS (
            SELECT 1 FROM yaya_skill_builds build
            WHERE build.tenant_id = NEW.tenant_id
              AND build.build_id = (NEW.evidence_json #>> '{source,source_id}')
    ) THEN
        RAISE EXCEPTION 'A8 Build Evidence mirror is immutable' USING ERRCODE = '55000';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TABLE yaya_learners (
    tenant_id TEXT NOT NULL,
    learner_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    content_hash CHAR(64) NOT NULL CHECK (content_hash ~ '^[a-f0-9]{64}$'),
    record_sha256 CHAR(64) NOT NULL CHECK (record_sha256 ~ '^[a-f0-9]{64}$'),
    record_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, learner_id),
    UNIQUE (tenant_id, learner_id, actor_id, content_hash)
);

CREATE TABLE yaya_agent_profiles (
    tenant_id TEXT NOT NULL,
    agent_profile_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    content_hash CHAR(64) NOT NULL CHECK (content_hash ~ '^[a-f0-9]{64}$'),
    record_sha256 CHAR(64) NOT NULL CHECK (record_sha256 ~ '^[a-f0-9]{64}$'),
    record_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, agent_profile_id),
    UNIQUE (tenant_id, agent_profile_id, actor_id, content_hash)
);

CREATE TABLE yaya_launch_authorities (
    tenant_id TEXT NOT NULL,
    authority_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    learner_id TEXT NOT NULL,
    content_unit_id TEXT NOT NULL,
    content_version TEXT NOT NULL,
    content_hash CHAR(64) NOT NULL CHECK (content_hash ~ '^[a-f0-9]{64}$'),
    world_id TEXT NOT NULL,
    agent_profile_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    versions_json JSONB NOT NULL,
    snapshot_sha256 CHAR(64) NOT NULL CHECK (snapshot_sha256 ~ '^[a-f0-9]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, authority_id),
    UNIQUE (tenant_id, authority_id, actor_id, content_hash),
    UNIQUE (
        tenant_id, authority_id, actor_id, content_hash, task_id, world_id,
        learner_id, agent_profile_id
    ),
    FOREIGN KEY (tenant_id, task_id, actor_id, content_hash)
        REFERENCES yaya_tasks (tenant_id, task_id, actor_id, content_hash),
    FOREIGN KEY (tenant_id, world_id, actor_id, content_hash)
        REFERENCES yaya_worlds (tenant_id, world_id, actor_id, content_hash),
    FOREIGN KEY (tenant_id, learner_id, actor_id, content_hash)
        REFERENCES yaya_learners (tenant_id, learner_id, actor_id, content_hash),
    FOREIGN KEY (tenant_id, agent_profile_id, actor_id, content_hash)
        REFERENCES yaya_agent_profiles (
            tenant_id, agent_profile_id, actor_id, content_hash
        )
);
CREATE INDEX yaya_launch_authorities_resolution_idx
    ON yaya_launch_authorities (
        tenant_id, actor_id, content_hash, task_id, world_id, learner_id,
        agent_profile_id
    );
CREATE UNIQUE INDEX yaya_launch_authorities_one_active_scope_idx
    ON yaya_launch_authorities (
        tenant_id, actor_id, learner_id, content_unit_id, content_version,
        content_hash, world_id, agent_profile_id
    ) WHERE active IS TRUE;

CREATE TABLE yaya_build_policies (
    tenant_id TEXT NOT NULL,
    build_policy_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    content_hash CHAR(64) NOT NULL CHECK (content_hash ~ '^[a-f0-9]{64}$'),
    compiler_profile TEXT NOT NULL,
    test_suite_version TEXT NOT NULL,
    compiler_image TEXT NOT NULL,
    compiler_version TEXT NOT NULL,
    compile_flags_json JSONB NOT NULL,
    public_tests_json JSONB NOT NULL,
    hidden_tests_json JSONB NOT NULL,
    approved_capabilities_json JSONB NOT NULL,
    limits_json JSONB NOT NULL,
    parameter_schema_json JSONB NOT NULL,
    semantic_version_major INTEGER NOT NULL CHECK (semantic_version_major >= 0),
    semantic_version_minor INTEGER NOT NULL CHECK (semantic_version_minor >= 0),
    runtime_abi_version TEXT NOT NULL,
    policy_sha256 CHAR(64) NOT NULL CHECK (policy_sha256 ~ '^[a-f0-9]{64}$'),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, build_policy_id),
    UNIQUE (tenant_id, build_policy_id, actor_id, content_hash)
);
CREATE UNIQUE INDEX yaya_build_policies_one_active_scope_idx
    ON yaya_build_policies (
        tenant_id, actor_id, content_hash, compiler_profile, test_suite_version
    ) WHERE active IS TRUE;

CREATE TABLE yaya_public_agent_sessions (
    tenant_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    authority_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    content_hash CHAR(64) NOT NULL CHECK (content_hash ~ '^[a-f0-9]{64}$'),
    task_id TEXT NOT NULL,
    world_id TEXT NOT NULL,
    learner_id TEXT NOT NULL,
    agent_profile_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'CLOSING', 'CLOSED', 'FAILED')),
    resource_sha256 CHAR(64) NOT NULL CHECK (resource_sha256 ~ '^[a-f0-9]{64}$'),
    resource_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, session_id),
    UNIQUE (tenant_id, session_id, actor_id, content_hash),
    FOREIGN KEY (
        tenant_id, authority_id, actor_id, content_hash, task_id, world_id,
        learner_id, agent_profile_id
    ) REFERENCES yaya_launch_authorities (
        tenant_id, authority_id, actor_id, content_hash, task_id, world_id,
        learner_id, agent_profile_id
    )
);

CREATE TABLE yaya_control_jobs (
    tenant_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    command_id TEXT NOT NULL,
    authority_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    content_hash CHAR(64) NOT NULL CHECK (content_hash ~ '^[a-f0-9]{64}$'),
    operation TEXT NOT NULL CHECK (
        operation IN ('CREATE_AGENT_SESSION', 'CREATE_SKILL_BUILD', 'ACTIVATE_SKILL_VERSION')
    ),
    idempotency_key TEXT NOT NULL,
    request_target TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    request_sha256 CHAR(64) NOT NULL CHECK (request_sha256 ~ '^[a-f0-9]{64}$'),
    request_body BYTEA NOT NULL CHECK (octet_length(request_body) BETWEEN 2 AND 8388608),
    request_json JSONB NOT NULL,
    operation_context_json JSONB NOT NULL,
    accepted_receipt_json JSONB NOT NULL,
    phase TEXT NOT NULL DEFAULT 'ACCEPTED',
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
    result_json JSONB,
    last_error_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, job_id),
    UNIQUE (tenant_id, command_id),
    UNIQUE (tenant_id, actor_id, operation, idempotency_key),
    FOREIGN KEY (tenant_id, authority_id, actor_id, content_hash)
        REFERENCES yaya_launch_authorities (
            tenant_id, authority_id, actor_id, content_hash
        ),
    CHECK (fencing_token = attempt),
    CHECK (
        (state = 'READY' AND worker_id IS NULL AND lease_id IS NULL
            AND claimed_at IS NULL AND heartbeat_at IS NULL AND lease_expires_at IS NULL)
        OR (state = 'LEASED' AND attempt >= 1 AND worker_id IS NOT NULL
            AND lease_id IS NOT NULL AND claimed_at IS NOT NULL
            AND heartbeat_at IS NOT NULL AND lease_expires_at > heartbeat_at)
        OR (state IN ('SUCCEEDED', 'FAILED') AND attempt >= 1
            AND worker_id IS NULL AND lease_id IS NULL AND claimed_at IS NULL
            AND heartbeat_at IS NULL AND lease_expires_at IS NULL)
    ),
    CHECK ((state = 'SUCCEEDED') = (result_json IS NOT NULL)),
    CHECK ((state = 'FAILED') = (last_error_code IS NOT NULL))
);
CREATE INDEX yaya_control_jobs_ready_idx
    ON yaya_control_jobs (available_at, tenant_id, job_id) WHERE state = 'READY';
CREATE INDEX yaya_control_jobs_takeover_idx
    ON yaya_control_jobs (lease_expires_at, tenant_id, job_id) WHERE state = 'LEASED';

CREATE TABLE yaya_skill_draft_revisions (
    tenant_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    draft_id TEXT NOT NULL,
    skill_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    content_hash CHAR(64) NOT NULL CHECK (content_hash ~ '^[a-f0-9]{64}$'),
    revision BIGINT NOT NULL CHECK (revision >= 1),
    draft_sha256 CHAR(64) NOT NULL CHECK (draft_sha256 ~ '^[a-f0-9]{64}$'),
    source_bundle_sha256 CHAR(64) NOT NULL CHECK (source_bundle_sha256 ~ '^[a-f0-9]{64}$'),
    source_bundle_json JSONB NOT NULL,
    resource_sha256 CHAR(64) NOT NULL CHECK (resource_sha256 ~ '^[a-f0-9]{64}$'),
    resource_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, session_id, draft_id, revision),
    UNIQUE (
        tenant_id, session_id, draft_id, skill_id, actor_id, content_hash,
        revision, draft_sha256
    ),
    FOREIGN KEY (tenant_id, session_id, actor_id, content_hash)
        REFERENCES yaya_public_agent_sessions (
            tenant_id, session_id, actor_id, content_hash
        )
);

CREATE TABLE yaya_skill_draft_heads (
    tenant_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    draft_id TEXT NOT NULL,
    skill_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    content_hash CHAR(64) NOT NULL CHECK (content_hash ~ '^[a-f0-9]{64}$'),
    current_revision BIGINT NOT NULL CHECK (current_revision >= 1),
    current_draft_sha256 CHAR(64) NOT NULL CHECK (current_draft_sha256 ~ '^[a-f0-9]{64}$'),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, session_id, draft_id),
    UNIQUE (tenant_id, session_id, draft_id, actor_id, content_hash),
    FOREIGN KEY (
        tenant_id, session_id, draft_id, skill_id, actor_id, content_hash,
        current_revision, current_draft_sha256
    ) REFERENCES yaya_skill_draft_revisions (
        tenant_id, session_id, draft_id, skill_id, actor_id, content_hash,
        revision, draft_sha256
    )
);

CREATE TABLE yaya_product_write_receipts (
    tenant_id TEXT NOT NULL,
    receipt_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    content_hash CHAR(64) NOT NULL CHECK (content_hash ~ '^[a-f0-9]{64}$'),
    operation TEXT NOT NULL,
    canonical_path TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 CHAR(64) NOT NULL CHECK (request_sha256 ~ '^[a-f0-9]{64}$'),
    request_body BYTEA NOT NULL CHECK (
        octet_length(request_body) BETWEEN 2 AND 8388608
    ),
    response_status INTEGER NOT NULL CHECK (response_status BETWEEN 200 AND 599),
    session_id TEXT NOT NULL,
    draft_id TEXT NOT NULL,
    skill_id TEXT NOT NULL,
    revision BIGINT NOT NULL CHECK (revision >= 1),
    draft_sha256 CHAR(64) NOT NULL CHECK (draft_sha256 ~ '^[a-f0-9]{64}$'),
    original_trace_id TEXT NOT NULL,
    response_sha256 CHAR(64) NOT NULL CHECK (response_sha256 ~ '^[a-f0-9]{64}$'),
    response_body BYTEA NOT NULL,
    response_headers JSONB NOT NULL,
    response_json JSONB NOT NULL,
    location TEXT NOT NULL,
    etag TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, receipt_id),
    UNIQUE (tenant_id, actor_id, operation, canonical_path, idempotency_key),
    FOREIGN KEY (
        tenant_id, session_id, draft_id, skill_id, actor_id, content_hash,
        revision, draft_sha256
    ) REFERENCES yaya_skill_draft_revisions (
        tenant_id, session_id, draft_id, skill_id, actor_id, content_hash,
        revision, draft_sha256
    )
);

CREATE TABLE yaya_skill_builds (
    tenant_id TEXT NOT NULL,
    build_id TEXT NOT NULL,
    authority_id TEXT NOT NULL,
    skill_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    content_hash CHAR(64) NOT NULL CHECK (content_hash ~ '^[a-f0-9]{64}$'),
    client_draft_revision BIGINT NOT NULL CHECK (client_draft_revision >= 0),
    source_bundle_sha256 CHAR(64) NOT NULL CHECK (source_bundle_sha256 ~ '^[a-f0-9]{64}$'),
    source_bundle_json JSONB NOT NULL,
    build_policy_id TEXT NOT NULL,
    compiler_profile TEXT NOT NULL,
    test_suite_version TEXT NOT NULL,
    requested_capabilities_json JSONB NOT NULL,
    command_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('ACCEPTED','QUEUED','COMPILING','TESTING','CERTIFYING','CERTIFIED','REJECTED','FAILED')
    ),
    terminal BOOLEAN NOT NULL DEFAULT FALSE,
    resource_sha256 CHAR(64) NOT NULL CHECK (resource_sha256 ~ '^[a-f0-9]{64}$'),
    resource_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, build_id),
    UNIQUE (tenant_id, command_id),
    UNIQUE (tenant_id, build_id, skill_id, actor_id, content_hash),
    FOREIGN KEY (tenant_id, authority_id, actor_id, content_hash)
        REFERENCES yaya_launch_authorities (
            tenant_id, authority_id, actor_id, content_hash
        ),
    FOREIGN KEY (tenant_id, build_policy_id, actor_id, content_hash)
        REFERENCES yaya_build_policies (
            tenant_id, build_policy_id, actor_id, content_hash
        ),
    CHECK (terminal = (status IN ('CERTIFIED','REJECTED','FAILED')))
);

CREATE TABLE yaya_skill_build_history (
    tenant_id TEXT NOT NULL,
    build_id TEXT NOT NULL,
    sequence BIGINT NOT NULL CHECK (sequence >= 1),
    status TEXT NOT NULL CHECK (
        status IN ('ACCEPTED','QUEUED','COMPILING','TESTING','CERTIFYING','CERTIFIED','REJECTED','FAILED')
    ),
    record_sha256 CHAR(64) NOT NULL CHECK (record_sha256 ~ '^[a-f0-9]{64}$'),
    record_json JSONB NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, build_id, sequence),
    FOREIGN KEY (tenant_id, build_id)
        REFERENCES yaya_skill_builds (tenant_id, build_id)
);

CREATE TABLE yaya_build_step_receipts (
    tenant_id TEXT NOT NULL,
    build_id TEXT NOT NULL,
    step TEXT NOT NULL CHECK (
        step IN ('VALIDATE_SOURCE','COMPILE','PUBLIC_TEST','HIDDEN_TEST','CERTIFY')
    ),
    attempt INTEGER NOT NULL CHECK (attempt >= 1),
    input_sha256 CHAR(64) NOT NULL CHECK (input_sha256 ~ '^[a-f0-9]{64}$'),
    output_sha256 CHAR(64) NOT NULL CHECK (output_sha256 ~ '^[a-f0-9]{64}$'),
    outcome TEXT NOT NULL CHECK (outcome IN ('PASSED','FAILED')),
    receipt_json JSONB NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, build_id, step, attempt),
    FOREIGN KEY (tenant_id, build_id)
        REFERENCES yaya_skill_builds (tenant_id, build_id)
);

CREATE TABLE yaya_artifacts (
    tenant_id TEXT NOT NULL,
    artifact_sha256 CHAR(64) NOT NULL CHECK (artifact_sha256 ~ '^[a-f0-9]{64}$'),
    build_id TEXT NOT NULL,
    skill_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    content_hash CHAR(64) NOT NULL CHECK (content_hash ~ '^[a-f0-9]{64}$'),
    source_sha256 CHAR(64) NOT NULL CHECK (source_sha256 ~ '^[a-f0-9]{64}$'),
    artifact_uri TEXT NOT NULL,
    metadata_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    -- The bytes are globally content-addressed on disk, while each successful
    -- Build keeps its own immutable attestation row.  This permits two
    -- independent Builds that produce identical bytes to reconcile the same
    -- physical digest without inventing a second artifact or stealing the
    -- first Build's provenance.
    PRIMARY KEY (tenant_id, artifact_sha256, build_id),
    UNIQUE (tenant_id, build_id),
    UNIQUE (tenant_id, artifact_sha256, build_id, skill_id, actor_id, content_hash),
    FOREIGN KEY (tenant_id, build_id, skill_id, actor_id, content_hash)
        REFERENCES yaya_skill_builds (
            tenant_id, build_id, skill_id, actor_id, content_hash
        )
);

CREATE TABLE yaya_skill_certifications (
    tenant_id TEXT NOT NULL,
    certification_id TEXT NOT NULL,
    build_id TEXT NOT NULL,
    skill_id TEXT NOT NULL,
    skill_version_id TEXT NOT NULL,
    artifact_sha256 CHAR(64) NOT NULL CHECK (artifact_sha256 ~ '^[a-f0-9]{64}$'),
    actor_id TEXT NOT NULL,
    content_hash CHAR(64) NOT NULL CHECK (content_hash ~ '^[a-f0-9]{64}$'),
    certification_sha256 CHAR(64) NOT NULL CHECK (certification_sha256 ~ '^[a-f0-9]{64}$'),
    record_json JSONB NOT NULL,
    issued_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (tenant_id, certification_id),
    UNIQUE (tenant_id, build_id),
    UNIQUE (tenant_id, skill_version_id),
    UNIQUE (tenant_id, certification_id, actor_id, content_hash),
    UNIQUE (
        tenant_id, certification_id, skill_id, skill_version_id,
        artifact_sha256, actor_id, content_hash
    ),
    FOREIGN KEY (
        tenant_id, artifact_sha256, build_id, skill_id, actor_id, content_hash
    ) REFERENCES yaya_artifacts (
        tenant_id, artifact_sha256, build_id, skill_id, actor_id, content_hash
    )
);

CREATE TABLE yaya_certification_revocations (
    tenant_id TEXT NOT NULL,
    revocation_id TEXT NOT NULL,
    certification_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    content_hash CHAR(64) NOT NULL CHECK (content_hash ~ '^[a-f0-9]{64}$'),
    reason TEXT NOT NULL,
    revocation_sha256 CHAR(64) NOT NULL CHECK (revocation_sha256 ~ '^[a-f0-9]{64}$'),
    record_json JSONB NOT NULL,
    revoked_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, revocation_id),
    UNIQUE (tenant_id, certification_id),
    FOREIGN KEY (tenant_id, certification_id, actor_id, content_hash)
        REFERENCES yaya_skill_certifications (
            tenant_id, certification_id, actor_id, content_hash
        )
);

CREATE TABLE yaya_session_skill_versions (
    tenant_id TEXT NOT NULL,
    binding_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    skill_id TEXT NOT NULL,
    skill_version_id TEXT NOT NULL,
    certification_id TEXT NOT NULL,
    artifact_sha256 CHAR(64) NOT NULL CHECK (artifact_sha256 ~ '^[a-f0-9]{64}$'),
    actor_id TEXT NOT NULL,
    content_hash CHAR(64) NOT NULL CHECK (content_hash ~ '^[a-f0-9]{64}$'),
    binding_sha256 CHAR(64) NOT NULL CHECK (binding_sha256 ~ '^[a-f0-9]{64}$'),
    bound_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, binding_id),
    UNIQUE (tenant_id, session_id, skill_id, skill_version_id),
    FOREIGN KEY (tenant_id, session_id, actor_id, content_hash)
        REFERENCES yaya_public_agent_sessions (
            tenant_id, session_id, actor_id, content_hash
        ),
    FOREIGN KEY (
        tenant_id, certification_id, skill_id, skill_version_id,
        artifact_sha256, actor_id, content_hash
    ) REFERENCES yaya_skill_certifications (
        tenant_id, certification_id, skill_id, skill_version_id,
        artifact_sha256, actor_id, content_hash
    )
);

CREATE TABLE yaya_registry_heads (
    tenant_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    content_hash CHAR(64) NOT NULL CHECK (content_hash ~ '^[a-f0-9]{64}$'),
    world_id TEXT NOT NULL,
    agent_profile_id TEXT NOT NULL,
    skill_id TEXT NOT NULL,
    revision BIGINT NOT NULL DEFAULT 0 CHECK (revision >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (
        tenant_id, actor_id, content_hash, world_id, agent_profile_id, skill_id
    ),
    FOREIGN KEY (tenant_id, world_id, actor_id, content_hash)
        REFERENCES yaya_worlds (tenant_id, world_id, actor_id, content_hash),
    FOREIGN KEY (tenant_id, agent_profile_id, actor_id, content_hash)
        REFERENCES yaya_agent_profiles (
            tenant_id, agent_profile_id, actor_id, content_hash
        )
);

CREATE TABLE yaya_registry_entries (
    tenant_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    content_hash CHAR(64) NOT NULL CHECK (content_hash ~ '^[a-f0-9]{64}$'),
    world_id TEXT NOT NULL,
    agent_profile_id TEXT NOT NULL,
    skill_id TEXT NOT NULL,
    revision BIGINT NOT NULL CHECK (revision >= 1),
    skill_version_id TEXT NOT NULL,
    certification_id TEXT NOT NULL,
    artifact_sha256 CHAR(64) NOT NULL CHECK (artifact_sha256 ~ '^[a-f0-9]{64}$'),
    previous_revision BIGINT NOT NULL CHECK (previous_revision >= 0),
    entry_sha256 CHAR(64) NOT NULL CHECK (entry_sha256 ~ '^[a-f0-9]{64}$'),
    record_json JSONB NOT NULL,
    activated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (
        tenant_id, actor_id, content_hash, world_id, agent_profile_id,
        skill_id, revision
    ),
    FOREIGN KEY (
        tenant_id, actor_id, content_hash, world_id, agent_profile_id, skill_id
    ) REFERENCES yaya_registry_heads (
        tenant_id, actor_id, content_hash, world_id, agent_profile_id, skill_id
    ),
    FOREIGN KEY (
        tenant_id, certification_id, skill_id, skill_version_id,
        artifact_sha256, actor_id, content_hash
    ) REFERENCES yaya_skill_certifications (
        tenant_id, certification_id, skill_id, skill_version_id,
        artifact_sha256, actor_id, content_hash
    ),
    CHECK (revision = previous_revision + 1)
);

CREATE TABLE yaya_skill_activations (
    tenant_id TEXT NOT NULL,
    activation_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    content_hash CHAR(64) NOT NULL CHECK (content_hash ~ '^[a-f0-9]{64}$'),
    world_id TEXT NOT NULL,
    agent_profile_id TEXT NOT NULL,
    skill_id TEXT NOT NULL,
    skill_version_id TEXT NOT NULL,
    certification_id TEXT NOT NULL,
    artifact_sha256 CHAR(64) NOT NULL CHECK (artifact_sha256 ~ '^[a-f0-9]{64}$'),
    previous_registry_revision BIGINT NOT NULL CHECK (previous_registry_revision >= 0),
    registry_revision BIGINT NOT NULL CHECK (registry_revision >= 1),
    activation_sha256 CHAR(64) NOT NULL CHECK (activation_sha256 ~ '^[a-f0-9]{64}$'),
    record_json JSONB NOT NULL,
    activated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, activation_id),
    UNIQUE (
        tenant_id, actor_id, content_hash, world_id, agent_profile_id,
        skill_id, registry_revision
    ),
    FOREIGN KEY (
        tenant_id, actor_id, content_hash, world_id, agent_profile_id,
        skill_id, registry_revision
    ) REFERENCES yaya_registry_entries (
        tenant_id, actor_id, content_hash, world_id, agent_profile_id,
        skill_id, revision
    ),
    FOREIGN KEY (
        tenant_id, certification_id, skill_id, skill_version_id,
        artifact_sha256, actor_id, content_hash
    ) REFERENCES yaya_skill_certifications (
        tenant_id, certification_id, skill_id, skill_version_id,
        artifact_sha256, actor_id, content_hash
    ),
    CHECK (registry_revision = previous_registry_revision + 1)
);

DO $$
DECLARE immutable_table TEXT;
BEGIN
    FOREACH immutable_table IN ARRAY ARRAY[
        'yaya_learners', 'yaya_agent_profiles',
        'yaya_skill_draft_revisions',
        'yaya_product_write_receipts', 'yaya_skill_build_history',
        'yaya_build_step_receipts', 'yaya_artifacts',
        'yaya_skill_certifications', 'yaya_certification_revocations',
        'yaya_session_skill_versions', 'yaya_registry_entries',
        'yaya_skill_activations'
    ] LOOP
        EXECUTE format(
            'CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON %I '
            'FOR EACH ROW EXECUTE FUNCTION yaya_reject_immutable_mutation()',
            immutable_table || '_immutable', immutable_table
        );
    END LOOP;
END;
$$;

CREATE TRIGGER yaya_launch_authorities_retirement_guard
BEFORE UPDATE OR DELETE ON yaya_launch_authorities
FOR EACH ROW EXECUTE FUNCTION yaya_allow_only_active_retirement();

CREATE TRIGGER yaya_build_policies_retirement_guard
BEFORE UPDATE OR DELETE ON yaya_build_policies
FOR EACH ROW EXECUTE FUNCTION yaya_allow_only_active_retirement();

CREATE TRIGGER yaya_public_agent_sessions_scope_guard
BEFORE UPDATE OR DELETE ON yaya_public_agent_sessions
FOR EACH ROW EXECUTE FUNCTION yaya_guard_public_agent_session_scope();

CREATE TRIGGER yaya_skill_builds_head_guard
BEFORE UPDATE OR DELETE ON yaya_skill_builds
FOR EACH ROW EXECUTE FUNCTION yaya_guard_skill_build_head_transition();

CREATE TRIGGER yaya_skills_a8_mirror_guard
BEFORE UPDATE OR DELETE ON yaya_skills
FOR EACH ROW EXECUTE FUNCTION yaya_guard_a8_skill_mirror();

CREATE TRIGGER yaya_registry_certifications_a8_mirror_guard
BEFORE UPDATE OR DELETE ON yaya_registry_certifications
FOR EACH ROW EXECUTE FUNCTION yaya_guard_a8_registry_certification_mirror();

CREATE TRIGGER yaya_compile_results_a8_mirror_guard
BEFORE UPDATE OR DELETE ON yaya_compile_results
FOR EACH ROW EXECUTE FUNCTION yaya_guard_a8_compile_result_mirror();

CREATE TRIGGER yaya_evidence_a8_mirror_guard
BEFORE UPDATE OR DELETE ON yaya_evidence
FOR EACH ROW EXECUTE FUNCTION yaya_guard_a8_evidence_mirror();
