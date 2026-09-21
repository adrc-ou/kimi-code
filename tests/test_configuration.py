import json
import re
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# The repository root as well as tools/, so that `tests.helpers` resolves under every way of
# running the suite; this file also shells out to the tools, so it needs both on the path.
for _directory in (ROOT, ROOT / "tools"):
    if str(_directory) not in sys.path:
        sys.path.insert(0, str(_directory))

from tests.helpers import shipped_plan  # noqa: E402


def _front_matter_list(text: str, key: str) -> list[str]:
    """Read one ``key:`` sequence out of a role file's front matter.

    The files are YAML, but PyYAML is not a dependency of this repository and a role listing is
    a flat block of ``- item`` lines, so reading it directly is cheaper than acquiring one.
    """
    body = text.split("---", 2)
    if len(body) < 3:
        raise AssertionError("role file has no front matter")
    entries: list[str] = []
    inside = False
    for line in body[1].splitlines():
        if not line.startswith(" "):
            inside = line.strip() == f"{key}:"
            continue
        if inside and line.strip().startswith("- "):
            entries.append(line.strip()[2:].strip())
    return entries


class ConfigurationTests(unittest.TestCase):
    def test_checked_in_json_and_toml_parse(self):
        for path in ROOT.rglob("*.json"):
            if "node_modules" not in path.parts and ".local" not in path.parts:
                json.loads(path.read_text())
        for path in ROOT.rglob("*.toml"):
            if ".local" in path.parts:
                continue
            with path.open("rb") as source:
                tomllib.load(source)

    def test_browser_sandbox_is_not_disabled(self):
        document = json.loads((ROOT / "runtime" / "mcp.json").read_text())
        args = document["mcpServers"]["chrome-devtools"]["args"]
        self.assertFalse(any("no-sandbox" in arg for arg in args))
        profile = json.loads((ROOT / "container" / "chromium-seccomp.json").read_text())
        allowed = [
            call
            for call in profile["syscalls"]
            if call.get("action") == "SCMP_ACT_ALLOW"
            and {"clone", "chroot", "setns", "unshare"}.issubset(call.get("names", []))
            and not call.get("includes")
        ]
        self.assertTrue(allowed)

    def test_every_role_grants_only_an_enabled_mcp_server(self):
        """A grant naming a disabled server is not inert - it is a hole in the role's contract.

        ``repo-researcher`` shipped four ``mcp__*`` wildcards for three servers that were
        switched off or absent, so the role advertised reach it did not have and the panel
        could not tell the operator which tools were real.
        """
        document = json.loads((ROOT / "runtime" / "mcp.json").read_text())
        enabled = {
            name for name, server in document["mcpServers"].items() if server.get("enabled")
        }
        self.assertTrue(enabled)
        for path in sorted((ROOT / "runtime" / "agents").glob("*.md")):
            for grant in _front_matter_list(path.read_text(), "tools"):
                if not grant.startswith("mcp__"):
                    continue
                server = grant.split("__")[1]
                self.assertIn(
                    server, enabled, f"{path.name} grants the disabled server {server!r}"
                )

    def test_context7_is_mountable_without_a_credential(self):
        """A server documented as keyless must not name a bearer variable.

        Kimi's remote MCP client throws CONFIG_INVALID when ``bearerTokenEnvVar`` names a variable
        that is unset or empty, and ``.env.example`` ships ``CONTEXT7_API_KEY=`` blank. Naming the
        variable therefore removed every Context7 tool from the session while the anonymous probe,
        which does not apply Kimi's gate, still reported a pass.
        """
        document = json.loads((ROOT / "runtime" / "mcp.json").read_text())
        server = document["mcpServers"]["context7"]
        self.assertTrue(server["enabled"], "Context7 needs no credential, so nothing gates it off")
        self.assertNotIn("bearerTokenEnvVar", server)

    def test_every_role_file_carries_the_contract(self):
        """A file-discovered profile inherits nothing.

        Kimi's built-in role prefix is interpolated into its own literals at module load and
        never applied to a profile loaded from a file, so a role that does not ask for
        ``${agents_md}`` runs without the operating contract - and asking for ``${base_prompt}``
        instead would hand it the main agent's voice.
        """
        for path in sorted((ROOT / "runtime" / "agents").glob("*.md")):
            text = path.read_text()
            with self.subTest(role=path.name):
                self.assertIn("${agents_md}", text)
                self.assertNotIn("${base_prompt}", text)
                self.assertIn("handoff", text.lower(), "a role must state its own handoff")

    def test_the_contract_names_both_staged_documents(self):
        """The contract is addressed to the agent, so it names what the agent can observe.

        Where the files come from is an operator question and belongs in the `.example` files
        and the docs; naming them here would read as an instruction to go and edit them.
        """
        text = (ROOT / "runtime" / "AGENTS.md").read_text()
        self.assertIn("`AGENTS.md`", text)
        self.assertIn("`SYSTEM.md`", text)
        self.assertIn("Model usage limits", text)
        self.assertIn("Model runtime envelope", text)
        # And it says plainly which of the two a subagent is missing, so a subagent that
        # looks for the lane table and finds none is not left guessing whether that is a bug.
        self.assertIn("never sees", text)

    def test_all_base_images_are_digest_pinned(self):
        for relative in (
            "container/Dockerfile",
            "proxy/Dockerfile",
            "search-adapter/Dockerfile",
        ):
            for line in (ROOT / relative).read_text().splitlines():
                if line.startswith("FROM ") and "${" not in line:
                    image = line.split()[1]
                    self.assertRegex(image, r"@sha256:[0-9a-f]{64}$")

    def test_platform_key_is_not_persisted(self):
        # Match any form (dict literal, CLI flag, variable): the selector writes
        # a session file that is persisted and sourced by the launcher.
        selector = (ROOT / "scripts" / "select_versions.py").read_text()
        self.assertIsNone(
            re.search(r"platform[\s_-]*key", selector, re.IGNORECASE),
            "select_versions.py must not accept or emit a platform key",
        )

    def test_read_env_uses_last_resolved_value(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as output:
            output.write("VALUE=first\nVALUE=second\n")
            path = Path(output.name)
        try:
            result = subprocess.run(
                ["python3", str(ROOT / "scripts" / "read_env.py"), str(path), "VALUE"],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.stdout.strip(), "second")
        finally:
            path.unlink()

    def test_dependency_lock_has_only_digests_and_commits(self):
        lock = json.loads((ROOT / "dependencies.lock.json").read_text())
        self.assertTrue(lock["images"])
        self.assertTrue(
            all(re.fullmatch(r"sha256:[0-9a-f]{64}", value) for value in lock["images"].values())
        )
        self.assertTrue(
            all(re.fullmatch(r"[0-9a-f]{40}", value) for value in lock["sources"].values())
        )

    def test_prompts_ask_for_models_before_the_kimi_version(self):
        # Every downstream number - lane sizes, fan-out, the proxy plan - is derived from
        # the model pair, so the pickers must run before scripts/select_versions.py.
        launcher = (ROOT / "start.sh").read_text()
        needles = {
            "select": "tools/models.py select",
            "resolve": "tools/models.py resolve",
            "modules": "tools/modules.py select",
            "version": "select_versions.py",
        }
        for name, needle in needles.items():
            self.assertIn(needle, launcher, f"start.sh no longer invokes {name}")
        positions = {name: launcher.index(needle) for name, needle in needles.items()}
        self.assertLess(positions["select"], positions["resolve"])
        self.assertLess(positions["resolve"], positions["modules"])
        self.assertLess(positions["resolve"], positions["version"])
        models = (ROOT / "tools" / "models.py").read_text()
        self.assertIn("last used", models)

    def test_a_command_that_can_render_is_never_routed_through_the_notes_spool(self):
        # Half of defect 2 is that the launcher printed over its own modal; the other half is the
        # trap the fix could walk into. A step's interface *is* its stdout, so spooling one would
        # hide the one question the operator is supposed to be able to see -- a silent failure
        # where the launch waits for an input nobody was asked for. The split is therefore worth
        # pinning per command rather than by comment, and both lists are checked so that a new
        # step added to the pass cannot land on the wrong side of it.
        launcher = (ROOT / "start.sh").read_text()
        body = launcher[launcher.index("harness_flow_pass() {") :]
        body = body[: body.index("\n}\n")]
        # Commands that draw a screen: they must run live, with their stdout on the terminal.
        rendering = (
            "tools/models.py select",
            "tools/modules.py select",
            "harness_modules select_version",
            "tools/modules.py environment",
            '"${selector[@]}"',
            '"${panel[@]}"',
            "tools/render_runtime.py",
        )
        # Commands that only print: their output waits in the notes and is read out after.
        quiet = (
            "tools/models.py resolve",
            "harness_modules configure",
            "tools/safe_workspace_init.py",
            "tools/resource_check.py",
            "tools/modules.py assemble",
        )
        for needle in rendering:
            line = next(line for line in body.splitlines() if needle in line)
            self.assertTrue(
                line.strip().startswith("harness_flow_step "),
                f"{needle} can put up a screen and must not be spooled",
            )
        for needle in quiet:
            line = next(line for line in body.splitlines() if needle in line)
            self.assertTrue(
                line.strip().startswith("harness_flow_work "),
                f"{needle} prints without asking and should wait for the window",
            )
        # And the work function is the one that redirects, so a pass line cannot be reworded into
        # a live print by accident.
        work = launcher[launcher.index("harness_flow_work() {") :]
        work = work[: work.index("\n}\n")]
        self.assertIn('>>"${flow_notes}" 2>&1', work)
        self.assertIn('if [[ "${screen_held:-false}" == true ]]; then', work)

    def test_the_notes_are_read_out_before_the_launch_says_anything_else(self):
        # Leaving the alternate screen discards what was painted on it, so a diagnostic written
        # while the modal was still up would be gone before anyone could read it -- the failure
        # path is exactly where the operator most needs the sentence that waited.
        launcher = (ROOT / "start.sh").read_text()
        report = launcher[launcher.index("harness_flow_report() {") :]
        report = report[: report.index("\n}\n")]
        self.assertLess(report.index("harness_flow_unhold"), report.index("printf"))
        unhold = launcher[launcher.index("harness_flow_unhold() {") :]
        unhold = unhold[: unhold.index("\n}\n")]
        self.assertLess(unhold.index("leave"), unhold.index("harness_flow_notes"))
        self.assertIn('unset HARNESS_TUI_SCREEN', unhold)
        # The trap is the other way out of a launch, and it owes the same two things.
        cleanup = launcher[launcher.index("cleanup() {") : launcher.index("trap cleanup EXIT")]
        self.assertIn('python3 tools/tui/screen.py', cleanup)
        self.assertIn('cat "${flow_notes', cleanup)
        self.assertLess(cleanup.index("screen.py"), cleanup.index("MODULE_PIDS"))
        # A killed launch's notes are not this launch's news.
        self.assertIn(': >"${flow_notes}"', launcher)

    def test_the_measurement_job_waits_for_a_real_prompt_then_leaves_nothing_behind(self):
        # A profile.bind record only exists after the operator's first request, so the job is
        # deferred; and the tree it copies holds conversation text, so the order of its two
        # operations is a security property rather than a style one.
        launcher = (ROOT / "start.sh").read_text()
        job = launcher[launcher.index("measure_prompts_after_first_request()"):]
        job = job[: job.index("\n}\n") + 3]
        self.assertLess(job.index("grep -lq"), job.index("compose cp"), "poll before copying")
        self.assertLess(job.index("compose cp"), job.index("prompt_measure.py"))
        self.assertLess(job.index("prompt_measure.py"), job.rindex("rm -rf"), "copy is deleted")
        self.assertIn("--plan \"${HARNESS_RUNTIME_DIR}/model-policy.json\"", job)
        self.assertIn("prompt-measurements.jsonl", job)
        # A finished measurement job is not a crashed module: the watchdog at the end of start.sh
        # treats any MODULE_PIDS exit as a reason to tear the stack down.
        self.assertNotIn("MODULE_PIDS", job)
        killed = launcher[launcher.index("cleanup() {"): launcher.index("trap cleanup EXIT")]
        self.assertIn('"${PROMPT_MEASURE_PID}"', killed)
        self.assertEqual(killed.count("PROMPT_MEASURE_PID"), 2, "kill and wait")
        # The history persists; the scratch tree and its log do not.
        deleted = launcher[launcher.index("  for file in proxy-token"):
                           launcher.index("harness_unlock")]
        self.assertIn("prompt-measure.log", deleted)
        self.assertIn("prompt-sessions", deleted)
        self.assertNotIn("prompt-measurements.jsonl", deleted)
        self.assertNotIn("prompt-context.json", deleted)

    def test_picker_ordering_is_alphabetical_by_the_label_the_operator_reads(self):
        # Directory ids deliberately sort the opposite way to the labels: an implementation
        # that ordered by id would return alpha, bravo, delta, mike, zeta and fail here. The
        # bravo/delta pair shares a casefolded label, so it also pins the id tie-break and
        # proves the comparison is case-insensitive.
        if str(ROOT / "tools") not in sys.path:
            sys.path.insert(0, str(ROOT / "tools"))
        import models as model_setup

        def entry(model_id: str, label: str, lane: str = "primary") -> dict:
            return {"id": model_id, "label": label, "lanes": [lane]}

        chosen = model_setup.selectable(
            [
                entry("zeta", "Alpha"),
                entry("mike", "beta"),
                entry("delta", "charlie"),
                entry("bravo", "Charlie"),
                entry("alpha", "Zulu"),
                entry("offlane", "Not offered", "subagent"),
            ],
            "primary",
        )
        self.assertEqual(
            [model["id"] for model in chosen],
            ["zeta", "mike", "bravo", "delta", "alpha"],
            "labels must sort case-insensitively, ties broken by id, other lanes filtered out",
        )


COMPOSE_SOURCE = (ROOT / "compose.yaml").read_text()
BOOTSTRAP_SOURCE = (ROOT / "compose.bootstrap.yaml").read_text()
ENV_EXAMPLE = (ROOT / ".env.example").read_text()
LANES = ("primary", "long", "subagent")


def compose_value(key: str) -> int:
    """The numeric fallback a compose environment entry is defaulted to."""
    match = re.search(rf'^\s*{key}: "\$\{{[A-Z_]+:?[-]?(-?\d+)\}}"', COMPOSE_SOURCE, re.MULTILINE)
    if match is None:
        raise AssertionError(f"{key} is not defaulted to a number in compose.yaml")
    return int(match.group(1))


class NoHardcodedModelFactsTests(unittest.TestCase):
    """The point of ./models and ./providers: nothing else may restate them."""

    def test_env_example_names_no_model_or_policy_number(self):
        forbidden = re.compile(
            r"^(LITELLM_|NRP_(MODEL|FAIR|PARALLEL|OUTPUT|PRIMARY|LONG|SUBAGENT|UPSTREAM)"
            r"|KIMI_SUBAGENT_CONCURRENCY=)",
            re.MULTILINE,
        )
        # MODEL_PROXY_* tunables and endpoint overrides are allowed; a retired model or policy
        # variable is not.
        offenders = [
            line.split("=", 1)[0] for line in ENV_EXAMPLE.splitlines() if forbidden.match(line)
        ]
        self.assertEqual(offenders, [])
        # Blank means "derive it from the selected definitions".
        self.assertRegex(ENV_EXAMPLE, r"(?m)^KIMI_BACKGROUND_TASK_SLOTS=$")

    def test_env_example_explains_key_scopes_without_naming_a_key_variable(self):
        # Which variables hold model keys depends on which models and providers are installed
        # and on how the operator scopes their keys, so the template must name none: it sends
        # the reader to the definitions instead. An operator who copies this file and greps for
        # a name must still find out how to get one.
        section = ENV_EXAMPLE.split("# Model provider credentials", 1)[1].split(
            "# Kimi background capacity", 1
        )[0]
        assignments = [
            line.split("=", 1)[0].strip()
            for line in section.splitlines()
            if "=" in line and not line.lstrip().startswith("#")
        ]
        self.assertEqual(
            assignments,
            ["NRP_BASE_URL"],
            "the credentials section may declare an endpoint override but never a key variable",
        )
        self.assertNotIn("_API_KEY", section)
        for needle in ("key_env", "[[credential]]", "models/<id>/model.toml", "providers/<id>"):
            self.assertIn(needle, section, "the two key scopes are no longer described")
        self.assertIn("grep", section, "the template no longer says how to find the names")

    def test_env_example_has_no_context_opt_out_variable(self):
        """The launch panel is the only control, so `.env` must not offer a second one."""
        self.assertNotIn("KIMI_SYSTEM_PROMPT_OMIT_ENVELOPE", ENV_EXAMPLE)
        self.assertNotRegex(ENV_EXAMPLE, r"(?m)^KIMI_(OMIT|[A-Z_]*OMIT[A-Z_]*)=")
        section = ENV_EXAMPLE.split("# Kimi session context", 1)[1].split(
            "# Optional GitHub MCP", 1
        )[0]
        for needle in ("panel", "./prompts.sh", "SYSTEM.md", "CONTEXT.md", "remembered"):
            self.assertIn(needle, section, "the template no longer says where context is chosen")

    def test_no_tracked_file_mentions_litellm_except_the_nrp_endpoint(self):
        # LiteLLM is one gateway an operator might happen to have behind the endpoint, and the
        # harness must not assume it. Four mentions are deliberately recorded here rather than
        # left to drift, each for a stated reason.
        allowed = {
            "providers/nrp/provider.toml": "the real endpoint and key-issuing URLs",
            "docs/models-providers.md": "the definition example, quoted from the file above",
            "README.md": "the migration table, which names the variable being retired",
            "tools/runtime.sh": "the retired-variable list, which warns an operator off it",
        }
        for path in sorted(ROOT.rglob("*")):
            if (
                not path.is_file()
                or ".git" in path.parts
                or ".local" in path.parts
                or "__pycache__" in path.parts
                or ".ruff_cache" in path.parts
                or path.suffix in {".so", ".pyc"}
            ):
                continue
            try:
                text = path.read_text()
            except (UnicodeDecodeError, OSError):
                continue
            where = path.relative_to(ROOT).as_posix()
            if where == "tests/test_configuration.py":
                continue  # the allow list above
            if "litellm" not in text.lower():
                continue
            self.assertIn(where, allowed, f"{where} assumes which gateway is behind the endpoint")
            if where != "providers/nrp/provider.toml":
                continue
            for line in text.splitlines():
                if "litellm" in line.lower():
                    self.assertTrue(
                        line.startswith(("base_url =", "key_url =", "# ")),
                        f"{where}: only the endpoint URL, key URL, and comments may name it: "
                        f"{line}",
                    )

    def test_runtime_agents_md_carries_no_model_number(self):
        text = (ROOT / "runtime" / "AGENTS.md").read_text()
        for token in ("262,144", "1,000,000", "320,000", "350,000", "64,000", "200,000"):
            self.assertNotIn(token, text)
        # The contract must name the two generated blocks after the headings it actually
        # composes them under, and never point into the workspace's AGENTS.md, which the
        # harness does not write. The lane table lives in the main prompt only; the limits
        # section is the one every lane receives.
        self.assertIn("Model runtime envelope", text)
        self.assertIn("Model usage limits", text)
        self.assertNotIn("workspace `AGENTS.md`", text)

    def test_bootstrap_declares_every_name_the_definitions_read(self):
        plan = shipped_plan()
        if str(ROOT / "tools") not in sys.path:
            sys.path.insert(0, str(ROOT / "tools"))
        import models as model_setup

        for name in model_setup.bootstrap_names(plan):
            self.assertIn(f"{name}:", BOOTSTRAP_SOURCE)

    def test_compose_declares_no_provider_credential(self):
        # Credential names come from the generated fragment, so compose.yaml cannot name one
        # and mount hygiene stays independent of which providers are installed.
        for path in sorted((ROOT / "providers").iterdir()):
            if not path.is_dir():
                continue
            for credential in re.findall(
                r'^env = "(.+)"$', (path / "provider.toml").read_text(), re.MULTILINE
            ):
                self.assertNotIn(
                    credential,
                    COMPOSE_SOURCE,
                    f"compose.yaml interpolates {credential} directly; the fragment should own it",
                )


class ResolvedEnvelopeTests(unittest.TestCase):
    """The shipped definitions must still resolve to the envelope we advertise."""

    @classmethod
    def setUpClass(cls):
        cls.plan = shipped_plan()
        cls.lanes = cls.plan["lanes"]
        cls.counters = cls.plan["counters"]

    def context_counter(self) -> dict:
        return next(c for c in self.counters.values() if c["family"] == "context")

    def shipped_slug(self, model_id: str) -> str:
        """The slug the checked-in definition declares, read independently of the plan.

        Deriving the expectation from the plan itself would make the alias assertion
        unfalsifiable: renaming a model moves both sides at once.
        """
        if str(ROOT / "tools") not in sys.path:
            sys.path.insert(0, str(ROOT / "tools"))
        import definitions

        _, models = definitions.load_definitions(ROOT)
        return next(model["slug"] for model in models if model["id"] == model_id)

    def test_three_lanes_exist_and_bind_to_their_own_route(self):
        self.assertEqual(set(self.lanes), set(LANES))
        for lane, entry in self.lanes.items():
            with self.subTest(lane=lane):
                self.assertEqual(entry["route"], f"/{lane}/v1")
                self.assertEqual(
                    entry["alias"], f"{self.shipped_slug(entry['model_id'])}-{lane}"
                )
                self.assertLessEqual(
                    entry["input_tokens"] + entry["output_clamp_tokens"],
                    entry["context_tokens"],
                )
                self.assertLessEqual(entry["input_tokens"] + self.plan["reserved_context_size"],
                                     entry["context_tokens"])

    def test_one_model_in_all_lanes_shares_one_context_counter(self):
        counter = self.context_counter()
        self.assertEqual(len(counter["lanes"]), len(LANES))
        self.assertTrue(self.plan["limits"]["shared_context_counters"])
        # One model serving three lanes must contend for one budget, not three.
        self.assertEqual(len([c for c in self.counters.values() if c["family"] == "context"]), 1)

    def test_aggregate_budget_is_the_threshold_times_the_margin(self):
        counter = self.context_counter()
        provider = self.plan["providers"][counter["provider"]]
        percent = min(
            rule["value"]
            for rule in provider["rules"]
            if rule["kind"] == "aggregate_context_fraction"
        )
        self.assertEqual(
            counter["ceiling"], counter["advertised_tokens"] * percent // 100
        )
        self.assertEqual(
            counter["budget"],
            min(counter["ceiling"], counter["ceiling"] * provider["context_margin_percent"] // 100),
        )
        self.assertLessEqual(counter["budget"], counter["ceiling"])

    def test_fanout_is_the_largest_the_budget_allows(self):
        subagent = self.lanes["subagent"]
        counter = self.context_counter()
        self.assertEqual(
            self.plan["limits"]["subagent_concurrency"],
            counter["budget"] // subagent["reservation"],
        )
        self.assertEqual(
            self.plan["limits"]["subagent_concurrency_basis"], "the tightest provider rule"
        )
        # Greedy: one more would genuinely not fit, rather than being a hand-picked cap.
        self.assertGreaterEqual(
            (self.plan["limits"]["subagent_concurrency"] + 1) * subagent["reservation"],
            counter["budget"],
        )
        declared = next(
            rule["value"]
            for rule in self.plan["providers"]["nrp"]["rules"]
            if rule["kind"] == "max_concurrent_requests"
        )
        # The context budget, not the provider's own request cap, must be what limits fan-out.
        # Named in the failure so a future cap change reports which rule became binding.
        self.assertLess(
            self.plan["limits"]["subagent_concurrency"],
            declared,
            f"the provider count rule ({declared}) is binding, not the context budget "
            f"({counter['budget']})",
        )

    def test_long_lane_is_exclusive_and_primary_is_not(self):
        self.assertTrue(self.lanes["long"]["exclusive"])
        for lane in ("primary", "subagent"):
            self.assertFalse(self.lanes[lane]["exclusive"])

    def test_the_primary_lane_overlaps_one_subagent_but_not_the_whole_swarm(self):
        budget = self.context_counter()["budget"]
        primary = self.lanes["primary"]["reservation"]
        subagent = self.lanes["subagent"]["reservation"]
        fan_out = self.plan["limits"]["subagent_concurrency"]
        # The margin exists to buy exactly this: one subagent concurrent with a primary request.
        self.assertLessEqual(primary + subagent, budget)
        # A full swarm beside a primary request is not, so the proxy queues it instead.
        self.assertGreater(fan_out * subagent + primary, budget)

    def test_rate_ledger_is_booked_in_the_provider_unit(self):
        rates = [c for c in self.counters.values() if c["family"] == "rate"]
        self.assertTrue(rates)
        for rate in rates:
            self.assertEqual(rate["unit"], "output_tokens")
            provider = self.plan["providers"][rate["provider"]]
            limit = next(
                rule["value"]
                for rule in provider["rules"]
                if rule["kind"] == rate["kind"] and rule["scope"] == rate["scope"]
            )
            self.assertEqual(
                rate["capacity"], limit * provider["output_rate_margin_percent"] // 100
            )

    def test_readme_quotes_the_plan_it_claims_to_quote(self):
        # README.md and docs/verification.md restate derived numbers in prose. This is the
        # only thing that notices when a margin edit in providers/nrp makes that prose stale.
        if str(ROOT / "tools") not in sys.path:
            sys.path.insert(0, str(ROOT / "tools"))
        readme = (ROOT / "README.md").read_text()
        verification = (ROOT / "docs" / "verification.md").read_text()
        counter = self.context_counter()
        rate = next(c for c in self.counters.values() if c["family"] == "rate")
        figures = {
            "advertised context": counter["advertised_tokens"],
            "aggregate ceiling": counter["ceiling"],
            "in-flight budget": counter["budget"],
            "rate ledger": rate["capacity"],
        }
        for lane in ("primary", "long", "subagent"):
            figures[f"{lane} reservation"] = self.lanes[lane]["reservation"]
        for name, value in figures.items():
            with self.subTest(figure=name):
                self.assertIn(f"{value:,}", readme, f"README does not state the {name}")
        fan_out = self.plan["limits"]["subagent_concurrency"]
        self.assertIn(f"which is {fan_out} at once", readme)
        # The condensed line the launcher prints is documented with the same budget, lane and
        # permit count.
        self.assertIn(f"context budget {counter['budget']}", verification)
        self.assertIn(f"{fan_out} subagent permits", verification)
        self.assertIn(f"{len(self.lanes)} lanes", verification)

    def test_background_capacity_does_not_steal_a_subagent_lane(self):
        if str(ROOT / "tools") not in sys.path:
            sys.path.insert(0, str(ROOT / "tools"))
        import models as model_setup

        derived = model_setup.model_environment(self.plan, {})
        self.assertEqual(
            derived["KIMI_SUBAGENT_CONCURRENCY"],
            str(self.plan["limits"]["subagent_concurrency"]),
        )
        self.assertGreaterEqual(
            int(derived["KIMI_BACKGROUND_TASK_SLOTS"]),
            self.plan["limits"]["subagent_concurrency"],
        )
        self.assertEqual(
            compose_value("KIMI_CODE_BACKGROUND_BASH_TASK_TIMEOUT_S"),
            0,
            "background Bash must default to no wall-clock timeout",
        )

    def test_subagent_wall_clock_is_unlimited_and_policy_pinned(self):
        # Unlimited has to live here: the equivalent environment variables outrank
        # this file and reject 0, so expressing it in compose.yaml would fail startup.
        with (ROOT / "runtime" / "config.toml").open("rb") as source:
            config = tomllib.load(source)
        self.assertEqual(config["subagent"]["timeout_ms"], 0)
        self.assertEqual(config["swarm"]["timeout_ms"], 0)
        policy_document = json.loads((ROOT / "runtime" / "config-policy.json").read_text())
        for table in ("subagent", "swarm", "secondary_model", "default_model", "models"):
            self.assertNotIn(table, policy_document["user_owned"])
        for key in ("KIMI_SUBAGENT_TIMEOUT_MS", "KIMI_CODE_SWARM_TIMEOUT_MS"):
            self.assertIsNone(re.search(rf'^\s*{key}\s*:', COMPOSE_SOURCE, re.MULTILINE))

    def test_guidance_publishes_the_envelope_and_tells_the_agent_to_fill_it(self):
        if str(ROOT / "tools") not in sys.path:
            sys.path.insert(0, str(ROOT / "tools"))
        import policy as policy_module

        limit = self.plan["limits"]["subagent_concurrency"]
        main = policy_module.render_guidance(self.plan, "main")
        lane = policy_module.render_guidance(self.plan, "lane")
        # This text is appended to a prompt that Kimi Code renders with a global template
        # substitution, so a dollar-brace here would expand into whatever the session defines.
        for text in (main, lane):
            self.assertNotIn("${", text)
        self.assertIn("Model runtime envelope", main)
        self.assertIn(f"up to {limit} subagents concurrently", main)
        self.assertIn("## Parallel work", main)
        for lane_entry in self.plan["lanes"].values():
            self.assertIn(lane_entry["alias"], main)
        # The lane core is what a subagent gets, and it is the part that is true of it: the
        # provider's published terms, and the budget every request spends whoever sends it.
        self.assertIn(self.plan["providers"]["nrp"]["policy_url"], lane)
        self.assertIn("## Model usage limits", lane)
        # What a subagent cannot act on must not be in its prompt. Roughly six hundred tokens of
        # parallelism advice reached every lane before the split, and none of it was usable.
        self.assertNotIn(f"up to {limit} subagents concurrently", lane)
        self.assertNotIn("## Parallel work", lane)
        for lane_entry in self.plan["lanes"].values():
            self.assertNotIn(lane_entry["alias"], lane)
        # Nothing names an opt-out variable any more: the startup panel is the only control.
        for text in (main, lane):
            self.assertNotIn("KIMI_SYSTEM_PROMPT_OMIT_ENVELOPE", text)


if __name__ == "__main__":
    unittest.main()
