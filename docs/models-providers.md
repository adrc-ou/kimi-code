# Model and provider definition contract (schema version 1)

Two operator-owned directories hold every fact the harness needs about *what
model serves traffic* and *what the provider permits*. Nothing in `.env`
describes either. A model change is a directory choice at startup; a policy
change is one edit to one file.

```text
providers/nrp/provider.toml        # endpoint, named credentials, policy rules, safety margins
models/qwen3_8_flash_next/model.toml  # immutable facts about one model, naming a provider
```

Like `modules/`, there is no central registry: a folder without its manifest is
ignored, so an operator adds support by copying a directory in and removes it by
deleting the directory. Both trees are discovered by `tools/definitions.py`, and
a tree that is present but malformed fails startup rather than being skipped.

Definitions are host-trusted operator configuration, **not a sandbox**. They are
still read strictly, because these files decide which endpoint receives an API
key: a manifest that is a symlink, a definition directory that is a symlink, and
any non-regular file inside either tree are refused outright.

Identifiers:

| Pattern | Applies to | Why |
| --- | --- | --- |
| `[a-z][a-z0-9_]*` | `providers/<id>`, `models/<id>`, `credential.id`, `model.provider`, `model.credential` | Directory names are stable keys; `_` is allowed, `-` is not, so `-` can separate fields in generated names |
| `[a-z][a-z0-9]*` | `model.slug` | The slug forms Kimi aliases (`qwen3-primary`), so it must not contain the `_`/`-` separators used around it |
| `[A-Z][A-Z0-9_]*` | `endpoint.base_url_env`, `credential.env`, `model.key_env` | All three name a variable in `.env` |

Neither a provider directory id nor a credential id may contain `__`, even
though `[a-z][a-z0-9_]*` alone would allow it: secrets are addressed by joining
those identifiers with `__`, so a component carrying the separator would make one
name spellable two ways. Discovery refuses such a directory or id.

A model-scoped `key_env` also may not equal the `env` of any credential of its
own provider — that would ask one variable to hold two different keys, and the
model might as well name that credential instead.

Slugs must be unique across all models: two models sharing a slug would
generate identical Kimi aliases, and identical model-scoped credential names,
which is refused at discovery.

## `models/<id>/model.toml`

```toml
schema_version = 1

label = "Qwen3.8-Flash-Next"   # user-facing name shown by ./start.sh
provider = "nrp"                # a directory that must exist in ./providers
model = "Qwen3.8 Flash Next"    # identifier sent upstream (the wire name)
slug = "qwen3"                  # alias stem: <slug>-primary, <slug>-long, <slug>-subagent
credential = "default"          # exactly one credential id of the named provider
key_env = "QWEN3_API_KEY"       # optional; .env variable holding a key for this model alone

capabilities = ["thinking", "image_in", "video_in", "tool_use"]  # optional
support_efforts = ["low", "medium", "xhigh"]         # optional
default_effort = "xhigh"                             # optional

[context]
advertised_tokens = 1000000     # optional; defaults to the largest lane window

[lane.primary]                  # required for the model to be selectable as primary
context_tokens = 262144
input_tokens = 196608
output_clamp_tokens = 65536
default_effort = ""             # optional override of the model default

[lane.long]                     # optional; declaring it creates the /long route
context_tokens = 1000000
input_tokens = 900000
output_clamp_tokens = 65536

[lane.subagent]                 # required for the model to be selectable for subagents
context_tokens = 64000
input_tokens = 55808
output_clamp_tokens = 8192
```

`schema_version`, `label`, `provider`, `model`, `slug`, `credential` and at
least one `[lane.*]` table are required; unrecognised keys are refused so a typo
cannot silently disable a limit. Lane names are exactly `primary`, `long` and
`subagent`. All three lane numbers are positive integers, and a model must
declare both `primary` and `subagent` to appear in either picker.

Three numbers per lane, and what they mean:

- `context_tokens` — the window Kimi is told the model has, i.e.
  `max_context_size` in Kimi's model table. This is a *slice* of the model, not
  necessarily the whole thing: the native window here is 262,144 while the
  provider advertises 1,000,000, and the extra headroom is opt-in through the
  long lane.
