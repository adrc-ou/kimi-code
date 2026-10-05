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

- Docker with Docker Compose v5.0.2 or newer (Docker Desktop on macOS/Windows);
  older releases omit an explicit `create_host_path: false` from their resolved
  configuration, so the launch hygiene gate cannot read it and the launcher
  refuses the launch rather than misreport it;
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
4. Run `./start.sh` once. Its first screen asks which directory the session
   should work in — the workspace is chosen there rather than configured in
   `.env`, because it is the one decision every launch has to re-make and it is
   where the agent's whole writable world begins. Point it at a dedicated
   directory containing only material the agent is allowed to inspect and
   change. The agent can always see this exact host path, so avoid a location
   whose name you need to keep private.
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

The questions come one per screen in a fullscreen modal that owns the terminal
while it is open. The modal is held for the whole interactive run rather than
raised per question, so the launch printout never shows between steps. Each step's
human label is the top-left line of the window, with a rail under it reading
`N of M` so you can see how much launch is left — `M` is fixed for the run and
counts only the screens you will actually be shown, so a skipped step is never
enumerated and the first screen you see is always step 1. Content scrolls inside
its own region when it does not fit, and a footer lists the keys that step answers
— in the order that matters when a narrow window makes it drop some.
↑/↓ move the focus marker, which is where `Space` lands: it ticks a checkbox row,
and on a radio list it moves the mark to the row under the cursor. `Enter` continues
with the answer as the screen shows it — whatever is ticked, or whichever row
carries the mark — so the cursor is never the answer by itself. `Backspace` returns
to the previous step, and `Ctrl-R` puts the currently visible choices back to their
starting state. Nothing is answered by typing a menu number,
and no key does anything the step has not named. `?` opens the full reference —
every spelling of every key, including `k` and `j` for the arrows, which the footer
keeps to one spelling each to stay short — on the steps that list something; the
steps that ask you to type a value hand `?` and `Space` to the value instead, so the
only keys they print are the ones that still work there. Only the step that shows
which system prompts the session will run uses the reading column at the right of a
wide window, so it is the only step that spends list width on one; every other step
has the row under its cursor's own words to say what it means, and gets the full
width.

The first question is asked before that modal opens, because everything after it
is keyed on the answer: which directory the session works in. It is drawn on your
terminal even when you pipe the launcher's output to a log, and only the chosen
path travels back to the launcher, so nothing else can mistake the question for
part of the answer. The screen is headed `Choose a workspace directory` and lists
up to ten directories this
checkout has been used with, newest first, with the most recent already marked —
so the ordinary launch is `Enter`. Each row shows the whole host path with its
final directory name in bold, since that is the part you are scanning for. Above
the list, `New Workspace...` becomes a text field the moment the cursor reaches
it, opening at `/` with the insertion point after it: `Tab` completes a directory
the way bash does (one match is filled in, several insert what they agree on and
are listed at the bottom of the panel, files are never offered), `←`/`→` step the
insertion point, `↑`/`↓` take it to either end, and a path too long for one row
wraps onto the next rather than scrolling out of sight. `Esc` hands the arrow keys
back to the list, and `Tab` takes them again.

An answer is checked before the screen closes. It must be absolute, must not be a
filesystem root, must be a directory rather than a file, must be one you can read
and write, and must not hold this checkout or your own home directory — either
would put the operator's credentials inside the agent's writable world. A symlink
is replaced by what it points at. A path whose last name
does not exist yet is allowed, since creating it is the usual intent. A remembered
row is asked again on every launch, because the disk may have moved: if it is gone
but could be created you are asked `That directory does not exist. Create it?`, and
if it cannot be, `Remove it from the Recent Workspaces list?` — `←`/`→` move
between the buttons, `Enter` takes one, and `Esc` cancels. Nothing is created and
nothing is deleted by closing the screen.

Your choice is remembered per checkout in `.local/workspaces.json`, which is the
one file that deliberately survives an instance, and it is what `./shell.sh`,
`./extensions.sh`, `./prompts.sh` and `--non-interactive` act on: an unattended
launch takes the most recently chosen workspace, and refuses with this remedy if
you have never been asked.

