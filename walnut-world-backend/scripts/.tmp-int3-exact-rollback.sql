\set ON_ERROR_STOP on

BEGIN TRANSACTION ISOLATION LEVEL SERIALIZABLE;

LOCK TABLE
  commands,
  workflow_jobs,
  job_step_receipts,
  idempotency_receipts,
  product_idempotency_receipts,
  agent_sessions,
  current_session_bindings,
  product_workspaces,
  product_skill_drafts,
  product_skill_draft_revisions,
  skill_builds,
  skill_build_provenance,
  skill_build_terminal_authority
IN ACCESS EXCLUSIVE MODE;

CREATE TEMP TABLE rollback_fingerprint_baseline (
  fingerprint_name text PRIMARY KEY,
  row_count bigint NOT NULL,
  material_md5 text NOT NULL
) ON COMMIT DROP;

INSERT INTO rollback_fingerprint_baseline
SELECT
  'audit_records',
  count(*),
  md5(COALESCE(jsonb_agg(to_jsonb(audit_row) ORDER BY audit_row.audit_id)::text, '[]'))
FROM audit_records AS audit_row
WHERE tenant_id = 'tenant_yaya';

INSERT INTO rollback_fingerprint_baseline
SELECT
  'seven_prerequisite_authorities',
  7,
  md5(jsonb_build_object(
    'product_content_units', (
      SELECT to_jsonb(authority_row) FROM product_content_units AS authority_row
      WHERE tenant_id = 'tenant_yaya'
    ),
    'world_snapshots', (
      SELECT to_jsonb(authority_row) FROM world_snapshots AS authority_row
      WHERE tenant_id = 'tenant_yaya'
    ),
    'learner_profiles', (
      SELECT to_jsonb(authority_row) FROM learner_profiles AS authority_row
      WHERE tenant_id = 'tenant_yaya'
    ),
    'agent_profiles', (
      SELECT to_jsonb(authority_row) FROM agent_profiles AS authority_row
      WHERE tenant_id = 'tenant_yaya'
    ),
    'build_policies', (
      SELECT to_jsonb(authority_row) FROM build_policies AS authority_row
      WHERE tenant_id = 'tenant_yaya'
    ),
    'launch_authorities', (
      SELECT to_jsonb(authority_row) FROM launch_authorities AS authority_row
      WHERE tenant_id = 'tenant_yaya'
    ),
    'registry_heads', (
      SELECT to_jsonb(authority_row) FROM registry_heads AS authority_row
      WHERE tenant_id = 'tenant_yaya'
    )
  )::text);

CREATE TEMP TABLE rollback_cross_tenant_baseline (
  table_name text PRIMARY KEY,
  row_count bigint NOT NULL,
  material_md5 text NOT NULL
) ON COMMIT DROP;

DO $cross_tenant_capture$
DECLARE
  candidate record;
  captured_count bigint;
  captured_md5 text;
BEGIN
  FOR candidate IN
    SELECT DISTINCT table_name
    FROM information_schema.columns
    WHERE table_schema = 'public' AND column_name = 'tenant_id'
    ORDER BY table_name
  LOOP
    EXECUTE format(
      'SELECT count(*), md5(COALESCE(jsonb_agg(to_jsonb(row_value) '
      'ORDER BY to_jsonb(row_value)::text)::text, ''[]'')) '
      'FROM public.%I AS row_value WHERE tenant_id IS DISTINCT FROM $1',
      candidate.table_name
    ) INTO captured_count, captured_md5 USING 'tenant_yaya';
    INSERT INTO rollback_cross_tenant_baseline
      (table_name, row_count, material_md5)
    VALUES (candidate.table_name, captured_count, captured_md5);
  END LOOP;
END
$cross_tenant_capture$;

DO $rollback_guard$
DECLARE
  unexpected_count bigint;