- `input_tokens` — Kimi's `max_input_size`, the prompt-side cap the proxy
  rejects above.
- `output_clamp_tokens` — the generation budget the proxy clamps
  `max_tokens` to, and the amount it books against a per-minute output rate
  before the request starts.

`[context].advertised_tokens` is what fraction-based provider rules are measured
against, so it is the provider's advertised window even when no single lane is
that large. It may not be smaller than any declared lane window.

A model declares **capabilities**, never costs or limits. `capabilities`,
`support_efforts` and the effort default are copied into the generated Kimi
model table; the long lane exists so a model's extended window is a deliberate
choice rather than something every request inherits.

## `providers/<id>/provider.toml`

```toml
schema_version = 1
label = "NRP"
policy_url = "https://nrp.ai/documentation/userdocs/ai/llm-managed/fair-use"

[endpoint]
base_url = "https://litellm.lib.ou.edu"   # origin only, no /v1 path
base_url_env = "NRP_BASE_URL"             # optional .env name that overrides it
protocol = "openai"                       # openai | anthropic

[[credential]]
id = "default"
label = "NRP managed-LLM API token"
prompt = "NRP API token"                  # optional; defaults to label
env = "NRP_API_KEY"                       # .env name the value is read from
key_url = "https://litellm.lib.ou.edu/ui/?page=api-keys"

[[rule]]
kind = "aggregate_context_fraction"
scope = "model"
percent = 35
models = ["Qwen3.8 Flash Next"]           # optional; defaults to every model of this provider

[safety]
context_margin_percent = 95               # optional, 1-100, default 100
output_rate_margin_percent = 90           # optional, 1-100, default 100
```

`label`, `[endpoint]`, at least one `[[credential]]` and at least one `[[rule]]`
are required. An empty `[[rule]]` list would mean "this provider has no terms",
which is a claim worth refusing rather than assuming.

**Endpoint.** `base_url` is the documented default for the deployment and
carries no path; the proxy appends its own route per request. `base_url_env`
lets an operator redirect the provider from `.env` without editing the
definition — the definition stays the documented default, and the override is
applied by `tools/models.py resolve`. `protocol` becomes the Kimi provider
`type`, so a future Anthropic-style endpoint needs no code change.

**Credentials and key scope.** A key can be handed out per model or per provider,
and the operator picks which without touching code. `credential` in a model is
always the *provider-scoped* identity: one `[[credential]]` table = one shared
key, and any number of models may name the same id to use it. `key_env` in the
same model is the optional *model-scoped* variable. Resolution then reads:

| `key_env` has a value | `credential.env` has a value | This model authenticates with |
| --- | --- | --- |
| yes | either | `key_env`, as a credential of its own |
| no | yes | `credential.env`, shared with every model naming that credential |
| no | no | a prompt at launch, for this model alone |

The third row is why a model that declares `key_env` keeps its own scope even
when neither variable is set: a key asked for *on behalf of one model* must not be
written into the file every model of that provider reads.

A model-scoped key becomes an ordinary credential identity, so nothing
downstream has to know the distinction exists. The id is synthesised as
`<credential>__<slug>`, so the shipped model reads `QWEN3_API_KEY` into a
credential called `default__qwen3` and the launcher writes the secret file
`nrp__default__qwen3`. Synthesis is refused if that id already exists in the
provider — rename the credential or the slug rather than silently reusing one.
Because the credential id is part of what `credential` and `credential_model`
rules count against, scoping happens **before** the resolver builds any counter:
two models on two keys share no upstream ledger, so they must share none here,
while two models on one provider key keep sharing one. A scoped credential still
records the provider-scoped name as `fallback_env`, which is all the launcher
needs to name both variables when it asks.

Inside the container a credential is addressed as `<provider>__<credential>`,
which is collision-free because no component may contain the `__` separator.
`env` is the `.env` name the launcher reads; `key_url` records where an operator
obtains the key, because that is a provider fact and belongs with the provider.

