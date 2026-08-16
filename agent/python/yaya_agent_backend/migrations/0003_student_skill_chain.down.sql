-- Explicit fail-safe reverse migration for 0003_student_skill_chain.sql.
--
-- This resource is deliberately excluded from the normal forward migrator by
-- its `.down.sql` suffix.  It must run as one PostgreSQL transaction.  A8
-- authority is immutable and cannot be losslessly projected back into the A6
-- schema, so downgrade is permitted only before any A8 business row exists.

DO $$
DECLARE
    authority_table TEXT;
    has_rows BOOLEAN;
BEGIN
    FOREACH authority_table IN ARRAY ARRAY[
        'yaya_learners', 'yaya_agent_profiles', 'yaya_launch_authorities',
        'yaya_build_policies', 'yaya_public_agent_sessions',
        'yaya_control_jobs', 'yaya_skill_draft_revisions',
        'yaya_skill_draft_heads', 'yaya_product_write_receipts',
        'yaya_skill_builds', 'yaya_skill_build_history',
        'yaya_build_step_receipts', 'yaya_artifacts',
        'yaya_skill_certifications', 'yaya_certification_revocations',
        'yaya_session_skill_versions', 'yaya_registry_heads',
        'yaya_registry_entries', 'yaya_skill_activations'
    ] LOOP
        EXECUTE format(
            'SELECT EXISTS (SELECT 1 FROM %I)',
            authority_table
        ) INTO has_rows;
        IF has_rows THEN
            RAISE EXCEPTION
                'refusing 0003 downgrade: A8 authority table % is not empty',
                authority_table
                USING ERRCODE = '55000';
        END IF;
    END LOOP;
END;
$$;

-- Conditional A8 guards attached to legacy A6 tables must be removed before
-- their validation functions can be dropped.
DROP TRIGGER yaya_evidence_a8_mirror_guard ON yaya_evidence;
DROP TRIGGER yaya_compile_results_a8_mirror_guard ON yaya_compile_results;
DROP TRIGGER yaya_registry_certifications_a8_mirror_guard
    ON yaya_registry_certifications;
DROP TRIGGER yaya_skills_a8_mirror_guard ON yaya_skills;

-- Drop child authority before its referenced parent.  Table-owned triggers and
-- indexes disappear with their table; no CASCADE is used, so dependency drift
-- makes the downgrade fail and roll back instead of silently deleting objects.
DROP TABLE yaya_skill_activations;
DROP TABLE yaya_registry_entries;
DROP TABLE yaya_registry_heads;
DROP TABLE yaya_session_skill_versions;
DROP TABLE yaya_certification_revocations;
DROP TABLE yaya_skill_certifications;
DROP TABLE yaya_artifacts;
DROP TABLE yaya_build_step_receipts;
DROP TABLE yaya_skill_build_history;
DROP TABLE yaya_skill_builds;
DROP TABLE yaya_product_write_receipts;
DROP TABLE yaya_skill_draft_heads;
DROP TABLE yaya_skill_draft_revisions;
DROP TABLE yaya_control_jobs;
DROP TABLE yaya_public_agent_sessions;
DROP TABLE yaya_build_policies;
DROP TABLE yaya_launch_authorities;
DROP TABLE yaya_agent_profiles;
DROP TABLE yaya_learners;

ALTER TABLE yaya_skills ALTER COLUMN session_id SET NOT NULL;

DROP FUNCTION yaya_guard_a8_evidence_mirror();
DROP FUNCTION yaya_guard_a8_compile_result_mirror();
DROP FUNCTION yaya_guard_a8_registry_certification_mirror();
DROP FUNCTION yaya_guard_a8_skill_mirror();
DROP FUNCTION yaya_guard_skill_build_head_transition();
DROP FUNCTION yaya_guard_public_agent_session_scope();
DROP FUNCTION yaya_allow_only_active_retirement();
DROP FUNCTION yaya_reject_immutable_mutation();

-- A committed DOWN removes only the corresponding forward ledger row so the
-- normal migrator can deterministically re-apply 0003.  Fresh/manual SQL
-- installations do not necessarily have the ledger table.
DO $$
BEGIN
    IF to_regclass('public.yaya_schema_migrations') IS NOT NULL THEN
        EXECUTE
            'DELETE FROM yaya_schema_migrations WHERE name = $1'
            USING '0003_student_skill_chain.sql';
    END IF;
END;
$$;