BEGIN
  IF (
    SELECT count(*)
    FROM pg_trigger AS trigger_row
    JOIN pg_class AS trigger_table
      ON trigger_table.oid = trigger_row.tgrelid
    JOIN pg_namespace AS trigger_namespace
      ON trigger_namespace.oid = trigger_table.relnamespace
    JOIN pg_proc AS trigger_function
      ON trigger_function.oid = trigger_row.tgfoid
    WHERE (
      trigger_table.relname,
      trigger_row.tgname,
      trigger_function.proname
    ) IN (
      (
        'skill_build_terminal_authority',
        'trg_skill_build_terminal_authority_append_only',
        'int2_reject_authority_mutation'
      ),
      (
        'skill_build_provenance',
        'trg_skill_build_provenance_append_only',
        'int2_reject_authority_mutation'
      ),
      (
        'product_skill_draft_revisions',
        'trg_product_skill_draft_revisions_append_only',
        'int2_reject_authority_mutation'
      )
    )
      AND NOT trigger_row.tgisinternal
      AND trigger_row.tgenabled = 'O'
      AND trigger_namespace.nspname = 'public'
  ) <> 3 THEN
    RAISE EXCEPTION 'exact append-only trigger guard failed';
  END IF;

  IF (
    SELECT count(*)
    FROM pg_trigger AS trigger_row
    JOIN pg_class AS trigger_table
      ON trigger_table.oid = trigger_row.tgrelid
    JOIN pg_namespace AS trigger_namespace
      ON trigger_namespace.oid = trigger_table.relnamespace
    WHERE NOT trigger_row.tgisinternal
      AND trigger_namespace.nspname = 'public'
      AND trigger_table.relname IN (
        'commands',
        'workflow_jobs',
        'job_step_receipts',
        'idempotency_receipts',
        'product_idempotency_receipts',
        'agent_sessions',
        'current_session_bindings',
        'product_workspaces',
        'product_skill_drafts',
        'product_skill_draft_revisions',
        'skill_builds',
        'skill_build_provenance',
        'skill_build_terminal_authority'
      )
  ) <> 3 THEN
    RAISE EXCEPTION 'unexpected non-internal trigger exists on scoped tables';
  END IF;

  IF (SELECT count(*) FROM agent_sessions WHERE tenant_id = 'tenant_yaya') <> 1
     OR NOT EXISTS (
       SELECT 1 FROM agent_sessions
       WHERE tenant_id = 'tenant_yaya'
         AND session_id = 'session_70337aec0566c2117dacfac8'
         AND command_id = 'cmd_a20b022c2da04c4eb6d28afb9f4933ef'
         AND status = 'ACTIVE'
         AND created_at = timestamptz '2026-08-15 21:03:25.706618+00'
     ) THEN
    RAISE EXCEPTION 'exact agent_session guard failed';
  END IF;

  IF (SELECT count(*) FROM current_session_bindings WHERE tenant_id = 'tenant_yaya') <> 1
     OR NOT EXISTS (
       SELECT 1 FROM current_session_bindings
       WHERE tenant_id = 'tenant_yaya'
         AND binding_id = 'binding_5e04f1bc97caf1be9b77011c'
         AND session_id = 'session_70337aec0566c2117dacfac8'
         AND authority_id = 'authority_build_e2e_0001'
         AND bound_at = timestamptz '2026-08-15 21:03:25.882351+00'
     ) THEN
    RAISE EXCEPTION 'exact current_session_binding guard failed';
  END IF;

  IF (SELECT count(*) FROM product_workspaces WHERE tenant_id = 'tenant_yaya') <> 1
     OR NOT EXISTS (
       SELECT 1 FROM product_workspaces
       WHERE tenant_id = 'tenant_yaya'
         AND workspace_id = 'workspace_6e11a9eb429e9b2389a447d2'
         AND session_id = 'session_70337aec0566c2117dacfac8'
         AND workspace_revision = 2
         AND updated_at = timestamptz '2026-08-15 21:03:27.444401+00'
     ) THEN
    RAISE EXCEPTION 'exact product_workspace guard failed';
  END IF;

  IF (SELECT count(*) FROM product_skill_drafts WHERE tenant_id = 'tenant_yaya') <> 1
     OR NOT EXISTS (
       SELECT 1 FROM product_skill_drafts
       WHERE tenant_id = 'tenant_yaya'
         AND draft_row_id = 1
         AND draft_id = 'draft_0595f9ada8b875045705739b'
         AND session_id = 'session_70337aec0566c2117dacfac8'
         AND revision = 2
         AND draft_sha256 = '39914052296c5b4332f5b11981d8217ca9c4871fc4e6c7befae2aeb79dc860e5'
     ) THEN
    RAISE EXCEPTION 'exact product_skill_draft guard failed';
  END IF;

  IF (SELECT count(*) FROM product_skill_draft_revisions WHERE tenant_id = 'tenant_yaya') <> 2
     OR NOT EXISTS (
       SELECT 1 FROM product_skill_draft_revisions
       WHERE tenant_id = 'tenant_yaya'
         AND draft_revision_row_id = 1
         AND parent_revision_row_id IS NULL
         AND draft_id = 'draft_0595f9ada8b875045705739b'
         AND revision = 1
         AND draft_sha256 = '860c4f8e5cc23ab6df7dbbee1df4ff7266f54d12e7bcefc3cf2b03d5697a7523'
     )
     OR NOT EXISTS (
       SELECT 1 FROM product_skill_draft_revisions
       WHERE tenant_id = 'tenant_yaya'
         AND draft_revision_row_id = 2
         AND parent_revision_row_id = 1
         AND draft_id = 'draft_0595f9ada8b875045705739b'
         AND revision = 2
         AND draft_sha256 = '39914052296c5b4332f5b11981d8217ca9c4871fc4e6c7befae2aeb79dc860e5'
     ) THEN
    RAISE EXCEPTION 'exact draft revision guard failed';
  END IF;

  IF (SELECT count(*) FROM commands WHERE tenant_id = 'tenant_yaya') <> 2
     OR NOT EXISTS (
       SELECT 1 FROM commands
       WHERE tenant_id = 'tenant_yaya'
         AND command_id = 'cmd_a20b022c2da04c4eb6d28afb9f4933ef'
         AND command_type = 'CREATE_AGENT_SESSION'
         AND status = 'APPLIED'
         AND revision = 3
         AND terminal
         AND accepted_at = timestamptz '2026-08-15 21:03:25.706618+00'
         AND updated_at = timestamptz '2026-08-15 21:03:25.940684+00'
     )
     OR NOT EXISTS (
       SELECT 1 FROM commands
       WHERE tenant_id = 'tenant_yaya'
         AND command_id = 'cmd_36d44bd7fef54855965ff039bcc373ce'
         AND command_type = 'CREATE_SKILL_BUILD'
         AND status = 'REJECTED'
         AND revision = 3
         AND terminal
         AND accepted_at = timestamptz '2026-08-15 21:03:27.624233+00'
         AND updated_at = timestamptz '2026-08-15 21:03:29.251491+00'
     ) THEN
    RAISE EXCEPTION 'exact command guard failed';
  END IF;

  IF (SELECT count(*) FROM workflow_jobs WHERE tenant_id = 'tenant_yaya') <> 2
     OR NOT EXISTS (
       SELECT 1 FROM workflow_jobs
       WHERE tenant_id = 'tenant_yaya'
         AND job_id = 'job_9c4e812edae54e750be08fa9'
         AND command_id = 'cmd_a20b022c2da04c4eb6d28afb9f4933ef'
         AND operation = 'CREATE_AGENT_SESSION'
         AND subject_type = 'AGENT_SESSION'
         AND subject_id = 'session_70337aec0566c2117dacfac8'
         AND phase = 'COMPLETE'
         AND status = 'SUCCEEDED'
         AND attempt = 1
         AND fencing_token = 1
         AND last_error_json = 'null'::jsonb
         AND created_at = timestamptz '2026-08-15 21:03:25.706618+00'
         AND updated_at = timestamptz '2026-08-15 21:03:25.955951+00'
     )
     OR NOT EXISTS (
       SELECT 1 FROM workflow_jobs
       WHERE tenant_id = 'tenant_yaya'
         AND job_id = 'job_985b545f8b1c3db03f3c9551'
         AND command_id = 'cmd_36d44bd7fef54855965ff039bcc373ce'
         AND operation = 'CREATE_SKILL_BUILD'
         AND subject_type = 'SKILL_BUILD'
         AND subject_id = 'build_c1279bc22f88575163b8e337'
         AND phase = 'COMPILE'
         AND status = 'FAILED'
         AND attempt = 1
         AND fencing_token = 1
         AND last_error_json->>'code' = 'SANDBOX_COMPILE_ERROR'
         AND created_at = timestamptz '2026-08-15 21:03:27.624233+00'
         AND updated_at = timestamptz '2026-08-15 21:03:29.267558+00'
     ) THEN
    RAISE EXCEPTION 'exact workflow guard failed';
  END IF;

  IF (SELECT count(*) FROM job_step_receipts WHERE tenant_id = 'tenant_yaya') <> 2
     OR NOT EXISTS (
       SELECT 1 FROM job_step_receipts
       WHERE tenant_id = 'tenant_yaya'
         AND receipt_id = 'receipt_e42a1e490eb4c84609b37c66'
         AND job_id = 'job_9c4e812edae54e750be08fa9'
         AND step_name = 'SESSION_BOUND'
         AND fencing_token = 1
         AND input_sha256 = '62a1c61b12e54204e4706ff1b093643ca664a340d324fa884adf3d09ea127cb1'
         AND output_sha256 = '893b3e8999cd66c77b6ff637a67ddd882a21b9b146cbaf75a5a649a213e0fd38'
         AND completed_at = timestamptz '2026-08-15 21:03:25.903602+00'
     )
     OR NOT EXISTS (
       SELECT 1 FROM job_step_receipts
       WHERE tenant_id = 'tenant_yaya'
         AND receipt_id = 'receipt_a3efb4cef86c480f55eed33b'
         AND job_id = 'job_985b545f8b1c3db03f3c9551'
         AND step_name = 'BUILD_REJECTED'
         AND fencing_token = 1
         AND input_sha256 = 'a69d564a2b25feb1997f71df82a18c5a018a711d0b61bf2c68dfca4c3d374ff1'
         AND output_sha256 = 'f843130adecdcf967dd6b77dc1cdbb41cf6bb7791369a1a06008c98d6cd47c04'
         AND completed_at = timestamptz '2026-08-15 21:03:29.253182+00'
     ) THEN
    RAISE EXCEPTION 'exact job receipt guard failed';
  END IF;

  IF (SELECT count(*) FROM idempotency_receipts WHERE tenant_id = 'tenant_yaya') <> 2
     OR NOT EXISTS (
       SELECT 1 FROM idempotency_receipts
       WHERE tenant_id = 'tenant_yaya'
         AND receipt_id = 1
         AND operation = 'CREATE_AGENT_SESSION'
         AND idempotency_key = 'idem_createagentsession_7aec13d1cabfcbd5b2c57c5a6f1fb47a'
         AND command_id = 'cmd_a20b022c2da04c4eb6d28afb9f4933ef'
         AND accepted_at = timestamptz '2026-08-15 21:03:25.706618+00'
     )
     OR NOT EXISTS (
       SELECT 1 FROM idempotency_receipts
       WHERE tenant_id = 'tenant_yaya'
         AND receipt_id = 2
         AND operation = 'CREATE_SKILL_BUILD'
         AND idempotency_key = 'idem_createskillbuild_b78f7fa7c165d4cec82399c85b9b9ffc'
         AND command_id = 'cmd_36d44bd7fef54855965ff039bcc373ce'
         AND accepted_at = timestamptz '2026-08-15 21:03:27.624233+00'
     ) THEN
    RAISE EXCEPTION 'exact idempotency guard failed';
  END IF;

  IF (SELECT count(*) FROM product_idempotency_receipts WHERE tenant_id = 'tenant_yaya') <> 1
     OR NOT EXISTS (
       SELECT 1 FROM product_idempotency_receipts
       WHERE tenant_id = 'tenant_yaya'
         AND receipt_id = 1
         AND operation = 'upsertProductSkillDraft'
         AND canonical_path = '/product-experience/v1/sessions/session_70337aec0566c2117dacfac8/skill-drafts/draft_0595f9ada8b875045705739b'
         AND idempotency_key = 'idem_upsertproductskilldraft_0562364a5df20b556994a82fef1a1da7'
         AND resource_id = 'draft_0595f9ada8b875045705739b'
         AND http_status = 200
         AND original_trace_id = 'trace_client_20260815210325_937778'
         AND created_at = timestamptz '2026-08-15 21:03:27.444401+00'
     ) THEN
    RAISE EXCEPTION 'exact product idempotency guard failed';
  END IF;

  IF (SELECT count(*) FROM skill_builds WHERE tenant_id = 'tenant_yaya') <> 1
     OR NOT EXISTS (
       SELECT 1 FROM skill_builds
       WHERE tenant_id = 'tenant_yaya'
         AND build_id = 'build_c1279bc22f88575163b8e337'
         AND command_id = 'cmd_36d44bd7fef54855965ff039bcc373ce'
         AND status = 'REJECTED'
         AND created_at = timestamptz '2026-08-15 21:03:27.624233+00'
     ) THEN
    RAISE EXCEPTION 'exact skill_build guard failed';
  END IF;

  IF (SELECT count(*) FROM skill_build_provenance WHERE tenant_id = 'tenant_yaya') <> 1
     OR NOT EXISTS (
       SELECT 1 FROM skill_build_provenance
       WHERE tenant_id = 'tenant_yaya'
         AND build_id = 'build_c1279bc22f88575163b8e337'
         AND workflow_job_id = 'job_985b545f8b1c3db03f3c9551'
         AND command_receipt_id = 2
         AND session_id = 'session_70337aec0566c2117dacfac8'
         AND draft_revision = 2
         AND created_at = timestamptz '2026-08-15 21:03:27.624233+00'
     ) THEN
    RAISE EXCEPTION 'exact build provenance guard failed';
  END IF;

  IF (SELECT count(*) FROM skill_build_terminal_authority WHERE tenant_id = 'tenant_yaya') <> 1
     OR NOT EXISTS (
       SELECT 1 FROM skill_build_terminal_authority
       WHERE tenant_id = 'tenant_yaya'
         AND build_id = 'build_c1279bc22f88575163b8e337'
         AND terminal_status = 'REJECTED'
         AND command_id = 'cmd_36d44bd7fef54855965ff039bcc373ce'
         AND workflow_job_id = 'job_985b545f8b1c3db03f3c9551'
         AND terminal_receipt_id = 'receipt_a3efb4cef86c480f55eed33b'
         AND created_at = timestamptz '2026-08-15 21:03:29.251491+00'
     ) THEN
    RAISE EXCEPTION 'exact build terminal guard failed';
  END IF;

  IF (SELECT count(*) FROM game_runs WHERE tenant_id = 'tenant_yaya') <> 0
     OR (SELECT count(*) FROM game_evidence WHERE tenant_id = 'tenant_yaya') <> 0
     OR (SELECT count(*) FROM learner_projection_jobs WHERE tenant_id = 'tenant_yaya') <> 0
     OR (SELECT count(*) FROM product_agent_interactions WHERE tenant_id = 'tenant_yaya') <> 0
     OR (SELECT count(*) FROM recoverable_llm_dispatches) <> 0 THEN
    RAISE EXCEPTION 'downstream authority is no longer empty';
  END IF;

  IF (SELECT count(*) FROM audit_records WHERE tenant_id = 'tenant_yaya') <> 10 THEN
    RAISE EXCEPTION 'append-only audit count drifted before rollback';
  END IF;
