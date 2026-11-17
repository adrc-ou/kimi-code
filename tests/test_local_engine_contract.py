"""The shared local-engine vocabulary, and the two-observer authority split it encodes.

These tests deliberately load ``proxy/local_engine_contract.py`` by path rather than through
``tests.test_model_proxy``, because the whole reason the module exists apart from the proxy is that
the proxy imports aiohttp at module scope and the host tools do not have it. A test of this
vocabulary that skipped on a host without aiohttp would skip on exactly the machines that need it,
which is the failure mode the split is meant to prevent.

The behavior under test is externally observable in only one direction: given what a stream showed,
what does a caller conclude. So every case here states an input the wire could actually produce and
an outcome a proxy or a worker would report, and nothing asserts a private helper.
"""

import ast
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.helpers import load_script  # noqa: E402

# Loaded by path, stdlib only: this must import on a host with no aiohttp installed, which is the
# whole reason the vocabulary lives outside the proxy rather than inside it.
contract = load_script("local_engine_contract", Path("proxy/local_engine_contract.py"))

Evidence = contract.StreamEvidence


def usable(outcome) -> bool:
    return contract.is_usable_completion(outcome)


class ImportBoundaryTests(unittest.TestCase):
    """The module is only worth having if the host side can actually reach it.

    Asserted against the parsed import statements rather than substrings of the file: the module's
    docstring discusses aiohttp and the proxy at length, and a check that tripped on that prose
    would be a test of wording instead of a test of the dependency graph.
    """

    def _top_level_modules(self) -> set[str]:
        tree = ast.parse(
            (ROOT / "proxy" / "local_engine_contract.py").read_text(encoding="utf-8")
        )
        found: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                found.add(node.module.split(".")[0])
        return found

    def test_it_imports_only_the_standard_library(self):
        # Anything outside this set is a dependency the launcher's python3 does not have.
        allowed = {
            "__future__",
            "dataclasses",
            "enum",
            "typing",
            "local_engine_contract",
        }
        self.assertFalse(self._top_level_modules() - allowed)

    def test_it_does_not_import_the_proxy_or_an_engine_it_describes(self):
        imported = self._top_level_modules()
        for forbidden in ("aiohttp", "model_proxy", "mlx", "mlx_lm"):
            self.assertNotIn(forbidden, imported)

    def test_the_host_side_can_execute_it_with_no_third_party_packages_present(self):
        # The claim this file makes is that tools/ and the worker can import it. Prove it in a
        # process where the proxy's own dependency genuinely cannot resolve.
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys, importlib.util;"
                "sys.modules['aiohttp']=None;"
                "spec=importlib.util.spec_from_file_location('lec', "
                f"{str(ROOT / 'proxy' / 'local_engine_contract.py')!r});"
                "m=importlib.util.module_from_spec(spec);sys.modules['lec']=m;"
                "spec.loader.exec_module(m);print('ok')",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertIn("ok", result.stdout, result.stderr)


class CompletionSaysWhetherTheCallerGotAnAnswerTests(unittest.TestCase):
    """The distinction that failed in the recorded incident was usable vs. merely successful."""

    def test_prose_arrived(self):
        self.assertEqual(
            contract.classify(Evidence(content_chars=11, terminated=True)),
            contract.Outcome.CONTENT,
        )

    def test_a_tool_call_is_a_valid_answer_even_with_no_prose(self):
        # The plan's explicit requirement: a tool-only response must not be filed as an empty one.
        outcome = contract.classify(Evidence(tool_calls=2, terminated=True))
        self.assertEqual(outcome, contract.Outcome.TOOL_ONLY)
        self.assertTrue(usable(outcome))

    def test_prose_and_tools_together_are_still_an_answer(self):
        outcome = contract.classify(
            Evidence(content_chars=40, tool_calls=1, terminated=True)
        )
        self.assertEqual(outcome, contract.Outcome.CONTENT)
        self.assertTrue(usable(outcome))

    def test_thinking_with_no_answer_behind_it_is_not_usable(self):
        outcome = contract.classify(Evidence(reasoning_chars=900, terminated=True))
        self.assertEqual(outcome, contract.Outcome.REASONING_ONLY)
        self.assertFalse(usable(outcome))

    def test_a_stream_cut_off_at_the_output_bound_is_truncation_not_success(self):
        outcome = contract.classify(
            Evidence(content_chars=50, terminated=True, finish_reason="length")
        )
        self.assertEqual(outcome, contract.Outcome.TRUNCATED)
        self.assertFalse(usable(outcome))

    def test_a_clean_stream_that_produced_nothing_is_empty(self):
        outcome = contract.classify(Evidence(terminated=True))
        self.assertEqual(outcome, contract.Outcome.EMPTY)
        self.assertFalse(usable(outcome))

    def test_terminology_that_never_ended_did_not_produce_a_completion(self):
        self.assertEqual(
            contract.classify(Evidence(content_chars=5, terminated=False)),
            contract.Outcome.TRUNCATED,
        )
        self.assertEqual(
            contract.classify(Evidence(terminated=False)),
            contract.Outcome.FRAMING_FAILURE,
        )


