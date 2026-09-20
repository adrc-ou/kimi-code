# Modular managed-LLM + Kimi Code development harness

This project runs Kimi Code against an operator-configured managed model
provider, enforces that provider's usage policy, provides selected MCP servers
and local SearXNG search. Optional modules add application services and tools.

The core runs on macOS (Intel or Apple Silicon) and Linux/WSL2 (x86-64 or
arm64), with Docker and Compose. Modules determine their own host compatibility.
The included [ComfyUI module](modules/comfyui/README.md) supports Apple Silicon
MPS and Linux/WSL2 NVIDIA CUDA. Intel Macs can run the core without that module.

Three directories hold declarative, drop-in configuration, each with its own
authoring contract:

- [`./models`](docs/models-providers.md) — what a model *is*: window size and
  which role lanes it serves. One directory per model.
- [`./providers`](docs/models-providers.md) — what a provider *permits*:
  endpoint, credentials, and policy rules such as NRP's fair-use limits. One
  directory per provider.
- [`./modules`](docs/modules.md) — optional services and tools that run beside
  the agent.

Nothing in `.env` describes a model or a policy limit. `.env` supplies
credentials and process bounds; the numbers that decide how much of a model you
may use at once are derived from the definitions you picked at launch.

## Prerequisites

For startup status checks, functional tool tests, and step-by-step optional
account setup, see [the verification guide](docs/verification.md). `./start.sh`
runs a quick service/MCP check at readiness and then the full functional pass in
the background once the stack settles, recording both in
`.local/runtime/<instance>/service-check.json`. `./prompts.sh` inspects and edits the session context
described below without starting a session, and reports a prompt file you changed
without restarting.

Hosts need:

- Docker with Docker Compose (Docker Desktop on macOS/Windows);
- Git;
- Python 3 for the host setup scripts;
- enough free disk space for container images and models;
- network access to GitHub, Python package indexes, Docker Hub, and configured
  model/MCP endpoints.

Run `./start.sh` as your own account, never with `sudo`: your UID becomes the
container user, so a root launch would run the agent as root and leave host
artifacts owned by root.

## Initial configuration

1. Copy `.env.example` to `.env`.
2. Set an API key for each model you intend to use. The variable names come
   from the definitions, not from a list in this README: each
   `models/<id>/model.toml` names the `.env` variable holding a key for that
   model alone in `key_env`, and the provider-scoped key it falls back to in
   `credential`, whose `[[credential]]` table in `providers/*/provider.toml`
   names its own variable and, in `key_url`, where to obtain the key. A
   model-scoped key wins; set only the provider-scoped one and every model of
   that provider shares it.
3. Set `SEARXNG_SECRET` to the output of `openssl rand -hex 32`.
4. Set `WORKSPACE_PATH` to a dedicated directory containing only material the
   agent is allowed to inspect and change. The agent can always see this exact
   host path, so avoid a location whose name you need to keep private.
5. Leave everything else blank or defaulted. `NRP_BASE_URL` redirects the NRP
   endpoint for this deployment only; `KIMI_BACKGROUND_TASK_SLOTS` blanks to a
   value derived from the resolved plan; the `MODEL_PROXY_*` names are process
   bounds, not policy.

Anything that says which model is served, how large its window is, or how much
of it you may use at once lives in `./models` and `./providers`, never here. If
you are migrating an older `.env`, the launcher prints what it ignored:

| Old variable | Now |
| --- | --- |
| `LITELLM_API_KEY` | a model `key_env` in `models/<id>/model.toml`, or `NRP_API_KEY` (named by `providers/nrp/provider.toml`) as the provider-wide fallback |
| `LITELLM_UPSTREAM_ORIGIN` | `providers/nrp` `[endpoint].base_url`, optionally overridden by `NRP_BASE_URL` |
| `LITELLM_MODEL_ID` | `models/<id>` `model = "…"` |
| `NRP_MODEL_CONTEXT`, `NRP_FAIR_USE_PERCENT`, `NRP_PARALLEL_CONTEXT_BUDGET`, `NRP_MODEL_MAX_CONCURRENCY`, `NRP_OUTPUT_TOKENS_PER_MINUTE`, `NRP_OUTPUT_RATE_HEADROOM_PERCENT` | `[[rule]]` / `[safety]` in the provider, and `[context]` / `[lane.*]` in the model |
| `NRP_{PRIMARY,LONG,SUBAGENT}_MAX_OUTPUT_TOKENS` | `[lane.*].output_clamp_tokens` |
| `KIMI_SUBAGENT_CONCURRENCY` | derived from the plan; see "Resolved policy enforcement" |
| `NRP_UPSTREAM_SOCK_READ_TIMEOUT`, `NRP_MAX_REQUEST_SECONDS`, `NRP_INPUT_GUARD_PERCENT`, `NRP_MEDIA_TOKEN_ESTIMATE`, `NRP_REQUEST_USAGE`, `NRP_MAX_*_BYTES`, `NRP_MAX_QUEUED` | same value under a `MODEL_PROXY_*` name |

