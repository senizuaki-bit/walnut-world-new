"""Read-only, whole-database side-effect fingerprints for the A8 failure matrices."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from psycopg import sql
from yaya_agent_backend.database import PostgresDatabase

# Every business table introduced by the three production migrations.  The
# collector also discovers additional public ``yaya_*`` tables so a later
# compatible migration cannot silently fall outside the no-side-effect proof.
A8_BUSINESS_TABLES = (
    "yaya_tasks",
    "yaya_worlds",
    "yaya_agent_sessions",
    "yaya_skills",
    "yaya_compile_results",
    "yaya_evidence",
    "yaya_runs",
    "yaya_skill_invocations",
    "yaya_counterexamples",
    "yaya_learner_models",
    "yaya_agent_messages",
    "yaya_commands",
    "yaya_command_jobs",
    "yaya_agent_turns",
    "yaya_agent_interactions",
    "yaya_projection_outbox",
    "yaya_agent_traces",
    "yaya_events",
    "yaya_outbox",
    "yaya_audit",
    "yaya_registry_certifications",
    "yaya_registry_active",
    "yaya_learner_projection_jobs",
    "yaya_learner_projection_job_evidence",
    "yaya_learner_projection_receipts",
    "yaya_learner_projection_failures",
    "yaya_learner_projection_terminal_audits",
    "yaya_learners",
    "yaya_agent_profiles",
    "yaya_launch_authorities",
    "yaya_build_policies",
    "yaya_public_agent_sessions",
    "yaya_control_jobs",
    "yaya_skill_draft_revisions",
    "yaya_skill_draft_heads",
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
)

A8_DRAFT_MUTATION_TABLES = frozenset(
    {
        "yaya_skill_draft_revisions",
        "yaya_skill_draft_heads",
        "yaya_product_write_receipts",
    }
)

# A failed Build still has to durably terminalize the already accepted
# Command/Job/Build and its append-only execution evidence.  No other business
# table is allowed to change.
A8_FAILED_BUILD_MUTATION_TABLES = frozenset(
    {
        "yaya_commands",
        "yaya_control_jobs",
        "yaya_skill_builds",
        "yaya_skill_build_history",
        "yaya_build_step_receipts",
    }
)


@dataclass(frozen=True, slots=True)
class TableStateFingerprint:
    exists: bool
    row_count: int
    rows_md5: str | None


type A8StateFingerprint = dict[str, TableStateFingerprint]


async def a8_state_fingerprint(database: PostgresDatabase) -> A8StateFingerprint:
    """Capture one repeatable-read count and canonical-ish row hash per table.

    ``to_jsonb(composite)::text`` gives deterministic JSONB key ordering and
    PostgreSQL text encodings for every column.  Each row is hashed separately,
    sorted by that fixed-width digest, and folded into one digest, so physical
    row order is irrelevant while duplicate rows still affect the result.
    Missing expected tables are represented explicitly instead of causing the
    diagnostic itself to fail.
    """

    connection = await database.connect()
    try:
        async with connection.transaction():
            await connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            cursor = await connection.execute(
                """
                SELECT tablename
                  FROM pg_catalog.pg_tables
                 WHERE schemaname='public' AND left(tablename,5)='yaya_'
                 ORDER BY tablename
                """
            )
            catalog_rows = await cursor.fetchall()
            existing = {cast(str, row["tablename"]) for row in catalog_rows}
            table_names = sorted(set(A8_BUSINESS_TABLES) | existing)
            result: A8StateFingerprint = {}
            for table_name in table_names:
                if table_name not in existing:
                    result[table_name] = TableStateFingerprint(False, 0, None)
                    continue
                fingerprint_cursor = await connection.execute(
                    sql.SQL(
                        """
                        SELECT count(*)::bigint AS row_count,
                               md5(COALESCE(string_agg(
                                   row_md5,'' ORDER BY row_md5
                               ),'')) AS rows_md5
                          FROM (
                              SELECT md5(to_jsonb(source_row)::text) AS row_md5
                                FROM {} AS source_row
                          ) AS row_digests
                        """
                    ).format(sql.Identifier("public", table_name))
                )
                row = await fingerprint_cursor.fetchone()
                if row is None:
                    raise AssertionError(f"fingerprint query returned no row for {table_name}")
                result[table_name] = TableStateFingerprint(
                    True,
                    cast(int, row["row_count"]),
                    cast(str, row["rows_md5"]),
                )
            return result
    finally:
        await connection.close()


def missing_a8_business_tables(fingerprint: A8StateFingerprint) -> tuple[str, ...]:
    return tuple(
        table_name for table_name in A8_BUSINESS_TABLES if not fingerprint[table_name].exists
    )


def fingerprint_without(
    fingerprint: A8StateFingerprint,
    excluded_tables: frozenset[str],
) -> A8StateFingerprint:
    """Project a full fingerprint after documenting its permitted mutations."""

    return {
        table_name: state
        for table_name, state in fingerprint.items()
        if table_name not in excluded_tables
    }


__all__ = [
    "A8_BUSINESS_TABLES",
    "A8_DRAFT_MUTATION_TABLES",
    "A8_FAILED_BUILD_MUTATION_TABLES",
    "A8StateFingerprint",
    "TableStateFingerprint",
    "a8_state_fingerprint",
    "fingerprint_without",
    "missing_a8_business_tables",
]
