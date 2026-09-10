from agent_runtime_fixtures import make_event
from yaya_agent_runtime import RoleRouter


def test_explicit_hint_always_calls_dingdang_even_after_repeated_failures():
    for count in (0, 1, 3, 5, 6, 10):
        assert (
            RoleRouter().route(make_event("hint_requested", failure_count=count)).role
            == "teaching_agent"
        )