## Starting and stopping

Run:

```bash
./start.sh
```

The launcher first asks which model serves the primary agent and which serves
subagents. Each picker lists the models defined in `./models` alphabetically by
label, marks the last-used choice, and pre-selects it; only models whose provider
exists in `./providers` are offered. Press ↑/↓ to move and Enter to continue.
Answering non-interactively (`--non-interactive`, or a single available model)
reuses the previous choice, and `HARNESS_PRIMARY_MODEL` /
`HARNESS_SUBAGENT_MODEL` override one picker.

Both models then resolve against their providers' rules into one enforcement plan,
and every number downstream — lane sizes, Kimi's model tables, the subagent
fan-out, the proxy's gates — is derived from that plan. Next the launcher shows
compatible modules in a checkbox menu. Use ↑/↓ to move, Space to toggle, and Enter
to continue. Last session's enabled modules appear first and are checked; each
group is alphabetical by label. On the first run all modules are unchecked. If
none are compatible, this step is skipped.

Next comes the existing Kimi version menu, followed by each selected module's
version menu. Missing required module variables are prompted for this session
only; add them to `.env` yourself to persist them. Secret inputs are hidden.

After all choices, the launcher initializes the workspace, installs selected
versions, and generates private runtime configuration. That is where each
selected model's API key is read: a model with no value in its own `key_env`
falls back to the provider-scoped variable its `credential` names, and when
neither is set you are asked for that model's key, for this session only. The
prompt names both variables, so adding either one to `.env` skips it next time —
the model-scoped name keeps the key to that model, the provider-scoped name
shares it across every model of that provider. The launcher then snapshots module
assets and approved project extensions, and starts the segmented stack. Ctrl-C
stops the containers and registered native module processes. Persistent
workspace data is never removed when a module is unchecked or deleted.

Before announcing readiness, the launcher registers `/workspace` with Kimi's
authenticated local API. This persists in Kimi's state volume and makes it the
default choice in a fresh UI. Existing sessions and remembered workspace choices
are preserved; select `/workspace` once if your browser remembers another folder.
Folder browsing and sandbox permissions are unchanged.
The launcher then tests the banner's localhost URL followed by its network URLs
in their displayed order and opens the first reachable one in the default system
browser, including its authentication fragment. macOS uses `open`; Linux uses
`xdg-open` (or `wslview` when installed on WSL). If no address is reachable or no
browser opener is available, startup continues with a message for manual access.

For repeatable automation, set explicit choices and disable prompts:

```bash
HARNESS_PRIMARY_MODEL=qwen3_8_flash_next HARNESS_SUBAGENT_MODEL=qwen3_8_flash_next \
  HARNESS_MODULES= KIMI_CODE_VERSION=0.42.0 ./start.sh --non-interactive
```

`HARNESS_PRIMARY_MODEL` and `HARNESS_SUBAGENT_MODEL` take model directory
identifiers and skip the corresponding picker; an identifier that is not available
for that role stops startup. `HARNESS_MODULES` is a comma-separated list of module
directory identifiers; an explicit empty value selects the core only. If omitted in
non-interactive mode, the previous compatible selection is reused. Missing required module
variables cause non-interactive startup to fail without printing their values.

The requested versions must still exist in the compatible official release
catalog. Release metadata failures stop startup instead of silently using stale
data.

Services are available at:

- Kimi Code: <http://127.0.0.1:5494>
- Enabled modules document their own service URLs.

Generated secrets and native logs are kept under the instance-specific
`.local/runtime/` directory and are excluded from Git. Ephemeral credentials,
rendered provider configuration, and bridge certificates are deleted at normal
shutdown. The private NRP cache salt persists so cached responses remain
isolated across restarts.

