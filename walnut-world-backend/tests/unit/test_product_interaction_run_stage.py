"""Product Interaction authority derives failure stage from canonical Run fields."""

from __future__ import annotations

from walnut_backend.adapters.postgres.product_interactions import (
    _expected_terminal_command_stage,
)


def test_failed_sandbox_status_closes_to_sandbox_command_stage() -> None:
    assert (
        _expected_terminal_command_stage(
            {"status": "FAILED", "sandbox": {"status": "FAILED"}}
        )
        == "SANDBOX"
    )
    assert (
        _expected_terminal_command_stage(
            {"status": "REJECTED", "sandbox": {"status": "TIMED_OUT"}}
        )
        == "SANDBOX"
    )


def test_world_rejection_and_success_close_to_their_exact_command_stages() -> None:
    assert (
        _expected_terminal_command_stage(
            {"status": "REJECTED", "sandbox": {"status": "SUCCEEDED"}}
        )
        == "WORLD_VALIDATE"
    )
    assert (
        _expected_terminal_command_stage(
            {"status": "SUCCEEDED", "sandbox": {"status": "SUCCEEDED"}}
        )
        == "COMPLETE"
    )
