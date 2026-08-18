"""Configuration and fixed-release contract access for the transport layer."""

from __future__ import annotations

import json
import os
import posixpath
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from walnut_backend.contract_release import verify_agent_contract_release

BACKEND_ROOT = Path(__file__).resolve().parents[2]
_BUNDLED_CONTRACT_PATH = BACKEND_ROOT / "agent"
_LEGACY_CONTRACT_PATH = BACKEND_ROOT.parent / "agent"
DEFAULT_CONTRACT_PATH = (
    _BUNDLED_CONTRACT_PATH
    if (_BUNDLED_CONTRACT_PATH / "contracts" / "manifest.json").is_file()
    else _LEGACY_CONTRACT_PATH
)


def _environment_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes"}:
        return True
    if normalized in {"0", "false", "no"}:
        return False
    raise ValueError(f"{name} must be a boolean flag")


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime settings, kept infrastructure-neutral at the transport boundary."""

    database_url: str
    contract_path: Path
    sandbox_url: str
    llm_url: str
    feishu_url: str
    request_timeout_seconds: float
    development_auth_enabled: bool
    auth_hmac_secret: str | None = field(repr=False, default=None)
    feishu_pseudonym_secret: str | None = field(repr=False, default=None)
    auth_issuer: str | None = None
    auth_audience: str | None = None
    auth_clock_skew_seconds: int = 30
    auth_maximum_lifetime_seconds: int = 3600
    contract_release_path: Path | None = None
    realtime_wss_enabled: bool = False
    client_event_batch_enabled: bool = False
    world_presentation_enabled: bool = False
    skill_patch_enabled: bool = False
    public_realtime_url: str = "wss://localhost/v1/realtime"
    feishu_mcp_dashboard_url: str | None = None
    feishu_mcp_teacher_workspace_url: str | None = None

    def __post_init__(self) -> None:
        configured = (self.auth_hmac_secret, self.auth_issuer, self.auth_audience)
        if any(value is not None and not value for value in configured):
            raise ValueError("production JWT settings must not be empty")
        if any(value is not None for value in configured) and not all(
            value is not None for value in configured
        ):
            raise ValueError("production JWT secret, issuer, and audience must be configured together")
        if not self.development_auth_enabled and not all(value is not None for value in configured):
            raise ValueError("production JWT secret, issuer, and audience are required")
        if self.auth_hmac_secret is not None and not 32 <= len(self.auth_hmac_secret) <= 4096:
            raise ValueError("production JWT HMAC secret must contain 32..4096 characters")
        if self.feishu_pseudonym_secret is not None and not 32 <= len(
            self.feishu_pseudonym_secret
        ) <= 4096:
            raise ValueError("Feishu pseudonym secret must contain 32..4096 characters")
        if not 0 <= self.auth_clock_skew_seconds <= 300:
            raise ValueError("JWT clock skew must be between 0 and 300 seconds")
        if not 60 <= self.auth_maximum_lifetime_seconds <= 86_400:
            raise ValueError("JWT maximum lifetime must be between 60 and 86400 seconds")
        if (
            not isinstance(self.realtime_wss_enabled, bool)
            or not isinstance(self.client_event_batch_enabled, bool)
            or not isinstance(self.world_presentation_enabled, bool)
            or not isinstance(self.skill_patch_enabled, bool)
        ):
            raise TypeError("excluded transport feature flags must be boolean")
        if self.skill_patch_enabled and not self.world_presentation_enabled:
            raise ValueError(
                "Skill Patch requires the authoritative World presentation milestone"
            )
        parsed_realtime = urlsplit(self.public_realtime_url)
        if (
            parsed_realtime.scheme != "wss"
            or not parsed_realtime.hostname
            or parsed_realtime.username is not None
            or parsed_realtime.password is not None
            or parsed_realtime.query
            or parsed_realtime.fragment
        ):
            raise ValueError("public realtime URL must be a credential-free wss URL")
        _validate_optional_https_url(
            self.feishu_mcp_dashboard_url,
            "Feishu MCP dashboard URL",
            allow_query=True,
        )
        _validate_optional_https_url(
            self.feishu_mcp_teacher_workspace_url,
            "Feishu MCP teacher workspace URL",
            allow_query=False,
        )

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            database_url=os.getenv("WALNUT_DATABASE_URL", "postgresql://localhost/walnut"),
            contract_path=Path(os.getenv("WALNUT_CONTRACT_PATH", str(DEFAULT_CONTRACT_PATH))),
            sandbox_url=os.getenv("WALNUT_SANDBOX_URL", "http://127.0.0.1:8791"),
            llm_url=os.getenv("WALNUT_LLM_URL", "http://127.0.0.1:8792"),
            feishu_url=os.getenv("WALNUT_FEISHU_URL", "http://127.0.0.1:8793"),
            request_timeout_seconds=float(os.getenv("WALNUT_REQUEST_TIMEOUT_SECONDS", "30")),
            development_auth_enabled=_environment_flag("WALNUT_DEVELOPMENT_AUTH", False),
            auth_hmac_secret=os.getenv("WALNUT_AUTH_HMAC_SECRET") or None,
            feishu_pseudonym_secret=os.getenv("WALNUT_FEISHU_PSEUDONYM_SECRET") or None,
            auth_issuer=os.getenv("WALNUT_AUTH_ISSUER") or None,
            auth_audience=os.getenv("WALNUT_AUTH_AUDIENCE") or None,
            auth_clock_skew_seconds=int(os.getenv("WALNUT_AUTH_CLOCK_SKEW_SECONDS", "30")),
            auth_maximum_lifetime_seconds=int(
                os.getenv("WALNUT_AUTH_MAXIMUM_LIFETIME_SECONDS", "3600")
            ),
            contract_release_path=Path(
                os.getenv(
                    "WALNUT_CONTRACT_RELEASE_PATH",
                    str(BACKEND_ROOT / "contract-release.json"),
                )
            ),
            realtime_wss_enabled=_environment_flag("WALNUT_ENABLE_REALTIME_WSS", False),
            client_event_batch_enabled=_environment_flag(
                "WALNUT_ENABLE_CLIENT_EVENT_BATCH", False
            ),
            world_presentation_enabled=_environment_flag(
                "WALNUT_ENABLE_WORLD_PRESENTATION", False
            ),
            skill_patch_enabled=_environment_flag("WALNUT_ENABLE_SKILL_PATCH", False),
            public_realtime_url=os.getenv(
                "WALNUT_PUBLIC_REALTIME_URL", "wss://localhost/v1/realtime"
            ),
            feishu_mcp_dashboard_url=os.getenv("WALNUT_FEISHU_MCP_DASHBOARD_URL") or None,
            feishu_mcp_teacher_workspace_url=(
                os.getenv("WALNUT_FEISHU_MCP_TEACHER_WORKSPACE_URL") or None
            ),
        )

    @classmethod
    def for_test(
        cls, *, contract_path: Path, contract_release_path: Path | None = None
    ) -> Settings:
        """Create test settings from the live Agent contract workspace.

        ``contract_release_path`` remains accepted temporarily so existing callers
        can migrate without selecting an obsolete immutable release.
        """
        del contract_release_path
        return cls(
            database_url="postgresql://test/walnut",
            contract_path=contract_path,
            sandbox_url="http://127.0.0.1:8791",
            llm_url="http://127.0.0.1:8792",
            feishu_url="http://127.0.0.1:8793",
            request_timeout_seconds=30.0,
            development_auth_enabled=True,
            feishu_pseudonym_secret="feishu-test-pseudonym-secret-" + "s" * 32,
            contract_release_path=BACKEND_ROOT / "contract-release.json",
        )

    def resolved_feishu_pseudonym_secret(self) -> str:
        """Return the one explicitly configured Feishu pseudonym key.

        Authentication keys and development defaults are deliberately not accepted:
        either fallback would silently change every externally persisted learner key.
        """
        if self.feishu_pseudonym_secret is None:
            raise ValueError("WALNUT_FEISHU_PSEUDONYM_SECRET is required")
        return self.feishu_pseudonym_secret


def _validate_optional_https_url(
    value: str | None,
    label: str,
    *,
    allow_query: bool,
) -> None:
    if value is None:
        return
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (parsed.query and not allow_query)
    ):
        raise ValueError(f"{label} must be a credential-free HTTPS URL")


class ContractRelease:
    """Reads only an Agent workspace that exactly matches the Backend release pin."""

    def __init__(self, settings: Settings) -> None:
        self._repository = settings.contract_path
        release_path = settings.contract_release_path or BACKEND_ROOT / "contract-release.json"
        verify_agent_contract_release(self._repository, release_path)
        self._documents: dict[str, Any] = {}

    def json_document(self, path: str) -> dict[str, Any]:
        if path not in self._documents:
            candidate = (self._repository / Path(path)).resolve()
            try:
                candidate.relative_to(self._repository.resolve())
            except ValueError as error:
                raise ValueError(f"contract path escapes Agent workspace: {path}") from error
            self._documents[path] = json.loads(candidate.read_text(encoding="utf-8"))
        document = self._documents[path]
        if not isinstance(document, dict):
            raise ValueError(f"contract document {path} is not an object")
        return document

    def validate(self, schema_path: str, payload: Any) -> list[str]:
        documents: dict[str, dict[str, Any]] = {}

        def collect(path: str) -> None:
            if path in documents:
                return
            document = self.json_document(path)
            documents[path] = document
            for reference in find_references(document):
                if reference.startswith("#") or "://" in reference:
                    continue
                referenced_path = posixpath.normpath(
                    str((Path(path).parent / reference.split("#", 1)[0]).as_posix())
                )
                collect(referenced_path)

        collect(schema_path)
        resources = []
        for document in documents.values():
            identifier = document.get("$id")
            if isinstance(identifier, str):
                resources.append((identifier, Resource.from_contents(document)))
        registry = Registry().with_resources(resources)
        validator = Draft202012Validator(documents[schema_path], registry=registry)
        return [error.message for error in validator.iter_errors(payload)]

    def error_catalog(self) -> LockedErrorCatalog:
        """Expose every HTTP error only with metadata from the immutable catalog."""
        document = self.json_document("contracts/error-catalog.json")
        entries = document.get("errors")
        if not isinstance(entries, list):
            raise ValueError("locked error catalog has no errors list")
        catalog: dict[str, tuple[int, str, bool, str]] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("locked error catalog contains a non-object entry")
            code = entry.get("code")
            status = entry.get("http_status")
            category = entry.get("category")
            retryable = entry.get("retryable")
            message_key = entry.get("user_message_key")
            if not (
                isinstance(code, str)
                and isinstance(status, int)
                and isinstance(category, str)
                and isinstance(retryable, bool)
                and isinstance(message_key, str)
            ):
                raise ValueError("locked error catalog entry is malformed")
            catalog[code] = (status, category, retryable, message_key)
        return LockedErrorCatalog(catalog, self.validate)


class LockedErrorCatalog(Mapping[str, tuple[int, str, bool, str]]):
    """Full immutable catalog plus the ErrorResponse validator that governs it."""

    def __init__(
        self,
        entries: Mapping[str, tuple[int, str, bool, str]],
        validate: Callable[[str, Any], list[str]],
    ) -> None:
        self._entries = dict(entries)
        self._validate = validate

    def __getitem__(self, code: str) -> tuple[int, str, bool, str]:
        return self._entries[code]

    def __iter__(self) -> Iterator[str]:
        return iter(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def validate_error_response(self, body: object) -> list[str]:
        return self._validate("contracts/schemas/common/error-response.schema.json", body)


def find_references(value: object) -> list[str]:
    """Return local JSON-Schema references without reimplementing schema validation."""
    if isinstance(value, dict):
        references = [value["$ref"]] if isinstance(value.get("$ref"), str) else []
        for nested in value.values():
            references.extend(find_references(nested))
        return references
    if isinstance(value, list):
        return [reference for nested in value for reference in find_references(nested)]
    return []
