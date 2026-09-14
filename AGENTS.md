# Harness maintenance instructions

This repository defines the Kimi Code sandbox and policy harness.
It is not the writable development workspace used by the contained agent.

`runtime/AGENTS.md` is the authoritative runtime operating contract.

When changing NRP model, concurrency, context, or proxy behavior:
- preserve NRP Fair Use compliance;
- keep primary and subagent lanes mutually exclusive;
- keep `secondary_model.force = true`;
- verify proxy policy against the actual runtime Kimi configuration;
- never place real model credentials in the kimi-agent container.
- preserve strict `/primary`, `/long`, and `/subagent` route separation;
- keep retries outside scarce fair-use permits during backoff;
- keep the persistent private cache salt out of logs and tracked files.

Generated runtime files belong only under `.local/runtime/<instance>/`. Do not
write credentials, rendered provider configuration, approval manifests, bridge
private keys, or launcher locks into the workspace.

Project agents, skills, and MCP configuration must pass `extensions.sh`
approval and remain mounted read-only during a running session. Ordinary
project `AGENTS.md` guidance remains part of the writable workspace.

Do not reintroduce legacy `.agent/`, `.agent-container/`, `SAFE_CONTEXT`,
or `KIMI_MODEL_*` configuration.

## Persistent workspace contract

ComfyUI application code belongs in its replaceable container image on CUDA or
its replaceable native virtual environment on Apple Silicon.

The following writable workspace paths must survive upgrades:

- `comfyui/user/`
- `comfyui/input/`
- `comfyui/output/`
- `comfyui/temp/`
- `comfyui/models/`
- `comfyui/custom_nodes/`

Do not move model files, workflows, custom-node source, or user configuration
into a container layer, anonymous volume, or `.local/comfy-macos/current`.

## Dependency and release policy

Fetch third-party applications and dependencies from official upstream sources;
do not vendor their packages or source distributions in this repository.

Resolve selectable releases to immutable commits or verify their published
SHA-256 digest. Keep GPU-framework and custom-node dependencies pinned and
update them deliberately. Do not install unreviewed custom-node dependencies at
service startup.

Update `dependencies.lock.json`, the applicable hash-checked requirements lock,
`container/package-lock.json`, and `comfy/compatibility.json` together with the
dependency they describe. Every compatibility entry must have current backend,
requirements, and custom-requirements digests plus backend-specific smoke-test
evidence.
