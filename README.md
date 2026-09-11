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
- Python 3.10 or newer;
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
2. fetches stable releases from the official Kimi Code and ComfyUI repositories;
3. presents at most ten versions of each, newest first;
4. marks the newest compatible release `(latest)`;
5. marks the verified local version `(installed)` and includes it even when it
   is older than the normal list;
6. fetches and verifies changed application versions;
7. initializes the persistent workspace;
8. verifies the selected GPU backend;
9. starts the full stack attached to the terminal.

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

On macOS, native logs are written to `.local/logs/`.

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

## Version and dependency policy

Kimi is downloaded from official Moonshot release assets. The launcher selects
the Linux arm64 asset on a Mac host and Linux x64 asset under WSL2, because Kimi
itself runs in a Linux container. The SHA-256 digest published with the release
is verified during the image build.

ComfyUI releases use the same official source tags on both hosts. The host-specific
part is the installation backend, not a separate ComfyUI version catalog. The
selected tag is resolved to a full commit before installation.

PyTorch versions are pinned separately in `comfy/backend.env`. They do not
automatically move with ComfyUI releases because a framework upgrade can change
CUDA driver requirements or MPS behavior. Review those pins deliberately.

`comfy/requirements-custom.txt` is the operator-reviewed dependency boundary for
custom nodes. Add exact package versions there and restart to rebuild/reinstall.
Do not add automatic custom-node dependency installation to container startup.

No third-party source distribution, model, custom node, or Python package is
vendored in this repository. Builds fetch dependencies from their official
upstream locations.

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

`COMFYUI_CONNECT_TIMEOUT` and `COMFYUI_READ_TIMEOUT` may be set in the
`kimi-agent` environment when the default 10-second connection and 60-second
read timeouts are unsuitable. Workflow waiting has its own `--timeout` option.

On CUDA, Kimi connects directly over the private Compose network. On macOS,
ComfyUI itself listens only on host loopback. An ephemeral authenticated
HTTP/WebSocket bridge on port 8190 lets the container reach it through
`host.docker.internal` without exposing an unauthenticated workflow API to the
LAN.

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

## Web search

`compose.search.yaml` starts the official SearXNG image and a small local adapter.
The adapter translates Kimi's `text_query` request and `search_results` response
schema to SearXNG's JSON API. Neither service is published to the host.

## Validation

Static and unit tests:

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
bash -n start.sh init-workspace.sh scripts/install_comfy_macos.sh tests/acceptance.sh
python3 -m compileall -q proxy scripts search-adapter tools tests
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

## What Docker does and does not protect

The agent runs without root privileges in a read-only container. The Docker
socket and host filesystem are not mounted. Its writable bind mount is limited
to `WORKSPACE_PATH`.

The workspace is intentionally not protected from the agent. The agent also has
network access, so Docker cannot prevent workspace exfiltration or unsafe
downloads. Use a dedicated workspace, retain manual approval mode, and monitor
tool calls.

ComfyUI custom nodes are executable Python code. On Windows they run in the
ComfyUI container, which receives no LiteLLM credential and only the ComfyUI data
mounts. On macOS, MPS requires native execution, so custom nodes run with the
permissions of the macOS user. Review every third-party custom node before
loading it and do not expose secrets in the ComfyUI process environment.
