"""Apply the existing teaching policy to Dingdang's continuous conversation."""

from datetime import datetime

from yaya_agent_contracts import EvidenceRef

from .context_builder import _competency_summaries
from .domain import LearnerProfileSnapshot, TaskSnapshot
from .pedagogy_policy import (
    PedagogyEvidence,
    PedagogyEvidenceOutcome,
    PedagogyInput,
    PedagogyPolicy,
    TeachingDirective,
)


def conversation_directive(
    task: TaskSnapshot,
    learner: LearnerProfileSnapshot,
    *,
    failure_count: int,
    evidence_refs: tuple[EvidenceRef, ...],
    event_time: datetime,
    teaching_spec_version: str,
) -> TeachingDirective:
    """A voice question uses the same policy inputs as a text hint.

    Speaking never creates another failed attempt or authorizes a code patch.
    The transport changes; phases and hint ceilings remain policy-owned.
    """
    directive = PedagogyPolicy().decide(
        PedagogyInput(
            role="teaching_agent",
            event_type="hint_requested",
            failure_count=failure_count,
            hint_requested=True,
            student_message_present=True,
            teaching_spec_version=teaching_spec_version,
            task_concepts=task.knowledge_points,
            max_hint_level=task.max_hint_level,
            learner_revision=learner.revision,
            learner_competencies=_competency_summaries(learner),
            learner_evidence_ids=tuple(item.evidence_id for item in learner.evidence_refs),
            current_validated_evidence=tuple(
                PedagogyEvidence(
                    evidence_id=item.evidence_id,
                    outcome=PedagogyEvidenceOutcome.FAILED
                    if failure_count
                    else PedagogyEvidenceOutcome.SUCCESS,
                    occurred_at=item.created_at,
                )
                for item in evidence_refs
            ),
            event_time=event_time,
        )
    )
    assert directive is not None
    return directive


def conversation_guidance(directive: TeachingDirective, failure_count: int) -> str:
    levels = {
        0: "先确认现象和目标，用一个问题引导观察。",
        1: "指出需要检查的代码区域，不直接给修改答案。",
        2: "解释具体错误原因，引导学生比较输入、条件和结果。",
        3: "给一个局部修改方向和验证方法，仍不提供整题答案。",
    }
    phases = {
        "REVIEW": "回顾基础",
        "HEURISTIC": "启发探索",
        "RECTIFICATION": "纠错辅导",
        "SUMMARIZATION": "总结迁移",
    }
    return (
        f"教学策略：阶段={directive.phase.value}（{phases[directive.phase.value]}）；"
        f"提示等级={directive.hint_level}；失败次数={failure_count}；知识点={directive.target_concept}。\n"
        f"课程辅导要求：{levels[directive.hint_level]}"
        "可结合下面的历史错误比较前后变化，不把不同报错说成完全相同的问题。"
        "闲聊和一般知识直接回应，不强行套用课程提示。不能代改代码或直接给整题代码。"
    )
