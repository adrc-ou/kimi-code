# Harness maintenance instructions

This repository defines the Kimi Code sandbox and policy harness.
It is not the writable development workspace used by the contained agent.

`runtime/AGENTS.md` is the authoritative runtime operating contract.

When changing NRP model, concurrency, context, or proxy behavior:
- preserve NRP Fair Use compliance;
- keep primary and subagent lanes mutually exclusive, which the reservation gate
  derives from the configured windows rather than from a special case;
- keep `secondary_model.force = true`;
- verify proxy policy against the actual runtime Kimi configuration;
- never place real model credentials in the kimi-agent container.
- preserve strict `/primary`, `/long`, and `/subagent` route separation;
- keep retries outside scarce fair-use permits during backoff;
- keep output-rate admission outside fair-use permits too, so waiting on the
  tokens-per-minute budget holds neither kind of capacity;
- derive each lane's fair-use reservation from its `max_input_size` plus its own
  output clamp in the rendered Kimi configuration, and revalidate it per request;
  a configuration the proxy cannot fit must fail closed rather than pass traffic;
- never reintroduce a hard-coded subagent or swarm wall-clock timeout in
  `compose.yaml`. Unlimited is expressed only as `timeout_ms = 0` in
  `runtime/config.toml`, where the launcher re-pins it, because those tables are
  not user-owned; the equivalent environment variables outrank the file and
  reject zero.
- keep the persistent private cache salt out of logs and tracked files.

Generated runtime files belong only under `.local/runtime/<instance>/`. Do not
write credentials, rendered provider configuration, approval manifests, bridge
private keys, or launcher locks into the workspace.

Project agents, skills, and MCP configuration must pass `extensions.sh`
approval and remain mounted read-only during a running session. Ordinary
project `AGENTS.md` guidance remains part of the writable workspace.

Do not reintroduce legacy `.agent/`, `.agent-container/`, `SAFE_CONTEXT`,
or `KIMI_MODEL_*` configuration.

## Agent state and settings

Kimi's home is a writable named volume because the UI saves `config.toml` with a
temporary-file rename. Never bind-mount a host file into `/home/agent/.kimi-code`
again, and never make that whole path read-only.

The root-only `agent-state-init` one-shot owns that volume's protected content:
it stages runtime files, merges the user-owned keys from
`runtime/config-policy.json` over the rendered baseline, and sets ext4 immutable
flags. Keep every file that carries NRP policy re-pinned at launch rather than
trusting in-session edits, keep the policy merge default-deny, and keep the
initializer failing closed if the flags are not honoured.

`kimi-agent` must receive no host binds other than the workspace, plus approved
project-extension snapshots. Bind sources are visible in the agent's mount table,
so never mount a path that names credentials, instance identities, or operator
directories.

## Persistent workspace contract

Module application code belongs in replaceable images or private native runtimes.
Persistent paths declared by modules must survive upgrades, deselection, and
module deletion. Never remove user data during workspace initialization.
Module-specific operating instructions belong inside the module.

## Dependency and release policy

Fetch third-party applications and dependencies from official upstream sources;
do not vendor their packages or source distributions in this repository.

Resolve selectable releases to immutable commits or verify their published
SHA-256 digest. Keep GPU-framework and custom-node dependencies pinned and
update them deliberately. Do not install unreviewed custom-node dependencies at
service startup.

Update `dependencies.lock.json`, the applicable hash-checked requirements lock,
`container/package-lock.json`, and module dependency/compatibility metadata with the
dependency they describe. Every compatibility entry must have current backend,
requirements, and custom-requirements digests plus backend-specific smoke-test
evidence.