## Resolved policy enforcement

`model-proxy` never holds a policy number of its own. At launch
`tools/models.py resolve` reads the two selected models and the `[[rule]]` tables of
their providers and writes one plan; the proxy mounts that plan as its enforcement
input and refuses to serve if it cannot honour it. Adding a provider whose terms
look nothing like NRP's — a token-per-minute ceiling, a hard context cap, no
concurrency rule at all — is a definition change, not a proxy change.

The rules NRP publishes, transcribed into `providers/nrp/provider.toml`:

- 200,000 output tokens per minute per API token *and* model, as a
  `output_tokens_per_minute` rule at `credential_model` scope. This is the only
  rule the gateway meters for you, and it returns HTTP 429. A rolling one-minute
  ledger books each attempt's full output clamp before it starts and settles it
  against measured usage; the gateway's own `x-ratelimit-*` header wins whenever
  it reports less headroom.
- Exactly one concurrent request once a request uses 35% or more of the model's
  context, as `exclusive_above_context_fraction` at `model` scope. The resolver
  marks such a lane exclusive and the proxy admits it only when the counter is
  empty.
- Otherwise at most 16 concurrent requests for the model, with their combined
  context inside 35% of the window, as `max_concurrent_requests` and
  `aggregate_context_fraction`, both at `model` scope.

Because both scope on the model identifier, selecting one model for both lanes
makes them contend for the same permits, while selecting two models shares only
the per-key rate ledger. The counter-keying table in
[docs/models-providers.md](docs/models-providers.md) states exactly how a
selection resolves.

Two numbers in the provider file are deliberate under-use, and they are the only
tunables: `[safety].context_margin_percent` (95) and
`output_rate_margin_percent` (90) multiply the provider's own thresholds, so a
margin can never push a budget above the published rule. At the shipped
definition the model advertises 1,000,000 tokens, so the aggregate ceiling is
350,000 and the in-flight budget is 332,500; the rate ledger books 180,000 output
tokens per minute.

A lane's reservation is its input cap plus its output clamp, both from
`[lane.*]` in the model definition and both revalidated against Kimi's live
rendered configuration on every policy refresh — a configuration the proxy cannot
honour answers HTTP 503 instead of passing unmeasured traffic. At the shipped
numbers `qwen3-primary` reserves its whole native 262,144-token window,
`qwen3-long` reserves 965,536 and is therefore strictly alone, and each
`qwen3-subagent` reserves 64,000, which is 5 at once against the 332,500
budget: the largest fan-out the rules allow. `qwen3-long` stays opt-in because its
extra context comes from YaRN extension of the model's native window rather than
native attention. `./start.sh` derives Kimi's subagent concurrency from the same
plan, so Kimi is told to run exactly as many children as the proxy will admit, and
the generated envelope appended to its system prompt instructs it to use the whole
envelope instead of self-limiting.

Subagent and swarm wall-clock limits are unlimited: `timeout_ms = 0` in
`runtime/config.toml`, re-pinned at every launch, never via the environment. A
long run is bounded instead by `MODEL_PROXY_SOCK_READ_TIMEOUT` per stalled
upstream read and `MODEL_PROXY_MAX_REQUEST_SECONDS` per client request, so a wedged
stream cannot hold a scarce fair-use permit. Neither is a task-length limit.
`docs/verification.md` shows how to read the policy currently in force.

## Persistent workspace contract

`start.sh` creates `.agent-state/` and its initial state files automatically.
Each selected module declares additional relative workspace directories.
Existing files and directories are preserved. Initialization rejects symlinked
children rather than following them outside the workspace.

Module `AGENTS.md` instructions are staged for the session and appended to its
system prompt, each under a heading naming the module that owns it. The workspace's
own `AGENTS.md` is never written: it belongs to the project Kimi is working on.
Deselection removes the module's active instructions and runtime assets, while
leaving its persistent data alone. No separate initialization command is needed.

## Executable project extensions

Kimi agents, skills, and project MCP declarations can execute code or replace
the agent identity. The launcher therefore requires host-side approval for
`.kimi-code/agents`, `.agents/agents`, `.kimi-code/skills`, `.agents/skills`,
and `.kimi-code/mcp.json`. Ordinary project `AGENTS.md` files remain writable
workspace guidance and do not require this approval.