END
$rollback_guard$;

-- These are the only three non-internal triggers on the thirteen scoped
-- failure-chain tables.  ALTER TABLE ... DISABLE TRIGGER <exact-name> leaves
-- every internal FK/constraint trigger enabled.  PostgreSQL also rolls these
-- ALTER statements back if any later assertion or DELETE fails.
ALTER TABLE skill_build_terminal_authority
  DISABLE TRIGGER trg_skill_build_terminal_authority_append_only;
ALTER TABLE skill_build_provenance
  DISABLE TRIGGER trg_skill_build_provenance_append_only;
ALTER TABLE product_skill_draft_revisions
  DISABLE TRIGGER trg_product_skill_draft_revisions_append_only;

DELETE FROM skill_build_terminal_authority
WHERE tenant_id = 'tenant_yaya'
  AND build_id = 'build_c1279bc22f88575163b8e337';

DELETE FROM skill_build_provenance
WHERE tenant_id = 'tenant_yaya'
  AND build_id = 'build_c1279bc22f88575163b8e337';

DELETE FROM job_step_receipts
WHERE tenant_id = 'tenant_yaya'
  AND job_id IN (
    'job_9c4e812edae54e750be08fa9',
    'job_985b545f8b1c3db03f3c9551'
  );

DELETE FROM workflow_jobs
WHERE tenant_id = 'tenant_yaya'
  AND job_id IN (
    'job_9c4e812edae54e750be08fa9',
    'job_985b545f8b1c3db03f3c9551'
  );

