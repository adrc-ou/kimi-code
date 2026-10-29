"""Qualification bookkeeping: what a run is permitted to conclude.

The HTTP against a live engine cannot be exercised here, so these tests pin the part that carries
the risk: a run that could not size its prompt, could not read a context length, or failed a case
must not produce a document that reads as a promotion signal.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for _entry in (str(ROOT / "tools"), str(ROOT / "proxy")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

import local_qualification as qualify  # noqa: E402

OK = {
    "saw_error_object": False,
    "content_characters": 20,
    "reasoning_characters": 0,
    "tool_calls": 0,
    "finish_reason": "stop",
    "tokens_per_second": 14.0,
    "usage": {"completion_tokens": 8},
    "seconds_total": 1.0,
    "seconds_to_first_token": 0.2,
}


def answer(**changes):
    return {**OK, **changes}


class SizingTests(unittest.TestCase):
    def test_a_request_that_cannot_be_sized_is_a_failure_not_an_approximation(self):
        # Qualifying against a prompt that is not the intended length would report a pass at the
        # wrong size, which is how a near-ceiling claim ends up resting on a short request.
        with mock.patch.object(qualify, "measure_tokens", return_value=1):
            with self.assertRaisesRegex(qualify.QualificationError, "could not size"):
                qualify.fit_request(("h", 1, "t"), 4096, tools=False)

    def test_a_pathological_count_cannot_blow_up_the_prompt(self):
        # The correction adjusts a unit count and is clamped. Scaling the existing string by
        # target/observed would compound once per pass and reach gigabytes of text here.
        seen = []

        def always_one(origin, payload):
            seen.append(len(payload["messages"][0]["content"]))
            return 1

        with mock.patch.object(qualify, "measure_tokens", side_effect=always_one):
            with self.assertRaises(qualify.QualificationError):
                qualify.fit_request(("h", 1, "t"), 4096, tools=False)
        ceiling = len(qualify._FILLER_UNIT) * qualify._MAX_FILLER_UNITS
        self.assertLessEqual(max(seen), ceiling)

    def test_sizing_converges_when_the_engine_agrees(self):
        counts = iter([100, 4090])
        with mock.patch.object(qualify, "measure_tokens", side_effect=lambda _o, _p: next(counts)):
            payload, count = qualify.fit_request(("h", 1, "t"), 4096, tools=False)
        self.assertEqual(count, 4090)
        self.assertTrue(payload["messages"][0]["content"])


class DeclaredContextTests(unittest.TestCase):
    def test_a_missing_status_means_no_near_ceiling_target_rather_than_a_guess(self):
        with mock.patch.object(qualify, "_post", side_effect=OSError("unreachable")):
            self.assertIsNone(qualify._declared_context(("h", 1, "t")))

    def test_a_boolean_context_length_is_not_a_number_of_tokens(self):
        with mock.patch.object(qualify, "_post", return_value={"context_tokens": True}):
            self.assertIsNone(qualify._declared_context(("h", 1, "t")))


class VerdictTests(unittest.TestCase):
    def _run(self, *, statuses=None, near=None, declared=4096, skip_near=False):
        def fake_fit(origin, target, *, tools):
            payload = {"messages": [{"role": "user", "content": "x"}], "stream": True}
            if tools:
                payload["tools"] = [{"type": "function"}]
            return payload, target

        def fake_timed(origin, payload, timeout):
            # The engine answers a tool request with a call. Tests that want the opposite say so
            # explicitly rather than relying on a shared stub that cannot tell the cases apart.
            return answer(tool_calls=1 if payload.get("tools") else 0,
                          content_characters=0 if payload.get("tools") else 20)

        def fake_cancel(origin, payload, after):
            return {"slot_recovered": True, "cancelled_after_seconds": 0.1, "recovery_seconds": 0.2}

        with (
            mock.patch.object(qualify, "_engine_origin", return_value=("h", 1, "t")),
            mock.patch.object(qualify, "fit_request", side_effect=fake_fit),
            mock.patch.object(qualify, "timed_generation", side_effect=fake_timed),
            mock.patch.object(qualify, "cancel_during", side_effect=fake_cancel),
            mock.patch.object(
                qualify, "_declared_context", return_value=declared
            ),
        ):
            argv = ["--skip-near-ceiling"] if skip_near else []
            return qualify.run(argv)

    def test_a_clean_run_permits_promotion(self):
        result = self._run()
        self.assertTrue(result["passes"], result["failures"])
        self.assertTrue(result["promotion_permitted"])
        self.assertIn("near_ceiling", result["cases"])

    def test_a_silent_engine_on_a_short_request_stops_everything_after_it(self):
        with (
            mock.patch.object(qualify, "_engine_origin", return_value=("h", 1, "t")),
            mock.patch.object(
                qualify, "fit_request", side_effect=lambda o, t, *, tools: ({"messages": [
                    {"role": "user", "content": "x"}]}, t)
            ),
            mock.patch.object(
                qualify, "timed_generation",
                return_value=answer(content_characters=0, tool_calls=0),
            ),
        ):
            result = qualify.run([])
        self.assertFalse(result["passes"])
        self.assertFalse(result["promotion_permitted"])
        # It stopped at the first case rather than reporting on a further four.
        self.assertEqual(sorted(result["cases"]), ["short_completion"])

    def test_an_error_object_inside_a_200_is_a_failure(self):
        with (
            mock.patch.object(qualify, "_engine_origin", return_value=("h", 1, "t")),
            mock.patch.object(
                qualify, "fit_request", side_effect=lambda o, t, *, tools: ({"messages": [
                    {"role": "user", "content": "x"}]}, t)
            ),
            mock.patch.object(
                qualify, "timed_generation", return_value=answer(saw_error_object=True)
            ),
        ):
            result = qualify.run([])
        self.assertFalse(result["passes"])

    def test_a_tool_turn_that_returns_prose_instead_of_a_call_fails(self):
        # The shape a server produces when it accepted the tools array and parsed with a template
        # that does not emit calls: a perfectly ordinary completion, and no tool call anywhere.
        def prose(origin, payload, timeout):
            return answer(tool_calls=0, content_characters=40)

        with (
            mock.patch.object(qualify, "_engine_origin", return_value=("h", 1, "t")),
            mock.patch.object(
                qualify, "fit_request",
                side_effect=lambda o, t, *, tools: (
                    ({"messages": [{"role": "user", "content": "x"}], "stream": True,
                     "tools": [{}] if tools else None}),
                    t,
                ),
            ),
            mock.patch.object(qualify, "timed_generation", side_effect=prose),
            mock.patch.object(qualify, "cancel_during"),
            mock.patch.object(qualify, "_declared_context", return_value=4096),
        ):
            result = qualify.run([])
        self.assertFalse(result["passes"])
        self.assertTrue(any("tool_use" in item for item in result["failures"]))

    def test_no_declared_context_blocks_the_near_ceiling_case(self):
        result = self._run(declared=None)
        self.assertFalse(result["passes"])
        self.assertTrue(any("no context length" in item for item in result["failures"]))

    def test_skipping_the_near_ceiling_case_is_recorded_not_silently_passed(self):
        result = self._run(skip_near=True)
        kinds = [item["kind"] for item in result["observations"]]
        self.assertIn("near_ceiling_skipped", kinds)

    def test_throughput_spread_is_an_observation_and_never_a_failure(self):
        # A slow measurement is evidence for the operator; failing a promotion on one noisy sample
        # would be the "silently reduce context" error wearing a different costume.
        result = self._run()
        kinds = [item["kind"] for item in result["observations"]]
        self.assertIn("throughput_spread_across_identical_turns", kinds)
        self.assertFalse(
            any("throughput" in item for item in result["failures"])
        )


if __name__ == "__main__":
    unittest.main()
