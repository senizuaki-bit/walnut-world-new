"""Dingdang's voice and text questions use the same teaching policy."""

import sys
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_runtime_fixtures import NOW, make_evidence, make_learner_profile, make_task  # noqa: E402
from yaya_agent_runtime.conversation_teaching import (  # noqa: E402
    conversation_directive,
    conversation_guidance,
)


class ConversationTeachingTests(unittest.TestCase):
    def test_voice_teaching_levels_follow_failure_count(self):
        for count, level, phase in [
            (0, 1, "REVIEW"),
            (1, 1, "RECTIFICATION"),
            (2, 2, "RECTIFICATION"),
            (3, 3, "RECTIFICATION"),
            (5, 3, "RECTIFICATION"),
        ]:
            with self.subTest(count=count):
                directive = conversation_directive(
                    make_task(),
                    make_learner_profile(),
                    failure_count=count,
                    evidence_refs=(make_evidence(),) if count else (),
                    event_time=NOW,
                    teaching_spec_version="teaching-1",
                )
                self.assertEqual(directive.hint_level, level)
                self.assertEqual(directive.phase.value, phase)
                self.assertFalse(directive.patch_eligible or directive.full_solution_eligible)
                self.assertIn("message", directive.allowed_response_types)
                self.assertIn(f"失败次数={count}", conversation_guidance(directive, count))

    def test_repeated_voice_questions_do_not_raise_the_level_and_task_cap_applies(self):
        args = dict(
            failure_count=3,
            evidence_refs=(make_evidence(),),
            event_time=NOW,
            teaching_spec_version="teaching-1",
        )
        task = replace(make_task(), max_hint_level=1)
        learner = make_learner_profile()
        first = conversation_directive(task, learner, **args)
        self.assertEqual(conversation_directive(task, learner, **args), first)
        self.assertEqual(first.hint_level, 1)