DELETE FROM skill_builds
WHERE tenant_id = 'tenant_yaya'
  AND build_id = 'build_c1279bc22f88575163b8e337';

DELETE FROM product_idempotency_receipts
WHERE tenant_id = 'tenant_yaya'
  AND receipt_id = 1
  AND operation = 'upsertProductSkillDraft'
  AND resource_id = 'draft_0595f9ada8b875045705739b';

DELETE FROM idempotency_receipts
WHERE tenant_id = 'tenant_yaya'
  AND receipt_id IN (1, 2)
  AND command_id IN (
    'cmd_a20b022c2da04c4eb6d28afb9f4933ef',
    'cmd_36d44bd7fef54855965ff039bcc373ce'
  );

DELETE FROM product_skill_draft_revisions
WHERE tenant_id = 'tenant_yaya'
  AND draft_revision_row_id = 2
  AND draft_id = 'draft_0595f9ada8b875045705739b';

DELETE FROM product_skill_draft_revisions
WHERE tenant_id = 'tenant_yaya'
  AND draft_revision_row_id = 1
  AND draft_id = 'draft_0595f9ada8b875045705739b';

DELETE FROM product_skill_drafts
WHERE tenant_id = 'tenant_yaya'
  AND draft_row_id = 1
  AND draft_id = 'draft_0595f9ada8b875045705739b';

