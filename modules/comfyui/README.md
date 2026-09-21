# ComfyUI module

Enable **ComfyUI** in `./start.sh`, or set `HARNESS_MODULES=comfyui` for automation.
Only enabled sessions serve a frontend. The host browser opens
`http://127.0.0.1:8188`; inside the sandbox the equivalent origin is
`http://comfyui:8188` on CUDA and `http://comfyui-ui:8188` on MPS, and
`comfyctl.py ui-url` prints whichever one this session has.

The Mac additionally needs:

- Apple Silicon and macOS 14 or newer;
- `curl` and `tar` (included with macOS);
- Xcode command-line tools (`xcode-select --install`).

For native ComfyUI, the launcher uses arm64 Python 3.12 from `PATH` when available.
Otherwise it downloads the standalone build pinned in `modules/comfyui/dependencies.lock.json`
from [Astral's official releases](https://github.com/astral-sh/python-build-standalone/releases),
verifies its SHA-256, and installs it under
`.local/runtime/<instance>/module-data/comfyui/python/<digest>/`. Setup reuses this interpreter to
create ComfyUI's dedicated virtual environment. No Homebrew, administrator
access, shell profile changes, or system Python changes are needed. Each user
should use their own writable harness checkout and workspace.

To use an existing interpreter (including for offline setup), set
`COMFYUI_MACOS_PYTHON` to its executable path. An explicit override must be
arm64 Python 3.12; an invalid override fails instead of triggering a download.

The Windows host additionally needs:

- current Windows 11 and WSL2 (`wsl --update`);
- Docker Desktop's WSL2 backend and integration enabled;
- a current NVIDIA Windows driver with the workstation GPU in WDDM mode.

Do not install an NVIDIA Linux display driver inside WSL2. The Windows driver
provides CUDA to WSL. Run this project from an Ubuntu/WSL shell, not Git Bash,
and place both the repository and workspace in the WSL filesystem rather than
under `/mnt/c`.

ComfyUI selection is limited to exact tuples in `modules/comfyui/backend/compatibility.json`.
Each tuple records the application commit, Python/GPU backend, dependency-lock
digests, and certification evidence. A stable semantic version is not treated
as compatible merely because of its tag. Entries marked `locked` have immutable
dependency resolution but still require the recorded controlled hardware run;
only entries marked `tested` may claim hardware certification.

PyTorch versions are pinned separately in `modules/comfyui/backend/backend.env`. They do not
automatically move with ComfyUI releases because a framework upgrade can change
CUDA driver requirements or MPS behavior. Review those pins deliberately.

`modules/comfyui/backend/requirements-linux.lock`, `modules/comfyui/backend/requirements-macos.lock`, and
`modules/comfyui/backend/requirements-custom.lock` are complete, hash-checked Python resolution
artifacts. `modules/comfyui/backend/requirements-custom.txt` is the operator-reviewed input for
custom nodes and accepts exact `name==version` pins only. After changing the
input or certified ComfyUI version, regenerate the affected locks with `uv pip
compile --generate-hashes` for Python 3.12 and the exact target platform. The
CUDA lock must retain the reviewed direct-wheel URLs and hashes from
`modules/comfyui/backend/torch-cuda-constraints.txt`; the macOS lock uses
`modules/comfyui/backend/torch-constraints.txt`. Update digests in `modules/comfyui/dependencies.lock.json` and
`modules/comfyui/backend/compatibility.json`, then run the backend acceptance test. Do not add
automatic custom-node dependency installation to container startup.

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
python /opt/kimi-runtime/tools/comfyctl.py ui-url
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

## Opening the frontend

ComfyUI's UI is a browser origin, and on macOS the bridge that makes the host
service reachable authenticates every path, including `/`. A browser attaches no
header to a navigation or to the scripts its page then asks for, so the two
origins below exist instead of a hole in the bridge.

The host gets ComfyUI's own loopback listener. On an MPS host, `module_start`
waits for the service and then opens `http://127.0.0.1:8188` in the default
browser, the way the launcher opens Kimi's web UI, so the operator sees the
workflow editor without asking for it. `COMFYUI_OPEN_FRONTEND=false` suppresses
it; nothing else changes. The opener refuses any URL that is not loopback and
probes the origin before handing it over, so it can neither point the browser at
someone else's host nor open a tab on a service that is still starting.

The sandbox gets an origin it can simply open. On MPS, `comfyui-ui` is a
plaintext listener on a private network with the agent, and it pumps every byte
to the bridge over TLS verified against the bridge's own certificate. The bridge
terminates that TLS, authenticates the request, and proxies it to ComfyUI on the
host, so the credential in play is the browser's session and never the bearer
token crossing a network in the clear.

`comfyctl.py ui-url` prints the link to open. It holds the bearer token, spends
it on one `POST /__bridge/grant`, and prints the returned frontend URL with the
grant in the fragment, which is the only part of a URL a browser never transmits,
never logs, and never repeats in a `Referer`. The page at that URL posts the
grant once to `/__bridge/session` and gets back an HttpOnly, SameSite=Strict
cookie, then rewrites its own location to `/` so the grant leaves the address
bar. From then on every path carries that cookie and the bridge accepts it in
place of the token. A grant works once and expires after
`COMFYUI_BRIDGE_GRANT_TTL` seconds; a session lasts `COMFYUI_BRIDGE_SESSION_TTL`
and is bound to the `Host` that minted it. Sessions are capped in number, and so
are concurrent grants. The cookie omits `Secure` because the sandbox origin is
plaintext by construction and publishes no port, so nothing outside the agent's
own network can observe it; on the host, the origin is loopback.

## Persistent data and installation

The module creates `comfyui/{models,custom_nodes,input,output,temp,user}` and
`comfyui/user/default/workflows` inside the workspace. These paths survive
upgrades, deselection, and module removal. Application code is replaceable:
CUDA uses a locally built image from the pinned upstream commit; MPS uses
`.local/runtime/<instance>/module-data/comfyui/app/current`.
An existing pre-module native installation is migrated on first enablement.

Optional absolute storage overrides are listed in [env.example](env.example).
Each override must already exist, be owned by the invoking user, and have no
symlinked components. Bind device/inode identities are checked again at launch.
No environment variable is required for local ComfyUI execution. `HF_TOKEN`
and all path/timeout/Python overrides are optional; copy desired values to the
harness `.env`.

Compatibility is detected within this module: Apple Silicon Macs or Linux/WSL2
x86-64 hosts with an NVIDIA GPU visible through `nvidia-smi -L`. CUDA containers
also need Docker GPU support. Startup verifies the actual selected torch backend.
Intel Macs and CPU-only hosts are excluded from the module menu.

## Agent tools and trust

This module contributes ComfyUI, frontend, tensor/model integration and GPU
skills; the tensor auditor agent; Hugging Face MCP (`hf_fs`); and an optional
NVIDIA CUDA documentation MCP entry. Edit `runtime/mcp.json` in this module
while stopped to configure these servers. Core project-extension approval
continues to apply to workspace skills, agents and MCP declarations.

Custom nodes are executable Python. CUDA isolates them in the application
container with no model-provider credential. MPS requires native host execution:
review custom nodes before loading them and keep secrets out of that process.
The bridge uses TLS, bearer authentication, size limits and connection limits;
its private key is never mounted into the agent container. Browsers satisfy that
authentication with a short-lived session cookie minted from a single-use grant,
so the bearer token stays out of every URL and out of the sandbox.

## Hardware verification

With a ComfyUI session running:

```bash
bash modules/comfyui/tests/acceptance.sh mps  # or cuda
```

The test uploads a tiny fixture, runs an EmptyImage → SaveImage workflow,
downloads the output and checks isolation and writable data paths. It needs no
model weights. After a controlled hardware run, update the relevant compatibility
entry with actual evidence; dependency resolution alone is not certification.
Lock-file generation comments retain their original paths as historical records;
use the current `modules/comfyui/backend/` paths when regenerating locks.
