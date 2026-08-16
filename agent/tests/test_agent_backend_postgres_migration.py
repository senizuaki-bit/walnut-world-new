from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

TEST_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(TEST_ROOT))

from postgres_test_support import postgres_test_server  # noqa: E402

MIGRATIONS_ROOT = (
    Path(__file__).resolve().parents[1] / "python" / "yaya_agent_backend" / "migrations"
)
EXPECTED_MIGRATIONS = (
    "0001_agent_turn.sql",
    "0002_learner_projection.sql",
    "0003_student_skill_chain.sql",
)
MIGRATIONS = tuple(MIGRATIONS_ROOT / name for name in EXPECTED_MIGRATIONS)


class AgentBackendPostgresMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._server_context = postgres_test_server()
        try:
            cls.server = cls._server_context.__enter__()
            for migration in MIGRATIONS:
                cls._psql_script(migration.read_text(encoding="utf-8"))
        except BaseException:
            cls._server_context.__exit__(*sys.exc_info())
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        cls._server_context.__exit__(None, None, None)

    @classmethod
    def _docker_psql(
        cls,
        *arguments: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "docker",
                "exec",
                cls.server.container_name,
                "psql",
                "--username",
                "yaya_test",
                "--dbname",
                "yaya_test",
                "--set",
                "ON_ERROR_STOP=1",
                *arguments,
            ],
            check=check,
            capture_output=True,
            text=True,
            timeout=30,
        )

    @classmethod
    def _psql_script(cls, sql: str) -> None:
        result = subprocess.run(
            [
                "docker",
                "exec",
                "--interactive",
                cls.server.container_name,
                "psql",
                "--username",
                "yaya_test",
                "--dbname",
                "yaya_test",
                "--set",
                "ON_ERROR_STOP=1",
            ],
            input=sql,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        if result.returncode != 0:
            raise AssertionError(
                "migration script failed inside PostgreSQL: "
                f"stdout={result.stdout[-2000:]!r}; stderr={result.stderr[-2000:]!r}"
            )

    @classmethod
    def _execute(cls, sql: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
        return cls._docker_psql("--command", sql, check=check)

    @classmethod
    def _scalar(cls, sql: str) -> str:
        result = cls._docker_psql("--tuples-only", "--no-align", "--command", sql)
        return result.stdout.strip()

    def _assert_sqlstate_55000(self, sql: str) -> None:
        statement = sql.strip().rstrip(";")
        result = self._execute(
            f"""
            DO $expect_55000$
            DECLARE rejected BOOLEAN := FALSE;
            BEGIN
                BEGIN
                    {statement};
                EXCEPTION WHEN SQLSTATE '55000' THEN
                    rejected := TRUE;
                END;
                IF rejected IS NOT TRUE THEN
                    RAISE EXCEPTION 'statement did not raise SQLSTATE 55000';
                END IF;
            END
            $expect_55000$
            """,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    @classmethod
    def _start_sql(cls, sql: str) -> subprocess.Popen[str]:
        return subprocess.Popen(
            [
                "docker",
                "exec",
                cls.server.container_name,
                "psql",
                "--username",
                "yaya_test",
                "--dbname",
                "yaya_test",
                "--set",
                "ON_ERROR_STOP=1",
                "--tuples-only",
                "--no-align",
                "--command",
                sql,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def test_migration_materializes_every_required_persistence_surface(self) -> None:
        required_tables = {
            "yaya_agent_interactions",
            "yaya_agent_messages",
            "yaya_agent_sessions",
            "yaya_agent_traces",
            "yaya_agent_turns",
            "yaya_audit",
            "yaya_command_jobs",
            "yaya_commands",
            "yaya_compile_results",
            "yaya_counterexamples",
            "yaya_events",
            "yaya_evidence",
            "yaya_learner_models",
            "yaya_learner_projection_failures",
            "yaya_learner_projection_job_evidence",
            "yaya_learner_projection_jobs",
            "yaya_learner_projection_receipts",
            "yaya_learner_projection_terminal_audits",
            "yaya_outbox",
            "yaya_registry_active",
            "yaya_registry_certifications",
            "yaya_runs",
            "yaya_skill_invocations",
            "yaya_skills",
            "yaya_tasks",
            "yaya_worlds",
            "yaya_learners",
            "yaya_agent_profiles",
            "yaya_launch_authorities",
            "yaya_build_policies",
            "yaya_public_agent_sessions",
            "yaya_control_jobs",
            "yaya_skill_draft_heads",
            "yaya_skill_draft_revisions",
            "yaya_product_write_receipts",
            "yaya_skill_builds",
            "yaya_skill_build_history",
            "yaya_build_step_receipts",
            "yaya_artifacts",
            "yaya_skill_certifications",
            "yaya_certification_revocations",
            "yaya_session_skill_versions",
            "yaya_registry_heads",
            "yaya_registry_entries",
            "yaya_skill_activations",
        }
        actual = set(
            self._scalar(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname='public' AND tablename LIKE 'yaya_%' "
                "ORDER BY tablename"
            ).splitlines()
        )
        self.assertTrue(required_tables <= actual, sorted(required_tables - actual))

    def test_student_skill_chain_columns_scopes_and_indexes_are_explicit(self) -> None:
        control_columns = set(
            self._scalar(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='yaya_control_jobs'"
            ).splitlines()
        )
        self.assertTrue(
            {
                "subject_id",
                "resource_id",
                "request_target",
                "request_body",
                "attempt",
                "fencing_token",
                "heartbeat_at",
                "lease_expires_at",
            }
            <= control_columns
        )
        self.assertFalse({"session_id", "turn_id"} & control_columns)

        build_columns = set(
            self._scalar(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='yaya_skill_builds'"
            ).splitlines()
        )
        self.assertTrue(
            {
                "client_draft_revision",
                "source_bundle_json",
                "source_bundle_sha256",
            }
            <= build_columns
        )
        self.assertFalse({"session_id", "draft_id", "draft_sha256"} & build_columns)
        policy_columns = set(
            self._scalar(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='yaya_build_policies'"
            ).splitlines()
        )
        self.assertTrue(
            {
                "parameter_schema_json",
                "semantic_version_major",
                "semantic_version_minor",
                "runtime_abi_version",
            }
            <= policy_columns
        )
        build_checks = self._scalar(
            "SELECT string_agg(pg_get_constraintdef(oid), ' ') "
            "FROM pg_constraint WHERE conrelid='yaya_skill_builds'::regclass "
            "AND contype='c'"
        )
        self.assertIn("client_draft_revision >= 0", build_checks)

        draft_head_columns = set(
            self._scalar(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='yaya_skill_draft_heads'"
            ).splitlines()
        )
        self.assertTrue({"current_revision", "current_draft_sha256"} <= draft_head_columns)
        receipt_columns = set(
            self._scalar(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' "
                "AND table_name='yaya_product_write_receipts'"
            ).splitlines()
        )
        self.assertTrue(
            {
                "operation",
                "request_body",
                "response_headers",
                "response_body",
                "session_id",
                "draft_id",
                "revision",
                "draft_sha256",
                "original_trace_id",
            }
            <= receipt_columns
        )

        index_names = set(
            self._scalar(
                "SELECT indexname FROM pg_indexes WHERE schemaname='public' "
                "AND tablename='yaya_control_jobs'"
            ).splitlines()
        )
        self.assertTrue(
            {"yaya_control_jobs_ready_idx", "yaya_control_jobs_takeover_idx"} <= index_names
        )
        authority_active_index = self._scalar(
            "SELECT indexdef FROM pg_indexes WHERE schemaname='public' "
            "AND tablename='yaya_launch_authorities' "
            "AND indexname='yaya_launch_authorities_one_active_scope_idx'"
        )
        self.assertIn("CREATE UNIQUE INDEX", authority_active_index)
        self.assertIn(
            "(tenant_id, actor_id, learner_id, content_unit_id, content_version, "
            "content_hash, world_id, agent_profile_id)",
            authority_active_index,
        )
        self.assertIn("WHERE (active IS TRUE)", authority_active_index)
        policy_active_index = self._scalar(
            "SELECT indexdef FROM pg_indexes WHERE schemaname='public' "
            "AND tablename='yaya_build_policies' "
            "AND indexname='yaya_build_policies_one_active_scope_idx'"
        )
        self.assertIn("CREATE UNIQUE INDEX", policy_active_index)
        self.assertIn(
            "(tenant_id, actor_id, content_hash, compiler_profile, test_suite_version)",
            policy_active_index,
        )
        self.assertIn("WHERE (active IS TRUE)", policy_active_index)
        rotation_triggers = set(
            self._scalar(
                "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal AND tgname IN ("
                "'yaya_launch_authorities_retirement_guard',"
                "'yaya_build_policies_retirement_guard',"
                "'yaya_launch_authorities_immutable',"
                "'yaya_build_policies_immutable') ORDER BY tgname"
            ).splitlines()
        )
        self.assertEqual(
            rotation_triggers,
            {
                "yaya_launch_authorities_retirement_guard",
                "yaya_build_policies_retirement_guard",
            },
        )
        build_closure_triggers = set(
            self._scalar(
                "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal AND tgname IN ("
                "'yaya_skill_builds_head_guard',"
                "'yaya_skills_a8_mirror_guard',"
                "'yaya_registry_certifications_a8_mirror_guard',"
                "'yaya_compile_results_a8_mirror_guard',"
                "'yaya_evidence_a8_mirror_guard') ORDER BY tgname"
            ).splitlines()
        )
        self.assertEqual(
            build_closure_triggers,
            {
                "yaya_skill_builds_head_guard",
                "yaya_skills_a8_mirror_guard",
                "yaya_registry_certifications_a8_mirror_guard",
                "yaya_compile_results_a8_mirror_guard",
                "yaya_evidence_a8_mirror_guard",
            },
        )
        artifact_uniques = self._scalar(
            "SELECT string_agg(pg_get_constraintdef(oid), E'\\n' ORDER BY conname) "
            "FROM pg_constraint WHERE conrelid='yaya_artifacts'::regclass "
            "AND contype='u'"
        )
        certification_uniques = self._scalar(
            "SELECT string_agg(pg_get_constraintdef(oid), E'\\n' ORDER BY conname) "
            "FROM pg_constraint WHERE conrelid='yaya_skill_certifications'::regclass "
            "AND contype='u'"
        )
        self.assertIn("UNIQUE (tenant_id, build_id)", artifact_uniques)
        self.assertIn("UNIQUE (tenant_id, build_id)", certification_uniques)
        registry_pk = self._scalar(
            "SELECT string_agg(a.attname, ',' ORDER BY u.ordinality) "
            "FROM pg_constraint c "
            "JOIN unnest(c.conkey) WITH ORDINALITY u(attnum, ordinality) ON TRUE "
            "JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=u.attnum "
            "WHERE c.conrelid='yaya_registry_heads'::regclass AND c.contype='p'"
        )
        self.assertEqual(
            registry_pk,
            "tenant_id,actor_id,content_hash,world_id,agent_profile_id,skill_id",
        )

    def test_append_only_authorities_reject_update_delete_and_launch_fk_drift(self) -> None:
        digest = "a" * 64
        self._execute(
            f"""
            INSERT INTO yaya_learners(
                tenant_id, learner_id, actor_id, content_hash,
                record_sha256, record_json
            ) VALUES (
                'tenant_chain_immutable', 'learner_chain_immutable_0001',
                'student_chain_immutable_0001', '{digest}', '{digest}', '{{}}'::jsonb
            )
            """
        )
        update = self._execute(
            "UPDATE yaya_learners SET record_json='{}'::jsonb "
            "WHERE tenant_id='tenant_chain_immutable'",
            check=False,
        )
        self.assertNotEqual(update.returncode, 0)
        self.assertIn("is immutable", update.stderr)
        delete = self._execute(
            "DELETE FROM yaya_learners WHERE tenant_id='tenant_chain_immutable'",
            check=False,
        )
        self.assertNotEqual(delete.returncode, 0)
        self.assertIn("is immutable", delete.stderr)

        missing_authority = self._execute(
            f"""
            INSERT INTO yaya_launch_authorities(
                tenant_id, authority_id, actor_id, learner_id, content_unit_id,
                content_version, content_hash, world_id, agent_profile_id,
                task_id, versions_json, snapshot_sha256
            ) VALUES (
                'tenant_chain_missing', 'authority_chain_missing_0001',
                'student_chain_missing_0001', 'learner_chain_missing_0001',
                'unit_chain_missing_0001', 'v1', '{digest}',
                'world_chain_missing_0001', 'profile_chain_missing_0001',
                'task_chain_missing_0001', '{{}}'::jsonb, '{digest}'
            )
            """,
            check=False,
        )
        self.assertNotEqual(missing_authority.returncode, 0)
        self.assertIn("foreign key constraint", missing_authority.stderr)

    def test_launch_and_build_policy_authority_rotation_is_one_way(self) -> None:
        digest = "c" * 64
        changed_digest = "d" * 64
        self._execute(
            f"""
            INSERT INTO yaya_tasks(
                tenant_id,task_id,actor_id,content_hash,snapshot_json
            ) VALUES
              ('tenant_authority_rotation','task_rotation_0001',
               'student_rotation_0001','{digest}','{{}}'::jsonb),
              ('tenant_authority_rotation','task_rotation_0002',
               'student_rotation_0001','{digest}','{{}}'::jsonb);
            INSERT INTO yaya_worlds(
                tenant_id,world_id,actor_id,content_hash,stream_id,revision,
                last_event_sequence,state_hash,world_rules_version,state_json,
                request_context_json
            ) VALUES (
                'tenant_authority_rotation','world_rotation_0001',
                'student_rotation_0001','{digest}','world:rotation:0001',0,0,
                '{digest}','rules-rotation-1','{{}}'::jsonb,'{{}}'::jsonb
            );
            INSERT INTO yaya_learners(
                tenant_id,learner_id,actor_id,content_hash,record_sha256,record_json
            ) VALUES (
                'tenant_authority_rotation','learner_rotation_0001',
                'student_rotation_0001','{digest}','{digest}','{{}}'::jsonb
            );
            INSERT INTO yaya_agent_profiles(
                tenant_id,agent_profile_id,actor_id,content_hash,
                record_sha256,record_json
            ) VALUES (
                'tenant_authority_rotation','profile_rotation_0001',
                'student_rotation_0001','{digest}','{digest}','{{}}'::jsonb
            );
            INSERT INTO yaya_launch_authorities(
                tenant_id,authority_id,actor_id,learner_id,content_unit_id,
                content_version,content_hash,world_id,agent_profile_id,task_id,
                active,versions_json,snapshot_sha256
            ) VALUES (
                'tenant_authority_rotation','authority_rotation_0001',
                'student_rotation_0001','learner_rotation_0001',
                'unit_rotation_0001','1.0.0','{digest}','world_rotation_0001',
                'profile_rotation_0001','task_rotation_0001',TRUE,
                '{{"api_version":"1.0.0"}}'::jsonb,'{digest}'
            )
            """
        )

        competing_launch = self._execute(
            f"""
            INSERT INTO yaya_launch_authorities(
                tenant_id,authority_id,actor_id,learner_id,content_unit_id,
                content_version,content_hash,world_id,agent_profile_id,task_id,
                active,versions_json,snapshot_sha256
            ) VALUES (
                'tenant_authority_rotation','authority_rotation_0002',
                'student_rotation_0001','learner_rotation_0001',
                'unit_rotation_0001','1.0.0','{digest}','world_rotation_0001',
                'profile_rotation_0001','task_rotation_0002',TRUE,
                '{{"api_version":"1.0.0"}}'::jsonb,'{digest}'
            )
            """,
            check=False,
        )
        self.assertNotEqual(competing_launch.returncode, 0)
        self.assertIn(
            "yaya_launch_authorities_one_active_scope_idx",
            competing_launch.stderr,
        )

        mutated_launch_retirement = self._execute(
            f"""
            UPDATE yaya_launch_authorities
            SET active=FALSE,snapshot_sha256='{changed_digest}'
            WHERE tenant_id='tenant_authority_rotation'
              AND authority_id='authority_rotation_0001'
            """,
            check=False,
        )
        self.assertNotEqual(mutated_launch_retirement.returncode, 0)
        self.assertIn(
            "permits only an active TRUE to FALSE retirement", mutated_launch_retirement.stderr
        )
        self.assertEqual(
            self._scalar(
                "SELECT active::text FROM yaya_launch_authorities "
                "WHERE tenant_id='tenant_authority_rotation' "
                "AND authority_id='authority_rotation_0001'"
            ),
            "true",
        )
        deleted_launch = self._execute(
            "DELETE FROM yaya_launch_authorities "
            "WHERE tenant_id='tenant_authority_rotation' "
            "AND authority_id='authority_rotation_0001'",
            check=False,
        )
        self.assertNotEqual(deleted_launch.returncode, 0)
        self.assertIn("permits only an active TRUE to FALSE retirement", deleted_launch.stderr)
        self._execute(
            "UPDATE yaya_launch_authorities SET active=FALSE "
            "WHERE tenant_id='tenant_authority_rotation' "
            "AND authority_id='authority_rotation_0001'"
        )
        self._execute(
            f"""
            INSERT INTO yaya_launch_authorities(
                tenant_id,authority_id,actor_id,learner_id,content_unit_id,
                content_version,content_hash,world_id,agent_profile_id,task_id,
                active,versions_json,snapshot_sha256
            ) VALUES (
                'tenant_authority_rotation','authority_rotation_0002',
                'student_rotation_0001','learner_rotation_0001',
                'unit_rotation_0001','1.0.0','{digest}','world_rotation_0001',
                'profile_rotation_0001','task_rotation_0002',TRUE,
                '{{"api_version":"1.0.0"}}'::jsonb,'{digest}'
            );
            UPDATE yaya_launch_authorities SET active=FALSE
            WHERE tenant_id='tenant_authority_rotation'
              AND authority_id='authority_rotation_0002'
            """
        )
        reactivated_launch = self._execute(
            "UPDATE yaya_launch_authorities SET active=TRUE "
            "WHERE tenant_id='tenant_authority_rotation' "
            "AND authority_id='authority_rotation_0001'",
            check=False,
        )
        self.assertNotEqual(reactivated_launch.returncode, 0)
        self.assertIn("permits only an active TRUE to FALSE retirement", reactivated_launch.stderr)
        self.assertEqual(
            self._scalar(
                "SELECT count(*) FROM yaya_launch_authorities "
                "WHERE tenant_id='tenant_authority_rotation' AND active"
            ),
            "0",
        )

        self._execute(
            f"""
            INSERT INTO yaya_build_policies(
                tenant_id,build_policy_id,actor_id,content_hash,compiler_profile,
                test_suite_version,compiler_image,compiler_version,compile_flags_json,
                public_tests_json,hidden_tests_json,approved_capabilities_json,
                limits_json,parameter_schema_json,semantic_version_major,
                semantic_version_minor,runtime_abi_version,policy_sha256,active
            ) VALUES (
                'tenant_authority_rotation','policy_rotation_0001',
                'student_rotation_0001','{digest}','YAYA_CPP20_SAFE_V1',
                'rotation-suite-1','gcc@sha256:{digest}','14.2.0','[]'::jsonb,
                '[]'::jsonb,'[]'::jsonb,'[]'::jsonb,'{{}}'::jsonb,'{{}}'::jsonb,
                1,0,'yaya-skill-json-stdio-v1','{digest}',TRUE
            )
            """
        )
        competing_policy = self._execute(
            f"""
            INSERT INTO yaya_build_policies(
                tenant_id,build_policy_id,actor_id,content_hash,compiler_profile,
                test_suite_version,compiler_image,compiler_version,compile_flags_json,
                public_tests_json,hidden_tests_json,approved_capabilities_json,
                limits_json,parameter_schema_json,semantic_version_major,
                semantic_version_minor,runtime_abi_version,policy_sha256,active
            ) VALUES (
                'tenant_authority_rotation','policy_rotation_0002',
                'student_rotation_0001','{digest}','YAYA_CPP20_SAFE_V1',
                'rotation-suite-1','gcc@sha256:{digest}','14.2.1','[]'::jsonb,
                '[]'::jsonb,'[]'::jsonb,'[]'::jsonb,'{{}}'::jsonb,'{{}}'::jsonb,
                1,1,'yaya-skill-json-stdio-v1','{changed_digest}',TRUE
            )
            """,
            check=False,
        )
        self.assertNotEqual(competing_policy.returncode, 0)
        self.assertIn("yaya_build_policies_one_active_scope_idx", competing_policy.stderr)
        mutated_policy_retirement = self._execute(
            "UPDATE yaya_build_policies SET active=FALSE,compiler_version='14.2.1' "
            "WHERE tenant_id='tenant_authority_rotation' "
            "AND build_policy_id='policy_rotation_0001'",
            check=False,
        )
        self.assertNotEqual(mutated_policy_retirement.returncode, 0)
        self.assertIn(
            "permits only an active TRUE to FALSE retirement", mutated_policy_retirement.stderr
        )
        self._execute(
            "UPDATE yaya_build_policies SET active=FALSE "
            "WHERE tenant_id='tenant_authority_rotation' "
            "AND build_policy_id='policy_rotation_0001'"
        )
        self._execute(
            f"""
            INSERT INTO yaya_build_policies(
                tenant_id,build_policy_id,actor_id,content_hash,compiler_profile,
                test_suite_version,compiler_image,compiler_version,compile_flags_json,
                public_tests_json,hidden_tests_json,approved_capabilities_json,
                limits_json,parameter_schema_json,semantic_version_major,
                semantic_version_minor,runtime_abi_version,policy_sha256,active
            ) VALUES (
                'tenant_authority_rotation','policy_rotation_0002',
                'student_rotation_0001','{digest}','YAYA_CPP20_SAFE_V1',
                'rotation-suite-1','gcc@sha256:{digest}','14.2.1','[]'::jsonb,
                '[]'::jsonb,'[]'::jsonb,'[]'::jsonb,'{{}}'::jsonb,'{{}}'::jsonb,
                1,1,'yaya-skill-json-stdio-v1','{changed_digest}',TRUE
            );
            UPDATE yaya_build_policies SET active=FALSE
            WHERE tenant_id='tenant_authority_rotation'
              AND build_policy_id='policy_rotation_0002'
            """
        )
        reactivated_policy = self._execute(
            "UPDATE yaya_build_policies SET active=TRUE "
            "WHERE tenant_id='tenant_authority_rotation' "
            "AND build_policy_id='policy_rotation_0001'",
            check=False,
        )
        self.assertNotEqual(reactivated_policy.returncode, 0)
        self.assertIn("permits only an active TRUE to FALSE retirement", reactivated_policy.stderr)
        deleted_policy = self._execute(
            "DELETE FROM yaya_build_policies "
            "WHERE tenant_id='tenant_authority_rotation' "
            "AND build_policy_id='policy_rotation_0002'",
            check=False,
        )
        self.assertNotEqual(deleted_policy.returncode, 0)
        self.assertIn("permits only an active TRUE to FALSE retirement", deleted_policy.stderr)
        self.assertEqual(
            self._scalar(
                "SELECT count(*) FROM yaya_build_policies "
                "WHERE tenant_id='tenant_authority_rotation' AND active"
            ),
            "0",
        )

    def test_skill_build_head_and_a8_mirrors_are_database_guarded(self) -> None:
        content_hash = "e" * 64
        source_hash = "f" * 64
        artifact_hash = "8" * 64
        certification_hash = "9" * 64
        accepted_at = "2026-08-10T00:00:00Z"
        compiling_at = "2026-08-10T00:01:00Z"
        terminal_at = "2026-08-10T00:02:00Z"
        rewritten_at = "2026-08-10T00:03:00Z"
        self._execute(
            f"""
            INSERT INTO yaya_tasks(
                tenant_id,task_id,actor_id,content_hash,snapshot_json
            ) VALUES (
                'tenant_build_guard','task_build_guard_0001','student_build_guard_0001',
                '{content_hash}','{{}}'::jsonb
            );
            INSERT INTO yaya_worlds(
                tenant_id,world_id,actor_id,content_hash,stream_id,revision,
                last_event_sequence,state_hash,world_rules_version,state_json,
                request_context_json
            ) VALUES (
                'tenant_build_guard','world_build_guard_0001','student_build_guard_0001',
                '{content_hash}','world:build:guard:0001',0,0,'{content_hash}',
                'rules-build-guard-1','{{}}'::jsonb,'{{}}'::jsonb
            );
            INSERT INTO yaya_learners(
                tenant_id,learner_id,actor_id,content_hash,record_sha256,record_json
            ) VALUES (
                'tenant_build_guard','learner_build_guard_0001','student_build_guard_0001',
                '{content_hash}','{content_hash}','{{}}'::jsonb
            );
            INSERT INTO yaya_agent_profiles(
                tenant_id,agent_profile_id,actor_id,content_hash,
                record_sha256,record_json
            ) VALUES (
                'tenant_build_guard','profile_build_guard_0001','student_build_guard_0001',
                '{content_hash}','{content_hash}','{{}}'::jsonb
            );
            INSERT INTO yaya_launch_authorities(
                tenant_id,authority_id,actor_id,learner_id,content_unit_id,
                content_version,content_hash,world_id,agent_profile_id,task_id,
                active,versions_json,snapshot_sha256
            ) VALUES (
                'tenant_build_guard','authority_build_guard_0001',
                'student_build_guard_0001','learner_build_guard_0001',
                'unit_build_guard_0001','1.0.0','{content_hash}',
                'world_build_guard_0001','profile_build_guard_0001',
                'task_build_guard_0001',TRUE,'{{}}'::jsonb,'{content_hash}'
            );
            INSERT INTO yaya_build_policies(
                tenant_id,build_policy_id,actor_id,content_hash,compiler_profile,
                test_suite_version,compiler_image,compiler_version,compile_flags_json,
                public_tests_json,hidden_tests_json,approved_capabilities_json,
                limits_json,parameter_schema_json,semantic_version_major,
                semantic_version_minor,runtime_abi_version,policy_sha256,active
            ) VALUES (
                'tenant_build_guard','policy_build_guard_0001','student_build_guard_0001',
                '{content_hash}','YAYA_CPP20_SAFE_V1','guard-suite-1',
                'gcc@sha256:{content_hash}','14.2.0','[]'::jsonb,'[]'::jsonb,
                '[]'::jsonb,'["WORLD_READ"]'::jsonb,'{{}}'::jsonb,'{{}}'::jsonb,
                1,0,'yaya-skill-json-stdio-v1','{content_hash}',TRUE
            );
            INSERT INTO yaya_build_policies(
                tenant_id,build_policy_id,actor_id,content_hash,compiler_profile,
                test_suite_version,compiler_image,compiler_version,compile_flags_json,
                public_tests_json,hidden_tests_json,approved_capabilities_json,
                limits_json,parameter_schema_json,semantic_version_major,
                semantic_version_minor,runtime_abi_version,policy_sha256,active
            ) VALUES (
                'tenant_build_guard','policy_build_guard_0002','student_build_guard_0001',
                '{content_hash}','YAYA_CPP20_SAFE_V1','guard-suite-2',
                'gcc@sha256:{content_hash}','14.2.0','[]'::jsonb,'[]'::jsonb,
                '[]'::jsonb,'["WORLD_READ"]'::jsonb,'{{}}'::jsonb,'{{}}'::jsonb,
                1,0,'yaya-skill-json-stdio-v1','{source_hash}',FALSE
            );
            INSERT INTO yaya_skill_builds(
                tenant_id,build_id,authority_id,skill_id,actor_id,content_hash,
                client_draft_revision,source_bundle_sha256,source_bundle_json,
                build_policy_id,compiler_profile,test_suite_version,
                requested_capabilities_json,command_id,status,terminal,
                resource_sha256,resource_json,created_at,updated_at
            ) VALUES (
                'tenant_build_guard','build_guard_0001','authority_build_guard_0001',
                'skill_build_guard_0001','student_build_guard_0001','{content_hash}',
                7,'{source_hash}','{{"language":"CPP20"}}'::jsonb,
                'policy_build_guard_0001','YAYA_CPP20_SAFE_V1','guard-suite-1',
                '["WORLD_READ"]'::jsonb,'cmd_build_guard_0001','ACCEPTED',FALSE,
                '{content_hash}',jsonb_build_object(
                    'request_context',jsonb_build_object(
                        'actor',jsonb_build_object(
                            'tenant_id','tenant_build_guard',
                            'actor_id','student_build_guard_0001'
                        ),
                        'content_ref',jsonb_build_object('content_hash','{content_hash}')
                    ),
                    'build_id','build_guard_0001',
                    'skill_id','skill_build_guard_0001',
                    'status','ACCEPTED','terminal',FALSE,
                    'created_at','{accepted_at}','updated_at','{accepted_at}',
                    'failure',NULL
                ),'{accepted_at}'::timestamptz,'{accepted_at}'::timestamptz
            );
            INSERT INTO yaya_skill_builds(
                tenant_id,build_id,authority_id,skill_id,actor_id,content_hash,
                client_draft_revision,source_bundle_sha256,source_bundle_json,
                build_policy_id,compiler_profile,test_suite_version,
                requested_capabilities_json,command_id,status,terminal,
                resource_sha256,resource_json,created_at,updated_at
            ) VALUES (
                'tenant_build_guard','build_guard_validate_0002',
                'authority_build_guard_0001','skill_build_guard_0002',
                'student_build_guard_0001','{content_hash}',7,'{source_hash}',
                '{{"language":"CPP20"}}'::jsonb,'policy_build_guard_0001',
                'YAYA_CPP20_SAFE_V1','guard-suite-1','["WORLD_READ"]'::jsonb,
                'cmd_build_guard_0002','ACCEPTED',FALSE,'{content_hash}',
                jsonb_build_object(
                    'request_context',jsonb_build_object(
                        'actor',jsonb_build_object(
                            'tenant_id','tenant_build_guard',
                            'actor_id','student_build_guard_0001'
                        ),
                        'content_ref',jsonb_build_object('content_hash','{content_hash}')
                    ),
                    'build_id','build_guard_validate_0002',
                    'skill_id','skill_build_guard_0002',
                    'status','ACCEPTED','terminal',FALSE,
                    'created_at','{accepted_at}','updated_at','{accepted_at}',
                    'failure',NULL
                ),'{accepted_at}'::timestamptz,'{accepted_at}'::timestamptz
            )
            """
        )

        self._execute(
            f"""
            UPDATE yaya_skill_builds
            SET status='COMPILING',terminal=FALSE,resource_sha256='{"1" * 64}',
                resource_json=resource_json || jsonb_build_object(
                    'status','COMPILING','terminal',FALSE,'updated_at','{compiling_at}'
                ),updated_at='{compiling_at}'::timestamptz
            WHERE tenant_id='tenant_build_guard' AND build_id='build_guard_0001'
            """
        )
        self._assert_sqlstate_55000(
            "UPDATE yaya_skill_builds SET authority_id='authority_build_guard_drift' "
            "WHERE tenant_id='tenant_build_guard' AND build_id='build_guard_0001'"
        )
        self._assert_sqlstate_55000(
            f"UPDATE yaya_skill_builds SET source_bundle_sha256='{'2' * 64}' "
            "WHERE tenant_id='tenant_build_guard' AND build_id='build_guard_0001'"
        )
        self._assert_sqlstate_55000(
            "UPDATE yaya_skill_builds SET build_policy_id='policy_build_guard_0002' "
            "WHERE tenant_id='tenant_build_guard' AND build_id='build_guard_0001'"
        )
        self._assert_sqlstate_55000(
            "UPDATE yaya_skill_builds SET requested_capabilities_json='[]'::jsonb "
            "WHERE tenant_id='tenant_build_guard' AND build_id='build_guard_0001'"
        )
        self._assert_sqlstate_55000(
            f"""
            UPDATE yaya_skill_builds
            SET status='CERTIFIED',terminal=TRUE,resource_sha256='{"3" * 64}',
                resource_json=resource_json || jsonb_build_object(
                    'status','COMPILING','terminal',TRUE,'updated_at','{terminal_at}'
                ),updated_at='{terminal_at}'::timestamptz
            WHERE tenant_id='tenant_build_guard' AND build_id='build_guard_0001'
            """
        )
        self._execute(
            f"""
            UPDATE yaya_skill_builds
            SET status='CERTIFIED',terminal=TRUE,resource_sha256='{"4" * 64}',
                resource_json=resource_json || jsonb_build_object(
                    'status','CERTIFIED','terminal',TRUE,'updated_at','{terminal_at}',
                    'failure',NULL
                ),updated_at='{terminal_at}'::timestamptz
            WHERE tenant_id='tenant_build_guard' AND build_id='build_guard_0001'
            """
        )
        self._assert_sqlstate_55000(
            f"UPDATE yaya_skill_builds SET updated_at='{rewritten_at}'::timestamptz "
            "WHERE tenant_id='tenant_build_guard' AND build_id='build_guard_0001'"
        )
        self._assert_sqlstate_55000(
            "DELETE FROM yaya_skill_builds WHERE tenant_id='tenant_build_guard' "
            "AND build_id='build_guard_0001'"
        )

        self._assert_sqlstate_55000(
            f"""
            UPDATE yaya_skill_builds
            SET status='FAILED',terminal=TRUE,resource_sha256='{"5" * 64}',
                resource_json=resource_json || jsonb_build_object(
                    'status','FAILED','terminal',TRUE,'updated_at','{compiling_at}',
                    'failure',jsonb_build_object('stage','COMPILE')
                ),updated_at='{compiling_at}'::timestamptz
            WHERE tenant_id='tenant_build_guard' AND build_id='build_guard_validate_0002'
            """
        )
        self._execute(
            f"""
            UPDATE yaya_skill_builds
            SET status='FAILED',terminal=TRUE,resource_sha256='{"6" * 64}',
                resource_json=resource_json || jsonb_build_object(
                    'status','FAILED','terminal',TRUE,'updated_at','{compiling_at}',
                    'failure',jsonb_build_object('stage','VALIDATE_SOURCE')
                ),updated_at='{compiling_at}'::timestamptz
            WHERE tenant_id='tenant_build_guard' AND build_id='build_guard_validate_0002'
            """
        )
        self.assertEqual(
            self._scalar(
                "SELECT string_agg(build_id || ':' || status || ':' || terminal::text, ',' "
                "ORDER BY build_id) FROM yaya_skill_builds "
                "WHERE tenant_id='tenant_build_guard'"
            ),
            "build_guard_0001:CERTIFIED:true,build_guard_validate_0002:FAILED:true",
        )

        self._execute(
            f"""
            INSERT INTO yaya_artifacts(
                tenant_id,artifact_sha256,build_id,skill_id,actor_id,content_hash,
                source_sha256,artifact_uri,metadata_json
            ) VALUES (
                'tenant_build_guard','{artifact_hash}','build_guard_0001',
                'skill_build_guard_0001','student_build_guard_0001','{content_hash}',
                '{source_hash}','artifact://sha256/{artifact_hash}','{{}}'::jsonb
            )
            """
        )
        duplicate_artifact = self._execute(
            f"""
            INSERT INTO yaya_artifacts(
                tenant_id,artifact_sha256,build_id,skill_id,actor_id,content_hash,
                source_sha256,artifact_uri,metadata_json
            ) VALUES (
                'tenant_build_guard','{"7" * 64}','build_guard_0001',
                'skill_build_guard_0001','student_build_guard_0001','{content_hash}',
                '{source_hash}','artifact://sha256/{"7" * 64}','{{}}'::jsonb
            )
            """,
            check=False,
        )
        self.assertNotEqual(duplicate_artifact.returncode, 0)
        self.assertIn("yaya_artifacts_tenant_id_build_id_key", duplicate_artifact.stderr)
        self._execute(
            f"""
            INSERT INTO yaya_skill_certifications(
                tenant_id,certification_id,build_id,skill_id,skill_version_id,
                artifact_sha256,actor_id,content_hash,certification_sha256,
                record_json,issued_at
            ) VALUES (
                'tenant_build_guard','cert_build_guard_0001','build_guard_0001',
                'skill_build_guard_0001','skillver_build_guard_0001','{artifact_hash}',
                'student_build_guard_0001','{content_hash}','{certification_hash}',
                '{{}}'::jsonb,'{terminal_at}'::timestamptz
            )
            """
        )
        duplicate_certification = self._execute(
            f"""
            INSERT INTO yaya_skill_certifications(
                tenant_id,certification_id,build_id,skill_id,skill_version_id,
                artifact_sha256,actor_id,content_hash,certification_sha256,
                record_json,issued_at
            ) VALUES (
                'tenant_build_guard','cert_build_guard_0002','build_guard_0001',
                'skill_build_guard_0001','skillver_build_guard_0002','{artifact_hash}',
                'student_build_guard_0001','{content_hash}','{"7" * 64}',
                '{{}}'::jsonb,'{terminal_at}'::timestamptz
            )
            """,
            check=False,
        )
        self.assertNotEqual(duplicate_certification.returncode, 0)
        self.assertIn(
            "yaya_skill_certifications_tenant_id_build_id_key",
            duplicate_certification.stderr,
        )

        self._execute(
            f"""
            INSERT INTO yaya_skills(
                tenant_id,skill_id,skill_version_id,certification_id,actor_id,
                session_id,content_hash,artifact_sha256,snapshot_json,active
            ) VALUES (
                'tenant_build_guard','skill_build_guard_0001',
                'skillver_build_guard_0001','cert_build_guard_0001',
                'student_build_guard_0001',NULL,'{content_hash}','{artifact_hash}',
                '{{"closure":"a8"}}'::jsonb,FALSE
            );
            INSERT INTO yaya_registry_certifications(
                tenant_id,certification_id,skill_id,skill_version_id,
                artifact_sha256,record_json,rejected
            ) VALUES (
                'tenant_build_guard','cert_build_guard_0001','skill_build_guard_0001',
                'skillver_build_guard_0001','{artifact_hash}',
                '{{"closure":"a8"}}'::jsonb,FALSE
            );
            INSERT INTO yaya_compile_results(
                tenant_id,build_id,actor_id,content_hash,snapshot_json
            ) VALUES (
                'tenant_build_guard','build_guard_0001','student_build_guard_0001',
                '{content_hash}','{{"closure":"a8"}}'::jsonb
            );
            INSERT INTO yaya_evidence(
                tenant_id,evidence_id,actor_id,content_hash,evidence_type,
                payload_sha256,evidence_json
            ) VALUES (
                'tenant_build_guard','evidence_build_guard_0001',
                'student_build_guard_0001','{content_hash}','TEST_REPORT',
                '{source_hash}',jsonb_build_object(
                    'source',jsonb_build_object(
                        'source_type','SKILL_BUILD','source_id','build_guard_0001'
                    )
                )
            )
            """
        )
        self._assert_sqlstate_55000(
            "UPDATE yaya_skills SET snapshot_json='{}'::jsonb "
            "WHERE tenant_id='tenant_build_guard' "
            "AND skill_version_id='skillver_build_guard_0001'"
        )
        self._assert_sqlstate_55000(
            "DELETE FROM yaya_skills WHERE tenant_id='tenant_build_guard' "
            "AND skill_version_id='skillver_build_guard_0001'"
        )
        self._assert_sqlstate_55000(
            "UPDATE yaya_registry_certifications SET record_json='{}'::jsonb "
            "WHERE tenant_id='tenant_build_guard' "
            "AND certification_id='cert_build_guard_0001'"
        )
        self._assert_sqlstate_55000(
            "DELETE FROM yaya_registry_certifications WHERE tenant_id='tenant_build_guard' "
            "AND certification_id='cert_build_guard_0001'"
        )
        self._assert_sqlstate_55000(
            "UPDATE yaya_compile_results SET snapshot_json='{}'::jsonb "
            "WHERE tenant_id='tenant_build_guard' AND build_id='build_guard_0001'"
        )
        self._assert_sqlstate_55000(
            "DELETE FROM yaya_compile_results WHERE tenant_id='tenant_build_guard' "
            "AND build_id='build_guard_0001'"
        )
        self._assert_sqlstate_55000(
            "UPDATE yaya_evidence SET evidence_json='{}'::jsonb "
            "WHERE tenant_id='tenant_build_guard' "
            "AND evidence_id='evidence_build_guard_0001'"
        )
        self._assert_sqlstate_55000(
            "DELETE FROM yaya_evidence WHERE tenant_id='tenant_build_guard' "
            "AND evidence_id='evidence_build_guard_0001'"
        )

        self._execute(
            f"""
            INSERT INTO yaya_skills(
                tenant_id,skill_id,skill_version_id,certification_id,actor_id,
                session_id,content_hash,artifact_sha256,snapshot_json,active
            ) VALUES (
                'tenant_build_guard','skill_legacy_guard_0001',
                'skillver_legacy_guard_0001','cert_legacy_guard_0001',
                'student_build_guard_0001',NULL,'{content_hash}','{"6" * 64}',
                '{{"closure":"legacy"}}'::jsonb,FALSE
            );
            UPDATE yaya_skills SET snapshot_json='{{"mutated":true}}'::jsonb
            WHERE tenant_id='tenant_build_guard'
              AND skill_version_id='skillver_legacy_guard_0001';
            DELETE FROM yaya_skills WHERE tenant_id='tenant_build_guard'
              AND skill_version_id='skillver_legacy_guard_0001';
            INSERT INTO yaya_registry_certifications(
                tenant_id,certification_id,skill_id,skill_version_id,
                artifact_sha256,record_json,rejected
            ) VALUES (
                'tenant_build_guard','cert_legacy_guard_0001',
                'skill_legacy_guard_0001','skillver_legacy_guard_0001','{"6" * 64}',
                '{{"closure":"legacy"}}'::jsonb,FALSE
            );
            UPDATE yaya_registry_certifications SET record_json='{{"mutated":true}}'::jsonb
            WHERE tenant_id='tenant_build_guard'
              AND certification_id='cert_legacy_guard_0001';
            DELETE FROM yaya_registry_certifications WHERE tenant_id='tenant_build_guard'
              AND certification_id='cert_legacy_guard_0001';
            INSERT INTO yaya_compile_results(
                tenant_id,build_id,actor_id,content_hash,snapshot_json
            ) VALUES (
                'tenant_build_guard','build_legacy_guard_0001',
                'student_build_guard_0001','{content_hash}',
                '{{"closure":"legacy"}}'::jsonb
            );
            UPDATE yaya_compile_results SET snapshot_json='{{"mutated":true}}'::jsonb
            WHERE tenant_id='tenant_build_guard' AND build_id='build_legacy_guard_0001';
            DELETE FROM yaya_compile_results WHERE tenant_id='tenant_build_guard'
              AND build_id='build_legacy_guard_0001';
            INSERT INTO yaya_evidence(
                tenant_id,evidence_id,actor_id,content_hash,evidence_type,
                payload_sha256,evidence_json
            ) VALUES (
                'tenant_build_guard','evidence_legacy_guard_0001',
                'student_build_guard_0001','{content_hash}','TEST_REPORT',
                '{source_hash}',jsonb_build_object(
                    'source',jsonb_build_object(
                        'source_type','SKILL_BUILD','source_id','build_legacy_guard_0001'
                    )
                )
            );
            UPDATE yaya_evidence SET evidence_json='{{"mutated":true}}'::jsonb
            WHERE tenant_id='tenant_build_guard'
              AND evidence_id='evidence_legacy_guard_0001';
            DELETE FROM yaya_evidence WHERE tenant_id='tenant_build_guard'
              AND evidence_id='evidence_legacy_guard_0001'
            """
        )
        self.assertEqual(
            self._scalar(
                "SELECT (SELECT count(*) FROM yaya_skills "
                "WHERE tenant_id='tenant_build_guard' "
                "AND skill_version_id='skillver_legacy_guard_0001') + "
                "(SELECT count(*) FROM yaya_registry_certifications "
                "WHERE tenant_id='tenant_build_guard' "
                "AND certification_id='cert_legacy_guard_0001') + "
                "(SELECT count(*) FROM yaya_compile_results "
                "WHERE tenant_id='tenant_build_guard' "
                "AND build_id='build_legacy_guard_0001') + "
                "(SELECT count(*) FROM yaya_evidence "
                "WHERE tenant_id='tenant_build_guard' "
                "AND evidence_id='evidence_legacy_guard_0001')"
            ),
            "0",
        )

    def test_legacy_skill_rows_allow_null_session_without_weakening_hash_checks(self) -> None:
        digest = "b" * 64
        self._execute(
            f"""
            INSERT INTO yaya_skills(
                tenant_id, skill_id, skill_version_id, certification_id,
                actor_id, session_id, content_hash, artifact_sha256,
                snapshot_json
            ) VALUES (
                'tenant_chain_legacy', 'skill_chain_legacy_0001',
                'skillver_chain_legacy_0001', 'cert_chain_legacy_0001',
                'student_chain_legacy_0001', NULL, '{digest}', '{digest}',
                '{{}}'::jsonb
            )
            """
        )
        self.assertEqual(
            self._scalar(
                "SELECT count(*) FROM yaya_skills "
                "WHERE tenant_id='tenant_chain_legacy' AND session_id IS NULL"
            ),
            "1",
        )

    def test_learner_projection_schema_is_fenced_and_authority_bound(self) -> None:
        learner_model_columns = set(
            self._scalar(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='yaya_learner_models' "
                "ORDER BY column_name"
            ).splitlines()
        )
        self.assertTrue(
            {
                "request_context_json",
                "projection_policy_version",
                "snapshot_sha256",
            }
            <= learner_model_columns
        )

        job_columns = set(
            self._scalar(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' "
                "AND table_name='yaya_learner_projection_jobs' "
                "ORDER BY column_name"
            ).splitlines()
        )
        required_job_columns = {
            "actor_id",
            "attempt",
            "available_at",
            "content_hash",
            "event_id",
            "event_sha256",
            "fencing_token",
            "heartbeat_at",
            "inference_sha256",
            "lease_expires_at",
            "lease_id",
            "operation_context_json",
            "source_event_id",
            "source_event_sha256",
            "source_stream_id",
            "source_stream_sequence",
            "state",
            "teaching_spec_version",
            "turn_commit_sha256",
            "worker_id",
        }
        self.assertTrue(
            required_job_columns <= job_columns,
            sorted(required_job_columns - job_columns),
        )

        constraint_names = set(
            self._scalar(
                "SELECT conname FROM pg_constraint "
                "WHERE connamespace='public'::regnamespace ORDER BY conname"
            ).splitlines()
        )
        required_constraints = {
            "yaya_agent_sessions_task_authority_key",
            "yaya_events_id_stream_sequence_key",
            "yaya_evidence_authority_hash_key",
            "yaya_learner_models_authority_key",
            "yaya_learner_models_projection_provenance_check",
            "yaya_learner_projection_jobs_terminal_generation_key",
            "yaya_learner_projection_jobs_source_stream_event_fkey",
            "yaya_learner_projection_terminal_audits_job_generation_fkey",
        }
        self.assertTrue(
            required_constraints <= constraint_names,
            sorted(required_constraints - constraint_names),
        )
        audit_fk_owner = self._scalar(
            "SELECT conrelid::regclass::text || '|' || confrelid::regclass::text "
            "FROM pg_constraint WHERE "
            "conname='yaya_learner_projection_terminal_audits_job_generation_fkey'"
        )
        self.assertEqual(
            audit_fk_owner,
            "yaya_learner_projection_terminal_audits|yaya_learner_projection_jobs",
        )
        audit_fk_definition = self._scalar(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE "
            "conname='yaya_learner_projection_terminal_audits_job_generation_fkey'"
        )
        self.assertIn(
            "FOREIGN KEY (tenant_id, job_id, terminal_state, attempt, fencing_token)",
            audit_fk_definition,
        )
        self.assertIn(
            "REFERENCES yaya_learner_projection_jobs(tenant_id, job_id, state, attempt, fencing_token)",
            audit_fk_definition,
        )
        learner_model_provenance = self._scalar(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE connamespace='public'::regnamespace "
            "AND conname='yaya_learner_models_projection_provenance_check'"
        )
        for required_fragment in (
            "request_context_json IS NULL",
            "projection_policy_version IS NULL",
            "snapshot_sha256 IS NULL",
            "snapshot_sha256 IS NOT NULL",
            "revision = projected_through_sequence",
        ):
            self.assertIn(required_fragment, learner_model_provenance)

        for table, minimum_foreign_keys in {
            "yaya_learner_projection_jobs": 6,
            "yaya_learner_projection_job_evidence": 2,
            "yaya_learner_projection_receipts": 3,
            "yaya_learner_projection_failures": 3,
            "yaya_learner_projection_terminal_audits": 1,
        }.items():
            with self.subTest(table=table):
                actual = int(
                    self._scalar(
                        "SELECT count(*) FROM pg_constraint "
                        f"WHERE conrelid='{table}'::regclass AND contype='f'"
                    )
                )
                self.assertGreaterEqual(actual, minimum_foreign_keys)

        job_checks = self._scalar(
            "SELECT string_agg(pg_get_constraintdef(oid), ' ') "
            "FROM pg_constraint "
            "WHERE conrelid='yaya_learner_projection_jobs'::regclass "
            "AND contype='c'"
        )
        for invariant in (
            "fencing_token = attempt",
            "lease_expires_at > heartbeat_at",
            "source_stream_id = ('learner:'::text || learner_id)",
            "state = 'LEASED'::text",
        ):
            with self.subTest(invariant=invariant):
                self.assertIn(invariant, job_checks)

        index_names = set(
            self._scalar(
                "SELECT indexname FROM pg_indexes WHERE schemaname='public' ORDER BY indexname"
            ).splitlines()
        )
        required_indexes = {
            "yaya_learner_projection_failures_active_idx",
            "yaya_learner_projection_jobs_ready_idx",
            "yaya_learner_projection_jobs_takeover_idx",
            "yaya_learner_projection_receipts_checkpoint_idx",
            "yaya_learner_projection_terminal_audits_pending_idx",
        }
        self.assertTrue(
            required_indexes <= index_names,
            sorted(required_indexes - index_names),
        )

        trigger_names = set(
            self._scalar(
                "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal ORDER BY tgname"
            ).splitlines()
        )
        required_triggers = {
            "yaya_learner_projection_terminal_audit_enqueue",
            "yaya_learner_projection_terminal_audit_immutable",
        }
        self.assertTrue(
            required_triggers <= trigger_names,
            sorted(required_triggers - trigger_names),
        )

    def test_learner_model_projection_provenance_is_atomic_or_legacy_null(self) -> None:
        digest = "a" * 64
        self._execute(
            """
            INSERT INTO yaya_learner_models(
                tenant_id,learner_id,actor_id,content_hash,revision,
                projected_through_sequence,snapshot_json
            ) VALUES (
                'tenant_model_provenance','learner_model_legacy',
                'learner_model_legacy','aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                0,0,'{}'::jsonb
            )
            """
        )
        self._execute(
            f"""
            INSERT INTO yaya_learner_models(
                tenant_id,learner_id,actor_id,content_hash,revision,
                projected_through_sequence,snapshot_json,request_context_json,
                projection_policy_version,snapshot_sha256
            ) VALUES (
                'tenant_model_provenance','learner_model_current',
                'learner_model_current','{digest}',0,0,'{{}}'::jsonb,
                '{{}}'::jsonb,'learner-policy-v1','{digest}'
            )
            """
        )
        mismatched_checkpoint = self._execute(
            f"""
            INSERT INTO yaya_learner_models(
                tenant_id,learner_id,actor_id,content_hash,revision,
                projected_through_sequence,snapshot_json,request_context_json,
                projection_policy_version,snapshot_sha256
            ) VALUES (
                'tenant_model_provenance','learner_model_mismatched_checkpoint',
                'learner_model_mismatched_checkpoint','{digest}',2,1,'{{}}'::jsonb,
                '{{}}'::jsonb,'learner-policy-v1','{digest}'
            )
            """,
            check=False,
        )
        self.assertNotEqual(mismatched_checkpoint.returncode, 0)
        self.assertIn(
            "yaya_learner_models_projection_provenance_check",
            mismatched_checkpoint.stderr,
        )
        partial_rows = (
            ("learner_model_no_hash", "'{}'::jsonb", "'learner-policy-v1'", "NULL"),
            ("learner_model_no_policy", "'{}'::jsonb", "NULL", f"'{digest}'"),
            ("learner_model_no_context", "NULL", "'learner-policy-v1'", f"'{digest}'"),
            (
                "learner_model_bad_hash",
                "'{}'::jsonb",
                "'learner-policy-v1'",
                f"'{digest.upper()}'",
            ),
        )
        for learner_id, request_context, policy_version, snapshot_hash in partial_rows:
            with self.subTest(learner_id=learner_id):
                result = self._execute(
                    f"""
                    INSERT INTO yaya_learner_models(
                        tenant_id,learner_id,actor_id,content_hash,revision,
                        projected_through_sequence,snapshot_json,request_context_json,
                        projection_policy_version,snapshot_sha256
                    ) VALUES (
                        'tenant_model_provenance','{learner_id}','{learner_id}',
                        '{digest}',0,0,'{{}}'::jsonb,{request_context},
                        {policy_version},{snapshot_hash}
                    )
                    """,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(
                    "yaya_learner_models_projection_provenance_check",
                    result.stderr,
                )

    def test_succeeded_projection_job_requires_a_real_claim_attempt(self) -> None:
        digest = "a" * 64
        rejected = self._execute(
            f"""
            CREATE TEMP TABLE projection_job_check
                (LIKE yaya_learner_projection_jobs INCLUDING CONSTRAINTS);
            INSERT INTO projection_job_check (
                tenant_id, job_id, event_id, source_event_id, learner_id,
                actor_id, content_hash, task_id, session_id, turn_id,
                command_id, source_stream_id, source_stream_sequence,
                event_sha256, source_event_sha256, turn_commit_sha256,
                inference_sha256, teaching_spec_version, role, event_json,
                operation_context_json, state, attempt, fencing_token,
                available_at, created_at, updated_at, succeeded_at
            ) VALUES (
                'tenant_claim', 'job_claim', 'event_claim', 'source_claim',
                'learner_claim', 'learner_claim', '{digest}', 'task_claim',
                'session_claim', 'turn_claim', 'command_claim',
                'learner:learner_claim', 1, '{digest}', '{digest}', '{digest}',
                '{digest}', 'teaching_v1', 'teaching_agent', '{{}}'::jsonb,
                '{{}}'::jsonb, 'SUCCEEDED', 0, 0, clock_timestamp(),
                clock_timestamp(), clock_timestamp(), clock_timestamp()
            );
            """,
            check=False,
        )

        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("check constraint", rejected.stderr.lower())

    def test_cross_tenant_session_links_are_rejected_by_foreign_keys(self) -> None:
        digest = "a" * 64
        self._execute(
            f"""
            INSERT INTO yaya_tasks(
                tenant_id, task_id, actor_id, content_hash, snapshot_json
            ) VALUES ('tenant_alpha', 'task_cross_tenant_0001', 'student_alpha_0001',
                      '{digest}', '{{}}'::jsonb);
            INSERT INTO yaya_worlds(
                tenant_id, world_id, actor_id, content_hash, stream_id, revision,
                last_event_sequence, state_hash, world_rules_version, state_json,
                request_context_json
            ) VALUES ('tenant_alpha', 'world_cross_tenant_0001', 'student_alpha_0001',
                      '{digest}', 'stream_cross_tenant_0001', 5, 40, '{digest}',
                      'farm-rules-1', '{{}}'::jsonb, '{{}}'::jsonb);
            """
        )
        rejected = self._execute(
            f"""
            INSERT INTO yaya_agent_sessions(
                tenant_id, session_id, actor_id, task_id, world_id, content_hash,
                snapshot_json
            ) VALUES ('tenant_beta', 'session_cross_tenant_0001', 'student_beta_0001',
                      'task_cross_tenant_0001', 'world_cross_tenant_0001', '{digest}',
                      '{{}}'::jsonb);
            """,
            check=False,
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("foreign key constraint", rejected.stderr.lower())
        self.assertEqual(
            self._scalar(
                "SELECT count(*) FROM yaya_agent_sessions "
                "WHERE session_id='session_cross_tenant_0001'"
            ),
            "0",
        )

    def test_same_tenant_actor_content_mislinks_are_rejected_upstream(self) -> None:
        digest = "5" * 64
        other_digest = "6" * 64
        self._execute(
            f"""
            INSERT INTO yaya_tasks(
                tenant_id, task_id, actor_id, content_hash, snapshot_json
            ) VALUES ('tenant_upstream', 'task_upstream_0001',
                      'student_upstream_0001', '{digest}', '{{}}'::jsonb);
            INSERT INTO yaya_worlds(
                tenant_id, world_id, actor_id, content_hash, stream_id, revision,
                last_event_sequence, state_hash, world_rules_version, state_json,
                request_context_json
            ) VALUES ('tenant_upstream', 'world_upstream_0001',
                      'student_upstream_0001', '{digest}', 'stream_upstream_0001',
                      5, 40, '{digest}', 'farm-rules-1', '{{}}'::jsonb, '{{}}'::jsonb);
            """
        )
        bad_session = self._execute(
            f"""
            INSERT INTO yaya_agent_sessions(
                tenant_id, session_id, actor_id, task_id, world_id, content_hash,
                snapshot_json
            ) VALUES ('tenant_upstream', 'session_upstream_bad_0001',
                      'student_upstream_wrong_0001', 'task_upstream_0001',
                      'world_upstream_0001', '{other_digest}', '{{}}'::jsonb);
            """,
            check=False,
        )
        self.assertNotEqual(bad_session.returncode, 0)
        self.assertIn("foreign key constraint", bad_session.stderr.lower())
        self._execute(
            f"""
            INSERT INTO yaya_agent_sessions(
                tenant_id, session_id, actor_id, task_id, world_id, content_hash,
                snapshot_json
            ) VALUES ('tenant_upstream', 'session_upstream_0001',
                      'student_upstream_0001', 'task_upstream_0001',
                      'world_upstream_0001', '{digest}', '{{}}'::jsonb);
            """
        )
        bad_skill = self._execute(
            f"""
            INSERT INTO yaya_skills(
                tenant_id, skill_id, skill_version_id, certification_id, actor_id,
                session_id, content_hash, artifact_sha256, snapshot_json
            ) VALUES ('tenant_upstream', 'skill_upstream_bad_0001',
                      'skill_version_upstream_bad_0001',
                      'certification_upstream_bad_0001',
                      'student_upstream_wrong_0001', 'session_upstream_0001',
                      '{other_digest}', '{digest}', '{{}}'::jsonb);
            """,
            check=False,
        )
        self.assertNotEqual(bad_skill.returncode, 0)
        self.assertIn("foreign key constraint", bad_skill.stderr.lower())
        bad_message = self._execute(
            f"""
            INSERT INTO yaya_agent_messages(
                tenant_id, message_id, actor_id, content_hash, session_id,
                snapshot_json
            ) VALUES ('tenant_upstream', 'message_upstream_bad_0001',
                      'student_upstream_wrong_0001', '{other_digest}',
                      'session_upstream_0001', '{{}}'::jsonb);
            """,
            check=False,
        )
        self.assertNotEqual(bad_message.returncode, 0)
        self.assertIn("foreign key constraint", bad_message.stderr.lower())
        self.assertEqual(
            self._scalar(
                "SELECT (SELECT count(*) FROM yaya_agent_sessions "
                "WHERE session_id='session_upstream_bad_0001')::text || ':' || "
                "(SELECT count(*) FROM yaya_skills "
                "WHERE skill_version_id='skill_version_upstream_bad_0001')::text || ':' || "
                "(SELECT count(*) FROM yaya_agent_messages "
                "WHERE message_id='message_upstream_bad_0001')::text"
            ),
            "0:0:0",
        )

    def test_failed_transaction_leaves_no_partial_command_or_job(self) -> None:
        digest = "b" * 64
        self._execute(
            f"""
            INSERT INTO yaya_tasks(
                tenant_id, task_id, actor_id, content_hash, snapshot_json
            ) VALUES ('tenant_atomic', 'task_atomic_0001', 'student_atomic_0001',
                      '{digest}', '{{}}'::jsonb);
            INSERT INTO yaya_worlds(
                tenant_id, world_id, actor_id, content_hash, stream_id, revision,
                last_event_sequence, state_hash, world_rules_version, state_json,
                request_context_json
            ) VALUES ('tenant_atomic', 'world_atomic_0001', 'student_atomic_0001',
                      '{digest}', 'stream_atomic_0001', 5, 40, '{digest}',
                      'farm-rules-1', '{{}}'::jsonb, '{{}}'::jsonb);
            INSERT INTO yaya_agent_sessions(
                tenant_id, session_id, actor_id, task_id, world_id, content_hash,
                snapshot_json
            ) VALUES ('tenant_atomic', 'session_atomic_0001', 'student_atomic_0001',
                      'task_atomic_0001', 'world_atomic_0001', '{digest}', '{{}}'::jsonb);
            """
        )
        failed = self._execute(
            f"""
            BEGIN;
            INSERT INTO yaya_commands(
                tenant_id, actor_id, operation, idempotency_key, command_id,
                session_id, turn_id, client_turn_sequence, request_sha256,
                content_hash, revision, status, updated_at, record_json
            ) VALUES ('tenant_atomic', 'student_atomic_0001', 'EXECUTE_AGENT_TURN',
                      'idem_atomic_0001', 'cmd_atomic_0001', 'session_atomic_0001',
                      'turn_atomic_0001', 1, '{digest}', '{digest}', 1,
                      'ACCEPTED', clock_timestamp(), '{{}}'::jsonb);
            INSERT INTO yaya_command_jobs(
                tenant_id, command_id, job_id, actor_id, content_hash, session_id, turn_id,
                client_turn_sequence, event_json, operation_context_json, request_body,
                accepted_receipt_json, created_at
            ) VALUES ('tenant_atomic', 'cmd_atomic_0001', 'job_atomic_00000001',
                      'student_atomic_0001',
                      '{digest}', 'session_atomic_0001', 'turn_atomic_0001', 1,
                      '{{}}'::jsonb, '{{}}'::jsonb,
                      convert_to('{{"event_id":"event_atomic_0001"}}','UTF8'),
                      '{{"job_id":"job_atomic_00000001",'
                      '"job_type":"EXECUTE_AGENT_TURN","status":"ACCEPTED",'
                      '"created_at":"2026-08-09T00:00:00Z",'
                      '"updated_at":"2026-08-09T00:00:00Z",'
                      '"command_id":"cmd_atomic_0001",'
                      '"trace_id":"trace_atomic_00000001","error":null}}'::jsonb,
                      clock_timestamp());
            SELECT 1 / 0;
            COMMIT;
            """,
            check=False,
        )
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("division by zero", failed.stderr.lower())
        self.assertEqual(
            self._scalar(
                "SELECT (SELECT count(*) FROM yaya_commands "
                "WHERE command_id='cmd_atomic_0001')::text || ':' || "
                "(SELECT count(*) FROM yaya_command_jobs "
                "WHERE command_id='cmd_atomic_0001')::text"
            ),
            "0:0",
        )

    def test_acceptance_rejects_command_and_job_identity_mislinks(self) -> None:
        digest = "7" * 64
        other_digest = "8" * 64
        self._execute(
            f"""
            INSERT INTO yaya_tasks(
                tenant_id, task_id, actor_id, content_hash, snapshot_json
            ) VALUES ('tenant_accept_identity', 'task_accept_identity_0001',
                      'student_accept_identity_0001', '{digest}', '{{}}'::jsonb);
            INSERT INTO yaya_worlds(
                tenant_id, world_id, actor_id, content_hash, stream_id, revision,
                last_event_sequence, state_hash, world_rules_version, state_json,
                request_context_json
            ) VALUES ('tenant_accept_identity', 'world_accept_identity_0001',
                      'student_accept_identity_0001', '{digest}',
                      'stream_accept_identity_0001', 5, 40, '{digest}',
                      'farm-rules-1', '{{}}'::jsonb, '{{}}'::jsonb);
            INSERT INTO yaya_agent_sessions(
                tenant_id, session_id, actor_id, task_id, world_id, content_hash,
                snapshot_json
            ) VALUES ('tenant_accept_identity', 'session_accept_identity_0001',
                      'student_accept_identity_0001', 'task_accept_identity_0001',
                      'world_accept_identity_0001', '{digest}', '{{}}'::jsonb);
            """
        )
        invalid_commands = {
            "actor": f"""
                INSERT INTO yaya_commands(
                    tenant_id, actor_id, operation, idempotency_key, command_id,
                    session_id, turn_id, client_turn_sequence, request_sha256,
                    content_hash, revision, status, updated_at, record_json
                ) VALUES ('tenant_accept_identity', 'student_accept_identity_wrong_0001',
                          'EXECUTE_AGENT_TURN', 'idem_accept_bad_actor_0001',
                          'cmd_accept_bad_actor_0001', 'session_accept_identity_0001',
                          'turn_accept_bad_actor_0001', 1, '{digest}', '{digest}', 1,
                          'ACCEPTED', clock_timestamp(), '{{}}'::jsonb);
            """,
            "content": f"""
                INSERT INTO yaya_commands(
                    tenant_id, actor_id, operation, idempotency_key, command_id,
                    session_id, turn_id, client_turn_sequence, request_sha256,
                    content_hash, revision, status, updated_at, record_json
                ) VALUES ('tenant_accept_identity', 'student_accept_identity_0001',
                          'EXECUTE_AGENT_TURN', 'idem_accept_bad_content_0001',
                          'cmd_accept_bad_content_0001', 'session_accept_identity_0001',
                          'turn_accept_bad_content_0001', 1, '{digest}', '{other_digest}', 1,
                          'ACCEPTED', clock_timestamp(), '{{}}'::jsonb);
            """,
        }
        for identity, sql in invalid_commands.items():
            with self.subTest(record="command", identity=identity):
                rejected = self._execute(sql, check=False)
                self.assertNotEqual(rejected.returncode, 0)
                self.assertIn("foreign key constraint", rejected.stderr.lower())

        self._execute(
            f"""
            INSERT INTO yaya_commands(
                tenant_id, actor_id, operation, idempotency_key, command_id,
                session_id, turn_id, client_turn_sequence, request_sha256,
                content_hash, revision, status, updated_at, record_json
            ) VALUES ('tenant_accept_identity', 'student_accept_identity_0001',
                      'EXECUTE_AGENT_TURN', 'idem_accept_valid_0001',
                      'cmd_accept_valid_0001', 'session_accept_identity_0001',
                      'turn_accept_valid_0001', 1, '{digest}', '{digest}', 1,
                      'ACCEPTED', clock_timestamp(), '{{}}'::jsonb);
            """
        )
        invalid_jobs = {
            "actor": ("student_accept_identity_wrong_0001", digest),
            "content": ("student_accept_identity_0001", other_digest),
        }
        for identity, (actor_id, content_hash) in invalid_jobs.items():
            with self.subTest(record="job", identity=identity):
                rejected = self._execute(
                    f"""
                    INSERT INTO yaya_command_jobs(
                        tenant_id, command_id, job_id, actor_id, content_hash, session_id,
                        turn_id, client_turn_sequence, event_json, operation_context_json,
                        request_body, accepted_receipt_json, created_at
                    ) VALUES ('tenant_accept_identity', 'cmd_accept_valid_0001',
                              'job_accept_{identity}_00000001', '{actor_id}', '{content_hash}',
                              'session_accept_identity_0001', 'turn_accept_valid_0001',
                              1, '{{}}'::jsonb, '{{}}'::jsonb,
                              convert_to('{{"event_id":"event_accept_identity_0001"}}','UTF8'),
                              '{{"job_id":"job_accept_{identity}_00000001",'
                              '"job_type":"EXECUTE_AGENT_TURN","status":"ACCEPTED",'
                              '"created_at":"2026-08-09T00:00:00Z",'
                              '"updated_at":"2026-08-09T00:00:00Z",'
                              '"command_id":"cmd_accept_valid_0001",'
                              '"trace_id":"trace_accept_identity_0001","error":null}}'::jsonb,
                              clock_timestamp());
                    """,
                    check=False,
                )
                self.assertNotEqual(rejected.returncode, 0)
                self.assertIn("foreign key constraint", rejected.stderr.lower())
        self.assertEqual(
            self._scalar(
                "SELECT (SELECT count(*) FROM yaya_commands "
                "WHERE command_id LIKE 'cmd_accept_bad_%')::text || ':' || "
                "(SELECT count(*) FROM yaya_command_jobs "
                "WHERE command_id='cmd_accept_valid_0001')::text"
            ),
            "0:0",
        )

    def test_command_and_outbox_idempotency_scopes_are_database_enforced(self) -> None:
        digest = "d" * 64
        self._execute(
            f"""
            INSERT INTO yaya_commands(
                tenant_id, actor_id, operation, idempotency_key, command_id,
                request_sha256, content_hash, revision, status, updated_at, record_json
            ) VALUES ('tenant_scope', 'student_scope_0001', 'EXECUTE_AGENT_TURN',
                      'idem_scope_0001', 'cmd_scope_0001', '{digest}', '{digest}', 1,
                      'ACCEPTED', clock_timestamp(), '{{}}'::jsonb);
            """
        )
        duplicate_command = self._execute(
            f"""
            INSERT INTO yaya_commands(
                tenant_id, actor_id, operation, idempotency_key, command_id,
                request_sha256, content_hash, revision, status, updated_at, record_json
            ) VALUES ('tenant_scope', 'student_scope_0001', 'EXECUTE_AGENT_TURN',
                      'idem_scope_0001', 'cmd_scope_0002', '{digest}', '{digest}', 1,
                      'ACCEPTED', clock_timestamp(), '{{}}'::jsonb);
            """,
            check=False,
        )
        self.assertNotEqual(duplicate_command.returncode, 0)
        self.assertIn("unique constraint", duplicate_command.stderr.lower())
        self._execute(
            f"""
            INSERT INTO yaya_commands(
                tenant_id, actor_id, operation, idempotency_key, command_id,
                request_sha256, content_hash, revision, status, updated_at, record_json
            ) VALUES ('tenant_scope', 'student_scope_0002', 'EXECUTE_AGENT_TURN',
                      'idem_scope_0001', 'cmd_scope_0003', '{digest}', '{digest}', 1,
                      'ACCEPTED', clock_timestamp(), '{{}}'::jsonb);
            INSERT INTO yaya_outbox(
                tenant_id, message_id, destination, idempotency_key, payload_sha256,
                status, attempt, message_json
            ) VALUES ('tenant_scope', 'message_scope_0001', 'product_projection',
                      'outbox_scope_0001', '{digest}', 'PENDING', 0, '{{}}'::jsonb);
            """
        )
        duplicate_outbox = self._execute(
            f"""
            INSERT INTO yaya_outbox(
                tenant_id, message_id, destination, idempotency_key, payload_sha256,
                status, attempt, message_json
            ) VALUES ('tenant_scope', 'message_scope_0002', 'product_projection',
                      'outbox_scope_0001', '{digest}', 'PENDING', 0, '{{}}'::jsonb);
            """,
            check=False,
        )
        self.assertNotEqual(duplicate_outbox.returncode, 0)
        self.assertIn("unique constraint", duplicate_outbox.stderr.lower())
        self._execute(
            f"""
            INSERT INTO yaya_outbox(
                tenant_id, message_id, destination, idempotency_key, payload_sha256,
                status, attempt, message_json
            ) VALUES ('tenant_scope', 'message_scope_0003', 'audit_projection',
                      'outbox_scope_0001', '{digest}', 'PENDING', 0, '{{}}'::jsonb);
            """
        )
        self.assertEqual(
            self._scalar(
                "SELECT (SELECT count(*) FROM yaya_commands "
                "WHERE tenant_id='tenant_scope' AND idempotency_key='idem_scope_0001')"
                "::text || ':' || (SELECT count(*) FROM yaya_outbox "
                "WHERE tenant_id='tenant_scope' AND idempotency_key='outbox_scope_0001')"
                "::text"
            ),
            "2:2",
        )

    def test_different_keys_racing_same_session_sequence_leave_one_command_job(self) -> None:
        digest = "1" * 64
        self._execute(
            f"""
            INSERT INTO yaya_tasks(
                tenant_id, task_id, actor_id, content_hash, snapshot_json
            ) VALUES ('tenant_sequence_race', 'task_sequence_race_0001',
                      'student_sequence_race_0001', '{digest}', '{{}}'::jsonb);
            INSERT INTO yaya_worlds(
                tenant_id, world_id, actor_id, content_hash, stream_id, revision,
                last_event_sequence, state_hash, world_rules_version, state_json,
                request_context_json
            ) VALUES ('tenant_sequence_race', 'world_sequence_race_0001',
                      'student_sequence_race_0001', '{digest}',
                      'stream_sequence_race_0001', 5, 40, '{digest}',
                      'farm-rules-1', '{{}}'::jsonb, '{{}}'::jsonb);
            INSERT INTO yaya_agent_sessions(
                tenant_id, session_id, actor_id, task_id, world_id, content_hash,
                snapshot_json
            ) VALUES ('tenant_sequence_race', 'session_sequence_race_0001',
                      'student_sequence_race_0001', 'task_sequence_race_0001',
                      'world_sequence_race_0001', '{digest}', '{{}}'::jsonb);
            """
        )

        def contender(ordinal: int) -> subprocess.Popen[str]:
            sql = f"""
            BEGIN;
            INSERT INTO yaya_commands(
                tenant_id, actor_id, operation, idempotency_key, command_id,
                session_id, turn_id, client_turn_sequence, request_sha256,
                content_hash, revision, status, updated_at, record_json
            ) VALUES ('tenant_sequence_race', 'student_sequence_race_0001',
                      'EXECUTE_AGENT_TURN', 'idem_sequence_race_000{ordinal}',
                      'cmd_sequence_race_000{ordinal}', 'session_sequence_race_0001',
                      'turn_sequence_race_0001', 1, '{digest}', '{digest}', 1, 'ACCEPTED',
                      clock_timestamp(), '{{}}'::jsonb);
            INSERT INTO yaya_command_jobs(
                tenant_id, command_id, job_id, actor_id, content_hash, session_id, turn_id,
                client_turn_sequence, event_json, operation_context_json, request_body,
                accepted_receipt_json, created_at
            ) VALUES ('tenant_sequence_race', 'cmd_sequence_race_000{ordinal}',
                      'job_sequence_race_000{ordinal}0000000',
                      'student_sequence_race_0001', '{digest}', 'session_sequence_race_0001',
                      'turn_sequence_race_0001', 1, '{{}}'::jsonb, '{{}}'::jsonb,
                      convert_to('{{"event_id":"event_sequence_race_000{ordinal}"}}','UTF8'),
                      '{{"job_id":"job_sequence_race_000{ordinal}0000000",'
                      '"job_type":"EXECUTE_AGENT_TURN","status":"ACCEPTED",'
                      '"created_at":"2026-08-09T00:00:00Z",'
                      '"updated_at":"2026-08-09T00:00:00Z",'
                      '"command_id":"cmd_sequence_race_000{ordinal}",'
                      '"trace_id":"trace_sequence_race_000{ordinal}","error":null}}'::jsonb,
                      clock_timestamp());
            SELECT pg_sleep(0.5);
            COMMIT;
            """
            return self._start_sql(sql)

        contenders = (contender(1), contender(2))
        outcomes: list[int] = []
        for process in contenders:
            _, stderr = process.communicate(timeout=30)
            outcomes.append(process.returncode)
            if process.returncode != 0:
                self.assertIn("unique constraint", stderr.lower())
        self.assertEqual(sum(outcome == 0 for outcome in outcomes), 1, outcomes)
        self.assertEqual(
            self._scalar(
                "SELECT (SELECT count(*) FROM yaya_commands "
                "WHERE tenant_id='tenant_sequence_race')::text || ':' || "
                "(SELECT count(*) FROM yaya_command_jobs "
                "WHERE tenant_id='tenant_sequence_race')::text"
            ),
            "1:1",
        )

    def test_same_turn_id_in_different_sessions_is_legal(self) -> None:
        digest = "2" * 64
        self._execute(
            f"""
            INSERT INTO yaya_tasks(
                tenant_id, task_id, actor_id, content_hash, snapshot_json
            ) VALUES ('tenant_cross_session', 'task_cross_session_0001',
                      'student_cross_session_0001', '{digest}', '{{}}'::jsonb);
            INSERT INTO yaya_worlds(
                tenant_id, world_id, actor_id, content_hash, stream_id, revision,
                last_event_sequence, state_hash, world_rules_version, state_json,
                request_context_json
            ) VALUES ('tenant_cross_session', 'world_cross_session_0001',
                      'student_cross_session_0001', '{digest}',
                      'stream_cross_session_0001', 5, 40, '{digest}',
                      'farm-rules-1', '{{}}'::jsonb, '{{}}'::jsonb);
            INSERT INTO yaya_agent_sessions(
                tenant_id, session_id, actor_id, task_id, world_id, content_hash,
                snapshot_json
            ) VALUES
                ('tenant_cross_session', 'session_cross_session_0001',
                 'student_cross_session_0001', 'task_cross_session_0001',
                 'world_cross_session_0001', '{digest}', '{{}}'::jsonb),
                ('tenant_cross_session', 'session_cross_session_0002',
                 'student_cross_session_0001', 'task_cross_session_0001',
                 'world_cross_session_0001', '{digest}', '{{}}'::jsonb);
            INSERT INTO yaya_commands(
                tenant_id, actor_id, operation, idempotency_key, command_id,
                session_id, turn_id, client_turn_sequence, request_sha256,
                content_hash, revision, status, updated_at, record_json
            ) VALUES
                ('tenant_cross_session', 'student_cross_session_0001',
                 'EXECUTE_AGENT_TURN', 'idem_cross_session_0001',
                 'cmd_cross_session_0001', 'session_cross_session_0001',
                 'turn_shared_cross_session_0001', 1, '{digest}', '{digest}', 1, 'ACCEPTED',
                 clock_timestamp(), '{{}}'::jsonb),
                ('tenant_cross_session', 'student_cross_session_0001',
                 'EXECUTE_AGENT_TURN', 'idem_cross_session_0002',
                 'cmd_cross_session_0002', 'session_cross_session_0002',
                 'turn_shared_cross_session_0001', 1, '{digest}', '{digest}', 1, 'ACCEPTED',
                 clock_timestamp(), '{{}}'::jsonb);
            INSERT INTO yaya_command_jobs(
                tenant_id, command_id, job_id, actor_id, content_hash, session_id, turn_id,
                client_turn_sequence, event_json, operation_context_json, request_body,
                accepted_receipt_json, created_at
            ) VALUES
                ('tenant_cross_session', 'cmd_cross_session_0001', 'job_cross_session_00000001',
                 'student_cross_session_0001', '{digest}', 'session_cross_session_0001',
                 'turn_shared_cross_session_0001', 1, '{{}}'::jsonb, '{{}}'::jsonb,
                 convert_to('{{"event_id":"event_cross_session_0001"}}','UTF8'),
                 '{{"job_id":"job_cross_session_00000001",'
                 '"job_type":"EXECUTE_AGENT_TURN","status":"ACCEPTED",'
                 '"created_at":"2026-08-09T00:00:00Z",'
                 '"updated_at":"2026-08-09T00:00:00Z",'
                 '"command_id":"cmd_cross_session_0001",'
                 '"trace_id":"trace_cross_session_0001","error":null}}'::jsonb,
                 clock_timestamp()),
                ('tenant_cross_session', 'cmd_cross_session_0002', 'job_cross_session_00000002',
                 'student_cross_session_0001', '{digest}', 'session_cross_session_0002',
                 'turn_shared_cross_session_0001', 1, '{{}}'::jsonb, '{{}}'::jsonb,
                 convert_to('{{"event_id":"event_cross_session_0002"}}','UTF8'),
                 '{{"job_id":"job_cross_session_00000002",'
                 '"job_type":"EXECUTE_AGENT_TURN","status":"ACCEPTED",'
                 '"created_at":"2026-08-09T00:00:00Z",'
                 '"updated_at":"2026-08-09T00:00:00Z",'
                 '"command_id":"cmd_cross_session_0002",'
                 '"trace_id":"trace_cross_session_0002","error":null}}'::jsonb,
                 clock_timestamp());
            """
        )
        self.assertEqual(
            self._scalar(
                "SELECT (SELECT count(*) FROM yaya_commands "
                "WHERE tenant_id='tenant_cross_session')::text || ':' || "
                "(SELECT count(*) FROM yaya_command_jobs "
                "WHERE tenant_id='tenant_cross_session')::text"
            ),
            "2:2",
        )

    def test_terminal_records_reject_tenant_actor_and_content_mislinks(self) -> None:
        digest = "3" * 64
        other_digest = "4" * 64
        self._execute(
            f"""
            INSERT INTO yaya_tasks(
                tenant_id, task_id, actor_id, content_hash, snapshot_json
            ) VALUES ('tenant_terminal', 'task_terminal_0001',
                      'student_terminal_0001', '{digest}', '{{}}'::jsonb);
            INSERT INTO yaya_worlds(
                tenant_id, world_id, actor_id, content_hash, stream_id, revision,
                last_event_sequence, state_hash, world_rules_version, state_json,
                request_context_json
            ) VALUES ('tenant_terminal', 'world_terminal_0001',
                      'student_terminal_0001', '{digest}', 'stream_terminal_0001',
                      5, 40, '{digest}', 'farm-rules-1', '{{}}'::jsonb, '{{}}'::jsonb);
            INSERT INTO yaya_agent_sessions(
                tenant_id, session_id, actor_id, task_id, world_id, content_hash,
                snapshot_json
            ) VALUES ('tenant_terminal', 'session_terminal_0001',
                      'student_terminal_0001', 'task_terminal_0001',
                      'world_terminal_0001', '{digest}', '{{}}'::jsonb);
            INSERT INTO yaya_skills(
                tenant_id, skill_id, skill_version_id, certification_id, actor_id,
                session_id, content_hash, artifact_sha256, snapshot_json, active
            ) VALUES ('tenant_terminal', 'skill_terminal_0001',
                      'skill_version_terminal_0001', 'certification_terminal_0001',
                      'student_terminal_0001', 'session_terminal_0001', '{digest}',
                      '{digest}', '{{}}'::jsonb, TRUE);
            INSERT INTO yaya_commands(
                tenant_id, actor_id, operation, idempotency_key, command_id,
                session_id, turn_id, client_turn_sequence, request_sha256,
                content_hash, revision, status, updated_at, record_json
            ) VALUES ('tenant_terminal', 'student_terminal_0001',
                      'EXECUTE_AGENT_TURN', 'idem_terminal_0001',
                      'cmd_terminal_0001', 'session_terminal_0001',
                      'turn_terminal_0001', 1, '{digest}', '{digest}', 1,
                      'APPLIED', clock_timestamp(), '{{}}'::jsonb);
            """
        )

        invalid_runs = (
            (
                "actor",
                f"""INSERT INTO yaya_runs(
                    tenant_id, run_id, actor_id, content_hash, session_id, turn_id,
                    command_id, world_id, skill_version_id, task_success,
                    snapshot_json, wire_json
                ) VALUES ('tenant_terminal', 'run_terminal_bad_actor_0001',
                          'student_terminal_wrong_0001', '{digest}',
                          'session_terminal_0001', 'turn_terminal_0001',
                          'cmd_terminal_0001', 'world_terminal_0001',
                          'skill_version_terminal_0001', TRUE, '{{}}'::jsonb,
                          '{{}}'::jsonb)""",
            ),
            (
                "content",
                f"""INSERT INTO yaya_runs(
                    tenant_id, run_id, actor_id, content_hash, session_id, turn_id,
                    command_id, world_id, skill_version_id, task_success,
                    snapshot_json, wire_json
                ) VALUES ('tenant_terminal', 'run_terminal_bad_content_0001',
                          'student_terminal_0001', '{other_digest}',
                          'session_terminal_0001', 'turn_terminal_0001',
                          'cmd_terminal_0001', 'world_terminal_0001',
                          'skill_version_terminal_0001', TRUE, '{{}}'::jsonb,
                          '{{}}'::jsonb)""",
            ),
            (
                "tenant",
                f"""INSERT INTO yaya_runs(
                    tenant_id, run_id, actor_id, content_hash, session_id, turn_id,
                    command_id, world_id, skill_version_id, task_success,
                    snapshot_json, wire_json
                ) VALUES ('tenant_terminal_wrong', 'run_terminal_bad_tenant_0001',
                          'student_terminal_0001', '{digest}',
                          'session_terminal_0001', 'turn_terminal_0001',
                          'cmd_terminal_0001', 'world_terminal_0001',
                          'skill_version_terminal_0001', TRUE, '{{}}'::jsonb,
                          '{{}}'::jsonb)""",
            ),
        )
        for identity, sql in invalid_runs:
            with self.subTest(record="run", identity=identity):
                rejected = self._execute(sql, check=False)
                self.assertNotEqual(rejected.returncode, 0)
                self.assertIn("foreign key constraint", rejected.stderr.lower())

        self._execute(
            f"""
            INSERT INTO yaya_runs(
                tenant_id, run_id, actor_id, content_hash, session_id, turn_id,
                command_id, world_id, skill_version_id, task_success,
                snapshot_json, wire_json
            ) VALUES ('tenant_terminal', 'run_terminal_0001',
                      'student_terminal_0001', '{digest}', 'session_terminal_0001',
                      'turn_terminal_0001', 'cmd_terminal_0001',
                      'world_terminal_0001', 'skill_version_terminal_0001', TRUE,
                      '{{}}'::jsonb, '{{}}'::jsonb);
            """
        )

        invalid_children = (
            (
                "invocation_actor",
                f"""INSERT INTO yaya_skill_invocations(
                    tenant_id, invocation_id, actor_id, content_hash, run_id,
                    request_sha256, result_json
                ) VALUES ('tenant_terminal', 'invocation_bad_actor_0001',
                          'student_terminal_wrong_0001', '{digest}',
                          'run_terminal_0001', '{digest}', '{{}}'::jsonb)""",
            ),
            (
                "invocation_content",
                f"""INSERT INTO yaya_skill_invocations(
                    tenant_id, invocation_id, actor_id, content_hash, run_id,
                    request_sha256, result_json
                ) VALUES ('tenant_terminal', 'invocation_bad_content_0001',
                          'student_terminal_0001', '{other_digest}',
                          'run_terminal_0001', '{digest}', '{{}}'::jsonb)""",
            ),
            (
                "invocation_tenant",
                f"""INSERT INTO yaya_skill_invocations(
                    tenant_id, invocation_id, actor_id, content_hash, run_id,
                    request_sha256, result_json
                ) VALUES ('tenant_terminal_wrong', 'invocation_bad_tenant_0001',
                          'student_terminal_0001', '{digest}', 'run_terminal_0001',
                          '{digest}', '{{}}'::jsonb)""",
            ),
            (
                "interaction_actor",
                f"""INSERT INTO yaya_agent_interactions(
                    tenant_id, interaction_id, actor_id, content_hash, session_id,
                    turn_id, command_id, run_id, sequence, projection_json
                ) VALUES ('tenant_terminal', 'interaction_bad_actor_0001',
                          'student_terminal_wrong_0001', '{digest}',
                          'session_terminal_0001', 'turn_terminal_0001',
                          'cmd_terminal_0001', 'run_terminal_0001', 1, '{{}}'::jsonb)""",
            ),
            (
                "interaction_content",
                f"""INSERT INTO yaya_agent_interactions(
                    tenant_id, interaction_id, actor_id, content_hash, session_id,
                    turn_id, command_id, run_id, sequence, projection_json
                ) VALUES ('tenant_terminal', 'interaction_bad_content_0001',
                          'student_terminal_0001', '{other_digest}',
                          'session_terminal_0001', 'turn_terminal_0001',
                          'cmd_terminal_0001', 'run_terminal_0001', 1, '{{}}'::jsonb)""",
            ),
            (
                "interaction_tenant",
                f"""INSERT INTO yaya_agent_interactions(
                    tenant_id, interaction_id, actor_id, content_hash, session_id,
                    turn_id, command_id, run_id, sequence, projection_json
                ) VALUES ('tenant_terminal_wrong', 'interaction_bad_tenant_0001',
                          'student_terminal_0001', '{digest}', 'session_terminal_0001',
                          'turn_terminal_0001', 'cmd_terminal_0001',
                          'run_terminal_0001', 1, '{{}}'::jsonb)""",
            ),
        )
        for identity, sql in invalid_children:
            with self.subTest(record="terminal_child", identity=identity):
                rejected = self._execute(sql, check=False)
                self.assertNotEqual(rejected.returncode, 0)
                self.assertIn("foreign key constraint", rejected.stderr.lower())

        self._execute(
            f"""
            INSERT INTO yaya_skill_invocations(
                tenant_id, invocation_id, actor_id, content_hash, run_id,
                request_sha256, result_json
            ) VALUES ('tenant_terminal', 'invocation_terminal_0001',
                      'student_terminal_0001', '{digest}', 'run_terminal_0001',
                      '{digest}', '{{}}'::jsonb);
            INSERT INTO yaya_agent_interactions(
                tenant_id, interaction_id, actor_id, content_hash, session_id,
                turn_id, command_id, run_id, sequence, projection_json
            ) VALUES ('tenant_terminal', 'interaction_terminal_0001',
                      'student_terminal_0001', '{digest}', 'session_terminal_0001',
                      'turn_terminal_0001', 'cmd_terminal_0001', 'run_terminal_0001',
                      1, '{{}}'::jsonb);
            """
        )
        self.assertEqual(
            self._scalar(
                "SELECT (SELECT count(*) FROM yaya_runs "
                "WHERE tenant_id='tenant_terminal')::text || ':' || "
                "(SELECT count(*) FROM yaya_skill_invocations "
                "WHERE tenant_id='tenant_terminal')::text || ':' || "
                "(SELECT count(*) FROM yaya_agent_interactions "
                "WHERE tenant_id='tenant_terminal')::text"
            ),
            "1:1:1",
        )

    def test_two_connections_racing_one_world_revision_have_one_winner(self) -> None:
        digest = "c" * 64
        self._execute(
            f"""
            INSERT INTO yaya_worlds(
                tenant_id, world_id, actor_id, content_hash, stream_id, revision,
                last_event_sequence, state_hash, world_rules_version, state_json,
                request_context_json
            ) VALUES ('tenant_cas', 'world_cas_0001', 'student_cas_0001', '{digest}',
                      'stream_cas_0001', 5, 40, '{digest}', 'farm-rules-1',
                      '{{}}'::jsonb, '{{}}'::jsonb);
            """
        )
        competing_sql = (
            "BEGIN; WITH changed AS ("
            "UPDATE yaya_worlds SET revision=revision+1 "
            "WHERE tenant_id='tenant_cas' AND world_id='world_cas_0001' AND revision=5 "
            "RETURNING revision) SELECT count(*) FROM changed; "
            "SELECT pg_sleep(0.5); COMMIT;"
        )
        contenders = (self._start_sql(competing_sql), self._start_sql(competing_sql))
        results: list[str] = []
        for contender in contenders:
            stdout, stderr = contender.communicate(timeout=30)
            self.assertEqual(contender.returncode, 0, stderr)
            values = [line.strip() for line in stdout.splitlines() if line.strip() in {"0", "1"}]
            self.assertEqual(len(values), 1, stdout)
            results.append(values[0])
        self.assertEqual(sorted(results), ["0", "1"])
        self.assertEqual(
            self._scalar(
                "SELECT revision FROM yaya_worlds "
                "WHERE tenant_id='tenant_cas' AND world_id='world_cas_0001'"
            ),
            "6",
        )


if __name__ == "__main__":
    unittest.main()