DELETE FROM product_workspaces
WHERE tenant_id = 'tenant_yaya'
  AND workspace_id = 'workspace_6e11a9eb429e9b2389a447d2'
  AND session_id = 'session_70337aec0566c2117dacfac8';

DELETE FROM current_session_bindings
WHERE tenant_id = 'tenant_yaya'
  AND binding_id = 'binding_5e04f1bc97caf1be9b77011c'
  AND session_id = 'session_70337aec0566c2117dacfac8';

DELETE FROM agent_sessions
WHERE tenant_id = 'tenant_yaya'
  AND session_id = 'session_70337aec0566c2117dacfac8'
  AND command_id = 'cmd_a20b022c2da04c4eb6d28afb9f4933ef';

DELETE FROM commands
WHERE tenant_id = 'tenant_yaya'
  AND command_id IN (
    'cmd_a20b022c2da04c4eb6d28afb9f4933ef',
    'cmd_36d44bd7fef54855965ff039bcc373ce'
  );

ALTER TABLE product_skill_draft_revisions
  ENABLE TRIGGER trg_product_skill_draft_revisions_append_only;
ALTER TABLE skill_build_provenance
  ENABLE TRIGGER trg_skill_build_provenance_append_only;
ALTER TABLE skill_build_terminal_authority
  ENABLE TRIGGER trg_skill_build_terminal_authority_append_only;

