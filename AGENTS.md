# Harness maintenance instructions

This repository defines the Kimi Code sandbox and policy harness.
It is not the writable development workspace used by the contained agent.

`runtime/AGENTS.md` is the authoritative runtime operating contract.

## This checkout is not the running instance

The live stack was started from a *different* clone of this repository on the host,
and it rendered all of its own configuration from that clone at the moment it was
launched. This checkout is where work is done and reviewed; it is not what is
running, and the running instance cannot see an edit made here.

Consequences worth internalising before spending a turn on them:

- Nothing changed in this checkout affects the session changing it. Not a lane
  label, a limit, a route, a credential, a prompt block, or a proxy behaviour.
  Every one of them is read from configuration staged before this session existed.
- Never try to restart, reload, or re-point the stack from inside a session, and
  never answer a question about current behaviour by running this checkout's code
  and reporting the result as what is live. The two answers differ, and only one
  of them can be verified from in here.
- Generated state found *in this checkout* is stale by definition: the launcher
  lock, rendered configuration, staged prompts and resolved plan under the
  instance runtime directory belong to some earlier launch of this clone, not to
  the stack serving this session. Do not quote them as current facts. An instance
  directory keyed to a *different* clone is only swept the next time the operator
  launches from this checkout, so treat any residue here as still present.
- The harness keeps no working files inside the project. The agent's session
  scratch is `/tmp/agent-state` in the agent container, and the launcher deletes
  the `.agent-state/` directory an older revision left in the workspace, so a
  tree that is checked out here is the project's own and not harness state.
- The trustworthy record of what the live instance was told is its own staged
  documents and its own resolved plan, which live in the other clone and are not
  reachable from here. When those matter, say so and ask, rather than
  reconstructing them from this checkout.
- Report work as "shipped, takes effect on the operator's next launch". The
  update cycle is external and manual: commit and push from this clone, pull in
  the running clone, stop the stack, start it again. Do not claim a change is in
  force, and do not treat observed in-session behaviour as evidence that a change
  here did or did not work.

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
  reservation gate decide which of them may overlap: a request runs alone only when
  the price it is admitted at reaches its provider's exclusive threshold, and that
  price is the live request's own estimate capped by the lane reservation, never a
  property of the lane name;
- keep `agent_lane` a launch-time designation of which lane the main agent opens on,
  rendered into `default_model` and validated against the plan at every policy
  refresh; the selection never removes a route, and nothing may attempt to rewrite
  Kimi's live configuration to move a session between lanes;
- keep `secondary_model.force = true`;
- verify proxy policy against the Kimi configuration the launcher staged, which is
  the only one the proxy can see;
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

Anything under that directory that can carry a key is swept at the start of a
launch as well as at the end, because a launcher that is killed outright runs no
shutdown; `harness_sweep_secrets` in `tools/runtime.sh` holds that list and is the
only place to extend it. The start-of-launch pass covers **every** instance
directory under `.local/runtime/`, not only the one this launch will use: the id
digests the checkout path and the workspace path, so a clone that has moved or been
re-pointed orphans its whole previous directory and nothing later ever names it.
`harness_sweep_stale_instances` decides which siblings are safe to touch by testing
whether the launcher lock is actually held, via `tools/instance_lock.py`, and never
by the `pid=` line inside it — that number belongs to a stranger after a reboot.
Never add a file to the sweep that is meant to persist — the cache salt is the
standing example of why, since rotating it silently invalidates every cached
response.

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

Two harness-owned documents carry instructions that are not the user's prompt:
the project-root `SYSTEM.md`, staged as Kimi's system prompt, and the
project-root `CONTEXT.md`, staged as the runtime `AGENTS.md` every lane reads.
Each resolves in two tiers and no more: the operator's file if it exists,
otherwise this harness's own text (`runtime/AGENTS.md` for `CONTEXT.md`, and for
`SYSTEM.md` a `${base_prompt}` wrapper that lets Kimi supply its own built-in
prompt). `.example` files are documentation of these conventions and must never
appear in a fallback chain — an operator who deletes or ignores an `.example`
file is entitled to believe it inert. Existence decides authority and emptiness
decides payload: a non-empty file is amended by the enabled add-ons, an empty one
leaves the add-ons as the whole prompt, and an empty one with no add-ons stages
the `EMPTY_PROMPT_SENTINEL`, because Kimi Code discards a prompt that is blank
once trimmed and would silently reinstate its own. Only an absent `SYSTEM.md` may
reach the built-in prompt. Amendment and replacement are both supported: a file
bearing `${base_prompt}` wraps Kimi's prompt, one without it replaces that prompt
completely. Do not copy the built-in prompt into either file — use the
placeholder, the `${kimi.*}` names that extract the individual built-in blocks
from the running image, or the template variables Kimi would have expanded.
`<!-- ... -->` comments are stripped before staging so the tracked defaults can
explain themselves without costing tokens in every request.

Whatever is chosen is staged through the same protection, so never make it
writable from inside the session. The add-ons — the generated usage limits, lane
table, parallelism guidance, and module guidance — are selected on the launch
panel, which is the only control: no `.env` variable governs session context, and
`tools/prompt_context.py` reads `prompt-context.json` from the instance runtime
directory rather than the environment. Never reintroduce an opt-out variable.
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
