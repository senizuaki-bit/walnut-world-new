"""Resume the one verified pre-document INT3 sync checkpoint."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = BACKEND_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from walnut_backend.adapters.lark_cli.feishu_learning import (  # noqa: E402
    LarkCliFeishuSyncPort,
)
from walnut_backend.adapters.postgres.feishu_learning import (  # noqa: E402
    PostgresFeishuLearningSyncReader,
)
from walnut_backend.adapters.postgres.session import create_session_factory  # noqa: E402
from walnut_backend.application.feishu.learning_queries import (  # noqa: E402
    stable_class_ref,
)
from walnut_backend.application.feishu.learning_sync import (  # noqa: E402
    TEMPLATE_VERSION,
    DocumentRef,
    FeishuAssets,
    growth_document_binding_xml,
    stable_business_key,
    student_base_fields,
    validate_growth_document_binding,
    validate_growth_template_xml,
)

TENANT_ID = "tenant_yaya"
ASSETS_PATH = BACKEND_ROOT / "config" / "int3_feishu_assets.target.json"


async def _run() -> None:
    database_url = os.environ["WALNUT_DATABASE_URL"]
    secret = os.environ["WALNUT_FEISHU_PSEUDONYM_SECRET"]
    executable = os.environ["WALNUT_LARK_CLI"]
    assets = FeishuAssets.from_mapping(json.loads(ASSETS_PATH.read_text(encoding="utf-8")))
    assets.assert_binding(TENANT_ID, secret)
    snapshot = await PostgresFeishuLearningSyncReader(
        create_session_factory(database_url), pseudonym_secret=secret
    ).load_tenant(TENANT_ID)
    if len(snapshot.learners) != 1:
        raise RuntimeError("recovery requires exactly one authoritative learner")
    learning = snapshot.learners[0].learning
    learner_ref = learning.profile.learner_ref
    learner_key = stable_business_key(secret, "fsp", TENANT_ID, learner_ref)
    port = LarkCliFeishuSyncPort(assets, executable=executable, identity="user")
    record = port.find_exact_record(assets.student_table_id, "学生业务键", learner_key)
    if record is None:
        raise RuntimeError("verified pending student record is missing")
    if (
        record.fields.get("学生代号") != learner_ref
        or record.fields.get("template_version") != f"{TEMPLATE_VERSION}:document-pending"
        or record.fields.get("成长档案") not in {None, ""}
    ):
        raise RuntimeError("student record is not the verified pre-document checkpoint")

    template_xml = port.fetch_document_xml(assets.template_document_token)
    validate_growth_template_xml(template_xml)
    document = DocumentRef(
        token="MaacdRNB5oZo9gxZ1B2cEyFZn1c",
        url="https://larkcommunity.feishu.cn/docx/MaacdRNB5oZo9gxZ1B2cEyFZn1c",
    )
    binding_xml = growth_document_binding_xml(
        learner_ref=learner_ref,
        learner_key=learner_key,
        tenant_binding=assets.tenant_binding_fingerprint,
        document=document,
        pseudonym_secret=secret,
    )
    completed = subprocess.run(
        [
            executable,
            "docs",
            "+update",
            "--doc",
            document.token,
            "--command",
            "append",
            "--content",
            binding_xml,
            "--doc-format",
            "xml",
            "--as",
            "user",
            "--format",
            "json",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"binding append failed: {completed.stderr.strip()}")
    validate_growth_document_binding(
        port.fetch_document_xml(document.token),
        learner_ref=learner_ref,
        learner_key=learner_key,
        tenant_binding=assets.tenant_binding_fingerprint,
        document=document,
        pseudonym_secret=secret,
    )
    fields = student_base_fields(
        learning,
        tenant_id=TENANT_ID,
        learner_key=learner_key,
        class_key=stable_class_ref(secret, TENANT_ID),
        document_url=document.url,
        template_version=TEMPLATE_VERSION,
        now=datetime.now(UTC),
    )
    updated = port.upsert_record(assets.student_table_id, fields, record_id=record.record_id)
    if updated.fields.get("成长档案") != document.url:
        raise RuntimeError("student document link was not verified after update")
    print(json.dumps({"status": "student-document-checkpoint-closed"}), flush=True)


if __name__ == "__main__":
    asyncio.run(_run())
