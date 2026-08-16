"""Idempotent INT3 projection from PostgreSQL learning authority into Feishu assets."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from html import escape, unescape
from typing import Any, Protocol
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

from walnut_backend.application.feishu.learning_queries import (
    EvidenceLearningBundle,
    LearnerLearningBundle,
    LearningProjectionAuthority,
    previous_knowledge_stage_for_feishu,
    projection_facts_for_feishu,
    redact_evidence_for_feishu,
    stable_class_ref,
)

MISSING = "暂无数据"
TEMPLATE_VERSION = "v1"
PENDING_APPEND = "待追加"
APPENDED = "已追加"
MAX_MIAODA_SQL_CHARS = 20_000
DAILY_GROWTH_FACT_FIELDS = (
    "任务ID",
    "任务名称",
    "完成结果",
    "尝试次数",
    "主要错误",
    "AI辅助程度",
    "AI辅助说明",
    "Skill Patch使用",
    "知识点",
    "阶段前",
    "阶段后",
    "今日进步",
    "下一步建议",
    "Run ID",
    "Evidence引用",
)
_BINDING_SCHEME = "WALNUT_FEISHU_HMAC_SHA256_V1"
_SECRET_FINGERPRINT_DOMAIN = b"walnut-feishu-pseudonym-secret-fingerprint-v1\x00"
_TENANT_BINDING_DOMAIN = b"walnut-feishu-tenant-binding-v1\x00"
_GROWTH_BINDING_DOMAIN = b"walnut-growth-document-binding-v1\x00"
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_DOC_TOKEN = re.compile(r"/docx/([A-Za-z0-9]+)(?:[/?#]|$)")
_GROWTH_BINDING_MARKER = "WALNUT_GROWTH_DOCUMENT_BINDING_V1:"
_GROWTH_BINDING_SCHEMA = "WALNUT_GROWTH_DOCUMENT_BINDING_V1"
_GROWTH_COLUMNS = (
    "基本信息",
    "日期",
    "今日任务",
    "完成结果",
    "尝试次数",
    "主要错误",
    "AI辅助程度",
    "知识点阶段变化",
    "今日进步",
    "下一步建议",
    "Evidence链接",
)
_STAGE_ZH = {
    "NOT_OBSERVED": "未观察",
    "EMERGING": "初现",
    "DEVELOPING": "发展中",
    "PROFICIENT": "熟练",
    "REVIEW_NEEDED": "需复习",
}


@dataclass(frozen=True, slots=True)
class FeishuAssets:
    base_token: str
    base_url: str
    dashboard_id: str
    dashboard_url: str
    student_table_id: str
    daily_table_id: str
    evidence_table_id: str
    template_document_token: str
    template_document_url: str
    backend_public_url: str
    miaoda_app_id: str
    miaoda_online_url: str
    miaoda_environment: str
    tenant_binding_fingerprint: str
    pseudonym_secret_fingerprint: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> FeishuAssets:
        if value.get("schema_version") != "1.0.0":
            raise ValueError("Feishu asset config schema_version must be 1.0.0")
        base = _required_mapping(value.get("base"), "base")
        tables = _required_mapping(base.get("tables"), "base.tables")
        dashboard = _required_mapping(value.get("dashboard"), "dashboard")
        growth = _required_mapping(value.get("growth_document"), "growth_document")
        miaoda = _required_mapping(value.get("miaoda"), "miaoda")
        binding = _required_mapping(value.get("binding"), "binding")
        if binding.get("scheme") != _BINDING_SCHEME:
            raise ValueError(f"binding.scheme must be {_BINDING_SCHEME}")
        tenant_fingerprint = _required_fingerprint(
            binding.get("tenant_fingerprint"),
            "binding.tenant_fingerprint",
            prefix="hmac-sha256",
        )
        secret_fingerprint = _required_fingerprint(
            binding.get("pseudonym_secret_fingerprint"),
            "binding.pseudonym_secret_fingerprint",
            prefix="sha256",
        )
        if growth.get("template_version") != TEMPLATE_VERSION:
            raise ValueError("growth document template_version must be v1")
        app_id = _required_text(miaoda.get("app_id"), "miaoda.app_id")
        if re.fullmatch(r"app_[A-Za-z0-9]+", app_id) is None:
            raise ValueError("miaoda.app_id must be an app_ identifier")
        environment = _required_text(miaoda.get("environment"), "miaoda.environment")
        if environment not in {"dev", "online"}:
            raise ValueError("miaoda.environment must be dev or online")
        online_url = _safe_http_url(miaoda.get("online_url"), "miaoda.online_url")
        if f"/app/{app_id}" not in online_url:
            raise ValueError("miaoda.online_url does not match miaoda.app_id")
        return cls(
            base_token=_required_text(base.get("token"), "base.token"),
            base_url=_safe_http_url(base.get("url"), "base.url"),
            dashboard_id=_required_text(dashboard.get("id"), "dashboard.id"),
            dashboard_url=_safe_http_url(dashboard.get("url"), "dashboard.url"),
            student_table_id=_required_text(tables.get("student_profiles"), "student table"),
            daily_table_id=_required_text(tables.get("daily_records"), "daily table"),
            evidence_table_id=_required_text(tables.get("evidence_summaries"), "evidence table"),
            template_document_token=_required_text(
                growth.get("template_token"), "growth_document.template_token"
            ),
            template_document_url=_safe_http_url(
                growth.get("template_url"), "growth_document.template_url"
            ),
            backend_public_url=_safe_http_url(
                value.get("backend_public_url"), "backend_public_url"
            ),
            miaoda_app_id=app_id,
            miaoda_online_url=online_url,
            miaoda_environment=environment,
            tenant_binding_fingerprint=tenant_fingerprint,
            pseudonym_secret_fingerprint=secret_fingerprint,
        )

    def assert_binding(self, tenant_id: str, pseudonym_secret: str) -> None:
        """Fail closed unless one secret and one tenant own these external assets."""
        expected_secret = pseudonym_secret_fingerprint(pseudonym_secret)
        expected_tenant = tenant_binding_fingerprint(pseudonym_secret, tenant_id)
        if not hmac.compare_digest(self.pseudonym_secret_fingerprint, expected_secret):
            raise ValueError("Feishu asset pseudonym secret fingerprint does not match")
        if not hmac.compare_digest(self.tenant_binding_fingerprint, expected_tenant):
            raise ValueError("Feishu assets are bound to a different tenant")


@dataclass(frozen=True, slots=True)
class LearnerSyncBundle:
    learning: LearnerLearningBundle
    evidence: tuple[EvidenceLearningBundle, ...]


@dataclass(frozen=True, slots=True)
class TenantLearningSnapshot:
    tenant_id: str
    learners: tuple[LearnerSyncBundle, ...]


@dataclass(frozen=True, slots=True)
class BaseRecord:
    record_id: str
    fields: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class DocumentRef:
    token: str
    url: str


@dataclass(frozen=True, slots=True)
class SyncReport:
    learners: int = 0
    learning_records: int = 0
    evidence_records: int = 0
    base_records_created: int = 0
    base_records_updated: int = 0
    documents_created: int = 0
    document_blocks_appended: int = 0
    miaoda_rows_upserted: int = 0


@dataclass(slots=True)
class _MiaodaCache:
    students: list[Mapping[str, Any]]
    daily_records: list[Mapping[str, Any]]
    evidence: list[Mapping[str, Any]]


class FeishuSyncPort(Protocol):
    def fetch_document_xml(self, document_token: str) -> str: ...

    def fetch_document_xml_with_ids(self, document_token: str) -> str: ...

    def find_exact_record(
        self, table_id: str, key_field: str, business_key: str
    ) -> BaseRecord | None: ...

    def upsert_record(
        self,
        table_id: str,
        fields: Mapping[str, Any],
        *,
        record_id: str | None = None,
    ) -> BaseRecord: ...

    def create_document(self, content_xml: str) -> DocumentRef: ...

    def append_document(self, document_token: str, content_xml: str) -> None: ...

    def replace_document_block(
        self, document_token: str, block_id: str, content_xml: str
    ) -> None: ...

    def execute_miaoda_sql(
        self, app_id: str, environment: str, sql: str
    ) -> int: ...


class AmbiguousSyncState(RuntimeError):
    """A prior cross-system write may have succeeded; stop instead of duplicating it."""


class FeishuLearningSynchronizer:
    """Synchronize read-only learning projections; never owns a business write port."""

    def __init__(
        self,
        port: FeishuSyncPort,
        assets: FeishuAssets,
        *,
        pseudonym_secret: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        _validate_pseudonym_secret(pseudonym_secret)
        self._port = port
        self._assets = assets
        self._secret = pseudonym_secret
        self._clock = clock or (lambda: datetime.now(UTC))

    def sync(self, snapshot: TenantLearningSnapshot) -> SyncReport:
        # This is intentionally the first observable operation.  No Base, Doc, or
        # Miaoda read/write is allowed under a different tenant or pseudonym key.
        self._assets.assert_binding(snapshot.tenant_id, self._secret)
        now = _aware(self._clock(), "clock")
        template = self._port.fetch_document_xml(self._assets.template_document_token)
        validate_growth_template_xml(template)
        report = SyncReport(
            learners=len(snapshot.learners),
            learning_records=sum(len(item.learning.projections) for item in snapshot.learners),
            evidence_records=sum(len(item.evidence) for item in snapshot.learners),
        )
        miaoda = _MiaodaCache(students=[], daily_records=[], evidence=[])
        for learner in snapshot.learners:
            report = self._sync_learner(
                snapshot.tenant_id, learner, template, now, report, miaoda
            )
        sql_batches = build_miaoda_upsert_sql_batches(
            students=miaoda.students,
            daily_records=miaoda.daily_records,
            evidence=miaoda.evidence,
            assets=self._assets,
            synced_at=now,
        )
        affected = sum(
            self._port.execute_miaoda_sql(
                self._assets.miaoda_app_id,
                self._assets.miaoda_environment,
                sql,
            )
            for sql in sql_batches
        )
        report = _report(report, miaoda_rows_upserted=affected)
        return report

    def _sync_learner(
        self,
        tenant_id: str,
        bundle: LearnerSyncBundle,
        template_xml: str,
        now: datetime,
        report: SyncReport,
        miaoda: _MiaodaCache,
    ) -> SyncReport:
        profile = bundle.learning.profile
        if profile.tenant_id != tenant_id:
            raise ValueError("learner profile escaped tenant boundary")
        learner_key = stable_business_key(
            self._secret, "fsp", tenant_id, profile.learner_ref
        )
        student = self._port.find_exact_record(
            self._assets.student_table_id, "学生业务键", learner_key
        )
        created_student = student is None
        if student is None:
            pending_fields = student_base_fields(
                bundle.learning,
                tenant_id=tenant_id,
                learner_key=learner_key,
                class_key=stable_class_ref(self._secret, tenant_id),
                document_url=None,
                template_version=f"{TEMPLATE_VERSION}:document-pending",
                now=now,
            )
            student = self._port.upsert_record(self._assets.student_table_id, pending_fields)
            report = _report(report, base_records_created=1)
        else:
            _require_same(student.fields, "学生代号", profile.learner_ref)

        document, document_created = self._student_document(
            student,
            profile.learner_ref,
            learner_key,
            created_student,
            template_xml,
        )
        if document_created:
            report = _report(report, documents_created=1)
        final_student_fields = student_base_fields(
            bundle.learning,
            tenant_id=tenant_id,
            learner_key=learner_key,
            class_key=stable_class_ref(self._secret, tenant_id),
            document_url=document.url,
            template_version=TEMPLATE_VERSION,
            now=now,
        )
        student = self._port.upsert_record(
            self._assets.student_table_id,
            final_student_fields,
            record_id=student.record_id,
        )
        report = _report(report, base_records_updated=1)
        miaoda.students.append(_miaoda_student_fields(final_student_fields))

        evidence_by_job: dict[str, list[EvidenceLearningBundle]] = defaultdict(list)
        for linked in bundle.evidence:
            if linked.profile.learner_ref != profile.learner_ref:
                raise ValueError("evidence escaped learner boundary")
            evidence_by_job[linked.projection.job_id].append(linked)

        projections_by_day: dict[date, list[LearningProjectionAuthority]] = defaultdict(list)
        for projection in bundle.learning.projections:
            local_day = (
                _aware(projection.completed_at, "completed_at")
                .astimezone(_SHANGHAI)
                .date()
            )
            projections_by_day[local_day].append(projection)
        for local_day in sorted(projections_by_day):
            projections = tuple(
                sorted(
                    projections_by_day[local_day],
                    key=lambda item: (_aware(item.completed_at, "completed_at"), item.run_id),
                )
            )
            report = self._sync_learning_day(
                tenant_id,
                bundle.learning,
                local_day,
                projections,
                evidence_by_job,
                learner_key,
                document,
                now,
                report,
                miaoda,
            )
        return report

    def _student_document(
        self,
        record: BaseRecord,
        learner_ref: str,
        learner_key: str,
        created_student: bool,
        template_xml: str,
    ) -> tuple[DocumentRef, bool]:
        version = _cell_text(record.fields.get("template_version"))
        url = _cell_text(record.fields.get("成长档案"))
        if not created_student:
            if version == f"{TEMPLATE_VERSION}:document-pending":
                raise AmbiguousSyncState(
                    "student document creation is pending; inspect the prior run before retrying"
                )
            if version != TEMPLATE_VERSION:
                raise ValueError("existing growth document is not template_version v1")
            document = trusted_growth_document_ref(
                url,
                trusted_template_url=self._assets.template_document_url,
            )
            validate_growth_document_binding(
                self._port.fetch_document_xml(document.token),
                learner_ref=learner_ref,
                learner_key=learner_key,
                tenant_binding=self._assets.tenant_binding_fingerprint,
                document=document,
                pseudonym_secret=self._secret,
            )
            return document, False

        document = self._port.create_document(
            growth_document_from_template_xml(template_xml, learner_ref)
        )
        document = trusted_growth_document_ref(
            document.url,
            trusted_template_url=self._assets.template_document_url,
            expected_token=document.token,
        )
        self._port.append_document(
            document.token,
            growth_document_binding_xml(
                learner_ref=learner_ref,
                learner_key=learner_key,
                tenant_binding=self._assets.tenant_binding_fingerprint,
                document=document,
                pseudonym_secret=self._secret,
            ),
        )
        validate_growth_document_binding(
            self._port.fetch_document_xml(document.token),
            learner_ref=learner_ref,
            learner_key=learner_key,
            tenant_binding=self._assets.tenant_binding_fingerprint,
            document=document,
            pseudonym_secret=self._secret,
        )
        return document, True

    def _sync_learning_day(
        self,
        tenant_id: str,
        learning: LearnerLearningBundle,
        local_day: date,
        projections: Sequence[LearningProjectionAuthority],
        evidence_by_job: Mapping[str, Sequence[EvidenceLearningBundle]],
        learner_key: str,
        document: DocumentRef,
        now: datetime,
        report: SyncReport,
        miaoda: _MiaodaCache,
    ) -> SyncReport:
        if not projections:
            raise ValueError("daily learning synchronization requires at least one Run")
        day_key = stable_business_key(
            self._secret,
            "fgd",
            tenant_id,
            learning.profile.learner_ref,
            local_day.isoformat(),
        )
        prepared: list[
            tuple[
                LearningProjectionAuthority,
                dict[str, Any],
                BaseRecord | None,
                tuple[str, ...],
                tuple[str, ...],
            ]
        ] = []
        for projection in projections:
            daily_key = stable_business_key(
                self._secret, "flr", tenant_id, projection.run_id
            )
            evidence_urls: list[str] = []
            evidence_ids: list[str] = []
            for linked in evidence_by_job.get(projection.job_id, ()):
                payload = redact_evidence_for_feishu(
                    linked,
                    purpose="TEACHER_SUPPORT",
                    trace_id="feishu-base-sync",
                    observed_now=now,
                )
                evidence_id = linked.evidence.evidence_id
                evidence_ids.append(evidence_id)
                evidence_key = stable_business_key(
                    self._secret, "fev", tenant_id, evidence_id
                )
                url = self._evidence_url(learner_key, evidence_key)
                evidence_urls.append(url)
                report = self._sync_evidence_record(
                    tenant_id,
                    learner_key,
                    daily_key,
                    evidence_key,
                    linked,
                    payload,
                    url,
                    document.url,
                    report,
                    miaoda,
                )
            fields = daily_base_fields(
                learning,
                projection,
                learner_key=learner_key,
                class_key=stable_class_ref(self._secret, tenant_id),
                daily_key=daily_key,
                day_key=day_key,
                evidence_ids=evidence_ids,
                document_url=document.url,
                dashboard_url=self._assets.dashboard_url,
                append_status=PENDING_APPEND,
                now=now,
            )
            existing = self._port.find_exact_record(
                self._assets.daily_table_id, "学习记录业务键", daily_key
            )
            if existing is not None:
                _require_same(existing.fields, "学生业务键", learner_key)
                _require_same(existing.fields, "Run ID", projection.run_id)
                _require_same(existing.fields, "档案追加键", day_key)
                status = _cell_text(existing.fields.get("档案追加状态"))
                if status not in {PENDING_APPEND, APPENDED}:
                    raise ValueError("growth document append status is invalid")
            prepared.append(
                (
                    projection,
                    fields,
                    existing,
                    tuple(evidence_ids),
                    tuple(evidence_urls),
                )
            )

        changed = any(
            existing is None or not _same_daily_growth_facts(existing.fields, fields)
            for _, fields, existing, _, _ in prepared
        )
        pending = any(
            existing is not None
            and _cell_text(existing.fields.get("档案追加状态")) == PENDING_APPEND
            for _, _, existing, _, _ in prepared
        )
        if not changed and not pending:
            for _, fields, existing, prepared_evidence_ids, _ in prepared:
                if existing is None:  # pragma: no cover - implied by changed=False
                    raise AssertionError("unchanged daily projection cannot be absent")
                fields["档案追加状态"] = APPENDED
                miaoda.daily_records.append(
                    _miaoda_daily_fields(fields, evidence_ids=prepared_evidence_ids)
                )
            return report

        document_xml = self._port.fetch_document_xml_with_ids(document.token)
        validate_growth_document_binding(
            document_xml,
            learner_ref=learning.profile.learner_ref,
            learner_key=learner_key,
            tenant_binding=self._assets.tenant_binding_fingerprint,
            document=document,
            pseudonym_secret=self._secret,
        )
        existing_table_id = growth_day_table_block_id(document_xml, local_day)
        if pending and existing_table_id is None:
            raise AmbiguousSyncState(
                "growth document append is pending and no dated block is visible; inspect before retrying"
            )

        for _, fields, existing, _, _ in prepared:
            fields["档案追加状态"] = PENDING_APPEND
            if existing is None:
                created = self._port.upsert_record(self._assets.daily_table_id, fields)
                report = _report(report, base_records_created=1)
                index = next(
                    position
                    for position, item in enumerate(prepared)
                    if item[1] is fields
                )
                (
                    prepared_projection,
                    _,
                    _,
                    prepared_evidence_ids,
                    prepared_evidence_urls,
                ) = prepared[index]
                prepared[index] = (
                    prepared_projection,
                    fields,
                    created,
                    prepared_evidence_ids,
                    prepared_evidence_urls,
                )
            elif _cell_text(existing.fields.get("档案追加状态")) != PENDING_APPEND:
                self._port.upsert_record(
                    self._assets.daily_table_id, fields, record_id=existing.record_id
                )
                report = _report(report, base_records_updated=1)

        facts = tuple(
            projection_facts_for_feishu(
                learning.profile,
                projection,
                learning.projections,
                observed_now=now,
            )
            for projection in projections
        )
        previous_stages = tuple(
            previous_knowledge_stage_for_feishu(projection, learning.projections)
            for projection in projections
        )
        evidence_urls_by_run = tuple(item[4] for item in prepared)
        block_xml = growth_daily_group_block_xml(
            learner_ref=learning.profile.learner_ref,
            projections=projections,
            facts=facts,
            evidence_urls_by_run=evidence_urls_by_run,
            stage_before_by_run=previous_stages,
        )
        if existing_table_id is None:
            self._port.append_document(document.token, block_xml)
            report = _report(report, document_blocks_appended=1)
        else:
            self._port.replace_document_block(
                document.token,
                existing_table_id,
                growth_daily_table_xml(
                    learner_ref=learning.profile.learner_ref,
                    projections=projections,
                    facts=facts,
                    evidence_urls_by_run=evidence_urls_by_run,
                    stage_before_by_run=previous_stages,
                ),
            )

        for _, fields, record, prepared_evidence_ids, _ in prepared:
            if record is None:  # pragma: no cover - created above
                raise AssertionError("daily projection record was not staged")
            fields["档案追加状态"] = APPENDED
            self._port.upsert_record(
                self._assets.daily_table_id, fields, record_id=record.record_id
            )
            miaoda.daily_records.append(
                _miaoda_daily_fields(fields, evidence_ids=prepared_evidence_ids)
            )
            report = _report(report, base_records_updated=1)
        return report

    def _sync_evidence_record(
        self,
        tenant_id: str,
        learner_key: str,
        daily_key: str,
        evidence_key: str,
        linked: EvidenceLearningBundle,
        payload: Mapping[str, Any],
        evidence_url: str | None,
        document_url: str,
        report: SyncReport,
        miaoda: _MiaodaCache,
    ) -> SyncReport:
        expected_key = stable_business_key(
            self._secret, "fev", tenant_id, linked.evidence.evidence_id
        )
        if not hmac.compare_digest(evidence_key, expected_key):
            raise ValueError("Evidence business key drifted during synchronization")
        fields = evidence_base_fields(
            payload,
            evidence_key=evidence_key,
            learner_key=learner_key,
            daily_key=daily_key,
            evidence_url=evidence_url,
            document_url=document_url,
            dashboard_url=self._assets.dashboard_url,
        )
        existing = self._port.find_exact_record(
            self._assets.evidence_table_id, "Evidence业务键", evidence_key
        )
        if existing is None:
            self._port.upsert_record(self._assets.evidence_table_id, fields)
            miaoda.evidence.append(_miaoda_evidence_fields(fields))
            return _report(report, base_records_created=1)
        _require_same(existing.fields, "学生业务键", learner_key)
        self._port.upsert_record(
            self._assets.evidence_table_id, fields, record_id=existing.record_id
        )
        miaoda.evidence.append(_miaoda_evidence_fields(fields))
        return _report(report, base_records_updated=1)

    def _evidence_url(self, learner_key: str, evidence_key: str) -> str:
        """Link teachers to the authenticated, redacted Evidence card in Miaoda."""
        learner_path = quote(learner_key, safe="")
        evidence_anchor = quote(evidence_key, safe="")
        return (
            f"{self._assets.miaoda_online_url.rstrip('/')}/students/{learner_path}"
            f"#evidence-{evidence_anchor}"
        )


def pseudonym_secret_fingerprint(secret: str) -> str:
    """Return a non-secret fingerprint used to reject pseudonym-key drift."""
    _validate_pseudonym_secret(secret)
    digest = hashlib.sha256(_SECRET_FINGERPRINT_DOMAIN + secret.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def tenant_binding_fingerprint(secret: str, tenant_id: str) -> str:
    """Irreversibly bind this one-class asset set to one tenant."""
    _validate_pseudonym_secret(secret)
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise ValueError("tenant id is required for Feishu asset binding")
    message = _TENANT_BINDING_DOMAIN + tenant_id.encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return f"hmac-sha256:{digest}"


def stable_business_key(secret: str, prefix: str, *parts: str) -> str:
    """Return an opaque key over the exact tenant-scoped business identity."""
    if prefix not in {"fsp", "flr", "fev", "fgd"}:
        raise ValueError("unsupported Feishu business key prefix")
    _validate_pseudonym_secret(secret)
    if not parts or any(not part for part in parts):
        raise ValueError("invalid Feishu business key input")
    message = json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode()
    digest = hmac.new(secret.encode(), message, hashlib.sha256).digest()
    encoded = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return f"{prefix}_{encoded}"


def snapshot_report(snapshot: TenantLearningSnapshot) -> SyncReport:
    """Return a non-identifying dry-run summary without touching Feishu."""
    return SyncReport(
        learners=len(snapshot.learners),
        learning_records=sum(len(item.learning.projections) for item in snapshot.learners),
        evidence_records=sum(len(item.evidence) for item in snapshot.learners),
    )


def student_base_fields(
    learning: LearnerLearningBundle,
    *,
    tenant_id: str,
    learner_key: str,
    class_key: str,
    document_url: str | None,
    template_version: str,
    now: datetime,
) -> dict[str, Any]:
    if learning.profile.tenant_id != tenant_id:
        raise ValueError("student Base projection escaped tenant boundary")
    latest = learning.projections[-1] if learning.projections else None
    facts = (
        projection_facts_for_feishu(
            learning.profile, latest, learning.projections, observed_now=now
        )
        if latest is not None
        else None
    )
    current_concept, current_stage, profile_assistance = _current_competency(
        learning, now
    )
    patch_count = sum(
        projection_facts_for_feishu(
            learning.profile, item, learning.projections, observed_now=now
        )["skill_patch_used"]
        is True
        for item in learning.projections
    )
    recent = latest.completed_at if latest is not None else learning.profile.updated_at
    local_now = _aware(now, "now").astimezone(_SHANGHAI)
    local_recent = _aware(recent, "recent").astimezone(_SHANGHAI)
    support = _support_reasons(learning, now)
    fields: dict[str, Any] = {
        "学生业务键": learner_key,
        "学生代号": learning.profile.learner_ref,
        "班级业务键": class_key,
        "当前知识点": current_concept,
        "当前知识点阶段": current_stage,
        "AI辅助程度": max(
            profile_assistance,
            int(facts["ai_assistance_level"]) if facts else 0,
        ),
        "AI辅助说明": "Skill Patch辅助" if patch_count else "未使用",
        "Skill Patch累计次数": patch_count,
        "最近活跃时间": _base_time(recent),
        "今日活跃": local_recent.date() == local_now.date() and latest is not None,
        "需要关注": bool(support),
        "关注原因": "；".join(support) if support else MISSING,
        "template_version": template_version,
        "数据时间": _base_time(now),
    }
    if document_url is not None:
        fields["成长档案"] = document_url
    return fields


def daily_base_fields(
    learning: LearnerLearningBundle,
    projection: LearningProjectionAuthority,
    *,
    learner_key: str,
    class_key: str,
    daily_key: str,
    day_key: str,
    evidence_ids: Sequence[str],
    document_url: str,
    dashboard_url: str,
    append_status: str,
    now: datetime,
) -> dict[str, Any]:
    facts = projection_facts_for_feishu(
        learning.profile, projection, learning.projections, observed_now=now
    )
    local_now = _aware(now, "now").astimezone(_SHANGHAI)
    local_completed = _aware(projection.completed_at, "completed_at").astimezone(_SHANGHAI)
    first_recent_day = local_now.date() - timedelta(days=6)
    completed = facts["task_result"] == "COMPLETED"
    task_name = _projection_task_name(projection)
    progress, suggestion = _progress_and_suggestion(facts)
    previous_stage = previous_knowledge_stage_for_feishu(
        projection, learning.projections
    )
    return {
        "学习记录业务键": daily_key,
        "学生业务键": learner_key,
        "班级业务键": class_key,
        "日期": _base_time(projection.completed_at),
        "是否今日": local_completed.date() == local_now.date(),
        "是否近7天": first_recent_day <= local_completed.date() <= local_now.date(),
        "任务ID": str(facts["task_ref"]),
        "任务名称": task_name,
        "完成结果": "已完成" if completed else "未完成",
        "完成值": 1 if completed else 0,
        "尝试次数": int(facts["attempt_count"]),
        "主要错误": _main_error_zh(facts["main_error"]),
        "AI辅助程度": int(facts["ai_assistance_level"]),
        "AI辅助说明": "Skill Patch辅助" if facts["skill_patch_used"] else "未使用",
        "Skill Patch使用": bool(facts["skill_patch_used"]),
        "知识点": _projection_concept(projection),
        "阶段前": _stage_zh(previous_stage) if previous_stage is not None else MISSING,
        "阶段后": _stage_zh(facts["knowledge_stage"]),
        "今日进步": progress,
        "下一步建议": suggestion,
        "Run ID": projection.run_id,
        "Evidence引用": "，".join(evidence_ids) if evidence_ids else MISSING,
        "成长档案": document_url,
        "Dashboard链接": dashboard_url,
        "档案追加状态": append_status,
        "档案追加键": day_key,
        "数据时间": _base_time(now),
    }


def evidence_base_fields(
    payload: Mapping[str, Any],
    *,
    evidence_key: str,
    learner_key: str,
    daily_key: str,
    evidence_url: str | None,
    document_url: str,
    dashboard_url: str,
) -> dict[str, Any]:
    facts_raw = payload.get("facts")
    if not isinstance(facts_raw, list):
        raise ValueError("redacted evidence facts are missing")
    facts = {
        item["name"]: item["value"]
        for item in facts_raw
        if isinstance(item, Mapping) and isinstance(item.get("name"), str)
    }
    fields: dict[str, Any] = {
        "Evidence业务键": evidence_key,
        "学生业务键": learner_key,
        "学习记录业务键": daily_key,
        "Evidence类型": _display(facts.get("evidence_kind")),
        "脱敏摘要": _display(payload.get("summary")),
        "客观事实": json.dumps(facts, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        "Run ID": _display(facts.get("run_ref")),
        "成长档案": document_url,
        "Dashboard链接": dashboard_url,
        "脱敏版本": "FEISHU_EVIDENCE_V1",
        "数据时间": _base_time(_parse_time(payload.get("observed_at"), "observed_at")),
    }
    if evidence_url is not None:
        fields["Evidence链接"] = evidence_url
    return fields


def _build_miaoda_upsert_sql(
    *,
    students: Sequence[Mapping[str, Any]],
    daily_records: Sequence[Mapping[str, Any]],
    evidence: Sequence[Mapping[str, Any]],
    assets: FeishuAssets,
    synced_at: datetime,
) -> str:
    """Build one deterministic, retry-safe transaction for the Miaoda read cache."""
    observed_at = _aware(synced_at, "synced_at")
    statements = ["BEGIN;"]
    table_specs = (
        (
            "student_profile",
            "learner_key",
            (
                "learner_key",
                "learner_alias",
                "class_key",
                "current_concept",
                "mastery_stage",
                "ai_assistance_level",
                "ai_assistance_label",
                "skill_patch_count",
                "last_active_at",
                "active_today",
                "needs_attention",
                "attention_reason",
                "growth_document_url",
                "template_version",
                "data_time",
            ),
            students,
        ),
        (
            "daily_learning_record",
            "learning_key",
            (
                "learning_key",
                "learner_key",
                "class_key",
                "learning_date",
                "is_today",
                "is_last_7_days",
                "task_id",
                "task_name",
                "completion_result",
                "completion_value",
                "attempt_count",
                "main_error",
                "ai_assistance_level",
                "ai_assistance_label",
                "used_skill_patch",
                "knowledge_point",
                "stage_before",
                "stage_after",
                "daily_progress",
                "next_suggestion",
                "run_id",
                "evidence_refs",
                "growth_document_url",
                "dashboard_url",
                "document_append_status",
                "document_append_key",
                "data_time",
            ),
            daily_records,
        ),
        (
            "evidence_summary",
            "evidence_key",
            (
                "evidence_key",
                "learner_key",
                "learning_key",
                "evidence_type",
                "redacted_summary",
                "objective_facts",
                "run_id",
                "evidence_url",
                "growth_document_url",
                "dashboard_url",
                "redaction_version",
                "data_time",
            ),
            evidence,
        ),
    )
    for table, key, columns, rows in table_specs:
        statement = _miaoda_upsert_statement(table, key, columns, rows)
        if statement is not None:
            statements.append(statement)
    sync_value = observed_at.isoformat().replace("+00:00", "Z")
    config = (
        {"config_key": "base_url", "config_value": assets.base_url, "data_time": observed_at},
        {
            "config_key": "dashboard_url",
            "config_value": assets.dashboard_url,
            "data_time": observed_at,
        },
        {
            "config_key": "template_url",
            "config_value": assets.template_document_url,
            "data_time": observed_at,
        },
        {"config_key": "last_synced_at", "config_value": sync_value, "data_time": observed_at},
    )
    config_statement = _miaoda_upsert_statement(
        "learning_center_config",
        "config_key",
        ("config_key", "config_value", "data_time"),
        config,
    )
    if config_statement is None:  # pragma: no cover - fixed non-empty config contract
        raise AssertionError("Miaoda config projection cannot be empty")
    statements.extend((config_statement, "COMMIT;"))
    return "\n".join(statements)


def build_miaoda_upsert_sql(
    *,
    students: Sequence[Mapping[str, Any]],
    daily_records: Sequence[Mapping[str, Any]],
    evidence: Sequence[Mapping[str, Any]],
    assets: FeishuAssets,
    synced_at: datetime,
) -> str:
    """Build a single batch and fail before spawning an unsafe Windows argv."""
    sql = _build_miaoda_upsert_sql(
        students=students,
        daily_records=daily_records,
        evidence=evidence,
        assets=assets,
        synced_at=synced_at,
    )
    if len(sql) > MAX_MIAODA_SQL_CHARS:
        raise ValueError("Miaoda SQL exceeds the Windows-safe command limit")
    return sql


def build_miaoda_upsert_sql_batches(
    *,
    students: Sequence[Mapping[str, Any]],
    daily_records: Sequence[Mapping[str, Any]],
    evidence: Sequence[Mapping[str, Any]],
    assets: FeishuAssets,
    synced_at: datetime,
    max_chars: int = MAX_MIAODA_SQL_CHARS,
) -> tuple[str, ...]:
    """Greedily split retry-safe upserts into deterministic Windows-safe batches."""
    if not 1_000 <= max_chars <= MAX_MIAODA_SQL_CHARS:
        raise ValueError("Miaoda SQL batch limit is outside the safe range")
    items = tuple(("student", row) for row in students) + tuple(
        ("daily", row) for row in daily_records
    ) + tuple(("evidence", row) for row in evidence)
    current: dict[str, list[Mapping[str, Any]]] = {
        "student": [],
        "daily": [],
        "evidence": [],
    }
    batches: list[str] = []

    def render() -> str:
        return _build_miaoda_upsert_sql(
            students=current["student"],
            daily_records=current["daily"],
            evidence=current["evidence"],
            assets=assets,
            synced_at=synced_at,
        )

    for kind, row in items:
        current[kind].append(row)
        candidate = render()
        if len(candidate) <= max_chars:
            continue
        current[kind].pop()
        if not any(current.values()):
            raise ValueError("one Miaoda projection row exceeds the Windows-safe command limit")
        batches.append(render())
        current = {"student": [], "daily": [], "evidence": []}
        current[kind].append(row)
        if len(render()) > max_chars:
            raise ValueError("one Miaoda projection row exceeds the Windows-safe command limit")
    batches.append(render())
    if any(len(sql) > max_chars for sql in batches):  # pragma: no cover - defensive
        raise AssertionError("Miaoda SQL batching exceeded its limit")
    return tuple(batches)


def miaoda_sql_text(value: str | None) -> str:
    """Quote one PostgreSQL text literal without allowing statement breakout."""
    if value is None:
        return "NULL"
    if "\x00" in value:
        raise ValueError("Miaoda text value contains a NUL character")
    return "'" + value.replace("'", "''") + "'"


def _miaoda_student_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "learner_key": _miaoda_required_text(fields.get("学生业务键"), "学生业务键"),
        "learner_alias": _miaoda_required_text(fields.get("学生代号"), "学生代号"),
        "class_key": _miaoda_required_text(fields.get("班级业务键"), "班级业务键"),
        "current_concept": _miaoda_optional_text(fields.get("当前知识点")),
        "mastery_stage": _miaoda_optional_text(fields.get("当前知识点阶段")),
        "ai_assistance_level": _miaoda_integer(fields.get("AI辅助程度"), "AI辅助程度"),
        "ai_assistance_label": _miaoda_required_text(fields.get("AI辅助说明"), "AI辅助说明"),
        "skill_patch_count": _miaoda_integer(
            fields.get("Skill Patch累计次数"), "Skill Patch累计次数"
        ),
        "last_active_at": _miaoda_base_timestamp(fields.get("最近活跃时间"), "最近活跃时间"),
        "active_today": _miaoda_boolean(fields.get("今日活跃"), "今日活跃"),
        "needs_attention": _miaoda_boolean(fields.get("需要关注"), "需要关注"),
        "attention_reason": _miaoda_optional_text(fields.get("关注原因")),
        "growth_document_url": _miaoda_optional_text(fields.get("成长档案")),
        "template_version": _miaoda_required_text(
            fields.get("template_version"), "template_version"
        ),
        "data_time": _miaoda_base_timestamp(fields.get("数据时间"), "数据时间"),
    }


def _miaoda_daily_fields(
    fields: Mapping[str, Any], *, evidence_ids: Sequence[str]
) -> dict[str, Any]:
    completed_at = _miaoda_base_timestamp(fields.get("日期"), "日期")
    status = _miaoda_required_text(fields.get("档案追加状态"), "档案追加状态")
    if status not in {PENDING_APPEND, APPENDED}:
        raise ValueError("档案追加状态 is invalid")
    return {
        "learning_key": _miaoda_required_text(fields.get("学习记录业务键"), "学习记录业务键"),
        "learner_key": _miaoda_required_text(fields.get("学生业务键"), "学生业务键"),
        "class_key": _miaoda_required_text(fields.get("班级业务键"), "班级业务键"),
        "learning_date": completed_at.date(),
        "is_today": _miaoda_boolean(fields.get("是否今日"), "是否今日"),
        "is_last_7_days": _miaoda_boolean(fields.get("是否近7天"), "是否近7天"),
        "task_id": _miaoda_optional_text(fields.get("任务ID")),
        "task_name": _miaoda_optional_text(fields.get("任务名称")),
        "completion_result": _miaoda_required_text(fields.get("完成结果"), "完成结果"),
        "completion_value": _miaoda_integer(fields.get("完成值"), "完成值"),
        "attempt_count": _miaoda_integer(fields.get("尝试次数"), "尝试次数"),
        "main_error": _miaoda_optional_text(fields.get("主要错误")),
        "ai_assistance_level": _miaoda_integer(fields.get("AI辅助程度"), "AI辅助程度"),
        "ai_assistance_label": _miaoda_required_text(fields.get("AI辅助说明"), "AI辅助说明"),
        "used_skill_patch": _miaoda_boolean(fields.get("Skill Patch使用"), "Skill Patch使用"),
        "knowledge_point": _miaoda_optional_text(fields.get("知识点")),
        "stage_before": _miaoda_optional_text(fields.get("阶段前")),
        "stage_after": _miaoda_optional_text(fields.get("阶段后")),
        "daily_progress": _miaoda_optional_text(fields.get("今日进步")),
        "next_suggestion": _miaoda_optional_text(fields.get("下一步建议")),
        "run_id": _miaoda_required_text(fields.get("Run ID"), "Run ID"),
        "evidence_refs": json.dumps(list(evidence_ids), ensure_ascii=False, separators=(",", ":")),
        "growth_document_url": _miaoda_optional_text(fields.get("成长档案")),
        "dashboard_url": _miaoda_optional_text(fields.get("Dashboard链接")),
        "document_append_status": "appended" if status == APPENDED else "pending",
        "document_append_key": _miaoda_optional_text(fields.get("档案追加键")),
        "data_time": _miaoda_base_timestamp(fields.get("数据时间"), "数据时间"),
    }


def _miaoda_evidence_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "evidence_key": _miaoda_required_text(fields.get("Evidence业务键"), "Evidence业务键"),
        "learner_key": _miaoda_required_text(fields.get("学生业务键"), "学生业务键"),
        "learning_key": _miaoda_required_text(
            fields.get("学习记录业务键"), "学习记录业务键"
        ),
        "evidence_type": _miaoda_required_text(fields.get("Evidence类型"), "Evidence类型"),
        "redacted_summary": _miaoda_required_text(fields.get("脱敏摘要"), "脱敏摘要"),
        "objective_facts": _miaoda_required_text(fields.get("客观事实"), "客观事实"),
        "run_id": _miaoda_required_text(fields.get("Run ID"), "Run ID"),
        "evidence_url": _miaoda_optional_text(fields.get("Evidence链接")),
        "growth_document_url": _miaoda_optional_text(fields.get("成长档案")),
        "dashboard_url": _miaoda_optional_text(fields.get("Dashboard链接")),
        "redaction_version": "v1",
        "data_time": _miaoda_base_timestamp(fields.get("数据时间"), "数据时间"),
    }


def _miaoda_upsert_statement(
    table: str,
    key: str,
    columns: Sequence[str],
    rows: Sequence[Mapping[str, Any]],
) -> str | None:
    if not rows:
        return None
    expected = set(columns)
    by_key: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if set(row) != expected:
            raise ValueError(f"Miaoda {table} projection columns are not whitelisted")
        business_key = row.get(key)
        if not isinstance(business_key, str) or not business_key:
            raise ValueError(f"Miaoda {table} projection lacks its stable key")
        by_key[business_key] = row
    ordered = [by_key[business_key] for business_key in sorted(by_key)]
    values = ",\n".join(
        "(" + ", ".join(_miaoda_sql_value(row[column]) for column in columns) + ")"
        for row in ordered
    )
    assignments = ",\n  ".join(
        f"{column} = EXCLUDED.{column}" for column in columns if column != key
    )
    return (
        f"INSERT INTO {table} ({', '.join(columns)})\n"
        f"VALUES\n{values}\n"
        f"ON CONFLICT ({key}) DO UPDATE SET\n  {assignments},\n"
        "  _updated_at = CURRENT_TIMESTAMP;"
    )


def _miaoda_sql_value(value: Any) -> str:
    if value is None or isinstance(value, str):
        return miaoda_sql_text(value)
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, datetime):
        timestamp = _aware(value, "Miaoda timestamp").isoformat()
        return f"{miaoda_sql_text(timestamp)}::timestamptz"
    if isinstance(value, date):
        return f"{miaoda_sql_text(value.isoformat())}::date"
    raise ValueError("Miaoda projection contains an unsupported value type")


def _miaoda_required_text(value: Any, field: str) -> str:
    text = _cell_text(value).strip()
    if not text:
        raise ValueError(f"{field} is required for the Miaoda projection")
    return text


def _miaoda_optional_text(value: Any) -> str | None:
    text = _cell_text(value).strip()
    return None if not text or text == MISSING else text


def _miaoda_integer(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{field} must be an integer for the Miaoda projection")
    return value


def _miaoda_boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean for the Miaoda projection")
    return value


def _miaoda_base_timestamp(value: Any, field: str) -> datetime:
    text = _miaoda_required_text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field} is not a timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=_SHANGHAI)
    return parsed


def validate_growth_template_xml(content_xml: str) -> None:
    if "template_version：</b>v1" not in content_xml:
        raise ValueError("growth document mother template is not v1")
    positions = []
    for column in _GROWTH_COLUMNS:
        marker = f"<b>{column}</b>"
        position = content_xml.find(marker)
        if position < 0:
            raise ValueError(f"growth document mother template lacks {column}")
        positions.append(position)
    if positions != sorted(positions) or len(set(positions)) != len(positions):
        raise ValueError("growth document mother template columns are out of order")


def growth_document_header_xml(learner_ref: str) -> str:
    learner = escape(_display(learner_ref))
    return (
        "<title>儿童学习成长档案 v1</title>"
        f"<p><b>学生档案代号：</b>{learner}</p>"
    )


def growth_document_from_template_xml(template_xml: str, learner_ref: str) -> str:
    """Create child XML from the verified mother template, stripping foreign block IDs."""
    validate_growth_template_xml(template_xml)
    without_ids = re.sub(r'\s+id="[^"]+"', "", template_xml)
    child, count = re.subn(
        r"<title(?:\s[^>]*)?>.*?</title>",
        growth_document_header_xml(learner_ref),
        without_ids,
        count=1,
        flags=re.DOTALL,
    )
    if count != 1:
        raise ValueError("growth document mother template title is missing")
    return child


def growth_document_binding_xml(
    *,
    learner_ref: str,
    learner_key: str,
    tenant_binding: str,
    document: DocumentRef,
    pseudonym_secret: str,
) -> str:
    """Render the one immutable, anonymous ownership seal for a child document."""
    _validate_pseudonym_secret(pseudonym_secret)
    core = {
        "document_token": document.token,
        "document_url": document.url,
        "learner_key": learner_key,
        "learner_ref": learner_ref,
        "schema": _GROWTH_BINDING_SCHEMA,
        "template_version": TEMPLATE_VERSION,
        "tenant_binding": tenant_binding,
    }
    payload = {
        **core,
        "binding_mac": _growth_document_binding_mac(pseudonym_secret, core),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"<p><b>{_GROWTH_BINDING_MARKER}</b>{escape(encoded)}</p>"


def validate_growth_document_binding(
    content_xml: str,
    *,
    learner_ref: str,
    learner_key: str,
    tenant_binding: str,
    document: DocumentRef,
    pseudonym_secret: str,
) -> None:
    """Fail closed unless one child document carries its exact ownership seal."""
    matches: list[str] = []
    for paragraph in re.finditer(r"<p\b[^>]*>(.*?)</p>", content_xml, re.DOTALL):
        visible = unescape(re.sub(r"<[^>]+>", "", paragraph.group(1))).strip()
        if _GROWTH_BINDING_MARKER in visible:
            matches.append(visible)
    if len(matches) != 1 or not matches[0].startswith(_GROWTH_BINDING_MARKER):
        raise ValueError("growth document ownership binding is missing or ambiguous")
    encoded = matches[0][len(_GROWTH_BINDING_MARKER) :]
    try:
        payload = json.loads(encoded)
    except json.JSONDecodeError as error:
        raise ValueError("growth document ownership binding is malformed") from error
    _validate_pseudonym_secret(pseudonym_secret)
    core = {
        "document_token": document.token,
        "document_url": document.url,
        "learner_key": learner_key,
        "learner_ref": learner_ref,
        "schema": _GROWTH_BINDING_SCHEMA,
        "template_version": TEMPLATE_VERSION,
        "tenant_binding": tenant_binding,
    }
    expected = {
        **core,
        "binding_mac": _growth_document_binding_mac(pseudonym_secret, core),
    }
    if not isinstance(payload, Mapping) or set(payload) != set(expected):
        raise ValueError("growth document ownership binding shape drifted")
    if any(
        not isinstance(payload.get(field), str)
        or not hmac.compare_digest(payload[field], value)
        for field, value in expected.items()
    ):
        raise ValueError("growth document ownership binding does not match student")


def _growth_document_binding_mac(
    pseudonym_secret: str, core: Mapping[str, str]
) -> str:
    message = json.dumps(
        core,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    digest = hmac.new(
        pseudonym_secret.encode("utf-8"),
        _GROWTH_BINDING_DOMAIN + message,
        hashlib.sha256,
    ).hexdigest()
    return f"hmac-sha256:{digest}"


def growth_daily_block_xml(
    *,
    learner_ref: str,
    projection: LearningProjectionAuthority,
    facts: Mapping[str, str | int | bool],
    evidence_url: str | None,
) -> str:
    """Compatibility wrapper for a one-Run day."""
    return growth_daily_group_block_xml(
        learner_ref=learner_ref,
        projections=(projection,),
        facts=(facts,),
        evidence_urls_by_run=((evidence_url,) if evidence_url is not None else (),),
    )


def growth_daily_group_block_xml(
    *,
    learner_ref: str,
    projections: Sequence[LearningProjectionAuthority],
    facts: Sequence[Mapping[str, str | int | bool]],
    evidence_urls_by_run: Sequence[Sequence[str]],
    stage_before_by_run: Sequence[str | None] | None = None,
) -> str:
    if not projections:
        raise ValueError("growth day requires at least one Run")
    local_day = _growth_group_day(projections)
    return (
        f"<hr/><h1>每日成长记录｜{local_day.isoformat()}</h1>"
        + growth_daily_table_xml(
            learner_ref=learner_ref,
            projections=projections,
            facts=facts,
            evidence_urls_by_run=evidence_urls_by_run,
            stage_before_by_run=stage_before_by_run,
        )
    )


def growth_daily_table_xml(
    *,
    learner_ref: str,
    projections: Sequence[LearningProjectionAuthority],
    facts: Sequence[Mapping[str, str | int | bool]],
    evidence_urls_by_run: Sequence[Sequence[str]],
    stage_before_by_run: Sequence[str | None] | None = None,
) -> str:
    if not projections or not (
        len(projections) == len(facts) == len(evidence_urls_by_run)
    ):
        raise ValueError("growth day Run/fact/Evidence groups must align")
    previous_stages = (
        tuple(stage_before_by_run)
        if stage_before_by_run is not None
        else (None,) * len(projections)
    )
    if len(previous_stages) != len(projections):
        raise ValueError("growth day prior stages do not align")
    local_day = _growth_group_day(projections)
    progress_and_suggestions = tuple(_progress_and_suggestion(item) for item in facts)
    values = (
        f"学生代号：{_display(learner_ref)}；template_version={TEMPLATE_VERSION}",
        local_day.isoformat(),
        _growth_run_list(
            projections,
            tuple(
                _projection_task_name(projection, fallback=str(item["task_ref"]))
                for projection, item in zip(projections, facts, strict=True)
            ),
        ),
        _growth_run_list(
            projections,
            tuple(
                "已完成" if item["task_result"] == "COMPLETED" else "未完成"
                for item in facts
            ),
        ),
        _growth_run_list(
            projections, tuple(str(item["attempt_count"]) for item in facts)
        ),
        _growth_run_list(
            projections, tuple(_main_error_zh(item["main_error"]) for item in facts)
        ),
        _growth_run_list(
            projections,
            tuple(
                f'{item["ai_assistance_level"]}（'
                f'{"Skill Patch辅助" if item["skill_patch_used"] else "未使用"}）'
                for item in facts
            ),
        ),
        _growth_run_list(
            projections,
            tuple(
                f"{_stage_zh(previous) if previous is not None else MISSING} → "
                f"{_stage_zh(item['knowledge_stage'])}"
                for previous, item in zip(previous_stages, facts, strict=True)
            ),
        ),
        _growth_run_list(
            projections, tuple(item[0] for item in progress_and_suggestions)
        ),
        _growth_run_list(
            projections, tuple(item[1] for item in progress_and_suggestions)
        ),
        _growth_evidence_cell(projections, evidence_urls_by_run),
    )
    rows = []
    for column, value in zip(_GROWTH_COLUMNS, values, strict=True):
        rendered = value if column == "Evidence链接" else escape(value)
        rows.append(f"<tr><td><b>{column}</b></td><td>{rendered}</td></tr>")
    return (
        "<table>"
        '<colgroup><col width="180"/><col width="520"/></colgroup>'
        '<thead><tr><th background-color="light-gray">栏目</th>'
        '<th background-color="light-gray">内容</th></tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def growth_day_table_block_id(content_xml: str, local_day: date) -> str | None:
    """Locate the one dated v1 table without exposing tenant or learner identity."""
    matches: list[str] = []
    for table in re.finditer(
        r"<table\b(?P<attrs>[^>]*)>(?P<body>.*?)</table>", content_xml, re.DOTALL
    ):
        body = unescape(re.sub(r"<[^>]+>", "", table.group("body")))
        if local_day.isoformat() not in body or any(column not in body for column in _GROWTH_COLUMNS):
            continue
        identifier = re.search(r'\bid="([A-Za-z0-9_-]+)"', table.group("attrs"))
        if identifier is None:
            raise ValueError("dated growth table has no stable Feishu block id")
        matches.append(identifier.group(1))
    if len(matches) > 1:
        raise ValueError("growth document contains duplicate daily blocks")
    return matches[0] if matches else None


def _growth_group_day(projections: Sequence[LearningProjectionAuthority]) -> date:
    days = {
        _aware(item.completed_at, "completed_at").astimezone(_SHANGHAI).date()
        for item in projections
    }
    if len(days) != 1:
        raise ValueError("growth day contains Runs from multiple local dates")
    return next(iter(days))


def _growth_run_list(
    projections: Sequence[LearningProjectionAuthority], values: Sequence[str]
) -> str:
    if len(projections) != len(values):
        raise ValueError("growth Run values do not align")
    if len(values) == 1:
        return values[0]
    return "；".join(
        f"{projection.run_id}：{value}"
        for projection, value in zip(projections, values, strict=True)
    )


def _growth_evidence_cell(
    projections: Sequence[LearningProjectionAuthority],
    evidence_urls_by_run: Sequence[Sequence[str]],
) -> str:
    links: list[str] = []
    multiple_runs = len(projections) > 1
    for projection, urls in zip(projections, evidence_urls_by_run, strict=True):
        for index, url in enumerate(urls, start=1):
            label = "查看脱敏 Evidence"
            if multiple_runs or len(urls) > 1:
                label = f"{projection.run_id} Evidence {index}"
            links.append(
                f'<a href="{escape(url, quote=True)}">{escape(label)}</a>'
            )
    return "<br/>".join(links) if links else MISSING


def _support_reasons(learning: LearnerLearningBundle, now: datetime) -> list[str]:
    failures: dict[str, int] = defaultdict(int)
    used_patch = False
    for item in learning.projections:
        facts = projection_facts_for_feishu(
            learning.profile, item, learning.projections, observed_now=now
        )
        if facts["task_result"] != "COMPLETED":
            failures[str(facts["task_ref"])] += 1
        used_patch = used_patch or bool(facts["skill_patch_used"])
    reasons = [f"任务 {task} 连续尝试尚未完成" for task, count in failures.items() if count >= 2]
    if used_patch:
        reasons.append("近期使用了 Skill Patch 辅助")
    concept, stage, assistance = _current_competency(learning, now)
    if stage == "需复习":
        reasons.append(f"知识点 {concept} 已到复习时间")
    if assistance >= 4:
        reasons.append(f"知识点 {concept} 需要逐步降低 AI 辅助")
    return reasons[:10]


def _current_competency(
    learning: LearnerLearningBundle, now: datetime
) -> tuple[str, str, int]:
    raw = learning.profile.profile.get("competencies")
    if not isinstance(raw, Mapping) or not raw:
        return MISSING, "未观察", 0
    candidates: list[tuple[datetime, str, Mapping[str, Any]]] = []
    for concept, value in raw.items():
        if not isinstance(concept, str) or not concept or not isinstance(value, Mapping):
            continue
        observed = value.get("last_observed_at")
        timestamp = (
            _parse_time(observed, "last_observed_at")
            if isinstance(observed, str)
            else learning.profile.updated_at
        )
        candidates.append((timestamp, concept[:512], value))
    if not candidates:
        return MISSING, "未观察", 0
    _, concept, competency = max(candidates, key=lambda item: (item[0], item[1]))
    review = competency.get("next_review_at")
    if isinstance(review, str) and _parse_time(review, "next_review_at") <= _aware(now, "now"):
        stage = "需复习"
    else:
        raw_stage = competency.get("evidence_stage")
        stage = {
            "OBSERVED": "初现",
            "DEMONSTRATED": "发展中",
            "RETAINED": "熟练",
            "TRANSFERRED": "熟练",
        }.get(raw_stage, "未观察") if isinstance(raw_stage, str) else "未观察"
    raw_assistance = competency.get("assistance_level")
    assistance = (
        raw_assistance
        if isinstance(raw_assistance, int) and not isinstance(raw_assistance, bool)
        else 0
    )
    return concept, stage, max(0, min(5, assistance))


def _progress_and_suggestion(
    facts: Mapping[str, str | int | bool],
) -> tuple[str, str]:
    task = _display(facts.get("task_ref"))
    if facts.get("task_result") == "COMPLETED":
        progress = f"已完成任务 {task}，形成了一次可追溯的真实学习记录。"
    else:
        progress = f"围绕任务 {task} 完成了 {facts.get('attempt_count', 0)} 次真实尝试。"
    if facts.get("skill_patch_used") is True:
        suggestion = "下一次先复述解题思路，再逐步减少 Skill Patch 辅助。"
    elif facts.get("task_result") != "COMPLETED":
        suggestion = f"针对主要错误“{_main_error_zh(facts.get('main_error'))}”讲解后再独立尝试。"
    elif facts.get("knowledge_stage") in {"NOT_OBSERVED", "EMERGING", "DEVELOPING"}:
        suggestion = "安排一次低辅助同类练习，并请学生独立解释思路。"
    else:
        suggestion = "保持当前节奏，并在下一次任务中检查知识迁移。"
    return progress, suggestion


def _projection_task_name(
    projection: LearningProjectionAuthority, *, fallback: str = MISSING
) -> str:
    task = projection.projection.get("task")
    if isinstance(task, Mapping):
        # The writer freezes exactly task_id/concept/task_sha256.  A task_name
        # key is not authority and must never become teacher-facing text.
        value = task.get("task_id")
        if isinstance(value, str) and value.strip():
            return value.strip()[:256]
    return fallback


def _projection_concept(projection: LearningProjectionAuthority) -> str:
    task = projection.projection.get("task")
    value = task.get("concept") if isinstance(task, Mapping) else None
    return _display(value)


def _main_error_zh(value: Any) -> str:
    return MISSING if value in {None, "", "NONE"} else _display(value)


def _stage_zh(value: Any) -> str:
    return _STAGE_ZH.get(value if isinstance(value, str) else "", "未观察")


def _base_time(value: datetime) -> str:
    return _aware(value, "datetime").astimezone(_SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")


def _parse_time(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} is not a timestamp")
    try:
        return _aware(datetime.fromisoformat(value.replace("Z", "+00:00")), field)
    except ValueError as error:
        raise ValueError(f"{field} is not a timestamp") from error


def _aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return value.astimezone(UTC)


def trusted_growth_document_ref(
    url: str,
    *,
    trusted_template_url: str,
    expected_token: str | None = None,
) -> DocumentRef:
    """Validate a child Docx URL against the configured Feishu document origin."""
    if not isinstance(url, str) or not url:
        raise ValueError("growth document URL is invalid")
    try:
        parsed = urlsplit(url)
        trusted = urlsplit(trusted_template_url)
        parsed_port = parsed.port
        trusted_port = trusted.port
    except ValueError as error:
        raise ValueError("growth document URL is invalid") from error
    if (
        parsed.scheme != "https"
        or trusted.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or trusted.username is not None
        or trusted.password is not None
        or parsed.hostname is None
        or trusted.hostname is None
        or parsed.hostname.casefold() != trusted.hostname.casefold()
        or parsed_port != trusted_port
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("growth document URL is outside the trusted Feishu origin")
    matched = _DOC_TOKEN.fullmatch(parsed.path)
    if matched is None:
        raise ValueError("growth document URL is invalid")
    token = matched.group(1)
    if expected_token is not None and not hmac.compare_digest(token, expected_token):
        raise ValueError("created growth document URL/token drifted")
    return DocumentRef(token=token, url=url)


def _cell_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        for key in ("link", "text", "value"):
            nested = value.get(key)
            if isinstance(nested, str):
                return nested
    if isinstance(value, list):
        return "".join(_cell_text(item) for item in value)
    return ""


def _require_same(fields: Mapping[str, Any], name: str, expected: str) -> None:
    if not hmac.compare_digest(_cell_text(fields.get(name)), expected):
        raise ValueError(f"existing Base record has mismatched {name}")


def _same_daily_growth_facts(
    existing: Mapping[str, Any], expected: Mapping[str, Any]
) -> bool:
    return all(
        existing.get(field) == expected.get(field)
        for field in DAILY_GROWTH_FACT_FIELDS
    )


def _display(value: Any) -> str:
    return value.strip()[:512] if isinstance(value, str) and value.strip() else MISSING


def _safe_http_url(value: Any, field: str) -> str:
    text = _required_text(value, field)
    if not re.fullmatch(r"https?://[^\s]+", text):
        raise ValueError(f"{field} must be a credential-free HTTP(S) URL")
    authority = text.split("//", 1)[1].split("/", 1)[0]
    if "@" in authority:
        raise ValueError(f"{field} must not contain credentials")
    return text


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _required_fingerprint(value: Any, field: str, *, prefix: str) -> str:
    text = _required_text(value, field).lower()
    if re.fullmatch(rf"{re.escape(prefix)}:[a-f0-9]{{64}}", text) is None:
        raise ValueError(f"{field} must be a {prefix} fingerprint")
    return text


def _validate_pseudonym_secret(secret: str) -> None:
    if not isinstance(secret, str) or not 32 <= len(secret) <= 4096:
        raise ValueError("Feishu pseudonym secret must contain 32..4096 characters")


def _required_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return value


def _report(report: SyncReport, **increments: int) -> SyncReport:
    values = {
        "learners": report.learners,
        "learning_records": report.learning_records,
        "evidence_records": report.evidence_records,
        "base_records_created": report.base_records_created,
        "base_records_updated": report.base_records_updated,
        "documents_created": report.documents_created,
        "document_blocks_appended": report.document_blocks_appended,
        "miaoda_rows_upserted": report.miaoda_rows_upserted,
    }
    for key, value in increments.items():
        values[key] += value
    return SyncReport(**values)


__all__ = [
    "APPENDED",
    "AmbiguousSyncState",
    "BaseRecord",
    "DocumentRef",
    "FeishuAssets",
    "FeishuLearningSynchronizer",
    "FeishuSyncPort",
    "LearnerSyncBundle",
    "MAX_MIAODA_SQL_CHARS",
    "MISSING",
    "PENDING_APPEND",
    "SyncReport",
    "TEMPLATE_VERSION",
    "TenantLearningSnapshot",
    "build_miaoda_upsert_sql",
    "build_miaoda_upsert_sql_batches",
    "daily_base_fields",
    "evidence_base_fields",
    "growth_daily_block_xml",
    "growth_daily_group_block_xml",
    "growth_daily_table_xml",
    "growth_document_binding_xml",
    "growth_document_from_template_xml",
    "growth_document_header_xml",
    "miaoda_sql_text",
    "pseudonym_secret_fingerprint",
    "snapshot_report",
    "stable_business_key",
    "student_base_fields",
    "tenant_binding_fingerprint",
    "trusted_growth_document_ref",
    "validate_growth_document_binding",
    "validate_growth_template_xml",
]