DO $post_rollback_guard$
DECLARE
  candidate record;
  row_count bigint;
  material_md5 text;
BEGIN
  IF (
    SELECT count(*)
    FROM pg_trigger AS trigger_row
    JOIN pg_class AS trigger_table
      ON trigger_table.oid = trigger_row.tgrelid
    JOIN pg_namespace AS trigger_namespace
      ON trigger_namespace.oid = trigger_table.relnamespace
    JOIN pg_proc AS trigger_function
      ON trigger_function.oid = trigger_row.tgfoid
    WHERE (
      trigger_table.relname,
      trigger_row.tgname,
      trigger_function.proname
    ) IN (
      (
        'skill_build_terminal_authority',
        'trg_skill_build_terminal_authority_append_only',
        'int2_reject_authority_mutation'
      ),
      (
        'skill_build_provenance',
        'trg_skill_build_provenance_append_only',
        'int2_reject_authority_mutation'
      ),
      (
        'product_skill_draft_revisions',
        'trg_product_skill_draft_revisions_append_only',
        'int2_reject_authority_mutation'
      )
    )
      AND NOT trigger_row.tgisinternal
      AND trigger_row.tgenabled = 'O'
      AND trigger_namespace.nspname = 'public'
  ) <> 3 THEN
    RAISE EXCEPTION 'named append-only triggers were not restored';
  END IF;

  IF (
    SELECT count(*)
    FROM pg_trigger AS trigger_row
    JOIN pg_class AS trigger_table
      ON trigger_table.oid = trigger_row.tgrelid
    JOIN pg_namespace AS trigger_namespace
      ON trigger_namespace.oid = trigger_table.relnamespace
    WHERE NOT trigger_row.tgisinternal
      AND trigger_namespace.nspname = 'public'
      AND trigger_table.relname IN (
        'commands',
        'workflow_jobs',
        'job_step_receipts',
        'idempotency_receipts',
        'product_idempotency_receipts',
        'agent_sessions',
        'current_session_bindings',
        'product_workspaces',
        'product_skill_drafts',
        'product_skill_draft_revisions',
        'skill_builds',
        'skill_build_provenance',
        'skill_build_terminal_authority'
      )
  ) <> 3 THEN
    RAISE EXCEPTION 'scoped non-internal trigger set changed';
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM rollback_fingerprint_baseline AS baseline
    WHERE baseline.fingerprint_name = 'audit_records'
      AND baseline.row_count = 10
      AND baseline.row_count = (
        SELECT count(*) FROM audit_records WHERE tenant_id = 'tenant_yaya'
      )
      AND baseline.material_md5 = (
        SELECT md5(COALESCE(
          jsonb_agg(to_jsonb(audit_row) ORDER BY audit_row.audit_id)::text,
          '[]'
        ))
        FROM audit_records AS audit_row
        WHERE tenant_id = 'tenant_yaya'
      )
  ) THEN
    RAISE EXCEPTION 'append-only audit fingerprint changed';
  END IF;

  IF (SELECT count(*) FROM product_content_units WHERE tenant_id = 'tenant_yaya') <> 1
     OR (SELECT count(*) FROM world_snapshots WHERE tenant_id = 'tenant_yaya') <> 1
     OR (SELECT count(*) FROM learner_profiles WHERE tenant_id = 'tenant_yaya') <> 1
     OR (SELECT count(*) FROM agent_profiles WHERE tenant_id = 'tenant_yaya') <> 1
     OR (SELECT count(*) FROM build_policies WHERE tenant_id = 'tenant_yaya') <> 1
     OR (SELECT count(*) FROM launch_authorities WHERE tenant_id = 'tenant_yaya') <> 1
     OR (SELECT count(*) FROM registry_heads WHERE tenant_id = 'tenant_yaya') <> 1 THEN
    RAISE EXCEPTION 'one of seven prerequisite authorities changed';
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM rollback_fingerprint_baseline AS baseline
    WHERE baseline.fingerprint_name = 'seven_prerequisite_authorities'
      AND baseline.row_count = 7
      AND baseline.material_md5 = md5(jsonb_build_object(
        'product_content_units', (
          SELECT to_jsonb(authority_row) FROM product_content_units AS authority_row
          WHERE tenant_id = 'tenant_yaya'
        ),
        'world_snapshots', (
          SELECT to_jsonb(authority_row) FROM world_snapshots AS authority_row
          WHERE tenant_id = 'tenant_yaya'
        ),
        'learner_profiles', (
          SELECT to_jsonb(authority_row) FROM learner_profiles AS authority_row
          WHERE tenant_id = 'tenant_yaya'
        ),
        'agent_profiles', (
          SELECT to_jsonb(authority_row) FROM agent_profiles AS authority_row
          WHERE tenant_id = 'tenant_yaya'
        ),
        'build_policies', (
          SELECT to_jsonb(authority_row) FROM build_policies AS authority_row
          WHERE tenant_id = 'tenant_yaya'
        ),
        'launch_authorities', (
          SELECT to_jsonb(authority_row) FROM launch_authorities AS authority_row
          WHERE tenant_id = 'tenant_yaya'
        ),
        'registry_heads', (
          SELECT to_jsonb(authority_row) FROM registry_heads AS authority_row
          WHERE tenant_id = 'tenant_yaya'
        )
      )::text)
  ) THEN
    RAISE EXCEPTION 'seven prerequisite authority fingerprint changed';
  END IF;

  IF (SELECT count(*) FROM agent_sessions WHERE tenant_id = 'tenant_yaya') <> 0
     OR (SELECT count(*) FROM current_session_bindings WHERE tenant_id = 'tenant_yaya') <> 0
     OR (SELECT count(*) FROM product_workspaces WHERE tenant_id = 'tenant_yaya') <> 0
     OR (SELECT count(*) FROM product_skill_drafts WHERE tenant_id = 'tenant_yaya') <> 0
     OR (SELECT count(*) FROM product_skill_draft_revisions WHERE tenant_id = 'tenant_yaya') <> 0
     OR (SELECT count(*) FROM skill_builds WHERE tenant_id = 'tenant_yaya') <> 0
     OR (SELECT count(*) FROM skill_build_provenance WHERE tenant_id = 'tenant_yaya') <> 0
     OR (SELECT count(*) FROM skill_build_terminal_authority WHERE tenant_id = 'tenant_yaya') <> 0
     OR (SELECT count(*) FROM commands WHERE tenant_id = 'tenant_yaya') <> 0
     OR (SELECT count(*) FROM workflow_jobs WHERE tenant_id = 'tenant_yaya') <> 0
     OR (SELECT count(*) FROM job_step_receipts WHERE tenant_id = 'tenant_yaya') <> 0
     OR (SELECT count(*) FROM idempotency_receipts WHERE tenant_id = 'tenant_yaya') <> 0
     OR (SELECT count(*) FROM product_idempotency_receipts WHERE tenant_id = 'tenant_yaya') <> 0 THEN
    RAISE EXCEPTION 'one of thirteen scoped failure-chain tables is not empty';
  END IF;

  FOR candidate IN
    SELECT DISTINCT table_name
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND column_name = 'tenant_id'
      AND table_name NOT IN (
        'audit_records',
        'product_content_units',
        'world_snapshots',
        'learner_profiles',
        'agent_profiles',
        'build_policies',
        'launch_authorities',
        'registry_heads'
      )
    ORDER BY table_name
  LOOP
    EXECUTE format(
      'SELECT count(*) FROM public.%I WHERE tenant_id = $1',
      candidate.table_name
    ) INTO row_count USING 'tenant_yaya';
    IF row_count <> 0 THEN
      RAISE EXCEPTION 'unexpected rows remain in %: %', candidate.table_name, row_count;
    END IF;
  END LOOP;

  FOR candidate IN
    SELECT
      baseline.table_name,
      baseline.row_count,
      baseline.material_md5
    FROM rollback_cross_tenant_baseline AS baseline
    ORDER BY baseline.table_name
  LOOP
    EXECUTE format(
      'SELECT count(*), md5(COALESCE(jsonb_agg(to_jsonb(row_value) '
      'ORDER BY to_jsonb(row_value)::text)::text, ''[]'')) '
      'FROM public.%I AS row_value WHERE tenant_id IS DISTINCT FROM $1',
      candidate.table_name
    ) INTO row_count, material_md5 USING 'tenant_yaya';
    IF row_count <> candidate.row_count OR material_md5 <> candidate.material_md5 THEN
      RAISE EXCEPTION 'cross-tenant fingerprint changed for %', candidate.table_name;
    END IF;
  END LOOP;

  IF (SELECT count(*) FROM recoverable_llm_dispatches) <> 0 THEN
    RAISE EXCEPTION 'relay dispatch rows remain';
  END IF;