The launcher first asks which model serves the main agent and which serves
subagents. Each picker lists the models defined in `./models` alphabetically by
label, marks the last-used choice, and pre-selects it; only models whose provider
exists in `./providers` are offered.

A model with both agent lanes is listed twice on the main-agent screen, because
how much context the main agent launches with is the operator's decision and it
is the one decision the two lanes differ on. The two rows are peers, not a
default and an upgrade, and each says which way it trades:

- `… - Long (queued, 1M window)` — the 1,000,000-token window, and the shipped
  default, listed first and pre-selected. Reserving that much of the provider's
  in-flight pool means a request which actually fills most of it runs alone and
  everything else waits for it.
- `… - Medium (concurrent)` — the model's own 262,144-token window, whose
  smaller reservation leaves the pool room to overlap requests.

Neither row is faster in the sense that matters most: both are served by the same
model at the same speed, and the difference is how much of one minute's capacity
a request spends. The wider row wins whenever the work is long, which is why it
opens first; the narrower row wins when many short requests want to be in flight
at once. Answering non-interactively (`--non-interactive`, or a single available
model) reuses the previous choice, and `HARNESS_PRIMARY_MODEL` /
`HARNESS_SUBAGENT_MODEL` override one picker. `HARNESS_PRIMARY_MODEL` also accepts
the wide row's id, which is the model id with an `@long` suffix.

Both models then resolve against their providers' rules into one enforcement plan,
and every number downstream — lane sizes, Kimi's model tables, the subagent
fan-out, the proxy's gates — is derived from that plan. Next the launcher shows
compatible modules as checkbox rows. Last session's enabled modules appear first
and are checked; each group is alphabetical by label. On the first run all modules
are unchecked. If none are compatible, this step is skipped.

Next comes the existing Kimi version menu, followed by each selected module's
version menu — radio rows both, so `Space` puts the mark on a version and `Enter`
takes the one carrying it. Missing required module variables are prompted for
this session only; add them to `.env` yourself to persist them. Secret inputs are
hidden.

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

Because the modal is still covering the terminal while that work runs, anything it
would have printed is spooled to `launch-notes.log` in the instance runtime
directory and read out the moment the window closes, in order, before the launch
says anything else. A step that fails mid-pass is reported the same way: the modal
is released first, since leaving the alternate screen discards whatever was painted
on it, so the diagnostic and the notes that precede it both land on the normal
screen.

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

On a Mac the launcher also holds off the idle-sleep timer for as long as it runs,
because a laptop that dozes takes the containers, the model proxy, and any module
job down with it. It uses `caffeinate`, which is present on every macOS release
and identical on Intel and Apple Silicon, and ties the assertion to the launcher's
own process, so the machine is releasable again even if the launcher is killed
outright. `HARNESS_KEEPWAKE=false` opts out and leaves the Mac on its normal
schedule. The setting is ignored on Linux and Windows, where sleep is the host's
business.

For repeatable automation, set explicit choices and disable prompts:

```bash
HARNESS_PRIMARY_MODEL=qwen3_8_flash_next HARNESS_SUBAGENT_MODEL=qwen3_8_flash_next \
  HARNESS_MODULES= KIMI_CODE_VERSION=0.42.0 ./start.sh --non-interactive
```

`HARNESS_PRIMARY_MODEL` and `HARNESS_SUBAGENT_MODEL` take model directory
identifiers and skip the corresponding picker; `HARNESS_PRIMARY_MODEL` additionally
takes the `<id>@long` suffix to choose the Long (queued) window instead of the
Medium (concurrent) one it would otherwise launch on. An identifier that is not
available
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
`.local/runtime/` directory and are excluded from Git. Ephemeral credentials and
rendered provider configuration are deleted at normal shutdown — and swept again
before a launch renders anything, because a launcher that is killed outright runs no
shutdown at all, and a key left in that directory would otherwise outlive every
reboot until someone stopped the stack politely. That pre-launch sweep reaches every
instance directory in the tree, not just the one this launch will use: the id
digests the checkout and workspace paths, so a clone that has moved leaves a
directory nothing later ever names. The private cache salt is the
deliberate exception: it persists so cached responses remain isolated across
restarts, and it is never a provider credential.

