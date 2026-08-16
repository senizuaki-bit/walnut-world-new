"""Keep Alembic 018 and SQLAlchemy metadata identical at corruption boundaries."""

from __future__ import annotations

import asyncio
import os
from typing import Any

from sqlalchemy import CheckConstraint, inspect

from walnut_backend.adapters.postgres.models import Base
from walnut_backend.adapters.postgres.session import create_engine


def test_world_presentation_migration_matches_orm_metadata() -> None:
    database_url = os.environ.get("WALNUT_TEST_DATABASE_URL")
    if database_url is None:
        raise RuntimeError("set WALNUT_TEST_DATABASE_URL for required PostgreSQL coverage")
    asyncio.run(_exercise_metadata_match(database_url))


async def _exercise_metadata_match(database_url: str) -> None:
    engine = create_engine(database_url)
    try:
        async with engine.connect() as connection:
            database = await connection.run_sync(_database_shape)
        expected_checks = {
            "world_presentation_streams": {
                "ck_world_presentation_last_sequence",
                "ck_world_presentation_world_head",
                "ck_world_presentation_gap_revision",
                "ck_world_presentation_stream_hashes",
            },
            "world_presentation_events": {
                "ck_world_presentation_event_sequence",
                "ck_world_presentation_action_index",
                "ck_world_presentation_event_version",
                "ck_world_presentation_event_hashes",
            },
        }
        for table_name, names in expected_checks.items():
            table = Base.metadata.tables[table_name]
            model_checks = {
                str(constraint.name): str(constraint.sqltext)
                for constraint in table.constraints
                if isinstance(constraint, CheckConstraint)
            }
            assert set(model_checks) == names
            assert set(database[table_name]["checks"]) == names
            for name in names:
                assert _semantic_tokens(name) <= _tokens(model_checks[name])
                assert _semantic_tokens(name) <= _tokens(database[table_name]["checks"][name])

        event_model = Base.metadata.tables["world_presentation_events"]
        stream_model = Base.metadata.tables["world_presentation_streams"]
        expected_lengths = {
            "event_id": 45,
            "payload_sha256": 64,
            "integrity_sha256": 64,
            "state_hash_before": 64,
            "state_hash_after": 64,
            "final_snapshot_state_hash": 64,
        }
        for column, length in expected_lengths.items():
            assert event_model.c[column].type.length == length
            assert database["world_presentation_events"]["lengths"][column] == length
        for column in ("initial_snapshot_state_hash", "last_snapshot_state_hash"):
            assert stream_model.c[column].type.length == 64
            assert database["world_presentation_streams"]["lengths"][column] == 64
    finally:
        await engine.dispose()


def _database_shape(connection: Any) -> dict[str, dict[str, Any]]:
    inspector = inspect(connection)
    result: dict[str, dict[str, Any]] = {}
    for table_name in ("world_presentation_streams", "world_presentation_events"):
        result[table_name] = {
            "checks": {
                item["name"]: item["sqltext"]
                for item in inspector.get_check_constraints(table_name)
            },
            "lengths": {
                item["name"]: getattr(item["type"], "length", None)
                for item in inspector.get_columns(table_name)
            },
        }
    return result


def _tokens(value: str) -> set[str]:
    normalized = value.replace("::text", "").replace("\"", "").lower()
    return {token for token in normalized.replace("(", " ").replace(")", " ").split()}


def _semantic_tokens(name: str) -> set[str]:
    return {
        "ck_world_presentation_event_version": {
            "event_type",
            "'world.action.harvested'",
            "event_version",
            "schema_version",
            "producer",
            "'walnut_world_engine'",
        },
        "ck_world_presentation_event_hashes": {
            "state_hash_before",
            "state_hash_after",
            "final_snapshot_state_hash",
            "payload_sha256",
            "integrity_sha256",
        },
        "ck_world_presentation_stream_hashes": {
            "initial_snapshot_state_hash",
            "last_snapshot_state_hash",
        },
        "ck_world_presentation_world_head": {
            "initial_world_revision",
            "initial_world_event_sequence",
            "last_world_revision",
            "last_world_event_sequence",
        },
        "ck_world_presentation_last_sequence": {"last_sequence"},
        "ck_world_presentation_gap_revision": {"gap_world_revision"},
        "ck_world_presentation_event_sequence": {"sequence"},
        "ck_world_presentation_action_index": {
            "action_count",
            "action_index",
        },
    }[name]
