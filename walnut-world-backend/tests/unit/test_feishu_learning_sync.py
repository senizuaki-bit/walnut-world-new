"""Unit closure for the idempotent PostgreSQL-to-Feishu INT3 synchronizer."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from yaya_agent_contracts import canonical_json_sha256

from walnut_backend.adapters.lark_cli.feishu_learning import (
    LarkCliError,
    LarkCliFeishuSyncPort,
    _safe_subprocess_command,
)
from walnut_backend.application.feishu.learning_queries import (
    EvidenceAuthority,
    EvidenceLearningBundle,
    LearnerLearningBundle,
    LearnerProfileAuthority,
    LearningProjectionAuthority,
    stable_learner_ref,
)
from walnut_backend.application.feishu.learning_sync import (
    APPENDED,
    DAILY_GROWTH_FACT_FIELDS,
    MAX_MIAODA_SQL_CHARS,
    MISSING,
    BaseRecord,
    DocumentRef,
    FeishuAssets,
    FeishuLearningSynchronizer,
    LearnerSyncBundle,
    TenantLearningSnapshot,
    build_miaoda_upsert_sql_batches,
    daily_base_fields,
    growth_daily_block_xml,
    miaoda_sql_text,
    pseudonym_secret_fingerprint,
    stable_business_key,
    tenant_binding_fingerprint,
    trusted_growth_document_ref,
    validate_growth_document_binding,
    validate_growth_template_xml,
)


def test_sync_cli_starts_from_clean_powershell_without_pythonpath() -> None:
    backend_root = Path(__file__).resolve().parents[2]
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)

    completed = subprocess.run(
        [sys.executable, str(backend_root / "scripts" / "sync_feishu_learning.py"), "--help"],
        cwd=backend_root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert "--tenant-id" in completed.stdout


def test_windows_lark_cli_npm_shim_uses_node_without_cmd_shell(tmp_path: Path) -> None:
    npm_root = tmp_path / "npm"
    shim = npm_root / "lark-cli.cmd"
    run_script = (
        npm_root
        / "node_modules"
        / "@larksuite"
        / "cli"
        / "scripts"
        / "run.js"
    )
    node = tmp_path / "node.exe"
    run_script.parent.mkdir(parents=True)
    shim.touch()
    run_script.touch()
    node.touch()
    sql = "BEGIN;\nINSERT INTO evidence_summary(payload) VALUES ('a&b|c');\nCOMMIT;"

    def which(executable: str) -> str | None:
        return {"lark-cli": str(shim), "node": str(node)}.get(executable)

    command = ["lark-cli", "apps", "+db-execute", "--sql", sql, "--yes"]

    resolved = _safe_subprocess_command(command, platform="win32", which=which)

    assert resolved == [str(node), str(run_script), *command[1:]]
    assert resolved[-2:] == [sql, "--yes"]
    assert all("cmd.exe" not in argument.casefold() for argument in resolved)


def test_windows_lark_cli_npm_shim_requires_fixed_adjacent_entrypoint(
    tmp_path: Path,
) -> None:
    shim = tmp_path / "npm" / "lark-cli.cmd"
    node = tmp_path / "node.exe"
    shim.parent.mkdir(parents=True)
    shim.touch()
    node.touch()

    def which(executable: str) -> str | None:
        return {"lark-cli": str(shim), "node": str(node)}.get(executable)

    with pytest.raises(FileNotFoundError, match="npm entrypoint"):
        _safe_subprocess_command(["lark-cli", "auth", "status"], platform="win32", which=which)


NOW = datetime(2026, 8, 15, 7, 0, tzinfo=UTC)
SECRET = "feishu-sync-test-secret-" + "s" * 40
TENANT = "tenant_sync_test"
CONTENT_HASH = "a" * 64


class FakeLark:
    def __init__(self) -> None:
        self.records: dict[str, dict[str, BaseRecord]] = {}
        self.document_xml: dict[str, str] = {}
        self.appends: dict[str, list[str]] = {}
        self.created_documents = 0
        self.miaoda_calls: list[tuple[str, str, str]] = []
        self.block_replacements = 0
        self._next_record = 1

    def fetch_document_xml(self, document_token: str) -> str:
        if document_token == _assets().template_document_token:
            return _mother_template()
        if document_token not in self.document_xml:
            raise AssertionError("unexpected document token")
        return self.document_xml[document_token] + "".join(self.appends[document_token])

    def fetch_document_xml_with_ids(self, document_token: str) -> str:
        content = self.document_xml[document_token] + "".join(self.appends[document_token])
        sequence = iter(range(1, content.count("<table") + 1))
        return re.sub(
            r"<table(?![^>]*\bid=)",
            lambda _: f'<table id="tbl_fake_{next(sequence)}"',
            content,
        )

    def find_exact_record(
        self, table_id: str, key_field: str, business_key: str
    ) -> BaseRecord | None:
        matches = [
            record
            for record in self.records.get(table_id, {}).values()
            if record.fields.get(key_field) == business_key
        ]
        assert len(matches) <= 1
        return matches[0] if matches else None

    def upsert_record(
        self,
        table_id: str,
        fields: Mapping[str, Any],
        *,
        record_id: str | None = None,
    ) -> BaseRecord:
        table = self.records.setdefault(table_id, {})
        if record_id is None:
            record_id = f"rec_{self._next_record:04d}"
            self._next_record += 1
        record = BaseRecord(record_id=record_id, fields=dict(fields))
        table[record_id] = record
        return record

    def create_document(self, content_xml: str) -> DocumentRef:
        self.created_documents += 1
        token = f"DocToken{self.created_documents:04d}"
        self.document_xml[token] = content_xml
        self.appends[token] = []
        return DocumentRef(token=token, url=f"https://example.feishu.cn/docx/{token}")

    def append_document(self, document_token: str, content_xml: str) -> None:
        self.appends[document_token].append(content_xml)

    def replace_document_block(
        self, document_token: str, block_id: str, content_xml: str
    ) -> None:
        index = int(block_id.rsplit("_", 1)[1])
        template_tables = self.document_xml[document_token].count("<table")
        appended_tables = [
            position
            for position, content in enumerate(self.appends[document_token])
            if "<table" in content
        ]
        append_index = appended_tables[index - template_tables - 1]
        current = self.appends[document_token][append_index]
        replaced, count = re.subn(
            r"<table\b[^>]*>.*?</table>", content_xml, current, count=1, flags=re.DOTALL
        )
        assert count == 1
        self.appends[document_token][append_index] = replaced
        self.block_replacements += 1

    def execute_miaoda_sql(self, app_id: str, environment: str, sql: str) -> int:
        assert self.records[_assets().student_table_id]
        assert self.records[_assets().daily_table_id]
        assert self.records[_assets().evidence_table_id]
        assert all(
            sum("WALNUT_GROWTH_DOCUMENT_BINDING_V1:" in item for item in items) == 1
            for items in self.appends.values()
        )
        assert any(
            "每日成长记录｜" in item
            for items in self.appends.values()
            for item in items
        )
        self.miaoda_calls.append((app_id, environment, sql))
        return 7


class ProjectedSearchFakeLark(FakeLark):
    """Approximate Base search, which returns only explicitly projected fields."""

    def __init__(self) -> None:
        super().__init__()
        self.daily_writes: list[str] = []

    def find_exact_record(
        self, table_id: str, key_field: str, business_key: str
    ) -> BaseRecord | None:
        record = super().find_exact_record(table_id, key_field, business_key)
        if record is None or table_id != _assets().daily_table_id:
            return record
        projected = {
            key_field,
            "学生业务键",
            "档案追加状态",
            "档案追加键",
            *DAILY_GROWTH_FACT_FIELDS,
        }
        return BaseRecord(
            record.record_id,
            {field: value for field, value in record.fields.items() if field in projected},
        )

    def upsert_record(
        self,
        table_id: str,
        fields: Mapping[str, Any],
        *,
        record_id: str | None = None,
    ) -> BaseRecord:
        if table_id == _assets().daily_table_id:
            self.daily_writes.append(str(fields.get("档案追加状态")))
        return super().upsert_record(table_id, fields, record_id=record_id)


def test_business_keys_are_stable_tenant_scoped_and_opaque() -> None:
    first = stable_business_key(SECRET, "fsp", TENANT, "lrn_private_01")

    assert first == stable_business_key(SECRET, "fsp", TENANT, "lrn_private_01")
    assert first != stable_business_key(SECRET, "fsp", "tenant_other", "lrn_private_01")
    assert first != stable_business_key(SECRET, "flr", TENANT, "lrn_private_01")
    assert TENANT not in first
    assert "private" not in first


def test_asset_binding_is_secret_and_tenant_scoped_without_plain_tenant() -> None:
    assets = _assets()

    assets.assert_binding(TENANT, SECRET)
    assert TENANT not in assets.tenant_binding_fingerprint
    assert SECRET not in assets.pseudonym_secret_fingerprint
    with pytest.raises(ValueError, match="different tenant"):
        assets.assert_binding("tenant_other", SECRET)
    with pytest.raises(ValueError, match="secret fingerprint"):
        assets.assert_binding(TENANT, SECRET + "drift")

    fake = FakeLark()
    learning, evidence = _authorities()
    wrong_snapshot = TenantLearningSnapshot(
        tenant_id="tenant_other",
        learners=(LearnerSyncBundle(learning=learning, evidence=(evidence,)),),
    )
    with pytest.raises(ValueError, match="different tenant"):
        FeishuLearningSynchronizer(
            fake, assets, pseudonym_secret=SECRET, clock=lambda: NOW
        ).sync(wrong_snapshot)
    assert fake.records == {}
    assert fake.created_documents == 0
    assert fake.miaoda_calls == []


def test_fixed_growth_structure_escapes_content_and_writes_missing_values() -> None:
    learning, _ = _authorities()
    original = learning.projections[0]
    projection = replace(
        original,
        projection={
            **original.projection,
            "task": {"task_id": "TASK_01", "concept": "loops", "task_name": "<unsafe>"},
        },
    )
    facts = {
        "task_ref": "TASK_01",
        "run_ref": projection.run_id,
        "task_result": "COMPLETED",
        "attempt_count": 1,
        "main_error": "NONE",
        "ai_assistance_level": 0,
        "skill_patch_used": False,
        "knowledge_stage": "DEVELOPING",
    }

    xml = growth_daily_block_xml(
        learner_ref=learning.profile.learner_ref,
        projection=projection,
        facts=facts,
        evidence_url=None,
    )

    expected = [
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
    ]
    positions = [xml.index(f"<b>{column}</b>") for column in expected]
    assert positions == sorted(positions)
    assert xml.count("<tr><td><b>") == 11
    assert MISSING in xml
    assert "raw_source_code" not in xml
    assert "TASK_01" in xml
    assert "unsafe" not in xml
    assert "<unsafe>" not in xml


def test_recent_seven_days_uses_shanghai_calendar_days_across_midnight() -> None:
    learning, _ = _authorities()
    local_now = datetime(2026, 8, 16, 15, 55, tzinfo=UTC)  # 23:55 in Shanghai

    def recent_flag(completed_at: datetime) -> bool:
        projection = replace(learning.projections[0], completed_at=completed_at)
        scoped = replace(learning, projections=(projection,))
        fields = daily_base_fields(
            scoped,
            projection,
            learner_key="fsp_calendar_test",
            class_key="cls_calendar_test",
            daily_key="flr_calendar_test",
            day_key="fgd_calendar_test",
            evidence_ids=(),
            document_url="https://example.feishu.cn/docx/DocCalendar",
            dashboard_url="https://example.feishu.cn/base/BaseToken?dashboard=Dashboard",
            append_status=APPENDED,
            now=local_now,
        )
        return fields["是否近7天"] is True

    # Today (Aug 16) plus the six preceding Shanghai dates begins Aug 10 00:00.
    assert recent_flag(datetime(2026, 8, 9, 16, 0, tzinfo=UTC)) is True
    # Aug 9 23:59 Shanghai is only 167h56m old, but belongs to the eighth date.
    assert recent_flag(datetime(2026, 8, 9, 15, 59, tzinfo=UTC)) is False
    # A future local date must not leak into the dashboard window.
    assert recent_flag(datetime(2026, 8, 16, 16, 0, tzinfo=UTC)) is False


def test_sync_twice_reuses_one_document_and_upserts_without_duplicate_records() -> None:
    learning, evidence = _authorities()
    snapshot = TenantLearningSnapshot(
        tenant_id=TENANT,
        learners=(LearnerSyncBundle(learning=learning, evidence=(evidence,)),),
    )
    fake = FakeLark()
    sync = FeishuLearningSynchronizer(
        fake, _assets(), pseudonym_secret=SECRET, clock=lambda: NOW
    )

    first = sync.sync(snapshot)
    second = sync.sync(snapshot)

    assert first.documents_created == 1
    assert first.document_blocks_appended == 1
    assert second.documents_created == 0
    assert second.document_blocks_appended == 0
    assert first.miaoda_rows_upserted == 7
    assert second.miaoda_rows_upserted == 7
    assert fake.created_documents == 1
    document_token, created_xml = next(iter(fake.document_xml.items()))
    assert created_xml.startswith("<title>儿童学习成长档案 v1</title>")
    assert " id=" not in created_xml
    assert created_xml.count("<tr><td><b>") == 11
    assert learning.profile.learner_ref in created_xml
    learner_key = stable_business_key(
        SECRET, "fsp", TENANT, learning.profile.learner_ref
    )
    document = DocumentRef(
        token=document_token,
        url=f"https://example.feishu.cn/docx/{document_token}",
    )
    validate_growth_document_binding(
        fake.fetch_document_xml(document_token),
        learner_ref=learning.profile.learner_ref,
        learner_key=learner_key,
        tenant_binding=_assets().tenant_binding_fingerprint,
        document=document,
        pseudonym_secret=SECRET,
    )
    binding_xml = fake.appends[document_token][0]
    assert "WALNUT_GROWTH_DOCUMENT_BINDING_V1:" in binding_xml
    assert learner_key in binding_xml
    assert _assets().tenant_binding_fingerprint in binding_xml
    assert learning.profile.learner_id not in binding_xml
    assert SECRET not in binding_xml
    with pytest.raises(ValueError, match="ownership binding does not match student"):
        validate_growth_document_binding(
            fake.fetch_document_xml(document_token).replace(
                "hmac-sha256:", "hmac-sha256:0", 1
            ),
            learner_ref=learning.profile.learner_ref,
            learner_key=learner_key,
            tenant_binding=_assets().tenant_binding_fingerprint,
            document=document,
            pseudonym_secret=SECRET,
        )
    assert [len(records) for records in fake.records.values()] == [1, 1, 1]
    assert sum(len(items) for items in fake.appends.values()) == 2
    daily = next(iter(fake.records[_assets().daily_table_id].values()))
    assert daily.fields["档案追加状态"] == APPENDED
    assert daily.fields["任务名称"] == "TASK_01"
    assert daily.fields["主要错误"] == MISSING
    assert daily.fields["Evidence引用"] == evidence.evidence.evidence_id
    projected_evidence = next(
        iter(fake.records[_assets().evidence_table_id].values())
    )
    evidence_link = projected_evidence.fields["Evidence链接"]
    assert evidence_link.startswith(
        f"{_assets().miaoda_online_url}/students/fsp_"
    )
    assert "#evidence-fev_" in evidence_link
    appended_xml = _growth_appends(fake)[0]
    assert evidence_link in appended_xml.replace("&amp;", "&")
    assert len(fake.miaoda_calls) == 2
    app_id, environment, first_sql = fake.miaoda_calls[0]
    assert app_id == _assets().miaoda_app_id
    assert environment == "online"
    assert first_sql == fake.miaoda_calls[1][2]
    assert first_sql.startswith("BEGIN;\n")
    assert first_sql.endswith("\nCOMMIT;")
    assert "INSERT INTO student_profile" in first_sql
    assert "ON CONFLICT (learner_key) DO UPDATE" in first_sql
    assert "INSERT INTO daily_learning_record" in first_sql
    assert "ON CONFLICT (learning_key) DO UPDATE" in first_sql
    assert "INSERT INTO evidence_summary" in first_sql
    assert "ON CONFLICT (evidence_key) DO UPDATE" in first_sql
    assert "INSERT INTO learning_center_config" in first_sql
    assert "ON CONFLICT (config_key) DO UPDATE" in first_sql
    assert '"evidence_sync_source_0001"' in first_sql
    assert "print('private')" not in first_sql
    assert "private chat" not in first_sql
    assert "password-123" not in first_sql
    serialized = json.dumps(
        {
            "records": {
                table: [record.fields for record in records.values()]
                for table, records in fake.records.items()
            },
            "docs": fake.document_xml,
            "appends": fake.appends,
        },
        ensure_ascii=False,
    )
    assert learning.profile.learner_id not in serialized
    assert "print('private')" not in serialized
    assert "private chat" not in serialized
    assert "password-123" not in serialized

    fake.records[_assets().daily_table_id][daily.record_id] = BaseRecord(
        daily.record_id, {**daily.fields, "档案追加状态": "待追加"}
    )
    recovered = sync.sync(snapshot)
    assert recovered.document_blocks_appended == 0
    assert sum(len(items) for items in fake.appends.values()) == 2
    assert fake.block_replacements == 1
    assert len(fake.miaoda_calls) == 3


def test_swapped_growth_document_links_fail_before_append_or_replace() -> None:
    first_learning, first_evidence = _authorities()
    second_learning = _other_learner(first_learning)
    snapshot = TenantLearningSnapshot(
        tenant_id=TENANT,
        learners=(
            LearnerSyncBundle(
                learning=first_learning,
                evidence=(first_evidence,),
            ),
            LearnerSyncBundle(learning=second_learning, evidence=()),
        ),
    )
    fake = FakeLark()
    sync = FeishuLearningSynchronizer(
        fake, _assets(), pseudonym_secret=SECRET, clock=lambda: NOW
    )
    sync.sync(snapshot)
    students = fake.records[_assets().student_table_id]
    first_record = next(
        record
        for record in students.values()
        if record.fields["学生代号"] == first_learning.profile.learner_ref
    )
    second_record = next(
        record
        for record in students.values()
        if record.fields["学生代号"] == second_learning.profile.learner_ref
    )
    first_url = first_record.fields["成长档案"]
    second_url = second_record.fields["成长档案"]
    students[first_record.record_id] = BaseRecord(
        first_record.record_id,
        {**first_record.fields, "成长档案": second_url},
    )
    students[second_record.record_id] = BaseRecord(
        second_record.record_id,
        {**second_record.fields, "成长档案": first_url},
    )
    append_counts = {token: len(items) for token, items in fake.appends.items()}

    with pytest.raises(ValueError, match="ownership binding does not match student"):
        sync.sync(snapshot)

    assert {token: len(items) for token, items in fake.appends.items()} == append_counts
    assert fake.block_replacements == 0
    assert fake.created_documents == 2
    assert len(fake.miaoda_calls) == 1


def test_growth_document_url_must_match_configured_https_feishu_origin() -> None:
    trusted = "https://example.feishu.cn/docx/TemplateToken"

    reference = trusted_growth_document_ref(
        "https://example.feishu.cn/docx/DocToken0001",
        trusted_template_url=trusted,
        expected_token="DocToken0001",
    )

    assert reference == DocumentRef(
        "DocToken0001", "https://example.feishu.cn/docx/DocToken0001"
    )
    for unsafe in (
        "http://example.feishu.cn/docx/DocToken0001",
        "https://evil.example/docx/DocToken0001",
        "https://user@example.feishu.cn/docx/DocToken0001",
        "https://example.feishu.cn/docx/DocToken0001?redirect=evil",
        "https://example.feishu.cn/base/DocToken0001",
    ):
        with pytest.raises(ValueError):
            trusted_growth_document_ref(
                unsafe,
                trusted_template_url=trusted,
                expected_token="DocToken0001",
            )


def test_projected_daily_search_replay_does_not_stage_or_replace_growth_block() -> None:
    learning, evidence = _authorities()
    snapshot = TenantLearningSnapshot(
        tenant_id=TENANT,
        learners=(LearnerSyncBundle(learning=learning, evidence=(evidence,)),),
    )
    fake = ProjectedSearchFakeLark()
    sync = FeishuLearningSynchronizer(
        fake, _assets(), pseudonym_secret=SECRET, clock=lambda: NOW
    )

    sync.sync(snapshot)
    fake.daily_writes.clear()
    replay = sync.sync(snapshot)

    assert replay.document_blocks_appended == 0
    assert fake.block_replacements == 0
    assert fake.daily_writes == []


def test_lark_cli_daily_search_requests_every_growth_comparison_field() -> None:
    fields = {
        "学习记录业务键": "flr_business_key",
        "学生业务键": "fsp_business_key",
        "档案追加状态": APPENDED,
        "档案追加键": "fgd_business_key",
        **{name: f"value-{index}" for index, name in enumerate(DAILY_GROWTH_FACT_FIELDS)},
    }
    commands: list[Sequence[str]] = []

    def runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        projected = [
            command[index + 1]
            for index, value in enumerate(command[:-1])
            if value == "--field-id"
        ]
        payload = {
            "ok": True,
            "data": {
                "records": [
                    {
                        "record_id": "rec_daily_0001",
                        "fields": {name: fields[name] for name in projected},
                    }
                ]
            },
        }
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    port = LarkCliFeishuSyncPort(_assets(), runner=runner)
    record = port.find_exact_record(
        _assets().daily_table_id,
        "学习记录业务键",
        "flr_business_key",
    )

    assert record is not None
    assert record.fields == fields
    projected = {
        commands[0][index + 1]
        for index, value in enumerate(commands[0][:-1])
        if value == "--field-id"
    }
    assert projected == set(fields)


def test_lark_cli_parses_columnar_search_and_verifies_upsert() -> None:
    commands: list[Sequence[str]] = []
    business_key = "fsp_columnar_0001"
    document_url = "https://example.feishu.cn/docx/DocToken0001"

    def runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if "+record-upsert" in command:
            payload = {
                "ok": True,
                "data": {"record": {"update": {"学生业务键": business_key}}, "created": True},
            }
        else:
            payload = {
                "ok": True,
                "data": {
                    "data": [
                        [
                            business_key,
                            "lrn_columnar",
                            f"[{document_url}]({document_url})",
                            "v1",
                        ]
                    ],
                    "fields": ["学生业务键", "学生代号", "成长档案", "template_version"],
                    "record_id_list": ["rec_columnar_0001"],
                    "has_more": False,
                },
            }
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    port = LarkCliFeishuSyncPort(_assets(), runner=runner)
    record = port.upsert_record(
        _assets().student_table_id,
        {
            "学生业务键": business_key,
            "学生代号": "lrn_columnar",
            "成长档案": document_url,
            "template_version": "v1",
        },
    )

    assert record == BaseRecord(
        record_id="rec_columnar_0001",
        fields={
            "学生业务键": business_key,
            "学生代号": "lrn_columnar",
            "成长档案": document_url,
            "template_version": "v1",
        },
    )
    assert len(commands) == 2
    assert "+record-upsert" in commands[0]
    assert "+record-search" in commands[1]


def test_lark_cli_rejects_malformed_columnar_search_shape() -> None:
    def runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        payload = {
            "ok": True,
            "data": {
                "data": [["fsp_exact"]],
                "fields": ["学生业务键", "学生代号"],
                "record_id_list": ["rec_columnar_0001"],
            },
        }
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    port = LarkCliFeishuSyncPort(_assets(), runner=runner)

    with pytest.raises(LarkCliError, match="malformed Base search rows"):
        port.find_exact_record(
            _assets().student_table_id,
            "学生业务键",
            "fsp_exact",
        )


def test_same_day_two_runs_keep_run_facts_in_one_idempotent_growth_block() -> None:
    single_learning, first_evidence = _authorities()
    learning, second_evidence = _with_second_same_day_run(single_learning)
    first_evidence = replace(first_evidence, learner_projections=learning.projections)
    snapshot = TenantLearningSnapshot(
        tenant_id=TENANT,
        learners=(
            LearnerSyncBundle(
                learning=learning,
                evidence=(first_evidence, second_evidence),
            ),
        ),
    )
    fake = FakeLark()
    sync = FeishuLearningSynchronizer(
        fake, _assets(), pseudonym_secret=SECRET, clock=lambda: NOW
    )

    first = sync.sync(snapshot)
    replay = sync.sync(snapshot)

    assert first.document_blocks_appended == 1
    assert replay.document_blocks_appended == 0
    assert len(fake.records[_assets().daily_table_id]) == 2
    daily_records = tuple(fake.records[_assets().daily_table_id].values())
    assert {record.fields["Run ID"] for record in daily_records} == {
        "run_sync_0001",
        "run_sync_0002",
    }
    assert len({record.fields["档案追加键"] for record in daily_records}) == 1
    assert all(record.fields["档案追加状态"] == APPENDED for record in daily_records)
    second_daily = next(
        record for record in daily_records if record.fields["Run ID"] == "run_sync_0002"
    )
    assert second_daily.fields["阶段前"] == "初现"
    assert sum(len(items) for items in fake.appends.values()) == 2
    growth = _growth_appends(fake)[0]
    assert growth.count("<h1>每日成长记录｜") == 1
    assert "run_sync_0001：已完成" in growth
    assert "run_sync_0002：未完成" in growth
    assert "run_sync_0002：TASK_INCOMPLETE" in growth
    assert "run_sync_0002：4（Skill Patch辅助）" in growth
    assert "evidence_sync_source_0001" not in growth
    assert "evidence_sync_source_0002" not in growth
    assert fake.block_replacements == 0


def test_later_same_day_run_replaces_table_instead_of_appending_second_block() -> None:
    single_learning, first_evidence = _authorities()
    full_learning, second_evidence = _with_second_same_day_run(single_learning)
    first_snapshot = TenantLearningSnapshot(
        tenant_id=TENANT,
        learners=(
            LearnerSyncBundle(learning=single_learning, evidence=(first_evidence,)),
        ),
    )
    full_first_evidence = replace(
        first_evidence, learner_projections=full_learning.projections
    )
    full_snapshot = TenantLearningSnapshot(
        tenant_id=TENANT,
        learners=(
            LearnerSyncBundle(
                learning=full_learning,
                evidence=(full_first_evidence, second_evidence),
            ),
        ),
    )
    fake = FakeLark()
    sync = FeishuLearningSynchronizer(
        fake, _assets(), pseudonym_secret=SECRET, clock=lambda: NOW
    )

    sync.sync(first_snapshot)
    sync.sync(full_snapshot)
    sync.sync(full_snapshot)

    assert sum(len(items) for items in fake.appends.values()) == 2
    assert fake.block_replacements == 1
    growth = _growth_appends(fake)[0]
    assert growth.count("<h1>每日成长记录｜") == 1
    assert "run_sync_0001：已完成" in growth
    assert "run_sync_0002：未完成" in growth


def test_miaoda_sql_text_escapes_statement_breakout_and_rejects_nul() -> None:
    unsafe = "O'Brien'); DROP TABLE student_profile; --"

    assert miaoda_sql_text(unsafe) == "'O''Brien''); DROP TABLE student_profile; --'"
    assert miaoda_sql_text(None) == "NULL"
    with pytest.raises(ValueError):
        miaoda_sql_text("bad\x00value")


def test_miaoda_projection_sql_is_split_below_windows_command_limit() -> None:
    rows = tuple(
        {
            "learner_key": f"fsp_{index:04d}",
            "learner_alias": f"lrn_{index:04d}",
            "class_key": "cls_fixed",
            "current_concept": "loops",
            "mastery_stage": "发展中",
            "ai_assistance_level": 0,
            "ai_assistance_label": "未使用",
            "skill_patch_count": 0,
            "last_active_at": NOW,
            "active_today": True,
            "needs_attention": False,
            "attention_reason": "x" * 500,
            "growth_document_url": "https://example.feishu.cn/docx/DocToken0001",
            "template_version": "v1",
            "data_time": NOW,
        }
        for index in range(12)
    )

    batches = build_miaoda_upsert_sql_batches(
        students=rows,
        daily_records=(),
        evidence=(),
        assets=_assets(),
        synced_at=NOW,
        max_chars=2_500,
    )

    assert len(batches) > 1
    assert all(len(sql) <= 2_500 for sql in batches)
    assert all(len(sql) <= MAX_MIAODA_SQL_CHARS for sql in batches)
    assert all(sql.startswith("BEGIN;\n") and sql.endswith("\nCOMMIT;") for sql in batches)
    combined = "\n".join(batches)
    assert all(combined.count(f"fsp_{index:04d}") == 1 for index in range(12))


def test_daily_projection_keeps_all_columns_when_optional_authority_is_absent() -> None:
    learning, _ = _authorities()
    projection = learning.projections[0]
    fields = daily_base_fields(
        learning,
        projection,
        learner_key="fsp_key",
        class_key="cls_key",
        daily_key="flr_key",
        day_key="fgd_key",
        evidence_ids=(),
        document_url="https://example.feishu.cn/docx/DocToken0001",
        dashboard_url="https://example.feishu.cn/base/BaseToken?dashboard=Dashboard",
        append_status="待追加",
        now=NOW,
    )

    assert len(fields) == 27
    assert fields["任务名称"] == "TASK_01"
    assert fields["主要错误"] == MISSING
    assert fields["阶段前"] == MISSING
    assert fields["Evidence引用"] == MISSING


def test_lark_subprocess_arguments_are_structured_and_failures_are_sanitized() -> None:
    commands: list[list[str]] = []

    def runner(command):
        commands.append(list(command))
        if "+db-execute" in command:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    {
                        "ok": True,
                        "data": [
                            {"command": "BEGIN"},
                            {"command": "INSERT", "rows_affected": 3},
                            {"command": "INSERT", "rows_affected": 4},
                            {"command": "COMMIT"},
                        ],
                    }
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "ok": True,
                    "data": {
                        "records": [
                            {
                                "record_id": "rec_01",
                                "fields": {"学生业务键": "fsp_exact"},
                            }
                        ]
                    },
                }
            ),
            stderr="",
        )

    port = LarkCliFeishuSyncPort(_assets(), runner=runner)

    record = port.find_exact_record(_assets().student_table_id, "学生业务键", "fsp_exact")

    assert record is not None and record.record_id == "rec_01"
    assert commands[0][0] == "lark-cli"
    assert "--filter-json" in commands[0]
    assert "fsp_exact" in commands[0]

    sql = "BEGIN;\nINSERT INTO learning_center_config DEFAULT VALUES;\nCOMMIT;"
    affected = port.execute_miaoda_sql(
        _assets().miaoda_app_id, _assets().miaoda_environment, sql
    )
    assert affected == 7
    miaoda_command = commands[1]
    assert miaoda_command[:2] == ["lark-cli", "apps"]
    assert "+db-execute" in miaoda_command
    assert ["--environment", "online"] == miaoda_command[
        miaoda_command.index("--environment") : miaoda_command.index("--environment") + 2
    ]
    assert "--yes" in miaoda_command
    assert sql in miaoda_command

    secret_from_stderr = "password-that-must-not-escape"

    def failing_runner(command):
        return subprocess.CompletedProcess(
            command,
            1,
            stdout=json.dumps(
                {"ok": False, "error": {"code": "E_AUTH", "message": secret_from_stderr}}
            ),
            stderr=secret_from_stderr,
        )

    failing = LarkCliFeishuSyncPort(_assets(), runner=failing_runner)
    with pytest.raises(LarkCliError) as captured:
        failing.find_exact_record(_assets().student_table_id, "学生业务键", "fsp_exact")
    assert secret_from_stderr not in str(captured.value)
    assert "E_AUTH" in str(captured.value)


def test_mother_template_validation_rejects_reordered_columns() -> None:
    validate_growth_template_xml(_mother_template())
    malformed = (
        _mother_template()
        .replace("<b>基本信息</b>", "<b>__swap__</b>", 1)
        .replace("<b>日期</b>", "<b>基本信息</b>", 1)
        .replace("<b>__swap__</b>", "<b>日期</b>", 1)
    )
    with pytest.raises(ValueError):
        validate_growth_template_xml(malformed)


def test_lark_cli_rejects_created_doc_on_untrusted_host() -> None:
    def runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        payload = {
            "ok": True,
            "data": {
                "document": {
                    "document_id": "DocToken0001",
                    "url": "https://evil.example/docx/DocToken0001",
                }
            },
        }
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    port = LarkCliFeishuSyncPort(_assets(), runner=runner)

    with pytest.raises(LarkCliError, match="invalid created document reference"):
        port.create_document("<title>child</title>")


def _authorities() -> tuple[LearnerLearningBundle, EvidenceLearningBundle]:
    learner_id = "direct_student_identity"
    learner_ref = stable_learner_ref(SECRET, TENANT, learner_id)
    profile = LearnerProfileAuthority(
        learner_ref=learner_ref,
        tenant_id=TENANT,
        learner_id=learner_id,
        actor_id="direct_actor_identity",
        content_hash=CONTENT_HASH,
        profile={
            "learner_id": learner_id,
            "actor_id": "direct_actor_identity",
            "content": {"content_hash": CONTENT_HASH},
            "competencies": {
                "loops": {
                    "evidence_stage": "DEMONSTRATED",
                    "assistance_level": 0,
                    "last_observed_at": _timestamp(NOW - timedelta(minutes=1)),
                    "next_review_at": _timestamp(NOW + timedelta(days=1)),
                    "evidence_ids": ["evidence_sync_source_0001"],
                }
            },
            "evidence_refs": [],
            "updated_at": _timestamp(NOW - timedelta(minutes=1)),
            "direct_identifier": "Student Name",
        },
        updated_at=NOW - timedelta(minutes=1),
    )
    projection = LearningProjectionAuthority(
        job_id="job_sync_0001",
        command_id="cmd_sync_0001",
        session_id="session_sync_0001",
        turn_id="turn_sync_0001",
        run_id="run_sync_0001",
        learner_id=learner_id,
        source_event_id="event_sync_0001",
        through_sequence=1,
        projection={
            "run": {"task_success": True, "failure_key": None},
            "task": {"task_id": "TASK_01", "concept": "<unsafe>"},
            "assistance": {"used_skill_patch": False},
            "source_feedback_event_id": "event_feedback_sync_0001",
            "source_evidence_ids": ["evidence_sync_source_0001"],
            "raw_source_code": "print('private')",
            "raw_chat_text": "private chat",
        },
        result={"learner": {"evidence_id": "evidence_sync_learner_0001"}},
        completed_at=NOW - timedelta(minutes=1),
    )
    learning = LearnerLearningBundle(profile=profile, projections=(projection,))
    evidence = EvidenceLearningBundle(
        evidence=EvidenceAuthority(
            evidence_id="evidence_sync_source_0001",
            command_id=projection.command_id,
            recorded_at=NOW - timedelta(minutes=1),
            document={
                "evidence_ref": {
                    "evidence_id": "evidence_sync_source_0001",
                    "evidence_type": "TEST_REPORT",
                    "created_at": _timestamp(NOW - timedelta(minutes=1)),
                    "sha256": "b" * 64,
                },
                "occurred_at": _timestamp(NOW - timedelta(minutes=1)),
                "payload": {
                    "evidence_kind": "TEST_REPORT",
                    "raw_source_code": "print('private')",
                    "raw_chat_text": "private chat",
                    "credentials": "password-123",
                },
            },
        ),
        profile=profile,
        projection=projection,
        learner_projections=(projection,),
    )
    return learning, evidence


def _other_learner(learning: LearnerLearningBundle) -> LearnerLearningBundle:
    learner_id = "other_direct_student_identity"
    actor_id = "other_direct_actor_identity"
    profile = replace(
        learning.profile,
        learner_ref=stable_learner_ref(SECRET, TENANT, learner_id),
        learner_id=learner_id,
        actor_id=actor_id,
        profile={
            **learning.profile.profile,
            "learner_id": learner_id,
            "actor_id": actor_id,
        },
    )
    projection = replace(
        learning.projections[0],
        job_id="job_sync_other_0001",
        command_id="cmd_sync_other_0001",
        session_id="session_sync_other_0001",
        turn_id="turn_sync_other_0001",
        run_id="run_sync_other_0001",
        learner_id=learner_id,
        source_event_id="event_sync_other_0001",
    )
    return LearnerLearningBundle(profile=profile, projections=(projection,))


def _with_second_same_day_run(
    learning: LearnerLearningBundle,
) -> tuple[LearnerLearningBundle, EvidenceLearningBundle]:
    first_profile = {
        "competencies": {
            "loops": {
                "concept": "loops",
                "evidence_stage": "OBSERVED",
                "next_review_at": _timestamp(NOW + timedelta(days=1)),
            }
        }
    }
    first = replace(
        learning.projections[0],
        projection={
            **learning.projections[0].projection,
            "task": {"task_id": "TASK_01", "concept": "loops"},
        },
        result={
            **learning.projections[0].result,
            "projection_receipt": {
                "receipt_json": {
                    "learner": {
                        "profile": first_profile,
                        "profile_sha256": canonical_json_sha256(first_profile),
                    }
                }
            },
        },
    )
    second_profile = {
        "competencies": {
            "loops": {
                "concept": "loops",
                "evidence_stage": "DEMONSTRATED",
                "next_review_at": _timestamp(NOW + timedelta(days=1)),
            }
        }
    }
    second = replace(
        first,
        job_id="job_sync_0002",
        command_id="cmd_sync_0002",
        turn_id="turn_sync_0002",
        run_id="run_sync_0002",
        source_event_id="event_sync_0002",
        through_sequence=2,
        projection={
            "run": {"task_success": False, "failure_key": "TASK_INCOMPLETE"},
            "task": {"task_id": "TASK_02", "concept": "loops"},
            "assistance": {"used_skill_patch": True},
            "source_feedback_event_id": "event_feedback_sync_0002",
            "source_evidence_ids": ["evidence_sync_source_0002"],
        },
        result={
            "learner": {"evidence_id": "evidence_sync_learner_0002"},
            "projection_receipt": {
                "receipt_json": {
                    "learner": {
                        "profile": second_profile,
                        "profile_sha256": canonical_json_sha256(second_profile),
                    }
                }
            },
        },
        completed_at=first.completed_at + timedelta(seconds=30),
    )
    full_learning = replace(learning, projections=(first, second))
    evidence = EvidenceLearningBundle(
        evidence=EvidenceAuthority(
            evidence_id="evidence_sync_source_0002",
            command_id=second.command_id,
            recorded_at=second.completed_at,
            document={
                "evidence_ref": {
                    "evidence_id": "evidence_sync_source_0002",
                    "evidence_type": "TEST_REPORT",
                    "created_at": _timestamp(second.completed_at),
                    "sha256": "c" * 64,
                },
                "occurred_at": _timestamp(second.completed_at),
                "payload": {"evidence_kind": "TEST_REPORT"},
            },
        ),
        profile=learning.profile,
        projection=second,
        learner_projections=full_learning.projections,
    )
    return full_learning, evidence


def _assets() -> FeishuAssets:
    return FeishuAssets(
        base_token="BaseToken",
        base_url="https://example.feishu.cn/base/BaseToken",
        dashboard_id="Dashboard",
        dashboard_url="https://example.feishu.cn/base/BaseToken?dashboard=Dashboard",
        student_table_id="tblStudent",
        daily_table_id="tblDaily",
        evidence_table_id="tblEvidence",
        template_document_token="TemplateToken",
        template_document_url="https://example.feishu.cn/docx/TemplateToken",
        backend_public_url="http://127.0.0.1:8790",
        miaoda_app_id="app_Test123",
        miaoda_online_url="https://example.feishuapp.com/app/app_Test123",
        miaoda_environment="online",
        tenant_binding_fingerprint=tenant_binding_fingerprint(SECRET, TENANT),
        pseudonym_secret_fingerprint=pseudonym_secret_fingerprint(SECRET),
    )


def _mother_template() -> str:
    columns = (
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
    rows = "".join(f"<tr><td><b>{column}</b></td><td>{MISSING}</td></tr>" for column in columns)
    return (
        '<title id="template">儿童学习成长档案 v1</title>'
        '<p id="version"><b>template_version：</b>v1</p>'
        f'<table id="columns">{rows}</table>'
    )


def _growth_appends(fake: FakeLark) -> list[str]:
    return [
        item
        for items in fake.appends.values()
        for item in items
        if "每日成长记录｜" in item
    ]


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