## Resolved policy enforcement

`model-proxy` never holds a policy number of its own. At launch
`tools/models.py resolve` reads the two selected models and the `[[rule]]` tables of
their providers and writes one plan; the proxy mounts that plan as its enforcement
input and refuses to serve if it cannot honour it. Adding a provider whose terms
look nothing like NRP's — a token-per-minute ceiling, a hard context cap, no
concurrency rule at all — is a definition change, not a proxy change.

The terms NRP publishes, transcribed into `providers/nrp/provider.toml`, are four,
and the proxy enforces all four with three mechanisms: an
`output_tokens_per_minute` rate ledger published at 200,000 tokens a minute per API
token *and* model, `max_concurrent_requests` at `model` scope, and one counter
carrying both `exclusive_above_context_fraction` and
`aggregate_context_fraction` — which is why three mechanisms cover four terms.
[docs/models-providers.md](docs/models-providers.md) owns that taxonomy: what each
rule kind means, which scopes it admits, and how a two-model selection resolves
against the counters.

What belongs here is the two choices that are this harness's rather than the
provider's. The ledger is rolling and books each attempt's full output clamp
*before* it starts, settling against measured usage, because an estimate that has
to be spent cannot be settled after the fact — stricter than NRP's fixed calendar
minute, and the gateway's own `x-ratelimit-*` header still wins whenever it reports
less headroom. And NRP does not enforce its concurrency table at all, so
`max_concurrent_requests` is honoured here or nowhere.

Two numbers in the provider file are deliberate under-use, and they are the only
tunables: `[safety].context_margin_percent` (95) and
`output_rate_margin_percent` (90) multiply the provider's own thresholds, so a
margin can never push a budget above the published rule. At the shipped
definition the model advertises 1,000,000 tokens, so the aggregate ceiling is
350,000 and the in-flight budget is 332,500; the rate ledger books 180,000 output
tokens per minute.

A lane's worst-case reservation is its input cap plus its output clamp, both from
`[lane.*]` in the model definition and both revalidated against the rendered Kimi
configuration the launcher staged for this run — a configuration the proxy cannot
honour answers HTTP 503 instead of passing unmeasured traffic. How the reservation
and the fan-out are derived is
[docs/models-providers.md](docs/models-providers.md); at the shipped numbers
`qwen3-primary` reserves its whole 262,144-token window, `qwen3-long` reserves
965,536, and each `qwen3-subagent` reserves 64,000, which is 5 at once against the
332,500 budget: the largest fan-out the rules allow. A permit is not charged at
that worst case. The gate prices each request from its own estimated input plus
that lane's output clamp, capped at the lane reservation, so nothing runs alone by
name: only a request whose price reaches 350,000 does, which for `qwen3-long` means
one that has actually filled most of its window. Which alias
the main agent launches on is the plan's `agent_lane`, answered on the main-agent
picker and rendered into Kimi's `default_model`; the wide lane is the shipped
answer, because under a mandate to use the allowance the provider granted, the
larger window is worth more than the overlap it spends. `./start.sh` derives
Kimi's subagent concurrency from the same plan, so Kimi is told to run exactly as
many children as the proxy will admit, and the generated envelope appended to its
system prompt instructs it to use the whole envelope instead of self-limiting.

That is also the whole of what the harness does with your habits, and it is worth
stating plainly, because the interesting cases are invisible:

- Choosing the wide row costs you nothing until a request is actually large. A
  short turn on `qwen3-long` is admitted beside other traffic like any other
  request; only one that cannot fit inside the pool alongside anything takes it
  alone. The price is the proxy's own estimate of the request's input — a
  character-count heuristic, not the provider's tokenizer — plus that lane's
  65,536-token output clamp, and two numbers can force solitude: the provider's
  own rule that a request at or above 350,000 tokens runs alone, and the plainer
  fact that a request priced above the 332,500-token pool has no room to share.
  The second binds first, so in practice a `qwen3-long` turn starts running alone
  somewhere past 267,000 tokens of context and overlaps freely below it. Leaving
  the launch on the wide lane is therefore free when the work is small and
  protective when it is not — there is no reason to pre-emptively narrow it.
