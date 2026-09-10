"""Read authorized, current-entry evidence for post-success practice."""

from sqlalchemy import select
from yaya_agent_contracts import Failure

from .models import (
    AgentSessionRow,
    RunRow,
    SkillBuildProvenanceRow,
    SkillBuildRow,
    SkillCertificationRow,
)
from .run_evidence import PostgresRunEvidenceStore


class PracticeReads:
    def __init__(self, sessions):
        self.sessions = sessions
        self.runs = PostgresRunEvidenceStore(sessions)

    async def authorize(self, session_id, context):
        async with self.sessions() as db:
            found = await db.scalar(
                select(AgentSessionRow.session_id).where(
                    AgentSessionRow.session_id == session_id,
                    AgentSessionRow.tenant_id == context.actor.tenant_id,
                    AgentSessionRow.actor_id == context.actor.actor_id,
                    AgentSessionRow.status == "ACTIVE",
                )
            )
        if found is None:
            raise ValueError("PRACTICE_SESSION_UNAVAILABLE")

    async def completed_context(self, session_id, run_id, started, context):
        result = await self.runs.get_run(run_id, context)
        if (
            isinstance(result, Failure)
            or result.value.get("status") != "SUCCEEDED"
            or result.value.get("session_id") != session_id
        ):
            raise ValueError("PRACTICE_MAIN_NOT_PASSED")
        run = result.value
        async with self.sessions() as db:
            row = await db.get(RunRow, run_id)
            if row.created_at < started:
                raise ValueError("PRACTICE_PREVIOUS_ENTRY_RUN")
            failed_runs = (
                await db.scalars(
                    select(RunRow)
                    .where(
                        RunRow.session_id == session_id,
                        RunRow.tenant_id == context.actor.tenant_id,
                        RunRow.actor_id == context.actor.actor_id,
                        RunRow.created_at >= started,
                        RunRow.created_at <= row.created_at,
                        RunRow.run_json["status"].as_string().in_(["REJECTED", "FAILED"]),
                    )
                    .order_by(RunRow.created_at.desc())
                    .limit(20)
                )
            ).all()
            failed_builds = (
                await db.scalars(
                    select(SkillBuildRow)
                    .join(
                        SkillBuildProvenanceRow,
                        SkillBuildProvenanceRow.build_id == SkillBuildRow.build_id,
                    )
                    .where(
                        SkillBuildProvenanceRow.session_id == session_id,
                        SkillBuildRow.tenant_id == context.actor.tenant_id,
                        SkillBuildRow.actor_id == context.actor.actor_id,
                        SkillBuildRow.created_at >= started,
                        SkillBuildRow.created_at <= row.created_at,
                        SkillBuildRow.status == "REJECTED",
                    )
                    .order_by(SkillBuildRow.created_at.desc())
                    .limit(20)
                )
            ).all()
            build = await db.scalar(
                select(SkillBuildRow)
                .join(
                    SkillCertificationRow, SkillCertificationRow.build_id == SkillBuildRow.build_id
                )
                .where(
                    SkillCertificationRow.certification_id == run["skill"]["certification_id"],
                    SkillBuildRow.tenant_id == context.actor.tenant_id,
                    SkillBuildRow.actor_id == context.actor.actor_id,
                )
            )
            failed_sources = dict(
                (
                    await db.execute(
                        select(SkillCertificationRow.certification_id, SkillBuildRow.request_json)
                        .join(
                            SkillBuildRow, SkillBuildRow.build_id == SkillCertificationRow.build_id
                        )
                        .where(
                            SkillCertificationRow.certification_id.in_(
                                [
                                    r.run_json.get("skill", {}).get("certification_id", "")
                                    for r in failed_runs
                                ]
                            ),
                            SkillBuildRow.tenant_id == context.actor.tenant_id,
                            SkillBuildRow.actor_id == context.actor.actor_id,
                        )
                    )
                ).all()
            )
        failures = [
            {
                "run_id": r.run_id,
                "feedback": r.run_json.get("agent_feedback", {}),
                "world_application": r.run_json.get("world_application", {}),
                "source": "\n".join(
                    str(f.get("content", ""))
                    for f in failed_sources.get(
                        r.run_json.get("skill", {}).get("certification_id", ""), {}
                    )
                    .get("source_bundle", {})
                    .get("files", [])
                )[:16000],
            }
            for r in failed_runs
        ]
        failures += [
            {
                "build_id": b.build_id,
                "diagnostics": b.build_json.get("diagnostics", []),
                "source": _source(b),
            }
            for b in failed_builds
        ]
        return {
            "run_id": run_id,
            "main_source": _source(build) if build else "",
            "failures": failures,
            "history_scope": "current_game_entry",
            "main_feedback": run.get("agent_feedback", {}),
        }


def _source(build):
    bundle = build.request_json.get("source_bundle", {})
    return "\n".join(str(f.get("content", "")) for f in bundle.get("files", []))[:16000]