Empty extension directories and a zero-byte MCP mount-point file left by Docker
do not require approval. The MCP placeholder is mounted as an empty JSON object.
Adding extension files or a nonempty MCP configuration requires approval.

With the stack stopped, inspect and approve the current exact content:

```bash
./extensions.sh list
./extensions.sh approve
```

Approved content is copied to an operator-owned snapshot and mounted read-only
for the next session. Stop the stack, edit the extension, review it, approve it
again, and restart. Revoke all approval with `./extensions.sh revoke`.

## Version and dependency policy

Kimi is downloaded from official Moonshot release assets. The launcher selects
the Linux asset matching the host CPU architecture, because Kimi
itself runs in a Linux container. The SHA-256 digest published with the release
is verified during the image build.

Base images are digest-pinned, the GitHub MCP source is commit-pinned, and npm
browser tooling is installed with `npm ci` from `container/package-lock.json`.
Core/module dependency locks, module compatibility catalogs, and vulnerability
exceptions are checked in CI. No third-party source distribution, model, custom
node, or Python package is vendored in this repository.

`.dockerignore` excludes `.env`, `.local`, Git metadata, bytecode, and generated
archives from the root build context. Do not remove the `.env` exclusion: the
provider API keys must never be sent as Docker build-context data.

## MCP servers

Local stdio MCP servers are child processes launched by Kimi when a workspace
session begins. They are not independent Compose services. Open a fresh session
after changing core or module `runtime/mcp.json`, then use `/mcp` to inspect connections.

Enabled by default:

- DeepWiki;
- Chrome DevTools;
- Serena;
- Context7, which needs no credential at all: Upstash answers anonymous calls at a reduced
  rate limit, so enabling it is a config flag, not a signup task.

Disabled pending operator credentials or configuration:

- GitHub.

Configure and authenticate one remote service at a time, create a fresh session,
inspect `/mcp`, and make one harmless read-only call before enabling the next.

Setting `CONTEXT7_API_KEY` raises Context7's limits and unlocks private repositories, but it
changes nothing while `runtime/mcp.json` omits `bearerTokenEnvVar` from that entry. Adding the
line is what activates the key, and it also makes Kimi require a non-empty value: an empty one
fails the server closed instead of falling back to anonymous access.

For GitHub, use a dedicated fine-grained read-only token restricted to required
repositories. The Kimi process can access any token placed in its environment.

Core and selected module MCP declarations are merged into a private runtime
snapshot and staged into the agent's read-only runtime mirror, alongside
selected skills, agents, and helper tools. Duplicate asset or MCP names fail
startup instead of overriding core definitions. Edit this repository and restart
to change declarations. OAuth and session state remain in Docker volumes.

The Kimi home copy of the MCP declaration is an immutable staged copy, so
`/mcp-config` and UI edits to it fail closed rather than half-applying. Add or
change a server on the host in `runtime/mcp.json` (or the project
`.kimi-code/mcp.json` approval path below), then restart and open a fresh
session.

Chromium runs as the non-root agent with its process sandbox enabled and a
reviewed seccomp profile. Do not add `--no-sandbox`, `SYS_ADMIN`, or an
unconfined seccomp setting.

## Web search

`compose.search.yaml` starts SearXNG as an unprivileged numeric user and a local
adapter with bounded workers, queue, request, response, result, and field sizes.
The adapter translates Kimi's schema to SearXNG's JSON API. Neither service is
published to the host.

Compose networks isolate the model proxy, search backend, and module services. Only
Kimi joins the intended client-side networks; separate egress networks prevent
the private networks from becoming a second flat service mesh. Each credential
the selection actually uses is mounted only into `model-proxy`, as a file secret
generated for that session.

## Validation

Install the test prerequisites first. `aiohttp` is what the proxy module imports, and
`proxy/requirements.in` is the file that pins its version, so install from there rather
than repeating a number that will go stale. Without it the policy-enforcement suite
reports a skip instead of running — a green `OK` that has not checked a single rate,
reservation, retry, or route assertion:

```bash
python3 -m pip install -r proxy/requirements.in
```

If your `TMPDIR` sits on a `noexec` filesystem (a tmpfs `/tmp` usually is), the launcher
tests skip for the same reason; point `TMPDIR` at an executable directory to run them.