class DamageIsNotTheSameQuestionTests(unittest.TestCase):
    """Latching an engine dead is the expensive action; it must not follow from a bad prompt."""

    def test_an_empty_completion_is_not_evidence_the_device_broke(self):
        # The plan states this outright: immediate EOS is not automatically a fatal backend fault.
        self.assertFalse(contract.implies_engine_damage(contract.Outcome.EMPTY))
        self.assertFalse(contract.implies_engine_damage(contract.Outcome.REASONING_ONLY))
        self.assertFalse(contract.implies_engine_damage(contract.Outcome.TRUNCATED))

    def test_an_error_object_inside_a_200_is_evidence_of_damage(self):
        self.assertTrue(contract.implies_engine_damage(contract.Outcome.ERROR_OBJECT))

    def test_unparseable_framing_is_evidence_of_damage(self):
        self.assertTrue(contract.implies_engine_damage(contract.Outcome.FRAMING_FAILURE))

    def test_an_error_object_outranks_the_text_that_arrived_before_it(self):
        outcome = contract.classify(
            Evidence(content_chars=30, saw_error_object=True, terminated=True)
        )
        self.assertEqual(outcome, contract.Outcome.ERROR_OBJECT)

    def test_a_framing_failure_outranks_the_content_it_corrupted(self):
        outcome = contract.classify(
            Evidence(content_chars=4000, saw_framing_failure=True, terminated=True)
        )
        self.assertEqual(outcome, contract.Outcome.FRAMING_FAILURE)

    def test_an_unusable_but_intact_completion_names_no_failure_class(self):
        # Reporting "not usable" and reporting "the engine failed" are separate statements.
        for outcome in (
            contract.Outcome.EMPTY,
            contract.Outcome.REASONING_ONLY,
            contract.Outcome.TRUNCATED,
        ):
            self.assertIsNone(contract.partition_for_outcome(outcome), outcome)

    def test_an_unknown_outcome_is_unavailable_until_proven_recoverable(self):
        self.assertEqual(
            contract.partition_for_outcome(contract.Outcome.UNKNOWN),
            contract.FailureClass.FATAL,
        )


class RequestVersusTransportTests(unittest.TestCase):
    def test_a_4xx_is_the_request_being_refused_not_the_engine_being_dead(self):
        for status in (400, 404, 413, 422):
            self.assertEqual(
                contract.partition_for_status(status),
                contract.FailureClass.INVALID,
                f"status {status}",
            )

    def test_429_is_rate_and_not_a_refusal_of_the_request_body(self):
        self.assertEqual(
            contract.partition_for_status(429), contract.FailureClass.TRANSIENT
        )

    def test_server_side_statuses_are_transient_until_recovery_is_shown(self):
        for status in (500, 502, 503, 504):
            self.assertEqual(
                contract.partition_for_status(status),
                contract.FailureClass.TRANSIENT,
                f"status {status}",
            )

    def test_a_body_that_rejects_the_payload_is_invalid_whatever_the_status(self):
        self.assertEqual(
            contract.partition_for_status(500, body_rejects=True),
            contract.FailureClass.INVALID,
        )


class TwoObserverAuthorityTests(unittest.TestCase):
    """Neither observer may overrule the other on the question only it can see.

    These used to be six cases over a ``reconcile(declared, inferred)`` helper. The helper is gone:
    nothing in the engine path ever declares an outcome, so half of every assertion here was
    exercising a parameter that production could only ever pass as ``None``. What actually enforces
    the split now is ``partition_for_outcome`` on the proxy side and the worker's own fatal
    reporting, and the authority rule that matters — a divergence-shaped observation must not latch
    — is covered where the latch lives, in tests/test_local_proxy.py.
    """

    def test_genuine_damage_a_proxy_observes_still_names_itself(self):
        # Deleting the reconciliation helper must not have softened the rule it was built around:
        # framing damage observed on the wire is still fatal, and still the thing that latches.
        self.assertEqual(
            contract.partition_for_outcome(contract.Outcome.FRAMING_FAILURE),
            contract.FailureClass.FATAL,
        )
        self.assertEqual(
            contract.partition_for_outcome(contract.Outcome.ERROR_OBJECT),
            contract.FailureClass.FATAL,
        )
        # And an intact-but-unusable completion is still not a failure class, so it cannot latch.
        self.assertIsNone(contract.partition_for_outcome(contract.Outcome.EMPTY))

    def test_every_classifiable_member_is_accounted_for(self):
        for outcome in contract.OUTCOMES:
            contract.partition_for_outcome(outcome)
            self.assertIn(usable(outcome), (True, False), outcome)


class VocabularyIsStableOnTheWireTests(unittest.TestCase):
    """These strings land in proxy event records an operator greps for.

    AIP-193's rule that a reason once observed by a consumer keeps being present applies to any
    machine-readable label, including one this narrow: renaming a member silently breaks every
    saved log, dashboard filter and test assertion that matched the old spelling.
    """

    def test_member_spellings(self):
        self.assertEqual(
            {str(member.value) for member in contract.Outcome},
            {
                "content",
                "tool_only",
                "reasoning_only",
                "truncated",
                "empty",
                "error_object",
                "framing_failure",
                "unknown",
            },
        )

    def test_failure_classes_are_the_three_the_plan_names(self):
        self.assertEqual(
            {str(member.value) for member in contract.FailureClass},
            {"fatal", "invalid", "transient"},
        )


if __name__ == "__main__":
    unittest.main()