END
$post_rollback_guard$;

COMMIT;

SELECT json_build_object(
  'status', 'EXACT_ROLLBACK_COMMITTED',
  'audit_records_preserved', (SELECT count(*) FROM audit_records WHERE tenant_id = 'tenant_yaya'),
  'product_content_units', (SELECT count(*) FROM product_content_units WHERE tenant_id = 'tenant_yaya'),
  'world_snapshots', (SELECT count(*) FROM world_snapshots WHERE tenant_id = 'tenant_yaya'),
  'learner_profiles', (SELECT count(*) FROM learner_profiles WHERE tenant_id = 'tenant_yaya'),
  'agent_profiles', (SELECT count(*) FROM agent_profiles WHERE tenant_id = 'tenant_yaya'),
  'build_policies', (SELECT count(*) FROM build_policies WHERE tenant_id = 'tenant_yaya'),
  'launch_authorities', (SELECT count(*) FROM launch_authorities WHERE tenant_id = 'tenant_yaya'),
  'registry_heads', (SELECT count(*) FROM registry_heads WHERE tenant_id = 'tenant_yaya'),
  'sessions', (SELECT count(*) FROM agent_sessions WHERE tenant_id = 'tenant_yaya'),
  'drafts', (SELECT count(*) FROM product_skill_drafts WHERE tenant_id = 'tenant_yaya'),
  'builds', (SELECT count(*) FROM skill_builds WHERE tenant_id = 'tenant_yaya'),
  'commands', (SELECT count(*) FROM commands WHERE tenant_id = 'tenant_yaya'),
  'runs', (SELECT count(*) FROM game_runs WHERE tenant_id = 'tenant_yaya'),
  'evidence', (SELECT count(*) FROM game_evidence WHERE tenant_id = 'tenant_yaya'),
  'relay_dispatches', (SELECT count(*) FROM recoverable_llm_dispatches)
)::text;
