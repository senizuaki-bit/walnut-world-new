"""Subprocess adapter for the documented lark-cli Base and Doc shortcuts."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from walnut_backend.application.feishu.learning_sync import (
    DAILY_GROWTH_FACT_FIELDS,
    MAX_MIAODA_SQL_CHARS,
    BaseRecord,
    DocumentRef,
    FeishuAssets,
    trusted_growth_document_ref,
)

_SAFE_CODE = re.compile(r"[^A-Za-z0-9_.:-]")
_MARKDOWN_IDENTITY_LINK = re.compile(r"^\[(https://[^\]]+)\]\(\1\)$")


class LarkCliError(RuntimeError):
    """Sanitized lark-cli failure that never includes argv, stdout, or environment."""


Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class LarkCliFeishuSyncPort:
    def __init__(
        self,
        assets: FeishuAssets,
        *,
        executable: str = "lark-cli",
        identity: str = "user",
        runner: Runner | None = None,
    ) -> None:
        if identity not in {"user", "bot"}:
            raise ValueError("lark-cli identity must be user or bot")
        if not executable:
            raise ValueError("lark-cli executable is required")
        self._assets = assets
        self._executable = executable
        self._identity = identity
        self._runner = runner or _default_runner

    def fetch_document_xml(self, document_token: str) -> str:
        return self._fetch_document_xml(document_token, detail="simple")

    def fetch_document_xml_with_ids(self, document_token: str) -> str:
        return self._fetch_document_xml(document_token, detail="with-ids")

    def _fetch_document_xml(self, document_token: str, *, detail: str) -> str:
        payload = self._run(
            "docs",
            "+fetch",
            "--doc",
            document_token,
            "--scope",
            "full",
            "--detail",
            detail,
            "--doc-format",
            "xml",
        )
        document = _mapping(_data(payload).get("document"), "document")
        content = document.get("content")
        if not isinstance(content, str):
            raise LarkCliError("lark-cli returned no document content")
        return content

    def find_exact_record(
        self, table_id: str, key_field: str, business_key: str
    ) -> BaseRecord | None:
        args = [
            "base",
            "+record-search",
            "--base-token",
            self._assets.base_token,
            "--table-id",
            table_id,
            "--keyword",
            business_key,
            "--search-field",
            key_field,
            "--filter-json",
            json.dumps(
                {"logic": "and", "conditions": [[key_field, "==", business_key]]},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "--limit",
            "10",
        ]
        for field in _projected_fields(self._assets, table_id, key_field):
            args.extend(("--field-id", field))
        payload = self._run(*args)
        records = _records(payload)
        exact = [record for record in records if _cell_text(record.fields.get(key_field)) == business_key]
        if len(exact) > 1:
            raise LarkCliError("duplicate Base records exist for one business key")
        return exact[0] if exact else None

    def upsert_record(
        self,
        table_id: str,
        fields: Mapping[str, Any],
        *,
        record_id: str | None = None,
    ) -> BaseRecord:
        args = [
            "base",
            "+record-upsert",
            "--base-token",
            self._assets.base_token,
            "--table-id",
            table_id,
        ]
        if record_id is not None:
            args.extend(("--record-id", record_id))
        args.extend(
            (
                "--json",
                json.dumps(fields, ensure_ascii=False, separators=(",", ":")),
            )
        )
        self._run(*args)
        key_field = _business_key_field(self._assets, table_id)
        business_key = _cell_text(fields.get(key_field))
        if not business_key:
            raise LarkCliError("Base upsert has no stable business key")
        parsed = self.find_exact_record(table_id, key_field, business_key)
        if parsed is None:
            raise LarkCliError("lark-cli Base upsert could not be verified")
        if record_id is not None and parsed.record_id != record_id:
            raise LarkCliError("lark-cli updated an unexpected Base record")
        return parsed

    def create_document(self, content_xml: str) -> DocumentRef:
        payload = self._run(
            "docs",
            "+create",
            "--content",
            content_xml,
            "--doc-format",
            "xml",
            "--parent-position",
            "my_library",
        )
        document = _mapping(_data(payload).get("document"), "document")
        token = document.get("document_id")
        url = document.get("url")
        if not isinstance(token, str) or not isinstance(url, str):
            raise LarkCliError("lark-cli returned no created document reference")
        try:
            return trusted_growth_document_ref(
                url,
                trusted_template_url=self._assets.template_document_url,
                expected_token=token,
            )
        except ValueError as error:
            raise LarkCliError(
                "lark-cli returned an invalid created document reference"
            ) from error

    def append_document(self, document_token: str, content_xml: str) -> None:
        self._run(
            "docs",
            "+update",
            "--doc",
            document_token,
            "--command",
            "append",
            "--content",
            content_xml,
            "--doc-format",
            "xml",
        )

    def replace_document_block(
        self, document_token: str, block_id: str, content_xml: str
    ) -> None:
        if re.fullmatch(r"[A-Za-z0-9_-]+", block_id) is None:
            raise LarkCliError("growth document block id is invalid")
        self._run(
            "docs",
            "+update",
            "--doc",
            document_token,
            "--command",
            "block_replace",
            "--block-id",
            block_id,
            "--content",
            content_xml,
            "--doc-format",
            "xml",
        )

    def execute_miaoda_sql(self, app_id: str, environment: str, sql: str) -> int:
        if self._identity != "user":
            raise LarkCliError("Miaoda database writes require lark-cli user identity")
        if app_id != self._assets.miaoda_app_id:
            raise LarkCliError("Miaoda app identifier does not match the asset config")
        if environment != self._assets.miaoda_environment or environment not in {
            "dev",
            "online",
        }:
            raise LarkCliError("Miaoda environment does not match the asset config")
        if not sql.startswith("BEGIN;\n") or not sql.endswith("\nCOMMIT;"):
            raise LarkCliError("Miaoda SQL must be one explicit transaction")
        if len(sql) > MAX_MIAODA_SQL_CHARS:
            raise LarkCliError("Miaoda SQL batch exceeds the Windows-safe command limit")
        payload = self._run(
            "apps",
            "+db-execute",
            "--app-id",
            app_id,
            "--environment",
            environment,
            "--sql",
            sql,
            "--yes",
        )
        raw = payload.get("data")
        if isinstance(raw, Mapping):
            nested = raw.get("results")
            results = nested if isinstance(nested, list) else [raw]
        elif isinstance(raw, list):
            results = raw
        else:
            raise LarkCliError("lark-cli returned no Miaoda database result")
        affected = 0
        found_dml = False
        for item in results:
            if not isinstance(item, Mapping):
                raise LarkCliError("lark-cli returned a malformed Miaoda database result")
            rows = item.get("rows_affected")
            if rows is None:
                continue
            if not isinstance(rows, int) or isinstance(rows, bool) or rows < 0:
                raise LarkCliError("lark-cli returned an invalid Miaoda affected-row count")
            affected += rows
            found_dml = True
        if not found_dml:
            raise LarkCliError("lark-cli returned no Miaoda DML result")
        return affected

    def _run(self, *arguments: str) -> Mapping[str, Any]:
        command = [
            self._executable,
            *arguments,
            "--as",
            self._identity,
            "--format",
            "json",
        ]
        try:
            completed = self._runner(command)
        except (OSError, subprocess.SubprocessError) as error:
            raise LarkCliError("lark-cli could not be executed") from error
        payload = _json_payload(completed.stdout)
        if completed.returncode != 0 or payload.get("ok") is not True:
            code = _error_code(payload)
            raise LarkCliError(f"lark-cli command failed (code={code})")
        return payload


def _default_runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _safe_subprocess_command(command),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        cwd=Path.cwd(),
        shell=False,
    )


def _safe_subprocess_command(
    command: Sequence[str],
    *,
    platform: str | None = None,
    which: Callable[[str], str | None] | None = None,
) -> list[str]:
    """Resolve the Windows npm shim without routing arguments through cmd.exe."""

    argv = list(command)
    if not argv:
        raise ValueError("lark-cli command is required")
    current_platform = sys.platform if platform is None else platform
    if current_platform != "win32":
        return argv

    resolve = shutil.which if which is None else which
    resolved = resolve(argv[0])
    if resolved is None:
        return argv
    shim = Path(resolved)
    if shim.stem.casefold() != "lark-cli" or shim.suffix.casefold() not in {
        ".bat",
        ".cmd",
        ".ps1",
    }:
        return [resolved, *argv[1:]]

    run_script = (
        shim.parent
        / "node_modules"
        / "@larksuite"
        / "cli"
        / "scripts"
        / "run.js"
    )
    if not run_script.is_file():
        raise FileNotFoundError("lark-cli npm entrypoint was not found")
    adjacent_node = shim.parent / "node.exe"
    node = str(adjacent_node) if adjacent_node.is_file() else resolve("node")
    if node is None:
        raise FileNotFoundError("node executable for lark-cli was not found")
    return [node, str(run_script), *argv[1:]]


def _projected_fields(
    assets: FeishuAssets, table_id: str, key_field: str
) -> tuple[str, ...]:
    fields = {
        assets.student_table_id: ("学生业务键", "学生代号", "成长档案", "template_version"),
        assets.daily_table_id: (
            "学习记录业务键",
            "学生业务键",
            "档案追加状态",
            "档案追加键",
            *DAILY_GROWTH_FACT_FIELDS,
        ),
        assets.evidence_table_id: ("Evidence业务键", "学生业务键"),
    }.get(table_id)
    if fields is None:
        return (key_field,)
    return fields


def _business_key_field(assets: FeishuAssets, table_id: str) -> str:
    fields = {
        assets.student_table_id: "学生业务键",
        assets.daily_table_id: "学习记录业务键",
        assets.evidence_table_id: "Evidence业务键",
    }
    try:
        return fields[table_id]
    except KeyError as error:
        raise LarkCliError("Base table is not part of the INT3 asset set") from error


def _records(payload: Mapping[str, Any]) -> list[BaseRecord]:
    data = _data(payload)
    matrix = data.get("data")
    record_ids = data.get("record_id_list")
    field_names = data.get("fields")
    if matrix is not None or record_ids is not None or field_names is not None:
        if (
            not isinstance(matrix, list)
            or not isinstance(record_ids, list)
            or not isinstance(field_names, list)
            or len(matrix) != len(record_ids)
            or any(not isinstance(name, str) for name in field_names)
            or len(set(field_names)) != len(field_names)
        ):
            raise LarkCliError("lark-cli returned malformed Base search columns")
        records: list[BaseRecord] = []
        for record_id, row in zip(record_ids, matrix, strict=True):
            if (
                not isinstance(record_id, str)
                or not isinstance(row, list)
                or len(row) != len(field_names)
            ):
                raise LarkCliError("lark-cli returned malformed Base search rows")
            records.append(
                BaseRecord(
                    record_id=record_id,
                    fields=dict(
                        zip(
                            field_names,
                            (_normalize_cell_value(value) for value in row),
                            strict=True,
                        )
                    ),
                )
            )
        return records
    raw = data.get("records")
    if not isinstance(raw, list):
        raw = data.get("items")
    if not isinstance(raw, list):
        nested = data.get("result")
        raw = nested.get("records") if isinstance(nested, Mapping) else None
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise LarkCliError("lark-cli returned malformed Base search records")
    return [_record(item) for item in raw if isinstance(item, Mapping)]


def _record(value: Mapping[str, Any]) -> BaseRecord:
    record_id = value.get("record_id")
    fields = value.get("fields")
    if not isinstance(record_id, str) or not isinstance(fields, Mapping):
        raise LarkCliError("lark-cli returned a malformed Base record")
    return BaseRecord(record_id=record_id, fields=dict(fields))


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


def _normalize_cell_value(value: Any) -> Any:
    if isinstance(value, str):
        matched = _MARKDOWN_IDENTITY_LINK.fullmatch(value)
        if matched is not None:
            return matched.group(1)
    return value


def _json_payload(value: str) -> Mapping[str, Any]:
    try:
        payload = json.loads(value)
    except (json.JSONDecodeError, TypeError) as error:
        raise LarkCliError("lark-cli returned invalid JSON") from error
    if not isinstance(payload, Mapping):
        raise LarkCliError("lark-cli returned a non-object JSON envelope")
    return payload


def _data(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    return _mapping(payload.get("data"), "data")


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise LarkCliError(f"lark-cli returned no {field}")
    return value


def _error_code(payload: Mapping[str, Any]) -> str:
    error = payload.get("error")
    raw = error.get("code") if isinstance(error, Mapping) else None
    if not isinstance(raw, (str, int)):
        return "UNKNOWN"
    return _SAFE_CODE.sub("", str(raw))[:64] or "UNKNOWN"


__all__ = ["LarkCliError", "LarkCliFeishuSyncPort", "Runner"]
