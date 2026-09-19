#!/usr/bin/env python3
"""Each generated block has to stand alone.

The startup panel lets an operator switch any block off on its own, so a block that leans on a
sibling for its numbers becomes a hole in the prompt the moment that sibling is unchecked. This
file is the test that makes that impossible to ship by accident: every block that produces text
must appear in the required-facts table below, and must state each fact from the resolved plan
without help.

It is deliberately written against the plan's values rather than literals. A hard-coded "5" would
pass on this machine and mean nothing on another.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# The repository root as well as tools/: unittest only puts the start directory on the path, not
# its parent, so `tests.helpers` needs the root added explicitly to resolve under every way of
# running the suite - `discover -s tests`, `python -m unittest tests.test_x`, and a bare
# `python -m unittest test_x` from inside tests/.
for _directory in (ROOT, ROOT / "tools"):
    if str(_directory) not in sys.path:
        sys.path.insert(0, str(_directory))

import policy  # noqa: E402
import prompt_context as pc  # noqa: E402
import prompt_measure as pm  # noqa: E402

from tests.helpers import shipped_plan  # noqa: E402

PLAN = shipped_plan()
#: What a reader must be able to learn from this block and no other, keyed by option id. Values
#: are callables so each one can be derived from the resolved plan instead of being typed in.
REQUIRED: dict[str, list[tuple[str, str]]] = {}


def _budgets() -> list[tuple[str, str]]:
    out = []
    for counter in PLAN["counters"].values():
        if counter["family"] == "context":
            out.append((f"{counter['budget']:,} tokens", "shared in-flight context budget"))
            if counter.get("exclusive_at") is not None:
                out.append((f"{counter['exclusive_at']:,} tokens", "exclusive-request threshold"))
        if counter["family"] == "rate":
            unit = policy.RATE_UNIT_LABELS.get(counter.get("unit", ""), "")
            out.append((f"{counter['capacity']:,} {unit}".strip(), "per-minute rate"))
    return out


def _lane_sizes(short: bool) -> list[tuple[str, str]]:
    out = []
    for name in policy._lane_order(PLAN):
        lane = PLAN["lanes"][name]
        out.append((f"{lane['context_tokens']:,}", f"`{name}` window"))
        out.append((f"{lane['input_tokens']:,}", f"`{name}` input cap"))
        out.append((f"{lane['output_clamp_tokens']:,}", f"`{name}` output clamp"))
        if not short:
            out.append((f"`{lane['alias']}`", f"`{name}` alias"))
            out.append((f"{lane['reservation']:,}", f"`{name}` in-flight cost"))
    return out


def _fanout() -> list[tuple[str, str]]:
    limit = PLAN["limits"].get("subagent_concurrency")
    return [(f"{limit} subagents", "the published fan-out ceiling")] if limit else []


def _providers() -> list[tuple[str, str]]:
    return [
        (provider["label"], "provider label")
        for provider in PLAN["providers"].values() if provider.get("label")
    ] + [
        (provider["policy_url"], "provider policy URL")
        for provider in PLAN["providers"].values() if provider.get("policy_url")
    ]


# The all-lane block publishes no fan-out ceiling at all, so it must not be asked to explain one.
REQUIRED[policy.OPTION_LANE_LIMITS] = _providers() + _budgets() + _lane_sizes(short=True)
REQUIRED[policy.OPTION_LANE_TABLE] = (
    _lane_sizes(short=False) + _budgets() + _fanout()
    + [(provider["label"], "provider label") for provider in PLAN["providers"].values()
       if provider.get("label")]
)
REQUIRED[policy.OPTION_PARALLELISM] = _fanout() + [
    (PLAN["limits"].get("subagent_concurrency_basis") or "", "why the ceiling is what it is")
]

#: Wording that points at another block instead of stating its own case. A block is allowed to
#: name a section it is not part of, but it may not depend on one being present to be understood.
DANGLING = (
    r"\bsee (?:the )?(?:section|table|envelope) above\b",
    r"\bas published above\b",
    r"\bin the envelope above\b",
    r"\babove section\b",
    r"\bthe rest of this (?:document|prompt) says\b",
)


def block_for(option: str) -> str:
    for audience in policy.GUIDANCE_AUDIENCES:
        for section in policy.guidance_sections(PLAN, audience):
            if section.option == option:
                return policy.guidance_block(section)
    raise AssertionError(f"no generated block for option {option}")


class IsolationTests(unittest.TestCase):
    def test_every_generated_block_declares_what_only_it_can_say(self):
        generated = {
            section.option
            for audience in policy.GUIDANCE_AUDIENCES
            for section in policy.guidance_sections(PLAN, audience)
        }
        self.assertEqual(generated, set(REQUIRED),
                         "a new block needs a required-facts entry, not a free pass")

    def test_a_block_rendered_alone_still_states_its_own_figures(self):
        for option, facts in REQUIRED.items():
            text = block_for(option)
            for needle, why in facts:
                with self.subTest(option=option, fact=why):
                    self.assertIn(needle, text, f"{option} borrows {why} from a sibling")

    def test_no_block_delegates_its_meaning_to_a_sibling(self):
        for option in REQUIRED:
            text = block_for(option)
            for pattern in DANGLING:
                with self.subTest(option=option, pattern=pattern):
                    self.assertIsNone(re.search(pattern, text, re.IGNORECASE))

    def test_disabling_one_block_cannot_silence_another_ones_figures(self):
        # The composer renders each enabled block on its own, so switching a sibling off has to
        # leave the survivor byte-identical. Anything else means the panel's prices are wrong.
        for option in REQUIRED:
            enabled = dict.fromkeys(pc.OPTION_IDS, False)
            enabled[option] = True
            audience = next(
                section.audience
                for name in policy.GUIDANCE_AUDIENCES
                for section in policy.guidance_sections(PLAN, name)
                if section.option == option
            )
            alone = policy.render_guidance(PLAN, audience, enabled)
            with_self = policy.render_guidance(PLAN, audience)
            self.assertEqual(alone.strip(), block_for(option).strip())
            self.assertIn(alone, with_self, "a sibling switching on rewrote this block")

    def test_the_lane_block_is_free_of_main_only_instructions(self):
        lane = block_for(policy.OPTION_LANE_LIMITS)
        self.assertNotIn("AgentSwarm", lane)
        self.assertNotIn("subagents concurrently", lane)
        self.assertNotIn("`qwen3", lane)
        self.assertIn("cannot delegate further", lane)

    def test_the_fanout_block_carries_its_ceiling_rather_than_referring_to_the_table(self):
        text = block_for(policy.OPTION_PARALLELISM)
        limit = PLAN["limits"]["subagent_concurrency"]
        self.assertIn(f"{limit} subagents", text)
        self.assertIn(f"{limit} as the default fan-out", text)

    def test_the_two_primary_blocks_share_no_text_so_no_tokens_are_paid_twice(self):
        limits = block_for(policy.OPTION_LANE_LIMITS)
        table = block_for(policy.OPTION_LANE_TABLE)
        shared = {line for line in limits.splitlines() if line.strip() and line in table}
        self.assertEqual(shared, set(), "identical lines would be paid for twice")


class AudienceTests(unittest.TestCase):
    def test_the_lane_audience_gets_the_core_and_nothing_else(self):
        sections = policy.guidance_sections(PLAN, "lane")
        self.assertEqual([s.option for s in sections], [policy.OPTION_LANE_LIMITS])
        self.assertEqual({s.audience for s in sections}, {"lane"})

    def test_the_main_audience_gets_the_table_and_the_fanout(self):
        sections = policy.guidance_sections(PLAN, "main")
        self.assertEqual(
            [s.option for s in sections],
            [policy.OPTION_LANE_TABLE, policy.OPTION_PARALLELISM],
        )

    def test_an_unknown_audience_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            policy.guidance_sections(PLAN, "everyone")

    def test_switching_a_block_off_removes_it_and_leaves_the_rest_contiguous(self):
        enabled = dict(pc.DEFAULT_ENABLED)
        enabled[policy.OPTION_PARALLELISM] = False
        text = policy.render_guidance(PLAN, "main", enabled)
        self.assertNotIn("## Parallel work", text)
        self.assertIn("## Model runtime envelope", text)
        self.assertTrue(text.endswith("\n"))

    def test_switching_everything_off_costs_exactly_nothing(self):
        off = dict.fromkeys(pc.OPTION_IDS, False)
        for audience in policy.GUIDANCE_AUDIENCES:
            with self.subTest(audience=audience):
                self.assertEqual(policy.render_guidance(PLAN, audience, off), "")

    def test_an_empty_plan_publishes_no_ceiling_instead_of_inventing_one(self):
        bare = {"lanes": {}, "counters": {}, "providers": {}, "limits": {}}
        for audience in policy.GUIDANCE_AUDIENCES:
            with self.subTest(audience=audience):
                text = policy.render_guidance(bare, audience)
                self.assertNotIn("None", text)
                self.assertNotIn("nan", text)
                if audience == "main":
                    self.assertIn("no subagent ceiling", text)


class ProseTests(unittest.TestCase):
    def test_every_block_is_markdown_that_starts_with_a_heading(self):
        for option in REQUIRED:
            text = block_for(option)
            with self.subTest(option=option):
                self.assertTrue(text.startswith("## "))
                self.assertTrue(text.strip())
                # guidance_block trims its edges and the composer joins with a blank line, so a
                # block must neither start nor end fused.
                self.assertFalse(text.startswith("\n"))
                self.assertFalse(text.endswith("\n"))
                self.assertNotIn("\n\n\n", text)

    def test_no_block_refers_to_a_file_the_harness_no_longer_owns(self):
        # The envelope used to live in the user's workspace AGENTS.md, which the harness now
        # leaves alone; prose that still points there teaches the agent to look in a file that
        # will not contain it.
        for option in REQUIRED:
            text = block_for(option)
            with self.subTest(option=option):
                self.assertNotIn("workspace `AGENTS.md`", text)
                self.assertNotIn("workspace AGENTS.md", text)

    def test_a_table_row_never_lies_about_a_lane_that_does_not_exist(self):
        table = block_for(policy.OPTION_LANE_TABLE)
        for name in policy.LANES:
            row = re.search(rf"^\| `{name}` \|.*\|$", table, re.MULTILINE)
            if name in PLAN["lanes"]:
                self.assertIsNotNone(row, f"{name} is in the plan but not the table")
            else:
                self.assertIsNone(row)



class SizeFenceTests(unittest.TestCase):
    """The trim's acceptance figure, kept as a fence rather than a footnote.

    Two units are in play and a fence is only honest if it names the one it holds. The figures in
    `docs/prompts.md` are *measured* regions of a prompt a live Kimi actually bound - 2,379 tokens
    of staged contract for a subagent. Everything computable without a container is the *estimate*
    over the bytes this checkout composes, which for that same document runs a little higher and
    moves with the workspace too. So these thresholds sit above both, loose enough to be a
    regression detector rather than a weather report and tight enough that restoring the
    hand-written provider-policy section this harness deleted - 1,142 tokens - would trip one.
    """

    #: Tokens of harness-written contract text that every lane pays, comments stripped.
    CONTRACT_TOKENS = 2_150

    #: Tokens of harness-written main-prompt text, which is the wrapper plus the main-only blocks.
    PROMPT_TOKENS = 1_300

    def contract(self, extra: str = "") -> str:
        """The shipped all-lane document with no module guidance, plus any bulk under test.

        Module guidance is whatever the selected modules happen to ship, so bounding it here would
        be bounding someone else's prose. The harness's own contribution is the part with a fence.
        """
        document = pc.compose_agents_document(ROOT, PLAN, "", dict(pc.DEFAULT_ENABLED))
        return document + extra

    def test_the_contract_every_lane_carries_stays_bounded(self):
        estimate = pm.estimate_tokens(self.contract())
        self.assertLessEqual(estimate, self.CONTRACT_TOKENS)
        self.assertGreater(estimate, self.CONTRACT_TOKENS - 400, "a loose fence catches nothing")

    def test_the_main_agent_still_pays_only_its_own_two_blocks(self):
        """A subagent receives no system prompt, so anything that grows here is main-only cost."""
        document = pc.compose_system_document(ROOT, PLAN, dict(pc.DEFAULT_ENABLED))
        estimate = pm.estimate_tokens(document)
        self.assertLessEqual(estimate, self.PROMPT_TOKENS)
        self.assertGreater(estimate, self.PROMPT_TOKENS - 600, "a loose fence catches nothing")

    def test_the_fence_is_breachable_or_it_is_not_a_test(self):
        bulk = "\n## Managed model provider policy\n" + "prose the envelope replaced. " * 200
        self.assertGreater(pm.estimate_tokens(self.contract(bulk)), self.CONTRACT_TOKENS)

    def test_switching_every_block_off_leaves_only_the_operators_contract(self):
        """The floor is the deleted file's own size, which is what makes the blocks measurable."""
        document = pc.compose_agents_document(
            ROOT, PLAN, "", dict.fromkeys(pc.OPTION_IDS, False)
        )
        default = (ROOT / "runtime" / "AGENTS.md").read_text(encoding="utf-8")
        self.assertEqual(
            pm.estimate_tokens(document), pm.estimate_tokens(pc.strip_html_comments(default))
        )
        self.assertLess(pm.estimate_tokens(document), self.CONTRACT_TOKENS)


if __name__ == "__main__":
    unittest.main()