Values never enter the agent container and are never written to `.env`, the
workspace, or any tracked file. The launcher writes one mode-0600 file per
*used* credential under `.local/runtime/<instance>/credentials/`, publishes them
through a generated Compose secrets fragment, and mounts them only into the
proxy. A value missing from `.env` is prompted for once, for that session only.

### Rule taxonomy

A rule is `{kind, scope, limit|percent, models?}`. `kind` decides which enforcer
it becomes and which unit it is measured in; `scope` decides **which traffic
shares one counter**, which is precisely why selecting the same model for both
lanes behaves differently from selecting two. The nine enforceable kinds:

| `kind` | Family | Field | Admissible `scope` | Becomes |
| --- | --- | --- | --- | --- |
| `aggregate_context_fraction` | context | `percent` | `model` | Aggregate in-flight token budget (`FairUseGate` budget) |
| `exclusive_above_context_fraction` | context | `percent` | `model` | Single-request exclusivity threshold (`FairUseGate` exclusive) |
| `max_concurrent_requests` | count | `limit` | `model`, `credential`, `credential_model`, `provider` | Permit ceiling on a counter |
| `output_tokens_per_minute` | rate | `limit` | `model`, `credential`, `credential_model`, `provider` | Rolling ledger, unit `output_tokens` |
| `input_tokens_per_minute` | rate | `limit` | same | Rolling ledger, unit `input_tokens` |
| `tokens_per_minute` | rate | `limit` | same | Rolling ledger, unit `total_tokens` |
| `requests_per_minute` | rate | `limit` | same | Rolling ledger, unit `requests` |
| `max_context_tokens` | per-request | `limit` | `model`, `provider` | Shrinks each lane's context window |
| `max_output_tokens_per_request` | per-request | `limit` | `model`, `provider` | Shrinks each lane's output clamp |

Adding a kind here without adding its enforcer in `proxy/model_proxy.py` is a
bug, so unknown kinds are refused at startup rather than quietly unenforced.
Combinations that cannot be honoured are refused too: a provider-scoped context
fraction has no single model window to measure, so `ALLOWED_SCOPES` rejects it
instead of guessing at a denominator.

Scopes resolve to a counter subject:

| `scope` | Counter subject key | Typical meaning |
| --- | --- | --- |
| `model` | `<provider>/<wire model>` | One counter per model, shared by every lane serving it |
| `credential` | `<provider>/<credential id>` | Shared by every model served with that key |
| `credential_model` | `<provider>/<credential id>/<wire model>` | NRP's "per token and per model" pairing |
| `provider` | `<provider>` | Anything served by this provider |

`models = [...]` narrows a rule to specific wire names, so one provider file can
hold per-model terms. Multiple rules of one kind in one scope collapse to the
**tightest** value.

This taxonomy is deliberately not NRP-shaped. NRP publishes no per-token
monetary ceiling and no hard requests-per-minute; a provider that does is
expressed by adding a `limit` kind, not by restructuring the resolver.
Conversely, a provider with *no* concurrency rule at all is legitimate — see
"greedy resolution" below.

**Safety margins** are the only tunables a provider file carries, and they exist
because request cost is estimated before it is known. Each is a percentage of
the provider's own threshold, applied by the resolver — never a number
subtracted by hand in `.env`. The derived budget is always `min(threshold,
threshold × margin)`, so a margin cannot push usage above the published rule.

## Resolution

`tools/policy.py resolve()` takes the two selected models plus their providers
and produces one enforcement plan (`model-policy.json`). It runs on every
launch, so nothing is configured twice, and it is the only input the proxy,
Kimi's config renderer, and the workspace guidance consume.

Key scope is settled first, before any lane or counter exists: `scope_keys()`
reads the launcher environment and gives every selected model that declares a
`key_env` a credential identity of its own, per the table under **Credentials and
key scope** above. No derived limit changes because of it — scoping a key per
model splits ledgers, it does not enlarge any allowance.

1. **Lane membership.** `primary` comes from the primary selection, `subagent`
   from the subagent selection, and `long` exists only if the primary model
   declares it. Dropping a lane drops its route, its alias, and its counters.