Static and unit tests:

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
python3 -m compileall -q proxy scripts search-adapter tests tools modules
git ls-files '*.sh' -z | xargs -0 -n1 bash -n
git ls-files '*.sh' -z | xargs -0 shellcheck
ruff check .
python3 scripts/check_locks.py
npm ci --ignore-scripts --prefix container
```

Run each installed module's test suite too:

```bash
for suite in modules/*/tests; do
  [ ! -d "$suite" ] || python3 -m unittest discover -s "$suite"
done
bash tests/compose-config.sh
```

Module hardware acceptance instructions live with each module.

Then open a fresh Kimi session, run `/mcp`, and make one harmless call through
each enabled server. Remote authentication cannot be validated without the
operator's credentials.

Use `./shell.sh` from a second terminal to open a shell in the running Kimi
container. It resolves the same instance, Compose files, generated secrets, and
verified external paths as the launcher.

## Persistent agent state and Kimi settings

The agent's own state lives in Docker named volumes, not on host binds: Kimi
home, Serena cache, the read-only runtime asset mirror, and the placeholder
volumes that shadow user-level agents, skills, and plugins. Settings saved in
the Kimi UI therefore persist across restarts, rebuilt images, and changes of
host identity. Do not delete state volumes to fix an ownership mismatch.

Before Kimi starts, a network-isolated root initializer repairs volume
ownership to the configured agent UID/GID and stages operator-controlled content
into those volumes: the rendered runtime `AGENTS.md` and `SYSTEM.md`, the merged
MCP declaration, and the selected skills/agents/tools tree. It then writes
`config.toml` by overlaying the user-owned keys from whatever the volume already
holds onto the freshly rendered baseline, and writes Serena's global
configuration from `runtime/serena-config.yml` into the Serena volume so that a
session cannot leave the language server selection altered. Those two are
agent-owned and re-pinned at every launch instead of flagged, because the tool
that owns each file rewrites it through its own directory and an immutable flag
would make that fatal at first use. Everything else it stages is root-owned and
readable but never writable by the agent; the three files inside the agent-owned
Kimi home additionally carry ext4 per-file immutable flags, because owning a
directory lets you unlink anything in it regardless of the file's
owner. That is what allows the Kimi home volume to be writable at all. The
initializer mounts only its own script, the staging inputs, and those volumes: no
workspace, Docker socket, network, or credentials. Kimi itself remains non-root
with all capabilities dropped.

`runtime/config.toml` is the commented source of truth for the static half of
generated settings; the model and provider tables are appended to it from the
resolved plan, so `default_model`, every `[models.*]` window, every
`[providers.*]` base URL, and `[secondary_model]` arrive from `./models` and
`./providers` rather than from that file. The user-owned keys are declared in
`runtime/config-policy.json` and currently cover only thinking, telemetry,
background-task, and experimental UI controls. Every other key is policy: a UI or
`/config` edit that changes one is silently re-pinned at the next launch, because
the proxy's provider lane cannot be negotiated from inside the sandbox. That
includes the default model, the forced subagent model, permission mode, plan mode,
skill and agent directories, provider definitions, model context sizes, and the
`[subagent]`/`[swarm]` `timeout_ms = 0` that keeps subagent runs uncapped by wall
clock. Widen `runtime/config-policy.json` deliberately if you want the UI to own
another key — but note that making `default_model` user-owned again would let an
in-session `/model` choice survive a restart and drift away from the plan the
proxy enforces. A stored `config.toml` that cannot be parsed is renamed to a
timestamped `.unreadable-*` file and replaced with the baseline rather than
starting Kimi with broken configuration.

Model provider credentials, the per-instance base URL, and the internal proxy
token are regenerated each launch and are never read back from the volume.

## Customising the session context

Two documents carry instructions that are not the user's prompt, and both are
yours to edit in the project root:

| file | what it becomes | who receives it |
| --- | --- | --- |
| `SYSTEM.md` | the session's system prompt | the primary agent |
| `CONTEXT.md` | the runtime operating contract, installed as `AGENTS.md` in Kimi's home | the primary agent and every subagent |

Each is loaded the same way: the file is used if it exists, and Kimi's or this
harness's own text is used if it does not. `SYSTEM.md.example` and
`CONTEXT.md.example` are written documentation of those conventions and are
**never loaded** — an `.example` file is an example, and a harness that quietly
read it would make a surprise of the one file the operator expected to be inert.
`CONTEXT.md` falls back to `runtime/AGENTS.md`, which is this harness's own
contract; `SYSTEM.md` falls back to Kimi Code's built-in prompt, which the
harness reaches by staging a `${base_prompt}` wrapper rather than by copying the
text out of the binary.

An empty file is a decision, not a missing file. Existence picks the authority
and emptiness picks the payload: a non-empty `SYSTEM.md` is amended by whatever
add-ons are switched on, an empty one leaves those add-ons as the whole prompt,
and an empty one with every add-on off asks for no instructions at all. Kimi
discards a prompt that is blank once trimmed and silently uses its own, so that
last case is staged as a lone period — one token, no instructions. Only an absent
`SYSTEM.md` can reach the built-in prompt.

Both documents are written into the instance runtime directory by
`tools/render_runtime.py`, passed to Compose as `KIMI_SYSTEM_MD` and
`KIMI_RENDERED_AGENTS_MD`, and installed by the root-only initializer as
root-owned, mode `440`, immutable files. Editing them and restarting changes the
agent's instructions; the agent cannot change them from inside a session, so a
mid-session edit does nothing until the next launch.

### What the startup panel adds

`./start.sh` opens a context panel before it renders anything: a diagram of the
documents above with their resolved sizes, then a checkbox for each block the
harness can add. Everything starts enabled and your changes are remembered for
the next launch. Run `./prompts.sh --configure --enable ID --disable ID` to
change them without starting a session (`--configure` alone only prints the current
selection), or `./prompts.sh --show` to see what an unattended launch will apply — a
launch with no terminal cannot prompt, so it prints the remembered selection
instead of asking. `./prompts.sh --live` reads what the running session's model
actually received, and `./prompts.sh --vars` lists every placeholder a prompt file
may hold and who resolves it.

Sizes on that screen are token counts, which is the unit the context window is
denominated in, each shown against the input cap of the lane serving that audience. A
number marked `~` is the harness pricing its own text before Kimi has rendered
anything; a bare number is a real request measured from this workspace's own session
logs, so it describes the previous launch rather than this one, and it ages on screen
instead of being presented as current. Kimi's framing cannot be priced without having
been seen, which is why a first launch shows `?` for the whole figure.

The blocks are the ones generated from this repository's own definitions rather
than written by you:

- **Usage limits** and the **per-model lane table** state the numbers derived
  from `./models` and `./providers`, which are the numbers `model-proxy`
  enforces. The table is for the primary agent, which is the one that decides how
  to fan out; the limits reach every lane, because every request spends them.
- **Parallelism guidance** is for the primary agent only, since a subagent cannot
  start one.
- **Module guidance** is each selected module's own `AGENTS.md`, which has no
  other route to the agent.
- **Skills, subagent roles, the full skill listing, and the permission banner**
  are the Kimi settings that decide which capabilities load at all.

Switching a block off changes no limit: the proxy enforces the same plan either
way, and the agent is simply no longer told what that plan is. Turning every
block off and emptying both prompt files is the tabula rasa — what reaches the
model is your own prompt and the tool schemas, nothing else.

### Placeholders

`SYSTEM.md` is the complete prompt for the primary agent, and two modes are
equally supported. Include the literal `${base_prompt}` where you want Kimi
Code's own built-in prompt and your text amends it, which is the pattern
`SYSTEM.md.example` demonstrates. Omit the placeholder and your text *is* the
prompt, replacing Kimi's built-in one outright.

Substitution happens after the harness strips comments, so every variable is
available in either mode: `base_prompt`, `role_additional`, `product_name`,
`reply_style_guide`, `notify_user_guidance`, `os`, `windows_notes`, `shell`,
`cwd`, `cwd_listing`, `agents_md`, `additional_dirs_info`,
`additional_dirs_section`, `skills`, `skills_section`, and `plugin_sections`.
That is deliberate power: much of what makes the built-in prompt useful arrives
through those variables rather than being fixed text — the working directory and
its listing, the applicable `AGENTS.md` files, the skills listing, and the plugin
sections. A replacement prompt that does not name them loses them, so name the
ones you want.

Five more names are resolved by the harness before staging rather than by Kimi.
`${harness.date}` expands to today's date, and `${kimi.system_default}`,
`${kimi.coder_role}`, `${kimi.explore_overlay}`, and `${kimi.task_agent_prefix}`
expand to Kimi Code's own built-in blocks read from the running image, so you can
quote a piece of it instead of copying it.

Substitution replaces *every* occurrence, so mentioning `${base_prompt}` twice
would make the whole built-in prompt appear twice. A misspelling would otherwise reach the
model as literal text with no error anywhere to be seen, so the launcher refuses
to start on a `${...}` within two characters of a real name, and on a duplicated
`${base_prompt}`. Every other `${...}` passes through untouched, and anything
inside a code span is exempt — quoting `$HOME` in an example is prose, not an
intention.

`<!-- ... -->` comments are stripped from both files before staging, which is
what lets `SYSTEM.md.example` and `CONTEXT.md.example` explain these conventions
in the place you would look for them. A comment is invisible in a Markdown reader
and fully visible to the model, so write them freely in `SYSTEM.md` and `CONTEXT.md`
and pay nothing for them.

Stripping happens before anything is measured, so the file you edit is not the
byte count you are charged for. A `CONTEXT.md` that reads as 12 KB in your editor
and costs 4 KB on the panel is behaving correctly; `wc -c` describes your draft,
and `./prompts.sh --show` describes the prompt.

## What the agent can see of the host

Bind sources appear verbatim in `/proc/self/mountinfo`, which is world-readable
and cannot be hidden from the container that owns the mount, and named-volume
sources appear as `/docker/volumes/<project>_<name>/_data`. The agent therefore
always learns the exact `WORKSPACE_PATH`, the Compose project name, and, when the
workspace has approved project extensions, the extension snapshot paths under
this checkout. Those paths normally sit below your home directory, so your
account name is included; keep it out of `WORKSPACE_PATH` and the harness
checkout location if it must stay private.

Everything else is now behind named volumes: the harness checkout layout, the
instance directory name, the rendered configuration, and the generated secret
filenames under `.local/runtime/` no longer appear in the agent's mount table.
Set `COMPOSE_PROJECT_NAME` to something neutral if the default
`kimi_code_<instance-id>` name is not one you want published inside the
sandbox.

## Optional host limits and cleanup

Service-local ingress, queue, metadata, transfer, PID, tmpfs, and log bounds are
always enabled. CPU and RAM needs vary widely with compilers and model sizes, so
host-wide ceilings are opt-in. Copy `compose.limits.yaml.example` to
`compose.limits.yaml`, tune it for the machine, and restart.

The launcher reports its instance ID. Generated runtime material for that
instance lives below `.local/runtime/INSTANCE_ID`; replaceable native module installations live
below `.local/runtime/INSTANCE_ID/module-data/<module>`. Stop the matching stack before removing
either directory. Never delete the workspace as part of cache cleanup.

An ordinary `launcher.lock` file may remain after a crash; advisory locking
makes an unlocked file harmless. Both macOS and WSL2 use Python's advisory
locking support; no separate `flock` command is required. If a launcher reports
another owner, stop that exact launcher first. Do not delete an active lock file
or remove the project, workspace, or the entire `.local` tree as lock recovery.

The host scripts report a file, line, and exit status for unexpected failures.
`start.sh` also checks that Docker is ready before selecting releases. Optional
module settings are documented with each module.

The release selector uses Python's verified HTTPS context. On macOS, if Python
has no default CA certificates, it loads the system bundle at `/etc/ssl/cert.pem`
for both release metadata and checksum downloads. Existing trust stores and
explicit `SSL_CERT_FILE` / `SSL_CERT_DIR` environment settings take precedence.
For an organization-specific CA bundle, export `SSL_CERT_FILE=/path/to/ca-bundle.pem`
before launching. Certificate and hostname verification remain enabled.

## What Docker does and does not protect

The agent normally runs without root privileges in a read-only container. UID
and GID collisions are resolved by reusing numeric base-image identities rather
than deleting accounts. The Docker socket and host filesystem are not mounted.
Its writable bind mount is limited to `WORKSPACE_PATH` and any explicitly
validated module mounts.

The workspace is intentionally not protected from the agent. The agent also has
network access, so Docker cannot prevent workspace exfiltration or unsafe
downloads. Use a dedicated workspace, retain manual approval mode, and monitor
tool calls.

Native module processes run with the host user's permissions. Read each module's
security boundary before enabling it. Modules are operator-trusted harness code,
not workspace-supplied plugins; install or edit them only while stopped.
