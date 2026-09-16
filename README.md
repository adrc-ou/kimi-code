# Modular NRP LiteLLM + Kimi Code development harness

This project runs Kimi Code against OU Libraries' NRP-backed LiteLLM gateway,
enforces the configured NRP concurrency policy, provides selected MCP servers
and local SearXNG search. Optional modules add application services and tools.

The core runs on macOS (Intel or Apple Silicon) and Linux/WSL2 (x86-64 or
arm64), with Docker and Compose. Modules determine their own host compatibility.
The included [ComfyUI module](modules/comfyui/README.md) supports Apple Silicon
MPS and Linux/WSL2 NVIDIA CUDA. Intel Macs can run the core without that module.

See [the module authoring contract](docs/modules.md) to add another module.

## Prerequisites

For startup status checks, functional tool tests, and step-by-step optional
account setup, see [the verification guide](docs/verification.md). `./start.sh`
runs a quick service/MCP check; use `./doctor.sh --full` while it is running for
read-only functional probes.

Hosts need:

- Docker with Docker Compose (Docker Desktop on macOS/Windows);
- Git;
- Python 3 for the host setup scripts;
- enough free disk space for container images and models;
- network access to GitHub, Python package indexes, Docker Hub, and configured
  model/MCP endpoints.

## Initial configuration

1. Create a virtual key in the
   [OU LiteLLM dashboard](https://litellm.lib.ou.edu/ui/?page=api-keys) and grant
   it access only to the intended model.
2. Copy `.env.example` to `.env`.
3. Set `LITELLM_API_KEY`.
4. Set `SEARXNG_SECRET` to the output of `openssl rand -hex 32`.
5. Set `WORKSPACE_PATH` to a dedicated directory containing only material the
   agent is allowed to inspect and change. The agent can always see this exact
   host path, so avoid a location whose name you need to keep private.
6. Keep the supplied NRP model and policy values unless the replacement model's
   identifier, context size, concurrency, and fair-use limits have been verified.

The model identifier is the human-readable value exposed by the OU LiteLLM
dashboard (not the upstream NRP model name/endpoint).

## Starting and stopping

Run:

```bash
./start.sh
```

The launcher first shows compatible modules in a checkbox menu. Use ↑/↓ to
move, Space to toggle, and Enter to continue. Last session's enabled modules
appear first and are checked; each group is alphabetical by label. On the first
run all modules are unchecked. If none are compatible, this step is skipped.

Next comes the existing Kimi version menu, followed by each selected module's
version menu. Missing required module variables are prompted for this session
only; add them to `.env` yourself to persist them. Secret inputs are hidden.

After all choices, the launcher initializes the workspace, installs selected
versions, generates private runtime configuration, snapshots module assets and
approved project extensions, and starts the segmented stack. Ctrl-C stops the
containers and registered native module processes. Persistent workspace data
is never removed when a module is unchecked or deleted.

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

For repeatable automation, set explicit versions and disable prompts:

```bash
HARNESS_MODULES= KIMI_CODE_VERSION=0.42.0 ./start.sh --non-interactive
```

`HARNESS_MODULES` is a comma-separated list of module directory identifiers;
an explicit empty value selects the core only. If omitted in non-interactive
mode, the previous compatible selection is reused. Missing required module
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

## Persistent workspace contract

`start.sh` creates `.agent-state/` and its initial state files automatically.
Each selected module declares additional relative workspace directories.
Existing files and directories are preserved. Initialization rejects symlinked
children rather than following them outside the workspace.

Module `AGENTS.md` instructions appear in a managed section of the workspace's
`AGENTS.md`. Restarting replaces only that section, keeping user guidance intact.
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
LiteLLM credential must never be sent as Docker build-context data.

## MCP servers

Local stdio MCP servers are child processes launched by Kimi when a workspace
session begins. They are not independent Compose services. Open a fresh session
after changing core or module `runtime/mcp.json`, then use `/mcp` to inspect connections.

Enabled by default:

- DeepWiki;
- Chrome DevTools;
- Serena.

Disabled pending operator credentials or configuration:

- GitHub;
- Context7.

The Context7 URL is already set to `https://mcp.context7.com/mcp`. Configure and
authenticate one remote service at a time, create a fresh session, inspect `/mcp`,
and make one harmless read-only call before enabling the next.

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
the private networks from becoming a second flat service mesh. The real NRP key
is mounted only into `model-proxy` as a file secret.

## Validation

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
holds onto the freshly rendered baseline. Everything it stages is root-owned and
readable but never writable by the agent; the three files that sit inside the
agent-owned Kimi home additionally carry ext4 per-file immutable flags, because
owning a directory lets you unlink anything in it regardless of the file's
owner. That is what allows the Kimi home volume to be writable at all. The
initializer mounts only its own script, the staging inputs, and those volumes: no
workspace, Docker socket, network, or credentials. Kimi itself remains non-root
with all capabilities dropped.

`runtime/config.toml` is the commented source of truth for generated settings.
The user-owned keys are declared in `runtime/config-policy.json` and currently
cover the default model, thinking, telemetry, background-task, and experimental
UI controls. Every other key is policy: a UI or `/config` edit that changes one
is silently re-pinned from the baseline at the next launch, because the proxy's
fair-use lane cannot be negotiated from inside the sandbox. That includes the
permission mode, plan mode, skill and agent directories, provider definitions,
model context sizes, and the forced subagent model. Widen
`runtime/config-policy.json` deliberately if you want the UI to own another key.
A stored `config.toml` that cannot be parsed is renamed to a timestamped
`.unreadable-*` file and replaced with the baseline rather than starting Kimi
with broken configuration.

Model provider credentials, the per-instance base URL, and the internal proxy
token are regenerated each launch and are never read back from the volume.

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