2. **Lane sizing.** Per-request caps shrink the declared window and clamp;
   `input_tokens` is then `min(declared, context - clamp)`. Kimi's
   `loop_control.reserved_context_size` (read from `runtime/config.toml`,
   because it describes how Kimi paces its own compaction rather than what the
   model is) must fit inside every lane or resolution fails.
3. **Counters.** One context counter per (provider, model) pair, plus rate and
   count counters per admissible scope, each keyed by the scope table above and
   carrying the list of lanes bound to it. A model serving itself with its own
   key contributes its synthesised credential id to those subjects, so a
   credential-scoped rule follows the key actually in use rather than the
   provider that issued it. A model-scoped concurrency limit is
   folded into that model's context counter rather than duplicated, since both
   describe the same set of in-flight requests.
4. **Exclusivity.** A lane is exclusive when its in-flight cost
   (`input + clamp`) reaches the tightest `exclusive_above_context_fraction`
   threshold bound to it.
5. **Feasibility.** A non-exclusive lane whose reservation exceeds its budget
   could never be admitted, so that is a hard error naming the fix. An
   exclusive lane may exceed the budget, because the provider itself permits
   exactly that request to run alone; the proxy admits it only when the counter
   is empty.
6. **Derived limits.** `subagent_concurrency` is the largest fan-out the rules
   allow: `1` when the subagent lane is exclusive, otherwise the minimum of the
   count ceilings and every `budget ÷ reservation` quotient. When the selected
   provider states no ceiling at all, the harness publishes its own default of
   8 and labels it as such — an unconstrained provider is a reason to use the
   allowance, not a reason to stall the workspace on 1.

The three structural cases a selection can fall into:

- **One model in both lanes** — one `model`-scoped counter, so primary and
  subagent traffic contend for the same permits and the aggregate context
  budget spans both lanes. Fan-out is `budget ÷ subagent reservation`.
- **Two models, one provider** — two model-scoped counters; only
  `credential`, `credential_model` and `provider` rules are shared, so a
  per-key rate limit still couples the lanes while context does not.
- **Two models, two providers** — disjoint key sets. Each provider's rules are
  enforced only against its own traffic, and the derived subagent fan-out comes
  from the subagent's provider alone.

### Greedy, not conservative

Every derived number is the largest the provider's rules allow after the declared
margin. Under-using a paid allowance buys nothing and costs wall-clock time, so
`./start.sh` also derives Kimi's subagent fan-out from the same plan the proxy
enforces — Kimi fills the envelope exactly instead of hoping two hand-written
numbers agree. `tools/policy.py render_guidance()` turns the plan into a table of
lanes, budgets and ceilings plus an explicit instruction to run the full allowed
number of subagents, and that text is what the launcher appends to the session's
system prompt.

## Artifacts and lifetimes

All generated files live under `.local/runtime/<instance>/` (mode 0600, created
atomically) and are deleted when the launcher exits:

| File | Written by | Read by | Contents |
| --- | --- | --- | --- |
| `model-selection.json` | `models.py select` | `models.py resolve` | `{"primary": id, "subagent": id}` |
| `last-model-selection.json` | `start.sh` after a successful launch | `models.py select` | Prior selection, shown as `(last used)` and pre-selected |
| `model-policy.json` | `models.py resolve` | proxy, `render_runtime.py`, `check_services.py` | The plan: lanes, counters, limits, providers, selection |
| `model.env` | `models.py resolve` | `start.sh`, then Compose interpolation | `KIMI_SUBAGENT_CONCURRENCY`, `KIMI_BACKGROUND_TASK_SLOTS` — only the values `compose.yaml` interpolates; lane aliases reach Kimi through the rendered config |
| `credentials/<provider>__<credential>` | `render_runtime.py` | proxy (via Docker secret) | One key value each, where a model-scoped credential id is the synthesised `<credential>__<slug>`; stale files from deselected models are removed |
| `compose/models.json` | `models.py resolve` | Compose (`-f`, appended by `tools/runtime.sh`) | Secrets declaration + the proxy service's secret list |