- A long session is what makes a queue. Compaction keeps a conversation inside
  its window rather than letting it grow until a request is refused, so the
  ordinary shape of a long chat — one very big request at a time — is exactly the
  shape that runs alone. Starting a swarm *while* that is in flight makes the
  children wait for it, and waiting is not failing: the permit is held for them.
- Fan-out is a budget decision, not a preference. Five subagents at 64,000 tokens
  each is 320,000 of the 332,500 in-flight budget, so a sixth cannot start and a
  wide-lane request cannot start at all beside them. Running ten children in the
  background does not get ten times the work done; it gets the same work done
  with more of it waiting.
- Nothing you can do in the web UI spends provider capacity the proxy has not
  metered. `/model` can move a live session between the two agent lanes and the
  request is then priced on the lane it arrived on; there is no route around the
  gate, and the lane the harness bound for subagents is re-forced at every
  refresh.

Subagent and swarm wall-clock limits are unlimited: `timeout_ms = 0` in
`runtime/config.toml`, re-pinned at every launch, never via the environment. There is
no request wall clock either — `MODEL_PROXY_MAX_REQUEST_SECONDS` defaults to 0, which is
the knob's own spelling of "never give up", matching the provider's advice to retry
indefinitely with a growing interval. A client request therefore ends when it completes,
when `MODEL_PROXY_SOCK_READ_TIMEOUT` fires on a stalled upstream read, or when the user
cancels it in the UI; only the stall timeout releases a scarce fair-use permit on the
proxy's own initiative. None of these is a task-length limit.
`docs/verification.md` shows how to read the policy currently in force.

The proxy also archives the prompts it forwards, always on. A request qualifies only
when the newest user message in it is the operator speaking: a user message carrying a
`<system-reminder>` tag, or no new user message at all, adds nothing, so the many calls
one turn makes cost one file between them rather than one each. That file holds the
outbound request - request line, headers, and the body pretty-printed - with the provider
key, the internal bearer, and the cache salt redacted, and its path on the host is printed
on one stdout line beside a timestamp and the first 65 characters of the prompt:

```
prompt_log lane=primary time=2026-09-21T03:07:21.964055+00:00 chars=53 file=/…/.local/runtime/<instance>/prompt-log/prompt-20260921T030721-964055-bede5997.txt prompt='Fix the flaky date test in tests/test_runtime_lock.py'
```

The archive lives in `.local/runtime/<instance>/prompt-log`, so the files are readable on
the host while the stdout lines that name them arrive in `docker compose logs model-proxy`.
The archive holds conversation text, so its contents are purged when the proxy stops and
any leftovers are cleared again when it starts. It is the one writable
host bind the proxy has, and it is mounted into no other container.

## Persistent workspace contract

The workspace belongs to the project checked out in it, so the harness creates no
state there of its own. Each selected module declares the relative workspace
directories it needs, and `start.sh` creates those and nothing else. Existing
files and directories are preserved. Initialization rejects symlinked children
rather than following them outside the workspace.

The agent's working memory — the `STATE.md` and experiment ledgers the operating
contract tells it to keep — lives at `/tmp/agent-state` inside the agent container,
on the container's own tmpfs. `start.sh` does not create it; the in-container
`tools/register_workspace.py` rebuilds it from empty once the stack is ready, and it
ceases to exist with the container. Older revisions kept it in the workspace as
`.agent-state/`; a launch retires that directory when the repository does not track
it, and leaves it alone when it does.

Module `AGENTS.md` instructions reach every lane and not the system prompt, each
under a heading naming the module that owns them. The workspace's own `AGENTS.md`
is never written: it belongs to the project Kimi is working on. Where the staged
guidance is merged from and how it is assembled is
[docs/modules.md](docs/modules.md).
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
- GitHub, which is enabled but stays anonymous until you set `GITHUB_PERSONAL_ACCESS_TOKEN`
  in `.env`.

Disabled pending operator credentials or configuration:

