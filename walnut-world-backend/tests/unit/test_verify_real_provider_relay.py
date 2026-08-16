from __future__ import annotations

import pytest

from scripts import verify_real_provider_relay


@pytest.mark.parametrize(
    ("skill_patch_enabled", "expected"),
    (("false", (12, 24)), ("true", (16, 32))),
)
def test_dispatch_bounds_follow_the_formal_m1_m2_feature_gate(
    monkeypatch: pytest.MonkeyPatch,
    skill_patch_enabled: str,
    expected: tuple[int, int],
) -> None:
    monkeypatch.setenv("WALNUT_ENABLE_SKILL_PATCH", skill_patch_enabled)

    assert verify_real_provider_relay._formal_dispatch_bounds() == expected


@pytest.mark.parametrize("value", (None, "", "TRUE", "1"))
def test_dispatch_bounds_reject_an_ambiguous_formal_mode(
    monkeypatch: pytest.MonkeyPatch,
    value: str | None,
) -> None:
    if value is None:
        monkeypatch.delenv("WALNUT_ENABLE_SKILL_PATCH", raising=False)
    else:
        monkeypatch.setenv("WALNUT_ENABLE_SKILL_PATCH", value)

    with pytest.raises(RuntimeError, match="WALNUT_ENABLE_SKILL_PATCH"):
        verify_real_provider_relay._formal_dispatch_bounds()
