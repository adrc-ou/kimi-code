## ComfyUI development boundary

The live ComfyUI service is available at `COMFYUI_URL`. If `COMFYUI_TOKEN` is
set, helper clients must send it as a bearer token.

Use `/opt/kimi-runtime/tools/comfyctl.py` and ComfyUI's machine-readable APIs for
schema inspection, input upload, queue inspection, workflow execution, history,
output download, and interruption.

To look at the web frontend itself, ask `comfyctl.py ui-url` for the address and
open that link with the `chrome-devtools` tools. Do not reuse or store the link:
it is a single-use login that expires in about a minute, so request a fresh one
each time. Never put `COMFYUI_TOKEN` in a URL, a page, or a workflow, and do not
expect ComfyUI's host loopback address to resolve from inside the sandbox.

Custom-node source belongs under `/workspace/comfyui/custom_nodes`. Workflows
belong under `/workspace/comfyui/user/default/workflows`. Both locations are
intentionally writable.

Do not modify the replaceable ComfyUI application or install packages at runtime.
When a custom node needs another dependency, identify and pin the exact package
version and report that `modules/comfyui/backend/requirements-custom.txt` in the operator-managed
harness must be reviewed. That file is one of the inputs the dependency lock is
keyed by, so once it is reviewed the next launch resolves a lock that agrees
with it; no lock is hand-compiled, and the running backend is only as certified
as the release record the chosen version came from.

Treat custom nodes as executable code. Inspect their source and dependency
metadata before asking the operator to restart and load them.

## Tensor and model integration

Never assume model-specific:

- latent channel count;
- image or video tensor layout;
- temporal packing;
- patch geometry;
- VAE spatial or temporal compression;
- dtype;
- normalization range;
- text encoder count;
- context length;
- scheduler/timestep convention.

Derive these from current model configuration or authoritative implementation
and record important boundaries in `.agent-state/TENSOR_CONTRACTS.md`.

