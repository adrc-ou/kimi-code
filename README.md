# NRP LiteLLM + Kimi Code + ComfyUI development harness

This project runs Kimi Code against OU Libraries' NRP-backed LiteLLM gateway,
enforces the configured NRP concurrency policy, provides selected MCP servers
and local SearXNG search, and starts a persistent ComfyUI development workspace.

The harness supports two GPU environments:

| Host | Kimi, proxy, search, MCP tools | ComfyUI | GPU backend |
| --- | --- | --- | --- |
| Apple Silicon macOS | Docker Desktop | Native Python virtual environment | MPS |
| Windows with NVIDIA | Docker Desktop using WSL2 | Docker container | CUDA |

Docker Desktop does not expose Apple Metal/MPS to Linux containers. On macOS,
only ComfyUI runs natively; the agent harness remains containerized.

ComfyUI does not publish an official Docker image. The CUDA image is built
locally from a selected, exact commit fetched from the official
[`Comfy-Org/ComfyUI`](https://github.com/Comfy-Org/ComfyUI) repository.

## Prerequisites

Both hosts need:

- Docker Desktop with Docker Compose;
- Git;
- Python 3.12 for the locked native MPS tuple;
- enough free disk space for container images and models;
- network access to GitHub, Python package indexes, Docker Hub, and configured
  model/MCP endpoints.

The Mac additionally needs:

- Apple Silicon and macOS 14 or newer;
- an arm64 build of Python;
- Xcode command-line tools (`xcode-select --install`).

The Windows host additionally needs:

- current Windows 11 and WSL2 (`wsl --update`);
- Docker Desktop's WSL2 backend and integration enabled;
- a current NVIDIA Windows driver with the workstation GPU in WDDM mode.

Do not install an NVIDIA Linux display driver inside WSL2. The Windows driver
provides CUDA to WSL. Run this project from an Ubuntu/WSL shell, not Git Bash,
and place both the repository and workspace in the WSL filesystem rather than
under `/mnt/c`.

## Initial configuration

1. Create a virtual key in the
   [OU LiteLLM dashboard](https://litellm.lib.ou.edu/ui/?page=api-keys) and grant
   it access only to the intended model.
2. Copy `.env.example` to `.env`.
3. Set `LITELLM_API_KEY`.
4. Set `SEARXNG_SECRET` to the output of `openssl rand -hex 32`.
5. Set `WORKSPACE_PATH` to a dedicated directory containing only material the
   agent is allowed to inspect and change.
6. Keep the supplied NRP model and policy values unless the replacement model's
   identifier, context size, concurrency, and fair-use limits have been verified.

The model identifier is the human-readable value exposed by the OU LiteLLM
dashboard (not the upstream NRP model name/endpoint).

## Starting and stopping

Run:

```bash
./start.sh
```

The launcher:

1. detects macOS/MPS or Windows/WSL2/CUDA;
2. fetches official Kimi Code releases and loads the checked-in ComfyUI catalog;
3. presents at most ten versions of each, newest first;
4. marks the newest compatible release `(latest)`;
5. marks the verified local version `(installed)` and includes it even when it
   is older than the normal list;
6. fetches and verifies changed application versions;
7. acquires an instance lock and generates per-run proxy/search/bridge credentials;
8. initializes and verifies the persistent workspace without following child symlinks;
9. snapshots approved executable project extensions as read-only mounts;
10. verifies the selected GPU backend;
11. starts the segmented stack attached to the terminal.

The installed version is the default choice. Selecting another version replaces
the active Kimi image and ComfyUI application while preserving user data.

Press Ctrl-C in the same terminal to stop every container and any native macOS
ComfyUI processes. No shutdown script is required. Images, named volumes, model
files, workflows, custom nodes, input, output, and user configuration remain.

For repeatable automation, set explicit versions and disable prompts:

```bash
KIMI_CODE_VERSION=0.42.0 \
COMFYUI_VERSION=v0.35.0 \
./start.sh --non-interactive
```

The requested versions must still exist in the compatible official release
catalog. Release metadata failures stop startup instead of silently using stale
data.

Services are available at:

- Kimi Code: <http://127.0.0.1:5494>
- ComfyUI: <http://127.0.0.1:8188>

Generated secrets and native logs are kept under the instance-specific
`.local/runtime/` directory and are excluded from Git. Ephemeral credentials,
rendered provider configuration, and bridge certificates are deleted at normal
shutdown. The private NRP cache salt persists so cached responses remain
isolated across restarts.

## Persistent workspace contract

`start.sh` creates:

```text
WORKSPACE_PATH/
├── .agent-state/
└── comfyui/
    ├── custom_nodes/
    ├── input/
    ├── models/
    ├── output/
    ├── temp/
    └── user/
        └── default/
            └── workflows/
```

Kimi can read and write the entire workspace, including custom nodes and
workflows. ComfyUI application code and Python environments are deliberately
outside the workspace and replaceable.

The optional `./init-workspace.sh` command initializes these directories without
starting anything. It is not required because `start.sh` performs the same work.

Workspace children used as host bind sources must be real directories. The
launcher refuses symlinks and rechecks device/inode identity immediately before
startup. To keep large data on another drive, set one or more explicit absolute
paths in `.env`:

```dotenv
COMFYUI_MODELS_PATH=/absolute/path/to/models
COMFYUI_CUSTOM_NODES_PATH=/absolute/path/to/custom_nodes
COMFYUI_INPUT_PATH=/absolute/path/to/input
COMFYUI_OUTPUT_PATH=/absolute/path/to/output
COMFYUI_TEMP_PATH=/absolute/path/to/temp
COMFYUI_USER_PATH=/absolute/path/to/user
```

Every configured path must already exist, be owned by the invoking user, and
contain no symlinked component.

## Executable project extensions

Kimi agents, skills, and project MCP declarations can execute code or replace
the agent identity. The launcher therefore requires host-side approval for
`.kimi-code/agents`, `.agents/agents`, `.kimi-code/skills`, `.agents/skills`,
and `.kimi-code/mcp.json`. Ordinary project `AGENTS.md` files remain writable
workspace guidance and do not require this approval.

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
the Linux arm64 asset on a Mac host and Linux x64 asset under WSL2, because Kimi
itself runs in a Linux container. The SHA-256 digest published with the release
is verified during the image build.

ComfyUI selection is limited to exact tuples in `comfy/compatibility.json`.
Each tuple records the application commit, Python/GPU backend, dependency-lock
digests, and certification evidence. A stable semantic version is not treated
as compatible merely because of its tag. Entries marked `locked` have immutable
dependency resolution but still require the recorded controlled hardware run;
only entries marked `tested` may claim hardware certification.

PyTorch versions are pinned separately in `comfy/backend.env`. They do not
automatically move with ComfyUI releases because a framework upgrade can change
CUDA driver requirements or MPS behavior. Review those pins deliberately.

`comfy/requirements-linux.lock`, `comfy/requirements-macos.lock`, and
`comfy/requirements-custom.lock` are complete, hash-checked Python resolution
artifacts. `comfy/requirements-custom.txt` is the operator-reviewed input for
custom nodes and accepts exact `name==version` pins only. After changing the
input or certified ComfyUI version, regenerate the affected locks with `uv pip
compile --generate-hashes` for Python 3.12 and the exact target platform. The
CUDA lock must retain the reviewed direct-wheel URLs and hashes from
`comfy/torch-cuda-constraints.txt`; the macOS lock uses
`comfy/torch-constraints.txt`. Update digests in `dependencies.lock.json` and
`comfy/compatibility.json`, then run the backend acceptance test. Do not add
automatic custom-node dependency installation to container startup.

Base images are digest-pinned, the GitHub MCP source is commit-pinned, and npm
browser tooling is installed with `npm ci` from `container/package-lock.json`.
`dependencies.lock.json`, the compatibility catalog, and expiring vulnerability
exceptions are checked in CI. No third-party source distribution, model, custom
node, or Python package is vendored in this repository.

`.dockerignore` excludes `.env`, `.local`, Git metadata, bytecode, and generated
archives from the root build context. Do not remove the `.env` exclusion: the
LiteLLM credential must never be sent as Docker build-context data.

## ComfyUI access from Kimi

Inside the agent container, use:

```bash
python /opt/kimi-runtime/tools/comfyctl.py stats
python /opt/kimi-runtime/tools/comfyctl.py queue
python /opt/kimi-runtime/tools/comfyctl.py schema CheckpointLoaderSimple
python /opt/kimi-runtime/tools/comfyctl.py upload /workspace/comfyui/input/example.png
python /opt/kimi-runtime/tools/comfyctl.py run --wait /workspace/comfyui/user/default/workflows/example-api.json
python /opt/kimi-runtime/tools/comfyctl.py download PROMPT_ID /workspace/comfyui/output/downloaded
python /opt/kimi-runtime/tools/comfyctl.py interrupt
```

`COMFYUI_CONNECT_TIMEOUT`, `COMFYUI_READ_TIMEOUT`, and
`COMFYUI_TOTAL_TIMEOUT` may be set in `.env` when the defaults are unsuitable.
JSON and media transfers have separate `COMFYUI_MAX_JSON_BYTES` and
`COMFYUI_MAX_TRANSFER_BYTES` limits. Native bridge body, WebSocket-message, and
connection limits are also configurable in `.env`. Downloads are
streamed to exclusive temporary files, fsynced, and atomically renamed without
overwriting an existing result.

On CUDA, Kimi connects directly over the private Compose network. On macOS,
ComfyUI itself listens only on host loopback. A short-lived TLS-authenticated
HTTP/WebSocket bridge on port 8190 lets the container reach it through
`host.docker.internal`. Its certificate covers the Docker host name and
loopback, its private key never enters a container, and HTTP/WebSocket sizes and
connections are bounded.

## MCP servers

Local stdio MCP servers are child processes launched by Kimi when a workspace
session begins. They are not independent Compose services. Open a fresh session
after changing `runtime/mcp.json`, then use `/mcp` to inspect connections.

Enabled by default:

- Hugging Face (`hf_fs` only);
- DeepWiki;
- Chrome DevTools;
- Serena.

Disabled pending operator credentials or configuration:

- GitHub;
- NVIDIA CUDA Docs;
- Context7.

The Context7 URL is already set to `https://mcp.context7.com/mcp`. Configure and
authenticate one remote service at a time, create a fresh session, inspect `/mcp`,
and make one harmless read-only call before enabling the next.

For GitHub, use a dedicated fine-grained read-only token restricted to required
repositories. The Kimi process can access any token placed in its environment.

The checked-in MCP declarations and Kimi configuration are mounted read-only.
Edit this repository and restart to change declarations. OAuth and session state
remain in Docker volumes.

Chromium runs as the non-root agent with its process sandbox enabled and a
reviewed seccomp profile. Do not add `--no-sandbox`, `SYS_ADMIN`, or an
unconfined seccomp setting.

## Web search

`compose.search.yaml` starts SearXNG as an unprivileged numeric user and a local
adapter with bounded workers, queue, request, response, result, and field sizes.
The adapter translates Kimi's schema to SearXNG's JSON API. Neither service is
published to the host.

Compose networks isolate the model proxy, search backend, and ComfyUI. Only
Kimi joins the intended client-side networks; separate egress networks prevent
the private networks from becoming a second flat service mesh. The real NRP key
is mounted only into `model-proxy` as a file secret.

## Validation

Static and unit tests:

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
python3 -m compileall -q proxy scripts search-adapter tools tests
git ls-files '*.sh' -z | xargs -0 -n1 bash -n
git ls-files '*.sh' -z | xargs -0 shellcheck
ruff check .
python3 scripts/check_locks.py
npm ci --ignore-scripts --prefix container
```

While the stack is running, use another terminal:

```bash
tests/acceptance.sh mps
```

or:

```bash
tests/acceptance.sh cuda
```

Then open a fresh Kimi session, run `/mcp`, and make one harmless call through
each enabled server. Remote authentication cannot be validated without the
operator's credentials.

Use `./shell.sh` from a second terminal to open a shell in the running Kimi
container. It resolves the same instance, Compose files, generated secrets, and
verified external paths as the launcher.

## Optional host limits and cleanup

Service-local ingress, queue, metadata, transfer, PID, tmpfs, and log bounds are
always enabled. CPU and RAM needs vary widely with compilers and model sizes, so
host-wide ceilings are opt-in. Copy `compose.limits.yaml.example` to
`compose.limits.yaml`, tune it for the machine, and restart.

The launcher reports its instance ID. Generated runtime material for that
instance lives below `.local/runtime/INSTANCE_ID`; native MPS environments live
below `.local/comfy-macos/INSTANCE_ID`. Stop the matching stack before removing
either directory. Never delete the workspace as part of cache cleanup.

An ordinary `launcher.lock` file may remain after a crash; advisory locking
makes an unlocked file harmless. Both macOS and WSL2 use Python's advisory
locking support; no separate `flock` command is required. If a launcher reports
another owner, stop that exact launcher first. Do not delete an active lock file
or remove the project, workspace, or the entire `.local` tree as lock recovery.

The host scripts report a file, line, and exit status for unexpected failures.
`start.sh` also checks that Docker is ready before selecting releases. Optional
ComfyUI path overrides may be left unset. `init-workspace.sh` creates the
workspace layout and exits successfully without starting containers.

After a controlled hardware acceptance run, update the matching compatibility
entry from `locked` to `tested`, add the run identifier and certification date,
and review those evidence changes with the lock digests. Do not label a tuple
`tested` based only on dependency resolution or a successful image build.

## What Docker does and does not protect

The agent normally runs without root privileges in a read-only container. UID
and GID collisions are resolved by reusing numeric base-image identities rather
than deleting accounts. The Docker socket and host filesystem are not mounted.
Its writable bind mount is limited to `WORKSPACE_PATH` and any explicitly
validated external ComfyUI directories.

The workspace is intentionally not protected from the agent. The agent also has
network access, so Docker cannot prevent workspace exfiltration or unsafe
downloads. Use a dedicated workspace, retain manual approval mode, and monitor
tool calls.

ComfyUI custom nodes are executable Python code. On Windows they run in the
ComfyUI container, which receives no LiteLLM credential and only the ComfyUI data
mounts. On macOS, MPS requires native execution, so custom nodes run with the
permissions of the macOS user. Review every third-party custom node before
loading it and do not expose secrets in the ComfyUI process environment.
