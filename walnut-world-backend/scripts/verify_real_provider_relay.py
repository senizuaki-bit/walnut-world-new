"""Verify a completed opt-in live gate without printing credentials or model output."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from sqlalchemy import text

from walnut_backend.adapters.postgres.session import create_session_factory


def _formal_dispatch_bounds() -> tuple[int, int]:
    skill_patch_enabled = os.environ.get("WALNUT_ENABLE_SKILL_PATCH")
    if skill_patch_enabled == "false":
        return 12, 24
    if skill_patch_enabled == "true":
        return 16, 32
    raise RuntimeError(
        "WALNUT_ENABLE_SKILL_PATCH must be exactly true (M2) or false (M1)"
    )


async def verify(database_url: str) -> dict[str, object]:
    minimum_dispatches, maximum_dispatches = _formal_dispatch_bounds()
    sessions = create_session_factory(database_url)
    try:
        async with sessions() as session:
            provider = await session.execute(
                text(
                    """
                    SELECT
                      count(*) FILTER (WHERE state = 'SUCCEEDED') AS succeeded,
                      count(*) AS unique_dispatches,
                      COALESCE(sum(generation_count), 0) AS total_generations,
                      COALESCE(max(generation_count), 0) AS max_generation_count,
                      count(*) FILTER (WHERE response_body_sha256 IS NOT NULL) AS response_hashes
                    FROM recoverable_llm_dispatches
                    """
                )
            )
            provider_row = provider.mappings().one()
            result = await session.execute(
                text(
                    """
                    SELECT receipt_json
                    FROM job_step_receipts r
                    JOIN workflow_jobs w
                      ON w.tenant_id = r.tenant_id AND w.job_id = r.job_id
                    WHERE w.operation = 'EXECUTE_AGENT_TURN'
                      AND r.step_name LIKE '%PROVIDER_RESULT_%'
                    ORDER BY r.completed_at DESC
                    LIMIT 1
                    """
                )
            )
            receipt = result.scalar_one_or_none()
    finally:
        await sessions.kw["bind"].dispose()
    if not isinstance(receipt, dict):
        raise RuntimeError("real Provider result receipt is absent")
    reply = receipt.get("result")
    if not isinstance(reply, dict) or reply.get("outcome") != "SUCCESS":
        raise RuntimeError("real Provider result did not succeed")
    value = reply.get("reply")
    if not isinstance(value, dict):
        raise RuntimeError("real Provider reply authority is absent")
    if (
        value.get("source") != "provider"
        or value.get("degraded") is not False
        or value.get("fallback_reason") is not None
    ):
        raise RuntimeError("real Provider result is not source=provider, degraded=false")
    unique_dispatches = int(provider_row["unique_dispatches"])
    total_generations = int(provider_row["total_generations"])
    if (
        unique_dispatches < minimum_dispatches
        or unique_dispatches > maximum_dispatches
        or int(provider_row["succeeded"]) != unique_dispatches
        or total_generations != unique_dispatches
        or int(provider_row["max_generation_count"]) != 1
        or int(provider_row["response_hashes"]) != unique_dispatches
    ):
        raise RuntimeError("relay database does not prove one generation per dispatch")
    return {
        "status": "PASS",
        "source": "provider",
        "degraded": False,
        "unique_dispatches": unique_dispatches,
        "total_generations": total_generations,
        "max_generation_count": 1,
        "response_body_hashes": int(provider_row["response_hashes"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", required=True)
    arguments = parser.parse_args()
    try:
        value = asyncio.run(verify(arguments.database_url))
    except Exception as error:
        print(
            "INT1_REAL_PROVIDER_RELAY_FAIL "
            + json.dumps(
                {"status": "FAIL", "reason": str(error)},
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return 1
    print(
        "INT1_REAL_PROVIDER_RELAY_PASS "
        + json.dumps(value, separators=(",", ":"), sort_keys=True)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