The plan is a versioned document (`schema_version = 1`); the proxy refuses a plan
whose version it does not understand. Only lanes present in the plan get routes,
so a model without a `long` lane 404s on `/long/v1/*` rather than serving it.

`compose.bootstrap.yaml` must declare every `.env` name the selected definitions
reference — each `credential.env`, each `model.key_env`, each
`endpoint.base_url_env`, and the harness's own `KIMI_BACKGROUND_TASK_SLOTS`.
`key_env` is declared even when the provider-scoped key won, because the name is
a property of the selected model and Compose silently drops undeclared names.
`models.py resolve` checks this because Compose interpolates only names a file
declares, and a missing declaration looks exactly like an operator who left the
key blank.

## System prompt composition

The generated envelope never enters the workspace. `tools/models.py resolve`
writes the plan, and `tools/render_runtime.py` renders `policy.render_guidance()`
from that same plan and appends it to the staged session prompt. The order is
fixed: the prompt file (`SYSTEM.md` or `SYSTEM.md.example`), then the selected
modules' guidance, then the envelope.

Nothing writes into `<workspace>/AGENTS.md`. That file belongs to the project Kimi
is working on, and an operator is entitled to find the harness left it untouched —
which is also why module guidance is staged in the instance runtime directory as
`module-guidance.md` rather than merged into it. Both are re-stamped at every
launch and neither carries a credential, so neither is session material that the
launcher has to delete on exit.

Appending the envelope is the default. Set `KIMI_SYSTEM_PROMPT_OMIT_ENVELOPE=1`
(or `true`, `yes`, `on`) in `.env` and the prompt is staged with nothing appended
to it; the flag is named after the omission, so a value that is not truthy omits
nothing. Module guidance is not behind that flag, since staging it is the only
path a module's own `AGENTS.md` has to reach the agent. Appended text goes through
Kimi's template substitution like the rest of the prompt, which is why the
generated envelope is written without a single `${...}`.

Kimi Code discards a system prompt that is blank once trimmed and substitutes its
own built-in prompt, so a deliberately empty prompt file stages as a lone period
rather than nothing. Only an absent `SYSTEM.md` with an absent `SYSTEM.md.example`
stages a blank file, and that tier means "use whatever Kimi ships".

## Adding a model

1. `cp -r models/qwen3_8_flash_next models/new_thing`
2. Set `label`, `model` (wire name), a unique `slug`, the `provider` and
   `credential` ids, `[context].advertised_tokens`, and the lanes you want.
3. Optionally set `key_env` to a variable name of your own — that is what lets
   this model carry its own key in `.env` instead of sharing the one its
   `credential` names. Whatever you choose, add that name to
   `compose.bootstrap.yaml`, alongside the names your provider contributes.
4. `./start.sh` — the new label appears alphabetically in both pickers.

Delete the directory to remove it. Nothing else references model ids.

## Adding a provider

1. Create `providers/acme/provider.toml` with `[endpoint]`, one or more
   `[[credential]]` tables, and the `[[rule]]` set you can actually enforce.
2. Add every `credential.env` / `base_url_env` name to
   `compose.bootstrap.yaml`'s bootstrap environment.
3. Point a model at it with `provider = "acme"`.

A model whose provider directory is missing is dropped from both pickers rather
than failing startup, which is how you park a model while its provider is being
rewritten. Removing a provider directory therefore removes its models in one
step.

## Kimi's model picker

`render_runtime.py` generates `[providers.*]`, `[models.*]` and
`[secondary_model]` from the plan, so the harness sets the default model and the
subagent model automatically. Kimi Code itself offers `/model`,
`/secondary-model`, `/provider` and `/settings` in-session, and there is no
documented way to disable that surface without patching the CLI, which this
project will not do. The mitigations are that `secondary_model.force = true`
keeps children on the subagent lane, `default_model` is not one of the
user-owned keys the initializer lets an in-session write keep, and the proxy
re-reads Kimi's live config at every policy refresh and fails closed if a lane
drifts from the plan. A wrong turn therefore loses a request, not fair-use
compliance.