- NVIDIA CUDA docs, from the ComfyUI module.

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
home, Serena cache, the agent's durable scratch tree, the read-only runtime
asset mirror, and the placeholder volumes that shadow user-level agents, skills,
and plugins. Settings saved in the Kimi UI therefore persist across restarts,
rebuilt images, and changes of host identity. Do not delete state volumes to fix
an ownership mismatch.

The agent has exactly two places to write that are not the project, and they
differ only in how long they last. `/tmp/agent-state` is container tmpfs and
dies with the stop, which is where session working memory belongs.
`/home/agent/.local` is the `harness-state` volume and survives, intended for
large re-creatable scratch — a cloned upstream tree, a scratch virtualenv, a
downloaded database. Nothing is swept out of it automatically, since it exists
precisely so that one launch's work is still there for the next; delete it with
`docker volume rm <project>_harness-state` when you want it gone. Both sit on
ext4 inside the VM rather than on the workspace's shared filesystem, which is
also why they are not slow to walk and do not confuse `git`. The project tree
itself holds only files you mean to commit; the runtime contract says so in as
many words, and `./start.sh` retires the `.agent-state/` directory that an older
revision used to leave there.

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

An empty file is a decision, not a missing file: existence picks the authority and
emptiness picks the payload, and only an absent `SYSTEM.md` can reach Kimi's own
built-in prompt. [docs/prompts.md](docs/prompts.md) states the resulting four rows
and why the deliberately-empty case stages a lone period rather than nothing. The
consequence for the panel is worth naming here: a block whose file is there but
empty opens as `off`, because that emptiness is an answer the panel has to show.

Both documents are written into the instance runtime directory by
`tools/render_runtime.py`, passed to Compose as `KIMI_SYSTEM_MD` and
`KIMI_RENDERED_AGENTS_MD`, and installed by the root-only initializer as
root-owned, mode `440`, immutable files. Editing them and restarting changes the
agent's instructions; the agent cannot change them from inside a session, so a
mid-session edit does nothing until the next launch.

### What the startup panel adds

`./start.sh` shows the composition as one map inside its modal: both the documents
above and every block the harness can add, each row with its resolved size. The
guides are the composition — a source that appears inside more than one parent gets
a `▶` at the joint, and a source some other file fully supersedes keeps its row but
goes dim with a hollow mark, so you can see what you are not getting. Both halves
answer to the same marks: a row you hold is a `[x]` or a `[ ]`, and a row that only
reports is `-x-` or `- -`. The sentence explaining a row is not stacked under it —
it appears beside the map in a detail pane while the window is wide enough for one,
and in the band under the status line when it is not — so the map itself stays short
enough to read as a map. Your changes are remembered for the next launch.

Run `./prompts.sh --configure --enable ID --disable ID --static ID=STATE` to
change them without starting a session (`--configure` alone only prints the current
selection), or `./prompts.sh --show` to see what an unattended launch will apply — it
prints both halves, the two documents and the nine add-ons. A launch with no
terminal cannot prompt, so it prints the remembered selection instead of asking.
`./prompts.sh --live` reads what the running session's model actually received, and
`./prompts.sh --vars` lists every placeholder a prompt file may hold and who
resolves it.

`auto` is the rule in the section above; `on` and `off` are this session's
overrides of it, and neither one edits anything — both files stay on disk exactly as
they were. [docs/prompts.md](docs/prompts.md) gives the row each override selects
and why. No `.env` or `HARNESS_*` variable governs either document, so this tree is
the only place to reach them, and the choice arrives intact because the launcher
exports the generated `runtime.env` before Compose is given `.env`, which keeps the
launcher's own paths above it.

Sizes in that tree are token counts, which is the unit the context window is
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
way, and the agent is simply no longer told what that plan is. Switching every
block off and both documents off is the tabula rasa, askable in one screen with no
file editing — what reaches the model is your own prompt and the tool schemas,
nothing else.

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

