# Harness maintenance instructions

This repository defines the Kimi Code sandbox and policy harness.
It is not the writable development workspace used by the contained agent.

`runtime/AGENTS.md` is the authoritative runtime operating contract.

`./models` and `./providers` are the only place model facts and provider policy
are stated; `docs/models-providers.md` is their schema contract, in the same
relationship `docs/modules.md` has to `./modules`. Do not add a model name,
context window, concurrency ceiling, or policy percentage anywhere else, and in
particular not to `.env`. When NRP republishes its terms, one edit to
`providers/nrp/provider.toml` must be enough.

When changing model, provider, concurrency, context, or proxy behavior:
- preserve compliance with every rule the selected providers publish, and keep
  the resolver's derived limits the largest those rules allow rather than
  hand-picking smaller ones;
- keep the three lanes on separate routes, providers and model aliases, and let the
  reservation gate decide which of them may overlap: only a lane whose reservation
  reaches its provider's exclusive threshold runs alone, and that is derived from the
  resolved windows rather than from a special case;
- keep `secondary_model.force = true`;
- verify proxy policy against the actual runtime Kimi configuration;
- never place real model credentials in the kimi-agent container, and mount a
  credential only into `model-proxy`, through the generated secrets fragment for
  the credentials this selection actually uses;
- preserve strict `/primary`, `/long`, and `/subagent` route separation, with
  only the lanes in the resolved plan routed at all;
- keep retries outside scarce permits during backoff;
- keep rate admission outside permit holds too, so waiting on a rolling ledger
  books neither kind of capacity;
- derive each lane's reservation from its `max_input_size` plus its own output
  clamp in the rendered Kimi configuration, and revalidate it per request; a
  configuration the proxy cannot fit must fail closed rather than pass traffic;
- never reintroduce a hard-coded subagent or swarm wall-clock timeout in
  `compose.yaml`. Unlimited is expressed only as `timeout_ms = 0` in
  `runtime/config.toml`, where the launcher re-pins it, because those tables are
  not user-owned; the equivalent environment variables outrank the file and
  reject zero.
- keep the persistent private cache salt out of logs and tracked files.

Generated runtime files belong only under `.local/runtime/<instance>/`. Do not
write credentials, rendered provider configuration, the resolved policy plan,
approval manifests, bridge private keys, or launcher locks into the workspace.

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
flags. Keep every file that carries provider policy re-pinned at launch rather
than trusting in-session edits, keep the policy merge default-deny, and keep the
initializer failing closed if the flags are not honoured.

The session system prompt resolves the project-root `SYSTEM.md` first, then the
tracked `SYSTEM.md.example`, then nothing — the same override/default pair as
`.env` and `.env.example`, with the operator's file kept out of git. An empty
file is a decision rather than a missing file and must not fall back to the
default — but Kimi Code discards a prompt that is blank once trimmed, so a
deliberately empty prompt stages as a lone period and only an absent pair of
files stages nothing at all. Whatever is chosen is staged through the same
protection, so never make it writable from inside the session. Amendment and
replacement are both supported: a file bearing the `${base_prompt}` placeholder
wraps Kimi Code's own prompt, and one without it replaces that prompt completely.
Do not copy the built-in prompt into either file — use the placeholder, or name
the individual template variables that the built-in prompt would have carried.
Keep comments out of the tracked default: Markdown comments are not stripped and
ship inside every request.

`tools/render_runtime.py` appends to that text, in order, the selected modules'
guidance and the generated Model runtime envelope. Module guidance is not behind
any flag: staging it is the only path a module's own `AGENTS.md` has to reach the
agent. The envelope is appended unless
`KIMI_SYSTEM_PROMPT_OMIT_ENVELOPE` in `.env`, read through
`compose.bootstrap.yaml`, holds a truthy value — the flag names the omission, so
an unset, blank, or unrecognised value appends it, which is the default.
Appending is what keeps the workspace sacrosanct. Nothing in the harness writes
the workspace's `AGENTS.md`: that file belongs to whatever project the agent is
working on, so never reintroduce a managed section, a marker pair, or any other
generated block there.

`kimi-agent` must receive no host binds other than the workspace, plus approved
project-extension snapshots. Bind sources are visible in the agent's mount table,
so never mount a path that names credentials, instance identities, or operator
directories. `tools/compose_hygiene.py` enforces this against the fully resolved
launch configuration, and `tests/compose-config.sh` runs it again over the core
files, the generated approved-extension fragment, and every module overlay. It
also requires every published port to be bound to `127.0.0.1` and every container
to keep a read-only root filesystem. Stage module-owned files into a named volume
with a root-only one-shot instead of adding a bind.

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
