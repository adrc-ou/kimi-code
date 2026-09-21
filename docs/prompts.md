# Prompt surfaces

Everything the model receives other than the text the operator typed, where each
piece is defined, what reaches it, and what the harness can and cannot change.

Two authorities were used. Kimi's behaviour is taken from the shipped bundle
(`/usr/local/bin/kimi`, a minified single-file build) at the byte offsets given
inline, cross-checked against [the official
docs](https://www.kimi.com/code/docs/en/kimi-code-cli/) and against the
`profile.bind` records that a live session writes to
`$KIMI_CODE_HOME/sessions/<workDirKey>/<sessionId>/agents/<agentId>/wire.jsonl`.
Harness behaviour is taken from the files cited as `path:line`.

Where the docs and the bundle disagree, both are stated and the bundle wins.

## Reading the tiers

A prompt surface belongs to exactly one of four tiers, and the tier decides how it
can be customised:

| Tier | Lifetime | Cleared by |
| --- | --- | --- |
| **agent prompt** | built once per agent, at its first bind, then reused every step | the file that defines it, then a restart |
| **per-step injection** | re-evaluated before every agent step, appended as a `role: "user"` message | state, feature flags, or nothing at all |
| **request payload** | travels beside the prompt in the same HTTP request, not in it | tool and server configuration |
| **side call** | a separate LLM request with its own prompt | not reachable |

Nothing in any tier is a message the operator typed. Kimi's own build note on the
subject is that a `<system-reminder>` is *"an authoritative directive from the
harness; always follow it"* — which is why an operator who wants a session that
obeys only them must deal with tiers two and three as well as tier one.

## Index

`reaches` answers the question that matters most: main agent, subagents, or both.
`clear` is whether the surface can be made to contribute nothing today.

| # | Surface | Defined in | Reaches | Override today | Clear |
| --- | --- | --- | --- | --- | --- |
| 1 | Built-in prompt template | bundle `127764670` | both | `SYSTEM.md` (main only), agent file, `--agent-file` | yes, by replacement |
| 2 | Role overlay | bundle `133583971`, `132979369`, `133580848`, `133412659` | both | same-name agent file with `override: true` | no |
| 3 | Instruction files (`AGENTS.md`) | bundle `127685303` | both | `CONTEXT.md` (ours), workspace files (user's) | **yes, empty is skipped** |
| 4 | Environment facts | bundle `127683689` | both | omit the placeholder from a replacement template | partially |
| 5 | Skill listing | bundle `127778746` | main and `coder` only | `merge_all_available_skills`, `builtin_product_skills`, `--skills-dir` | yes |
| 6 | Plugin contributions | bundle `127779602` | both | `${plugin_sections}` placement; no plugins installed | yes (already nil) |
| 7 | Additional directories | bundle `127778552` | both | `/add-dir`, `.kimi-code/local.toml` | yes (already nil) |
| 8 | Reply style, notify guidance | bundle `127774158`, `127777711` | both | CLI/server only, not file or env | no |
| 9 | Harness envelope | `tools/policy.py:624-915` | split: limits both, table+parallelism **main only** | the launch panel, per block | yes, per block |
| 10 | Module guidance | `tools/modules.py:139` | all lanes | deselect the module or switch the block off | yes |
| 11 | Subagent role files | `runtime/agents/*.md` | that subagent only | project `.kimi-code/agents/` + `override: true` | yes, delete the file |
| 12 | Per-step injections | bundle `132970005` registry | each agent separately | one kill switch exists, for `permission_mode` | **no** |
| 13 | Tool schemas | bundle tool catalogue + MCP servers | per profile | `[tools]`, `tools:`/`disallowedTools:`, `runtime/mcp.json` | partially |
| 14 | Compaction hand-off | bundle `130144708`, `130148294`, `130148965` | the agent being compacted | `[loop_control]` only controls *when* | no |
| 15 | Adjacent tags | `<skill-loaded>`, `<hook_result>`, `<cron-fire>` | the agent that triggered them | the owning feature | partially |
| 16 | Title side call | bundle `130214357` | not this conversation | none; OAuth-gated HTTP endpoint | n/a |

---

## Tier 1 — the agent prompt

Built once per agent at its first bind and reused for every step of that agent's
life. A second agent in the same session gets its own. `llm.request` records log a
`systemPromptHash`, and it holds for the agent's whole life: every request one
agent makes carries the same value, and it is exactly `sha256` of that agent's
`profile.bind` `systemPrompt`, read back rather than assumed. It is therefore a
faithful identity for *this agent's* prompt and nothing more.

It is not a *build* identity, and the reason is content rather than clock. The
rendered prompt embeds both `${cwd_listing}` and the contract text, so subagents
bound at different moments hash differently once either has changed: the
subagents of one audited session fell into six hash groups by spawn order rather
than sharing one hash. Diffing two adjacent groups gives 14 changed lines of
directory listing against 48 of instruction prose — the harness had genuinely
been edited in between, which is the point. The hash tracks content, so it moves
for reasons that are real and reasons that are incidental alike, and nothing here
can key a cached literal on it. The date is not the cause: `Current date` occurs
nowhere in the bundle, and no `YYYY-MM-DD` date occurs anywhere in a bound
prompt. Kimi's own prompt says so — the date "is disclosed through reminders at
the start of the conversation and whenever the date changes", which is
`dateChangeService.ts` (bundle `133025369`) appending a `<system-reminder>` as a
message rather than editing the prompt.

### 1. Kimi's built-in prompt template

**Defined.** `system_default`, bundle byte `127764670`, region
`packages/agent-core-v2/src/app/agentProfileCatalog/system.md?raw`. Exactly
6,705 bytes, terminated by `";` at `127771375`.

**Structure.** Its own literal headings, in order: `# Communicating with the
user`, `# Tool use`, `# Coding`, `# Risky actions`, `# Delivering work`,
`# Context management`, `# Environment`, `# Project information`. Headings that
appear between them in a live prompt — `## Additional Directories`, `# Skills`,
`# Plugin Instructions` — come from variables, not from the literal.

**Template injection.** It references 13 of the 15 variables that
`systemPromptVars(context, options)` (bundle `127774158`) returns:
`product_name`, `role_additional`, `reply_style_guide`, `notify_user_guidance`,
`os`, `shell`, `windows_notes`, `cwd`, `cwd_listing`,
`additional_dirs_section`, `agents_md`, `skills_section`, `plugin_sections`.

The complete variable bag is those 13 plus `skills` and
`additional_dirs_info`, which exist **only for operator templates** —
`system_default` never reads them. `base_prompt` is not a member at all:
`renderPromptTemplateResult` (bundle `127775774`) adds it only when the operator
template contains the literal substring `${base_prompt}`, and it then expands to
`this.user.getDefaultProfile().renderSystemPrompt(context)` — that is, to Kimi's
`system_default` fully rendered. So in our `SYSTEM.md.example`, `${base_prompt}`
means "wrap", and its absence means "replace", exactly as the four-row table in
`prompt_context.compose_system_document()` (`tools/prompt_context.py:529-549`) specifies.

`renderPrompt` (bundle `127763660`, `_base/utils/render-prompt.ts`) substitutes
with `/\$\{([A-Za-z_][A-Za-z0-9_]*)\}/g`, replacing only `string` and `number`
values and leaving every unknown name in the output **verbatim**.

> **Docs discrepancy.** `customization/agents.html` lists `${now}` as an available
> variable. It is not in `systemPromptVars`. Writing `${now}` in a prompt file
> puts the six literal characters `${now}` in front of the model.

**Injected into.** All five built-in agent profiles — `agent`, `coder`, `explore`,
`plan`, `tower-worker` — render through this one template
(`renderSystemPromptResult`, bundle `127776222`). There is no separate built-in
prompt for subagents.

**Override.** `loadSystemMdProfile` (bundle `127792381`) reads
`$KIMI_CODE_HOME/SYSTEM.md` and, when the text survives `.trim()`, registers a
replacement profile named `agent` — so it applies to the **default profile only**,
and therefore to the main agent. Precedence, highest first: `--agent-file`, a
project agent file with `override: true`, `SYSTEM.md`, a user-scope agent file,
built-in. `--agent <other>` bypasses `SYSTEM.md` entirely.

**Emptiness.** A blank file is discarded (`systemFile.ts`,
`text.trim().length === 0`) and the built-in is used instead. This is documented
behaviour, not a quirk, and it is the sole reason `EMPTY_PROMPT_SENTINEL`
(`tools/prompt_context.py:95`) exists.

### 2. Role overlay — `${role_additional}`

**Defined.** A string argument to `renderSystemPromptResult`, not a file:

| profile | overlay | bundle | bytes |
| --- | --- | --- | --- |
| `agent` (main) | `""` | `133584431` | 0 |
| `coder` | `CODER_ROLE` | `133583971` | 442 |
| `plan` | `PLAN_ROLE` | `132979369` | 909 |
| `explore` | `explore_overlay_default` | `133580848` | 1,869 |
| `tower-worker` | `CODER_ROLE` + tower text | `133412659` | ~890 |

All four non-empty overlays begin with `TASK_AGENT_ROLE_PREFIX` (bundle
`127776972`, 368 bytes): *"You are now running as a subagent. All the `user`
messages are sent by the main agent. The main agent cannot see your context…"*

**Trigger.** Which profile the agent binds. The main agent binds `agent` unless
overridden; a subagent binds the `subagent_type` the model named in its `Agent`
call, defaulting to `coder`.

**Purpose relative to surface 1.** Surface 1 is the shared constitution; this is
the job description. It is the reason "subagents do not get `SYSTEM.md`" is true:
they do get surface 1, and they get a different overlay, through a code path
`SYSTEM.md` cannot touch.

**Override.** Only by shipping a same-name agent file with `override: true` —
which replaces the whole profile, tools included. The overlay text itself is
hard-coded. Note the trap: an `override: true` file named `coder` or `explore`
also removes the `TASK_AGENT_ROLE_PREFIX` framing, and the docs warn that a
custom agent delegated as a subagent *"run[s] without the built-in sub-agent
framing"* unless its body restates it.

### 3. Instruction files — `${agents_md}`

**Defined.** Nothing composes it; these are files on disk. Loader
`loadAgentsMdForRoots` (bundle `127685303`), fed by
`prepareSystemPromptContext` (bundle `127683689`).

**Discovery order**, deduplicated by `realpath`:

1. `$KIMI_CODE_HOME/AGENTS.md` (default `/home/agent/.kimi-code/AGENTS.md`) — the
   composed all-lane contract lands here: `CONTEXT.md` if the operator wrote one,
   else `runtime/AGENTS.md`, plus the enabled all-lane blocks.
2. `~/.agents/AGENTS.md`, else `~/.agents/agents.md` — real OS home, ignores
   `KIMI_CODE_HOME`. The harness creates neither.
3. For each additional directory, then the work directory: every directory from
   the git-worktree root **down to** the working directory, contributing
   `<dir>/.kimi-code/AGENTS.md` then `<dir>/AGENTS.md` (else `agents.md`).
4. Two undocumented extras: `<gitRoot>/branch_kimi-agent.md` and
   `<gitRoot>/info/kimi-agent.md`.

**Format.** `renderAgentFiles` (bundle `127689642`) emits each file as
`<!-- From: <abs path> -->\n<contents>` joined by a blank line, and returns `""`
for an empty list. `agents_md: context.agentsMd ?? ""`.

**Two answers to "how does Kimi find subdirectory `AGENTS.md`?"** Files between
the git root and the working directory are read **eagerly**, every step, because
they are on the `dirsRootToLeaf` ancestor chain. Files in a *sibling* or *deeper*
directory the agent later touches are read **lazily**, and only as a pointer — see
surface 12, variant `agents_md`.

**Size.** Total is measured with `Buffer.byteLength`; above **32,768 bytes** a
warning is pushed — *"AGENTS.md total N KB exceeds the recommended 32 KB"*
(`AGENTS_MD_RECOMMENDED_MAX_BYTES = 32 * 1024`, bundle `127692695`; the comparison
itself uses a literal `32768`) — and published as
`WarningIssued` with code `agents-md-oversized` (bundle `135478559`).
**Instruction text is never truncated.** The changelog records silent truncation
being *replaced* by this warning. The docs give no number.

This stack ships 6,067 bytes of contract source (`runtime/AGENTS.md` with its
comments stripped) and 9,592 bytes once the all-lane blocks are composed in, out of
the 32,768-byte budget that the user's own `AGENTS.md` files also draw on. The cap
is shared with the operator's project instructions, so growth here spends headroom
that belongs to them.

**Emptiness.** A file whose contents are empty or whitespace-only is skipped by
`collect()` (`...trim().length > 0`), so an empty `runtime/AGENTS.md` removes it
from context cleanly. Contrast surface 1, where emptiness means "use Kimi's".

**Harness responsibility.** The composed document is written to
`.local/runtime/<instance>/AGENTS.md`, published to Compose as
`KIMI_RENDERED_AGENTS_MD`, bound to `/stage/AGENTS.md:ro` (`compose.yaml:11`), and
installed by the root-only initializer at `0440` with the ext4 immutable flag
(`container/initialize-agent-state.py:29,37,77`). It is *not* a verbatim copy of
`runtime/AGENTS.md`: that file is only the tier-two source, and the enabled blocks
are composed after it. The file is always written, even when it composes to empty,
because Docker turns a missing bind source into a directory and that fails at
container start rather than at render.

`<workspace>/AGENTS.md` belongs to the project under development and the harness
must never write it. It used to carry a generated envelope inside a marker pair;
that mechanism is deleted, and the envelope is composed into the prompt documents
instead (`docs/models-providers.md`, "How the envelope reaches a prompt").

### 4. Environment facts

`${cwd}` from `view.workDir`; `${cwd_listing}` from
`packages/nodejs/services/hostDirectory` `listDirectory(…, { collapseHiddenDirs:
true })` — hidden names omitted, directories before files then alphabetical,
capped at 100 entries per level with a trailing `…` at depths 0, 4 and 8 and
25-character elided names; `${os}`, `${shell}` from `lease.runtime.environment`;
`${windows_notes}` only when `osKind === "Windows"`.

Separately, `environmentForTemplate(context)` (bundle `127776723`) returns
`{ cwd }`, which `mergeEnvironmentDisclosure` folds into the render result and
`profile.bind` logs as `environmentDisclosure`. It is **not** prompt text — no
`${...}` reference expands to it; it is session metadata that also gates the
`date_change` reminder.

Measured in a live rendered `explore` prompt on this stack: `# Environment` costs
3,550 bytes, of which most is the directory listing, and the `# Project
information` literal is 554 bytes before `${agents_md}` expands it — the expanded
instruction payload there cost 18,338 bytes, 62% of that agent's whole system
prompt.

### 5. Skill listing

`SKILLS_SECTION_PROSE` (bundle `127778746`, 828 bytes) is the
`# Skills` heading and its instructions; the list itself is
`getModelSkillListing()` (bundle `127887931`), grouped by scope with
`Project overrides User overrides Extra overrides Built-in` precedence, each
entry one to five lines (`name` when it differs from the directory, `description`,
`whenToUse`, a multi-line `when to use` clause, and the `Path`).

Gate: `const skills = context.skillActive ?? options.skillActive ? context.skills
?? "" : ""`. `skillActiveFor(tools) = tools.includes("Skill")`. So `explore` and
`plan` render **no skills section at all** regardless of catalogue contents, and
`agent` and `coder` render the full list. Measured with the whole catalogue
empty: 4,384 → 1,453 bytes, the residue being the three `### scope` headers and
the `flow-canvas` built-in that the catalogue filter always re-adds.

Knobs: top-level `merge_all_available_skills`, `builtin_product_skills` (env
`KIMI_CODE_BUILTIN_PRODUCT_SKILLS`), `extra_skill_dirs`, and `--skills-dir`, which
*replaces* discovery for one launch. Full skill bodies do not live here — see
surface 15.

### 6, 7. Plugin contributions and additional directories

Both are conditional and both are currently nil in this stack: no plugins are
installed, and `/add-dir` is unused, so `additional_dirs_info` is `""` and
`additional_dirs` is empty. Plugins are capped at 32 KB per field and 64 KB per
prompt build. A replacement template must place `${plugin_sections}` itself, and
must not repeat it when `${base_prompt}` already carries it.

### 8. Reply style and notification guidance

`reply_style_guide` defaults to an inline 341-byte paragraph in
`systemPromptVars`, overridable from `bootstrap.args.replyStyleGuide` — a CLI
option or the server API's host identity. There is no config key and no env var,
so the harness cannot set it from a file.

`notify_user_guidance` is gated on the `notify_user` flag plus tool availability
and renders `""` in this stack. `renderAgentProfilePrompt` additionally
**force-appends** the same prose to any rendered prompt that lacks it — a second
path that a `SYSTEM.md` author cannot suppress by omitting the placeholder.

### 9, 10. Harness-composed text: the generated blocks and module guidance

**Two documents, two audiences.** `tools/prompt_context.py` composes the all-lane
contract and the main agent's prompt separately, because a subagent cannot act on
what the primary agent is told and every extra paragraph is charged to the shared
in-flight context budget. `policy.guidance_sections(plan, audience)` returns the
blocks for one audience and `compose_*_document()` appends the enabled ones:

| block | option id | audience | heading |
| --- | --- | --- | --- |
| usage limits | `policy.OPTION_LANE_LIMITS` | every lane | `## Model usage limits (generated at launch)` |
| lane table | `policy.OPTION_LANE_TABLE` | main only | `## Model runtime envelope (generated at launch)` |
| parallelism | `policy.OPTION_PARALLELISM` | main only | `## Parallel work` |
| module guidance | `prompt_context.OPTION_MODULE_GUIDANCE` | every lane | `## Module: <label>` per module |

Each block is written to stand alone — a subagent that receives only the usage
limits must not be pointed at a table it cannot see — and each costs what it
costs: 503 tokens for the limits, 670 for the table, 474 for the parallelism
advice, and 378 for a ComfyUI module's guidance, measured with the estimator in
`tools/prompt_measure.py` against the shipped definitions.

**Which blocks are composed in is a launch-panel choice.** `start.sh` runs
`tools/prompt_panel.py` before `tools/render_runtime.py`; the selection is stored
in `prompt-context.json` in the instance runtime directory — the nine add-ons under
`enabled`, the two documents under the tri-state `static` key — and
`./prompts.sh --configure --enable ID --disable ID --static ID=STATE` changes it
without a launch. There is no `.env`
variable for any of this. The retired `KIMI_SYSTEM_PROMPT_OMIT_ENVELOPE` is named
in `harness_retired_env` (`tools/runtime.sh`) so an operator's stale `.env` is
reported rather than silently ignored.

**Where the composition lands.** The contract goes to
`.local/runtime/<instance>/AGENTS.md` and is published as
`KIMI_RENDERED_AGENTS_MD`; the prompt goes to `.../SYSTEM.md` and is published as
`KIMI_SYSTEM_MD` (`tools/render_runtime.py:164-173`). Both are bound read-only into
the initializer (`compose.yaml:11,14`) and installed `0440` and immutable. Both are
regenerated every launch, so in-session edits do nothing until the next start, and
neither is on the cleanup delete-list: they are re-stamped rather than secret, and
the launcher's checks read them after the session ends.

**Four rows, one rule.** `SYSTEM.md` resolves by *existence* for authority and by
*emptiness* for payload — `prompt_context.compose_system_document()` states the
table. Absent file plus any enabled block stages a `${base_prompt}` wrapper with
the blocks, which is the only route to Kimi's own prompt; absent file with nothing
enabled stages an empty file, which Kimi also reads as "use mine". A non-empty file
gets the blocks appended. A deliberately empty file does not get wrapped, because
that would reinstate the prompt the operator just declined: the blocks become the
whole prompt, and with nothing enabled the stage is `EMPTY_PROMPT_SENTINEL`, a lone
period, which is the only thing that survives Kimi's `text.trim().length === 0`
check while telling it nothing.

Those four rows describe `auto`. The panel's other two settings are this-session
overrides of the same rule, resolved by the same `prompt_context.static_source()`:
`on` reads the chain as though the file were absent, which for `SYSTEM.md` is
row one, and `off` forces the row the empty file already meant. Nothing is written
to the workspace; the override lives in `prompt-context.json` and dies with the
launch.

**Generated text carries no placeholders.** Appended blocks pass through Kimi's
template substitution like the rest of the prompt, so every block is written with
zero `${...}`; `tests/test_configuration.py` enforces that. The harness does
substitute five names of its own first — `${harness.date}` and the four
`${kimi.*}` extractions — and refuses to stage a near-miss placeholder that Kimi
would otherwise ship as literal text.

### 11. Subagent role files

**Defined.** `runtime/agents/debug-runner.md` and `runtime/agents/repo-researcher.md`.
Frontmatter carries `name`, `description`, `whenToUse`, `override`, `tools`,
`disallowedTools`, optional `subagents`; **the body is that agent's entire system
prompt template**, rendered by `agentProfileFromFile` →
`renderPromptTemplateResult(definition.prompt, context, { skillActive },
basePrompt)`.

**Delivery.** `assemble()` (`tools/modules.py:153`) merges `runtime/{skills,
agents, tools}` and every selected module's `runtime/{skills, agents, tools}`
into `.local/runtime/<instance>/assets/` (duplicates raise), which reaches the
container as volume `kimi-assets` → `/opt/kimi-runtime` (`compose.yaml:194-195`),
read by `extra_agent_dirs` in `runtime/config.toml`.

**Selection.** Entirely model-driven. Each discoverable agent becomes an enum
value in the `Agent` tool's `subagent_type` description, so adding a file adds a
routing option; `whenToUse` is the hint the model reads. There is no harness-side
matcher, no automatic load balancing, and no way to force a role from
configuration.

**The consequence nobody has written down.** Because the body is the template, a
body containing no `${...}` gets *only its own text* — no surface 1, no
`${agents_md}`, no envelope. Both shipped bodies avoid that fate by half: each
ends with `${agents_md}` (`runtime/agents/debug-runner.md:14`,
`runtime/agents/repo-researcher.md:19`) and neither names `${base_prompt}`. A
`debug-runner` subagent therefore gets the composed all-lane contract, including
the fair-use limits that bind its lane, and does not get Kimi's built-in prompt
or the main-only lane table.

To take part in the composition, a body must opt in:

- `${base_prompt}` → the default profile's fully rendered prompt, which **is** our
  staged `SYSTEM.md` when one exists. This is the supported way to hand a subagent
  the session prompt.
- `${agents_md}` → the instruction files, which is how the composed all-lane
  contract reliably reaches every lane.

`runtime/config.toml` also ships `builtin_product_skills = true` with an inline
note to consider `false`; that is an acknowledged per-request cost on surfaces 5
and 13 together.

---

## Tier 2 — per-step injections

### 12. System reminders and their siblings

**Mechanism.** One registry, `IAgentReminderService.register(variant, provider)`
(bundle `132970005`, `features/reminder/reminderService.ts`), driven by a loop
hook registered as `"context-injector"` with `{ before: "full-compaction" }`:
every agent step calls `inject(runtime, context.firstStepOfTurn || rearmed)`. A
`ContextSpliced` event matching `isCompactionSplice` re-arms it. Entries are
sorted by `REMINDER_VARIANT_PRIORITY`, in which only `date_change` is special
(`-1`) — it always comes first. A provider that throws is logged
*"context provider failed, skipping it"* and dropped for that step.

**Delivery.** Decided by `appendResult` (bundle `132971670`). A bare string
becomes `wrapSystemReminder(text)` — `SYSTEM_REMINDER_PREFIX`/`_SUFFIX` at bundle
`130144591` — as its own **`role: "user"` message** tagged
`origin {kind: "injection", variant}`. A returned content-part array is appended
as `role: "user"` parts *unwrapped*, and a returned `{message}` is appended
verbatim at its own role. So reminders are not appended to the operator's message
and are not a turn boundary of their own; they are synthetic user turns, which is
why each provider dedupes by scanning history for
`origin.kind === "injection" && origin.variant === entry.variant` (`findInjections`,
bundle `132973120`).

**The thirteen registered providers.** `plan_mode`, `date_change`, `swarm_mode`,
`goal`, `background_task_status`, `tower_mode`, `agents_md`, `loadable-tools`,
`dynamic_tool_schema`, `plugin_session_start`, `todo_list_reminder`,
`permission_mode`, `notify_user_nudge`. Triggers worth knowing, because they are
the ones an operator notices:

| variant | fires when |
| --- | --- |
| `date_change` | the local date differs from the last disclosed one; suppressed if the session's disclosed `cwd` differs |
| `agents_md` | a tool *executed* against a path whose directory holds an `AGENTS.md` not already known — target dirs from Read/Edit/Write/Glob/Grep arguments, **or a parsed Bash command** (`BASH_PARSE_OPTIONS`, 500 ms) |
| `todo_list_reminder` | main agent only, only after the list was used, and only after ≥10 assistant turns since the last write **and** ≥10 since the last reminder |
| `permission_mode` | the mode changed; also once if already `auto` and nothing was injected this session |
| `background_task_status` | only while `activeTaskReminderPending`, i.e. only after a compaction |
| `plugin_session_start` | main agent only |
| `dynamic_tool_schema` | pending tool schemas exist — and it is the one variant that arrives as a raw `role: "system"` message, not tag-wrapped |

**Injection sites outside the registry.** `notify(text, {variant})`
(bundle `132970066`) appends immediately with no provider: `btw`,
`fork_context`, `agents_md_change`, `plugin_change`,
`shell_command_backgrounded`, `init`, `interruption`, `goal_cancelled`. The
repeated-tool-call breaker (`features/toolDedupe`, bundle `133456142`) appends its
three escalating reminders **into the tool result** rather than as a message, at
streaks of 3, 5 and 8, with a forced stop at 12.

**Adjacent channels that are deliberately *not* reminders**, so a filter for
`<system-reminder>` will miss them: `<skill-loaded …>` (bundle `132916770`),
`<hook_result hook_event="…">` (bundle `133047757`), `<cron-fire …>` (bundle
`133552005`), the compaction hand-off pair, media degradation placeholders, and
permission denials, which return `{output: reason, isError: true}` unwrapped.

**Disabling.** There is no master switch; the literal `<system-reminder>` occurs
only 5 times in the whole bundle, and the bare `system-reminder` substring only 8. The single per-reminder kill switch is
`KIMI_CODE_PERMISSION_MODE_REMINDER=false`, which skips registering that one
provider (bundle `135418703`). Everything else is suppressed indirectly, by state
or by the feature that owns it — see [What cannot be
cleared](#what-cannot-be-cleared).

---

## Tier 3 — beside the prompt

### 13. Tool schemas

Confirmed from the wire: `llm.request` records carry only
`{agentId, kind, maxTokens, messageCount, model, modelAlias, provider,
systemPromptHash, thinkingEffort, thinkingKeep, time, toolSelect, toolsHash,
turnStep, type}` — no `tools` field — while the request builder assembles
`tools: [...builtIns, ...mcpTools]` (bundle `143234217`), with descriptions
declared per tool via `defineDeclarativeTool(…, {description…})`. **The prose
lives in the `tools` array, never in the system prompt.**

Per-profile built-in counts, read from the bundle's own tool-name arrays:
`AGENT_TOOLS` **32**, `CODER_TOOLS` **22**, `EXPLORE_TOOLS` **8**,
`PLAN_TOOLS` **7**. MCP tools are then appended per request, and on this stack
they dominate — `runtime/mcp.json` enables `chrome-devtools` and `serena` with all
tools and `deepwiki` with `enabledTools` narrowed to three. Each server's
descriptions are its own, not Kimi's, so the harness cannot shorten them except by
narrowing `enabledTools` or disabling the server.

> The **byte** cost of the assembled `tools` array was measured twice by
> independent reads that disagreed (33 tools / 54 KB versus roughly 81 tools for
> the same request), because a session's MCP set is not visible in the bundle.
> Treat the totals as unreconciled and the counts above as the reliable
> statement. What is not in dispute is that the description prose is large, that
> it sits outside the system prompt, and that profile scoping is what keeps it
> affordable.

`toolsHash` is stable per profile — the audited session had two profiles and one
hash each — so it is the field to diff when a change is suspected of altering the
tool set.

Levers, in order of power: `tools:`/`disallowedTools:` in an agent file;
`[tools] enabled`/`disabled` in `config.toml`, which intersects every profile;
`experimental.select_tools`, which moves descriptions out of every step; and
`runtime/mcp.json`'s `enabled` plus `enabledTools` per server. Individual
built-in descriptions are hard-coded and not overridable.

### 14. The subagent's first message, and compaction

The parent's `prompt` argument reaches the child as a plain `role: "user"`
message: `runAgentTurn` (`src/session/subagent/runAgentTurn.ts`, bundle
`133744454`) calls `loop.submit({ message: { role: "user", content: [{ type:
"text", text: request.prompt }] } })`, with nothing prepended in the normal
path. The one exception is `explore`, the sole profile carrying a
`promptPrefix`: `collectGitContext` (bundle `133575288`, with its
`<git-context>` prose at `133582500`) puts a git-status block ahead of the
prompt as an extra content part.

Compaction is not a tool the model calls. `apply_compaction` exists in the
bundle only as the **event** `context.apply_compaction` (bundle `127946215`),
and there is no `context.compaction` tool definition anywhere in it. What a
summariser produces is written back through
`src/agent/contextMemory/compactionHandoff.ts`:
`buildContextCompactionShape` (bundle `130145635`) assembles the new history,
`buildCompactionSummaryText` (bundle `130147609`) prefixes the model's summary
with `compaction_summary_prefix_default` — a raw markdown import,
`src/agent/contextMemory/compaction-summary-prefix.md?raw`, bundle `130144708`,
the text that opens "The conversation so far has been compacted…" — and
`createCompactionSummaryMessage` (bundle `130147779`) returns it as
`{ role: "user", content: [{ type: "text", text }], toolCalls: [], origin:
{ kind: "compaction_summary" } }`, with no reminder wrapper. Its output is
therefore a user message like any other, distinguishable only by `origin`.

The two tag-wrapped hand-off notes are `COMPACTION_ELISION_VARIANT` and
`COMPACTION_CONTINUATION_VARIANT`, returned by `buildCompactionElisionText` and
`buildCompactionContinuationText` (bundle `130148294`, `130148965`) through
`wrapSystemReminder(...)`; `createCompactionElisionMessage` decides how many
tokens were omitted. `TASK_RESUME_TERMINATION_VARIANT` (bundle `133342901`) is a tier-two
reminder variant for background tasks that outlived a compaction, not part of the
summary template.

`[loop_control] reserved_context_size` and `compaction_max_attempts` control only
*when* it fires; `PreCompact`/`PostCompact` hooks exist (`runPreCompact`, bundle
`133059871`) but *"their return values are completely ignored"*, so the
compaction prompt is not replaceable.

### 15. Title side call

There is **no title prompt in the client**. `generateTitle` (bundle `130214357`)
and `generateAndApply` (bundle `130214900`) compose the input with
`composeTitleInput` (bundle `130211301`) from whatever registers
`IAgentTitlePromptSource` (bundle `130182319`), then hand it to
`fetchChatTitle(kimiCodeToolsUrl(baseUrl), accessToken, chatContent, …)` (bundle
`130216158`): the title is produced by a **remote HTTP endpoint on the Kimi Code
tools URL**, not by a prompt Kimi renders and sends through the agent loop. The
call is gated on `isOAuthCatalogVendor(provider.type)` and on resolving an OAuth
access token, and returns silently when either is missing — which is every
launch of this harness, whose provider is not an OAuth catalog vendor. The
result is truncated to `MAX_GENERATED_TITLE_LENGTH` (bundle `130212879`).

Because the title never becomes a request on the model route, it is invisible to
`model-proxy` and consumes none of the plan's budget. Session slugs and branch
names are computed, not generated. `kimi export`, `/export-md` and
`/export-debug-zip` are deterministic renderers.

There is **no memory-extraction or recall prompt**, and no memories placeholder
in the compaction template either: neither `WORKSPACE Memories` nor
`memoriesContent` occurs anywhere in the bundle. "Memory" in the bundle means
in-memory caches, and the `contextMemory` directory name is Kimi's own label for
compaction. `mcp__serena__*` memory tools belong to the Serena server, not Kimi.
A directory like `.agent-state/` is a project convention that only the operating
contract in the prompt tells the agent about.

---

## What each session costs

Two numbers per row, because they answer different questions. **Composed** is
`tools/prompt_measure.estimate_tokens()` over what this checkout builds right now —
deterministic, reproducible, and comparable across checkouts. **Measured** is a real
`profile.bind` record read out of a live session's `wire.jsonl` by `measure()`; it
is the only figure that includes what Kimi adds around our text, and it moves with
the workspace as well as the harness.

Estimated, this checkout, all blocks enabled and the ComfyUI module selected:

| piece | tokens | bytes | reaches |
| --- | --- | --- | --- |
| `runtime/AGENTS.md`, comments stripped | 1,517 | 6,067 | both |
| usage limits block | 503 | — | both |
| module guidance, ComfyUI | 378 | 1,510 | both |
| **composed all-lane contract** | **2,398** | **9,592** | both |
| lane table block | 670 | — | main only |
| parallel-work block | 474 | — | main only |
| **composed system prompt**, `${base_prompt}` wrapper plus both blocks | **1,149** | **4,593** | main only |
| tool schemas | priced by `--live`, never diagrammed — see surface 13 | | per profile |
| compaction summariser prompt | 9,157 per compaction, not per step | | the agent compacted |

Measured from one live session, main audience on `qwen3-primary`:

| region | tokens | who owns it |
| --- | --- | --- |
| Kimi framing | 2,369 | the built-in template, environment facts, skills listing |
| harness contract | 2,379 | our composed `AGENTS.md`, as it reached that session |
| project instructions | 2,853 | the user's own `AGENTS.md` files |
| **whole prompt** | **7,601** | 3.9% of the primary lane's 196,608-token input cap |

Same session, subagent audience on `qwen3-subagent`: framing 2,877, contract 2,379,
project 1,843, **whole prompt 7,099** — 12.7% of that lane's 55,808-token cap, which
is why the subagent lane is the one that runs out of room first.

Three cautions that belong with these numbers rather than in a footnote. The
estimator is Kimi's own heuristic — one token per four ASCII characters, one per
non-ASCII character — and **not** the provider's tokenizer, so a percentage is a
sense of scale and not a reservation. The measured contract of 2,379 predates the
final wording of the blocks, against 2,398 estimated today: the two disagree by
less than one percent and are not expected to agree exactly, which is the whole
reason both columns exist. And `${cwd_listing}` is inside the framing region, so a
prompt changes size during a session without the harness changing anything; a
measurement is a sample, not a constant. The panel therefore re-measures every
launch, appends to `prompt-measurements.jsonl`, and prints the age of the figure it
shows. `caps()` for the shipped definitions is primary 196,608, long 900,000, and
subagent 55,808 — input caps, not windows, because the proxy clamps the output
budget out of the same reservation.

The ratio worth acting on is unchanged: the harness's own contract is the largest
thing every agent carries and the only large surface the harness fully controls,
and the subagent pays for it without receiving the main prompt at all.

The panel itself costs the model nothing: the 75 lines and roughly 4.4 KB of
screen it draws — in the modal, or as the plain printout an unattended launch
gets — are never written into a file the agent reads, none of it
reaches a prompt. Worth saying, because the panel is otherwise the only
place these figures are added up, and an instrument that measures a
budget should not quietly spend one of its own.

Two of these numbers are a fence rather than a note.
`tests/test_prompt_guidance.py::SizeFenceTests` holds the harness's own
contract text under 2,150 estimated tokens and the generated half of the
main prompt under 1,300, both with module guidance excluded so the fence
bounds what this repository writes and not what a module happens to
ship. The thresholds sit above the estimated figures in the first table
and above the measured 2,379 in the second, deliberately: a fence tight
enough to be a weather report fails on every reword and gets loosened
until it means nothing. Restoring the hand-written provider-policy
section that this harness deleted - 1,142 tokens - trips it by a mile,
and one of the tests writes that bulk back to prove the fence can still
feel it.

## Inspecting the result from the host

`./prompts.sh` is the read side of all of the above, and it never starts a session.
Five modes, one per question an operator actually asks:

| command | question it answers |
| --- | --- |
| `./prompts.sh` | what will the next unattended launch compose? (the panel's screen, drawn once, with no prompt) |
| `./prompts.sh --vars` | which `${...}` names may I use, and who resolves each one? |
| `./prompts.sh --configure --enable ID --disable ID --static ID=STATE` | set the choices without launching (`--static` takes `context` or `system` as `auto`, `on` or `off`; `--all-on`/`--all-off` reset the add-ons and leave the documents alone; `--show` prints both halves and the valid ids) |
| `./prompts.sh --live` | what did the running session's model actually receive? |
| `./prompts.sh --extract` | re-read Kimi's own built-in blocks out of the running image |

No argument and `--show` draw the same screen; an unattended `./start.sh` reaches that
same drawing through the panel's own `--plain`, so a launch that cannot ask still prints
what it applied.

`--live` is the only mode that leaves the harness's own composition and reads Kimi's: it
copies the agent's Kimi home out of the container, parses every `profile.bind` record in
every session log, and prints the three-region split per audience, any residual `${`, and
the delta against the same record from the previous launch. It reads
`prompt-measurements.jsonl` and writes nothing to it - appending is the deferred job in
`start.sh`, which runs the same parse unattended after the first request, so history
accumulates whether or not anyone asks. The copy holds conversation text, so it is removed
before and after whatever else happens, and nothing under
`.local/runtime/<instance>/prompt-sessions` is meant to survive the command.

`--extract` writes `kimi-prompts/literals.json`, which is what makes `${kimi.*}` resolvable
on the next launch. Until that file exists a `${kimi.*}` reference is reported rather than
guessed at, and `start.sh` refreshes it unattended after every build.

Both the panel and `start.sh` compare the documents on disk against
`prompt-sources.json`, the digest record the renderer leaves beside the pair it staged,
and name any file that moved afterwards: staged documents are immutable, so that edit
needs a restart. At startup the panel deliberately skips the comparison, because it is
drawn before staging and would call every file stale seconds before its new bytes shipped.

## Known defects in the current composition

Recorded here because they are consequences of where text is placed, and the
placement is ours to fix.

1. **Subagent role files receive nothing by default.** See surface 11. A role body
   without `${agents_md}` carries neither the operating contract nor the workspace
   instructions; both shipped files name it now, which is the only way the
   composition reaches them.
2. **`runtime/config.toml` ships `builtin_product_skills = true`** with an inline
   note to consider `false`; that is an acknowledged per-request cost on surfaces 5
   and 13 together. The panel can switch it off, which makes the note in the config
   redundant rather than wrong.
3. **The percentage in the panel is a heuristic over a sample.** Nothing wrong with
   the arithmetic — but a reader who takes 3.9% as a reservation the proxy made
   will be misled, which is why the legend states the estimator's provenance on
   every render rather than in this file only.
4. **Tool schemas are priced but not diagrammed** (surface 13). They ride
   beside the prompt in their own request field, so no prompt figure contains them and
   the diagram's boxes still sum to the prompt alone. `prompt_measure.estimate_tools_tokens`
   prices them, `llm.tools_snapshot` is where the wire log carries them, and
   `./prompts.sh --live` prints the figure. On a session with the harness's MCP servers
   attached it is several times the size of the entire prompt, and no checkbox here
   removes any of it. The one number MCP servers can move is
   `mcp.json`, which is why `mcp.json` is listed as a staleness source.
   The earlier claim that the framing residual was "mostly" schemas was wrong: that
   residual is one token of rounding, because the schemas were never inside the figure
   it was computed from.

Historical, and fixed: the contract used to point every lane at a
*"Model runtime envelope"* section that only the main agent received, because
`${agents_md}` reaches every profile while `SYSTEM.md` reaches one. That is what
the audience split in `guidance_sections()` is for. The envelope also used to live
in a managed section of `<workspace>/AGENTS.md`, which the harness must never
write; `tools/managed_section.py` is deleted and
`tests/test_configuration.py::test_runtime_agents_md_carries_no_model_number` keeps
the contract pointing at the generated headings rather than at the workspace file.
`module-guidance.md` and `extension-snapshot/` are
both on the launcher's cleanup list now (`start.sh:69,72`), and `${now}`, which the
upstream docs advertise and `systemPromptVars()` does not implement, is unused
anywhere in this repository — `${harness.date}` is the name we do resolve.

## What cannot be cleared

| surface | can it be emptied? | how |
| --- | --- | --- |
| built-in template | yes | replacement `SYSTEM.md`, an agent file, or the panel's `system` row |
| role overlay | **no** | hard-coded; only swappable by replacing the whole profile |
| instruction files | yes | the panel's `context` row, an empty `CONTEXT.md`, and the workspace's own files are the user's |
| environment facts | mostly | omit the placeholders from a replacement template |
| skill listing | yes | panel: Skills off, full listing off, product Skills off |
| plugin, additional dirs | already nil | — |
| reply style | **no** | CLI/server argument only |
| notify guidance | **no** | force-appended by `renderAgentProfilePrompt` |
| generated blocks | yes | panel, one branch row per block |
| module guidance | yes | panel, or deselect the module |
| subagent roles | yes | panel: roles off, or delete the files |
| permission banner | yes | panel |
| per-step reminders | **no**, except `permission_mode` | indirect suppression only |
| tool schemas | partially | `[tools]`, `disallowedTools`, `mcp.json`; MCP descriptions are the server's |
| compaction | **no** | timing only; hook return values ignored |
| title call | n/a | fixed to an unrouted model |

A tabula rasa therefore has a floor. Switching every panel block off and setting
both documents to `off` — nine checkboxes and two words on one screen, no file
editing — removes everything this harness says. It does not remove Kimi's role
overlay, its force-appended notification guidance, its per-step reminders, or the
tool schemas, and no file in this repository can. That is the honest boundary of
the mechanism, and the panel says so as it prices it: with those choices made the
most this harness still stages is the main agent's lone period, and its contract
row reads zero.

## Reproducing these numbers

Every bundle offset above was read with the same two commands, and both are
safe on a 182 MB file:

```sh
LC_ALL=C grep -a -b -o 'PATTERN' /usr/local/bin/kimi      # byte offsets
dd if=/usr/local/bin/kimi bs=1 skip=OFFSET count=4000 | tr ';{' '\n\n'
```

Do not `cat` the bundle, and do not rely on `grep -o '.\{400\}pat'` — `.` will
not cross its single giant minified line. To attribute an offset, take the first
`//#region ../../packages/...` marker at or before it.

Offsets are properties of a **build**, so a rebuilt image can move every one of
them; a citation that lands in unrelated bytes is stale, not a contradiction.
Every bundle number here was read back out of the file rather than copied from
prose, and an identifier appears only if `grep -a -b -o` finds it at least once.
The rest come from this stack's own wire logs, by the two commands below. Check
either kind again after an image update before correcting a behaviour on the
strength of one.

For live prompts, read the `profile.bind` record, which carries the whole
rendered system prompt:

```sh
jq -r 'select(.type=="profile.bind") | .systemPrompt' \
  "$KIMI_CODE_HOME"/sessions/*/*/agents/main/wire.jsonl
```

The claim that `systemPromptHash` is that prompt's `sha256` is checkable the same
way — hash the record and compare it against the `llm.request` rows. Use `-j`, not
`-r`: the trailing newline `jq` adds is hashed too, and turns the digest into
something else.

```sh
jq -j 'select(.type=="profile.bind") | .systemPrompt' \
  "$KIMI_CODE_HOME"/sessions/*/*/agents/main/wire.jsonl | sha256sum
```

`docs/verification.md` has the runnable end-to-end check, including the failure
mode where a second `${base_prompt}` duplicates the built-in prompt.
