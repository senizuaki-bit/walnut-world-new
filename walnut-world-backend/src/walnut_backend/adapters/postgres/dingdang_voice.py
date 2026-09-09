"""Small read-only context for the game's ephemeral Doubao conversation."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yaya_agent_contracts import ContentRef, OperationContext
from yaya_agent_runtime.conversation_teaching import conversation_directive, conversation_guidance
from yaya_agent_runtime.voice import VoiceError

from .agent_runtime import AgentRuntimeAuthorityError, PostgresAgentRuntimeReads, _evidence_refs
from .build_failures import list_current_build_failure_streak
from .models import (
    AgentSessionRow,
    JobStepReceiptRow,
    LearnerProfileRow,
    RunRow,
    SkillBuildProvenanceRow,
    SkillBuildRow,
    WorkflowJobRow,
)
from .product_workspaces import PostgresProductWorkspaceStore
from .session import snapshot_read


class PostgresDingdangVoiceContext:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions
        self._workspaces = PostgresProductWorkspaceStore(sessions)
        self._reads = PostgresAgentRuntimeReads(sessions)

    async def latest_run_revision(
        self, session_id: str, context: OperationContext
    ) -> tuple[object, ...] | None:
        """One query watches Run, Build, teaching outcome and learner changes."""
        latest_run = (
            select(RunRow.run_id)
            .where(
                RunRow.tenant_id == AgentSessionRow.tenant_id,
                RunRow.actor_id == AgentSessionRow.actor_id,
                RunRow.session_id == AgentSessionRow.session_id,
                RunRow.content_hash
                == AgentSessionRow.session_json["content"]["content_hash"].as_string(),
            )
            .order_by(RunRow.created_at.desc(), RunRow.run_id.desc())
            .limit(1)
            .correlate(AgentSessionRow)
            .scalar_subquery()
        )
        latest_build = (
            select(func.concat(SkillBuildRow.build_id, ":", SkillBuildRow.status))
            .join(
                SkillBuildProvenanceRow, SkillBuildProvenanceRow.build_id == SkillBuildRow.build_id
            )
            .where(
                SkillBuildRow.tenant_id == AgentSessionRow.tenant_id,
                SkillBuildRow.actor_id == AgentSessionRow.actor_id,
                SkillBuildProvenanceRow.session_id == AgentSessionRow.session_id,
            )
            .order_by(SkillBuildRow.created_at.desc(), SkillBuildRow.build_id.desc())
            .limit(1)
            .correlate(AgentSessionRow)
            .scalar_subquery()
        )
        async with self._sessions() as db:
            row = (
                await db.execute(
                    select(
                        RunRow.run_id,
                        RunRow.run_json["updated_at"].as_string(),
                        # Feedback may reuse the Run's causal timestamp. Its
                        # payload must participate even when updated_at is equal.
                        func.md5(RunRow.run_json["agent_feedback"].as_string()),
                        latest_build,
                        JobStepReceiptRow.output_sha256,
                        LearnerProfileRow.profile_sha256,
                    )
                    .select_from(AgentSessionRow)
                    .outerjoin(RunRow, RunRow.run_id == latest_run)
                    .outerjoin(
                        WorkflowJobRow,
                        (WorkflowJobRow.command_id == RunRow.command_id)
                        & (WorkflowJobRow.tenant_id == AgentSessionRow.tenant_id),
                    )
                    .outerjoin(
                        JobStepReceiptRow,
                        (JobStepReceiptRow.job_id == WorkflowJobRow.job_id)
                        & (JobStepReceiptRow.tenant_id == AgentSessionRow.tenant_id)
                        & (JobStepReceiptRow.step_name == "OUTCOME_DERIVED"),
                    )
                    .outerjoin(
                        LearnerProfileRow,
                        (LearnerProfileRow.tenant_id == AgentSessionRow.tenant_id)
                        & (LearnerProfileRow.actor_id == AgentSessionRow.actor_id)
                        & (
                            LearnerProfileRow.content_hash
                            == AgentSessionRow.session_json["content"]["content_hash"].as_string()
                        ),
                    )
                    .where(
                        AgentSessionRow.tenant_id == context.actor.tenant_id,
                        AgentSessionRow.actor_id == context.actor.actor_id,
                        AgentSessionRow.session_id == session_id,
                        AgentSessionRow.status == "ACTIVE",
                    )
                )
            ).one_or_none()
        return None if row is None else tuple(row)

    async def load(self, session_id: str, context: OperationContext, client_context: dict) -> str:
        workspace = await self._workspaces.get(session_id, context)
        if not workspace.ok:
            raise VoiceError("VOICE_SESSION_UNAVAILABLE")
        value = workspace.value
        context = replace(context, content_ref=ContentRef(**value["content_ref"]))
        try:
            session = await self._reads.get_session(session_id, context)
            task = await self._reads.get_task(session.task_id, context)
            learner = await self._reads.get_profile(
                context.actor.actor_id, task.knowledge_points, context
            )
        except AgentRuntimeAuthorityError:
            raise VoiceError("VOICE_SESSION_UNAVAILABLE") from None
        async with snapshot_read(self._sessions) as db:
            run = await db.scalar(
                select(RunRow.run_json)
                .where(
                    RunRow.tenant_id == context.actor.tenant_id,
                    RunRow.actor_id == context.actor.actor_id,
                    RunRow.session_id == session_id,
                    RunRow.content_hash == context.content_ref.content_hash,
                )
                .order_by(RunRow.created_at.desc(), RunRow.run_id.desc())
                .limit(1)
            )
            builds = await list_current_build_failure_streak(
                db, session_id=session_id, context=context
            )
            outcome = None
            if run:
                outcome = await db.scalar(
                    select(JobStepReceiptRow.receipt_json)
                    .join(WorkflowJobRow, WorkflowJobRow.job_id == JobStepReceiptRow.job_id)
                    .join(RunRow, RunRow.command_id == WorkflowJobRow.command_id)
                    .where(
                        JobStepReceiptRow.tenant_id == context.actor.tenant_id,
                        JobStepReceiptRow.step_name == "OUTCOME_DERIVED",
                        RunRow.tenant_id == context.actor.tenant_id,
                        RunRow.actor_id == context.actor.actor_id,
                        RunRow.session_id == session_id,
                        RunRow.run_json["run_id"].as_string() == run["run_id"],
                    )
                )
        failure_count = 0
        evidence_refs = ()
        history = []
        current_diagnostics = ()
        if builds:
            failure_count = len(builds)
            evidence_refs = builds[-1].snapshot.evidence_refs
            current_diagnostics = builds[-1].snapshot.diagnostics
            history = [
                {"attempt": index, "diagnostics": item.snapshot.diagnostics}
                for index, item in enumerate(builds, start=1)
            ]
        elif outcome:
            saved_event = outcome["event"]
            failure_count = saved_event["failure_count"]
            evidence_refs = _evidence_refs(saved_event.get("evidence_refs", []))
        directive = conversation_directive(
            task,
            learner,
            failure_count=failure_count,
            evidence_refs=evidence_refs,
            event_time=max(
                datetime.now(UTC),
                context.requested_at,
                *(item.created_at for item in evidence_refs),
            ),
            teaching_spec_version=os.environ.get(
                "WALNUT_TEACHING_SPEC_VERSION", "agent-teaching-v1"
            ),
        )
        teaching = conversation_guidance(directive, failure_count)
        if run and not builds and not outcome:
            teaching += "\n本次运行的教学计数尚未整理完成；失败次数暂为0不代表运行成功或没有错误。"
        # Tool results have a 4000-character limit. Keep prompt data readable,
        # with explicit truncation rather than sending an entire Run resource.
        run_summary = None
        task_status = "尚无正式运行，任务完成情况未验证"
        if run:
            sandbox = run.get("sandbox") or {}
            world = run.get("world_application") or {}
            feedback = run.get("agent_feedback") or {}
            completed = run["status"] == "SUCCEEDED" and world.get("status") == "COMMITTED"
            task_status = "本次任务已完成" if completed else "最近一次正式运行未完成任务"
            run_summary = {
                "run_id": run["run_id"],
                "status": run["status"],
                "sandbox_status": sandbox.get("status"),
                "sandbox_failure": sandbox.get("failure"),
                "world_status": world.get("status"),
                "world_failure": world.get("failure"),
                "feedback": feedback.get("message"),
                "feedback_status": "已生成" if feedback else "尚未生成，不影响已确认的运行结果",
            }
        return (
            f"关卡：{task.title[:100]}\n目标：{task.goal[:250]}\n"
            f"知识点：{', '.join(task.knowledge_points)[:160]}\n"
            f"{teaching}\n"
            f"当前编译报错（超过400字截断）：\n{json.dumps(current_diagnostics, ensure_ascii=False)[:400]}\n"
            f"本题历史编译错误（最近6次，按顺序；超过400字截断）：\n{json.dumps(history[-6:], ensure_ascii=False)[:400]}\n"
            f"任务状态（依据最近正式运行）：{task_status}\n"
            f"当前编辑器代码（学生尚未提交的内容；超过1200字截断）：\n{str(client_context.get('code', ''))[:1200]}\n"
            f"最近正式运行（独立于当前编辑器代码；超过450字截断）：\n{json.dumps(run_summary, ensure_ascii=False)[:450]}\n"
            f"客户端观察（不代表正式运行；超过200字截断）：\n{str(client_context.get('observation', ''))[:200]}"
        )
