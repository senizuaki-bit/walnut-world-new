"""The Backend mounts three released reads plus one non-contract MCP adapter."""

from __future__ import annotations

from pathlib import Path

import pytest

from walnut_backend.api.app import create_app
from walnut_backend.bootstrap import DEFAULT_CONTRACT_PATH, ContractRelease, Settings


def test_three_feishu_learning_routes_are_mounted_from_locked_openapi() -> None:
    settings = Settings.for_test(contract_path=DEFAULT_CONTRACT_PATH)
    app = create_app(settings)
    mounted: set[tuple[str, str]] = set()
    operation_ids: dict[tuple[str, str], str] = {}
    for route in app.routes:
        candidates = (route, *getattr(getattr(route, "original_router", None), "routes", ()))
        for candidate in candidates:
            path = getattr(candidate, "path", "")
            if path.startswith("/integrations/feishu/v1/"):
                for method in getattr(candidate, "methods", ()):
                    mounted.add((method, path))
                    operation_ids[(method, path)] = getattr(candidate, "operation_id", "")
    expected = {
        ("POST", "/integrations/feishu/v1/learner-queries"),
        ("POST", "/integrations/feishu/v1/class-insights"),
        ("GET", "/integrations/feishu/v1/evidence/{evidence_id}"),
    }
    assert mounted - {("POST", "/integrations/feishu/v1/mcp")} == expected
    assert ("POST", "/integrations/feishu/v1/mcp") in mounted
    assert ("GET", "/integrations/feishu/v1/mcp") not in mounted

    released = ContractRelease(settings).json_document(
        "contracts/openapi/feishu-integration.openapi.json"
    )["paths"]
    for method, path in expected:
        operation = released[path][method.lower()]
        assert operation_ids[(method, path)] == operation["operationId"]
        assert operation["x-read-only"] is True
        assert operation["x-audit-access"] is True
    assert "/integrations/feishu/v1/mcp" not in released


def test_locked_feishu_schemas_accept_projection_shapes_without_mutation_fields() -> None:
    settings = Settings.for_test(contract_path=DEFAULT_CONTRACT_PATH)
    release = ContractRelease(settings)
    learner_schema = release.json_document("contracts/schemas/feishu/learner-query.schema.json")
    class_schema = release.json_document(
        "contracts/schemas/feishu/class-insights-query.schema.json"
    )
    evidence_schema = release.json_document("contracts/schemas/feishu/evidence-view.schema.json")

    property_names = _property_names((learner_schema, class_schema, evidence_schema))
    for forbidden in (
        "source_bundle",
        "world_command",
        "mastery_override",
        "activation_id",
        "raw_chat_text",
        "raw_source_code",
        "credential",
    ):
        assert forbidden not in property_names


def test_locked_class_contract_admits_fractional_ratios_without_canonical_scope() -> None:
    settings = Settings.for_test(contract_path=DEFAULT_CONTRACT_PATH)
    release = ContractRelease(settings)
    schema = release.json_document("contracts/schemas/feishu/class-insights-result.schema.json")
    example = release.json_document("contracts/examples/feishu-class-insights-response.json")[
        "value"
    ]

    assert "canonicalization" not in schema
    assert any(
        isinstance(item.get("ratio"), float)
        and not item["ratio"].is_integer()
        and item.get("suppressed") is False
        for item in example["insights"]
    )
    assert (
        release.validate("contracts/schemas/feishu/class-insights-result.schema.json", example)
        == []
    )


def test_feishu_pseudonym_key_is_separate_stable_and_validated() -> None:
    settings = Settings.for_test(contract_path=DEFAULT_CONTRACT_PATH)
    assert settings.resolved_feishu_pseudonym_secret() == settings.feishu_pseudonym_secret
    assert len(settings.resolved_feishu_pseudonym_secret()) >= 32


def test_feishu_pseudonym_key_never_falls_back_to_auth_or_development_defaults() -> None:
    settings = Settings(
        database_url="postgresql://test/walnut",
        contract_path=DEFAULT_CONTRACT_PATH,
        sandbox_url="http://127.0.0.1:8791",
        llm_url="http://127.0.0.1:8792",
        feishu_url="http://127.0.0.1:8793",
        request_timeout_seconds=30.0,
        development_auth_enabled=True,
        auth_hmac_secret="auth-secret-must-never-drive-pseudonyms-" + "s" * 32,
        auth_issuer="https://identity.example",
        auth_audience="walnut",
    )

    with pytest.raises(ValueError, match="WALNUT_FEISHU_PSEUDONYM_SECRET"):
        settings.resolved_feishu_pseudonym_secret()


def test_postgres_feishu_adapter_has_no_business_authority_write_primitive() -> None:
    source = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "walnut_backend"
        / "adapters"
        / "postgres"
        / "feishu_learning.py"
    ).read_text(encoding="utf-8")
    for forbidden in ("session.add(", "insert(", "update(", "delete(", "merge("):
        assert forbidden not in source
    assert "PostgresAudit" in source


def _property_names(value: object) -> set[str]:
    if isinstance(value, dict):
        names = (
            set(value.get("properties", {})) if isinstance(value.get("properties"), dict) else set()
        )
        return names | {name for nested in value.values() for name in _property_names(nested)}
    if isinstance(value, tuple | list):
        return {name for nested in value for name in _property_names(nested)}
    return set()