The agent's own durable state is behind named volumes, and so is everything the
launcher renders: the rendered configuration, the resolved policy plan, and the
generated secret filenames under `.local/runtime/` reach no container the agent
can see. One disclosure is structural rather than accidental. An approved
extension snapshot has to be staged under the instance directory before it can be
mounted read-only into the workspace, and a bind source is visible to the
container that owns it, so approving project extensions does publish the harness
checkout path and the instance directory name into the agent's mount table.
Nothing else from that directory travels with it — the snapshot holds only the
approved agents, skills, and MCP declaration — and `tools/compose_hygiene.py`
refuses every other agent bind, including any bind in any container that contains
the credential directory rather than sitting inside it, so an overlay cannot
acquire that view either.
The prompt archive is one of those instance-directory paths, and it is a host bind of
`model-proxy` rather than of the agent, so the agent cannot read it or learn its name.
Set `COMPOSE_PROJECT_NAME` to something neutral if the default
`kimi_code_<instance-id>` name is not one you want published inside the
sandbox.

## Optional host limits and cleanup

Service-local ingress, queue, metadata, transfer, PID, tmpfs, and log bounds are
always enabled. CPU and RAM needs vary widely with compilers and model sizes, so
host-wide ceilings are opt-in: the launcher appends `compose.limits.yaml` to the
Compose files whenever that file exists, and `compose.limits.yaml.example` is the
template it starts from.

This checkout carries a tuned `compose.limits.yaml`, so the ceilings apply from
the next restart. It is tuned for one machine — a Docker Desktop virtual machine
of 12 vCPU and 8 GiB — and Compose reads it on every launch, so a change lands on
the following restart rather than the current session. Re-tune it after any change
to the VM's resources, and treat the memory caps as headroom rather than budgets:
a cap that fires is the kernel killing a process mid-run, and a killed test parent
is how an abandoned terminal child was left behind this stack once. The file is
yours to un-ship: it is read by existence alone, so deleting it returns the stack
to running unbounded, exactly as before.

Two resident walkers need telling about `.local/`, because that is where the
launcher and agent sessions put scratch and it grows far past the repository
itself. `git` and ripgrep already honour `.gitignore`. The language server does
not: pyright reads `pyproject.toml` or `pyrightconfig.json`, so `pyproject.toml`
carries an `exclude` list for it. If you add another tool that indexes the
checkout, check whether it needs the same treatment.

Nothing in `.local/` is project source — it is ignored wholesale, so no file
below it is recoverable from Git — but two parts of it are load-bearing and only
two. `.local/runtime/<instance>/` is swept by name around every launch, with the
cache salt deliberately excepted, and `.local/workspaces.json` is never swept.
Anything else at the top of `.local/` is declared by no document and matched by
no sweep: it is residue from a session that wrote scratch into the checkout
instead of the container's own `/tmp/agent-state`, and it accumulates until
someone removes it by hand.

The launcher reports its instance ID. Generated runtime material for that
instance lives below `.local/runtime/INSTANCE_ID`; replaceable native module installations live
below `.local/runtime/INSTANCE_ID/module-data/<module>`. Stop the matching stack before removing
either directory. Never delete the workspace as part of cache cleanup.

`.local/workspaces.json` is not cache and must not be swept with it: it is the list of workspaces
this checkout has been pointed at, and it is the only record of which directory an unattended launch
should mount. Removing it does not free anything worth having — it costs you the recent list and
leaves `--non-interactive` with no workspace to start.

An ordinary `launcher.lock` file may remain after a crash; advisory locking
makes an unlocked file harmless. Both macOS and WSL2 use Python's advisory
locking support; no separate `flock` command is required. If a launcher reports
another owner, stop that exact launcher first. Do not delete an active lock file
or remove the project, workspace, or the entire `.local` tree as lock recovery.

The host scripts report a file, line, and exit status for unexpected failures.
`start.sh` also checks that Docker is ready before selecting releases. Optional
module settings are documented with each module.

The release selector and the ComfyUI lock resolver use Python's verified HTTPS
context. On macOS, if Python has no default CA certificates, they load the
system bundle at `/etc/ssl/cert.pem` for release metadata, checksum, and
requirements downloads. Existing trust stores and explicit `SSL_CERT_FILE` /
`SSL_CERT_DIR` environment settings take precedence. For an
organization-specific CA bundle, export `SSL_CERT_FILE=/path/to/ca-bundle.pem`
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
