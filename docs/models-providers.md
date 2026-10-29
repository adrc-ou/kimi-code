# Model and provider definition contract

Two operator-owned directories hold every fact the harness needs about *what
model serves traffic*, *what the provider permits*, and *what a managed engine
can allocate*. Nothing in `.env`
describes either. A model change is a directory choice at startup; a policy
change is one edit to one file.

```text
providers/nrp/provider.toml              # endpoint, credentials, host requirement, policy rules
models/nrp/qwen3_8_flash_next/model.toml # one model, as one provider serves it
```

Like `modules/`, there is no central registry: a folder without its manifest is
ignored, so an operator adds support by copying a directory in and removes it by
deleting the directory. Both trees are discovered by `tools/definitions.py`, and
a tree that is present but malformed fails startup rather than being skipped.

Model definitions nest under the provider that serves them, because that pair -
not the model name - is the unit everything else already treats as one thing:
policy keys each counter `<provider>/<wire model>`, secrets are
`<provider>__<credential>`, and one model name can legitimately be served two
ways. A hosted serving can have published fair-use terms while a local serving
allocates the operator's own memory. Those are two definitions, and the qualified
identifier records which serving was selected. The directory half of that name also makes removal
atomic: dropping a provider directory, or a host that cannot satisfy its
`requires`, retires every model it served in one step.

A `model.toml` one level too shallow is refused with the move to make, rather
than ignored: a discovery rule that quietly skipped an operator's copied
directory would look like a model that had disappeared.

Definitions are host-trusted operator configuration, **not a sandbox**. They are
still read strictly, because these files decide which endpoint receives an API
key: a manifest that is a symlink, a definition directory that is a symlink, and
any non-regular file inside either tree are refused outright.

## Versions and authority

Repository validation and the pending native rollout are recorded in
[capacity compatibility evidence](capacity-compatibility.md).

Manifest versioning and resolved-plan versioning are separate contracts.
`schema_version = 1` manifests retain fixed lane envelopes, fixed output clamps
and their existing provider-rule semantics. NRP's definitions remain version 1;
their lanes, counters, margins, routing and credential scopes are unchanged.

Version 2 manifests explicitly declare managed execution and capacity-derived
lanes. A managed provider declares its engine profile and native-context execution
policy, plus any provider rules that also apply. `rule = []` explicitly means no
provider fair-use rules for that managed serving. Omitting the rule declaration,
execution profile or capacity policy is an error; absence never grants unlimited
operation. Execution slots and provider obligations can both constrain one
request, and their counter identities stay distinct.

The resolver emits a version 1 plan for a selection using only existing fixed
envelopes. A selection containing managed engines emits a version 3 plan with
native windows and explicit engine slots in addition to the ordinary lanes,
counters and limits. There is one authoritative plan for engine launch, Kimi
configuration, proxy admission and diagnostics.

Identifiers:

| Pattern | Applies to | Why |
| --- | --- | --- |
| `[a-z][a-z0-9_]*` | `providers/<id>`, the `<id>` in `models/<provider>/<id>`, `credential.id`, `model.provider`, `model.credential` | Directory names are stable keys; `_` is allowed, `-` is not, so `-` can separate fields in generated names |
| `[a-z][a-z0-9]*` | `model.slug` | The slug forms Kimi aliases (`qwen3-primary`), so it must not contain the `_`/`-` separators used around it |

The model's identifier elsewhere in the launcher is `"<provider>/<id>"` - the
qualified pair - because two providers may serve one model name and a selection
that recorded only the second half would not say which was chosen. Slugs stay
globally unique, since two servings sharing a slug would generate identical
aliases.
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

## `models/<provider>/<id>/model.toml`

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
context_tokens = 66500
input_tokens = 55808
output_clamp_tokens = 10692
default_effort = "low"          # optional override of the model default

# Below here belongs to a model served on the operator's own machine, and is
# refused on one a remote gateway serves. Both blocks describe bytes that run here.
[requires]
memory_gib = 32                 # host floors in GiB, measured by tools/host_caps.py; 0 = none
disk_gib = 24

[install]
variant = "UD-Q4_K_XL"          # the quantised build weights.lock.json pins
lock = "weights.lock.json"      # optional; a file name in this directory, never a path
port = 8181                     # optional; replaces only the port of the provider's origin

[install.sampling]              # optional; the vendor's defaults for the engine itself
temperature = 1.0
top_p = 0.95
top_k = 20
min_p = 0.0
```

`schema_version`, `label`, `provider`, `model`, `slug`, `credential` and at
least one `[lane.*]` table are required; unrecognised keys are refused so a typo
cannot silently disable a limit. Lane names are exactly `primary`, `long` and
`subagent`. In version 1, all three lane numbers are positive integers. A model must
declare both `primary` and `subagent` to appear in either picker.

`[requires]` and `[install]` are the local half of that contract, and both are
refused on a model whose provider declares no host requirement: a floor nobody
measures and a download nobody performs are worse than absent, because they read
as protection. The converse holds too - every model under a provider that does
name a capability must declare `[install]`, because without a pinned lock there
is nothing to verify and no honest answer for the picker's `Ready?` column.
`port` is what lets two local models coexist: one engine process serves one
model, so one listener each, while `base_url_env` still moves the host for all of
them. Two models of one provider naming the same port is refused at discovery.

Three numbers per lane, and what they mean:

- `context_tokens` — the window Kimi is told the model has, i.e.
  `max_context_size` in Kimi's model table. This is a *slice* of the model, not
  necessarily the whole thing: the model here declares 262,144 on its `primary`
  lane and 1,000,000 on its `long` lane, and `[context].advertised_tokens` says
  the provider advertises 1,000,000 for the model as a whole. Two lanes is the
  norm, not an exception — they are the two ways of spending one allowance, and a
  lane is what the operator picks between.
- `input_tokens` — Kimi's `max_input_size`, the prompt-side cap the proxy
  rejects above.
- `output_clamp_tokens` — the generation budget the proxy clamps
  `max_tokens` to, and the amount it books against a per-minute output rate
  before the request starts.

`[context].advertised_tokens` is what fraction-based provider rules are measured
against, so it is the provider's advertised window even when no single lane is
that large. It may not be smaller than any declared lane window.

### Version 2 engine-derived model envelopes

Checkpoint, quantization, capabilities, reasoning defaults, installation locks
and static installed-RAM/disk eligibility remain model facts. These floors gate
model selection; they do not size context or assert residency. A managed model replaces the
fixed lane numbers with an engine binding and names its native ceiling:

```toml
schema_version = 2
# Existing identity, capabilities, requires and install fields are retained.

[context]
native_tokens = <checkpoint-native ceiling>

[lane.primary]
allocation = "engine"
output_mode = "remaining_context"

[lane.subagent]
allocation = "engine"
output_mode = "remaining_context"
default_effort = "low"         # optional, as for fixed lanes
```

The placeholder above is an integer from the model's own manifest, not TOML to
copy literally. Version 2 requires `native_tokens`; it is neither an extended
YaRN ceiling nor a provider-advertised denominator. Fixed `context_tokens`,
`input_tokens` and `output_clamp_tokens` are refused in these lane tables. The
resolver gives every lane bound to an engine the same fixed per-sequence
context, while preserving separate routes, aliases and reasoning defaults.
`input_tokens` becomes that window less Kimi's existing compaction reserve.
That is an early-compaction target, separate from the hard context boundary.
The absolute output ceiling is the engine window; each request's output
allowance is its actual remaining context, further bounded by any client maximum.

A model declares **capabilities**, never costs or limits. `capabilities`,
`support_efforts` and the effort default are copied into the generated Kimi
model table, and a lane may override that default with its own
`default_effort` — which is how a child gets asked to think less than the agent
that spawned it without either agent-facing lane following it. The effort is the
one lane knob that costs no fair-use arithmetic: it changes neither the
reservation nor the window, only what the lane spends its window on. The `long` lane exists so the wide window is a lane with its own
name, route, alias and accounting rather than a number every request inherits,
and because the two agent lanes are peers it is the one the launcher opens on by
default; a model that declares only `primary` simply offers one row.

## `providers/<id>/provider.toml`

```toml
schema_version = 1
label = "NRP"
policy_url = "https://nrp.ai/documentation/userdocs/ai/llm-managed/fair-use"
requires = []                             # optional; host capabilities this provider needs
                                          # ("metal", "cuda"). Absent = served from anywhere.

[endpoint]
base_url = "https://litellm.lib.ou.edu"   # origin only, no /v1 path
base_url_env = "NRP_BASE_URL"             # optional .env name that overrides it
protocol = "openai"                       # openai | anthropic
sends_cache_salt = true                   # optional; false for an engine with no shared cache

[[credential]]
id = "default"
label = "NRP managed-LLM API token"
prompt = "NRP API token"                  # optional; defaults to label
env = "NRP_API_KEY"                       # .env name the value is read from
key_url = "https://litellm.lib.ou.edu/ui/?page=api-keys"
                                          # generate = true instead: the launcher mints the
                                          # value per session, and key_url must then be absent

[[rule]]
kind = "aggregate_context_fraction"
scope = "model"
percent = 35
models = ["Qwen3.8 Flash Next"]           # optional; defaults to every model of this provider

[safety]
context_margin_percent = 95               # optional, 1-100, default 100
output_rate_margin_percent = 90           # optional, 1-100, default 100
```

`label`, `[endpoint]` and at least one `[[credential]]` are required. Version 1
also requires a nonempty `[[rule]]` list. Version 2 requires explicit managed
execution and capacity declarations; only that version may use an explicit
empty `rule = []` list. Both versions reject unknown or unenforceable rules.

**Endpoint.** `base_url` is the documented default for the deployment and
carries no path; the proxy appends its own route per request. `base_url_env`
lets an operator redirect the provider from `.env` without editing the
definition — the definition stays the documented default, and the override is
applied by `tools/models.py resolve`. `protocol` becomes the Kimi provider
`type`, so a future Anthropic-style endpoint needs no code change.

**Host requirement.** `requires` names capabilities `tools/host_caps.py` measures
on the machine running the launcher - `metal` for Darwin on arm64, `cuda`
reserved for the provider that will need it. Unknown names are refused at load,
because a typo in a capability list silences a whole provider and nothing else
would say so. A provider that names none is served from anywhere and is never
filtered. Filtering happens before the picker: the provider is removed from the
catalog and `discover_models`' existing rule ("a model whose provider is absent
is dropped") takes its models with it, so there is one mechanism for hiding a
model and it is the one that already exists. On a host that supports no local
provider, `./start.sh` shows nothing about local models at all.

**Cache salt.** `sends_cache_salt` defaults to true. The harness adds a private
salt to every request so a shared gateway cannot key its prompt cache on one
operator's content; an engine running on that operator's own machine has no such
cache to protect and no obligation to tolerate a field it does not model, so a
local provider sets it false and the proxy omits the key for those lanes.

**Generated credentials.** `generate = true` declares a credential nobody
issues. The launcher mints one value per session, writes it through the same
mode-0600 path the proxy mounts as a Docker secret, and hands the same value to
the engine it starts - which is how a listener that must be reachable from the VM
refuses strangers with nothing for the operator to configure. `env` stays
meaningful beside it as the override, so it is still required, and `key_url` is
refused: a credential cannot both have no issuer and name where to get one.

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
provider — rename the credential or the slug rather than silently reusing one —
and also if the resulting secret file name is longer than 64 characters, which
is the longest path the proxy will open: a slug long enough to overrun it fails
resolution instead of staging a credential file nothing can read.
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

A rule is `{kind, scope, limit|percent, models?, default?}`. `kind` decides which enforcer
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

A count rule may additionally carry `default = true`, which marks it as the
harness's own stand-in rather than a number the provider publishes for that
traffic. Only a count kind may be declared default, and only at a scope looser
than `model` — a model-scoped rule is already the most specific statement its
kind can make, and marking that default would only obscure it. Both are refused
at load rather than ignored. Resolution then runs in this order:

1. a **model-scoped** count rule that applies to the model displaces every
   `default`. The two must not stack: a model's own ceiling and a catch-all
   standing in for "nothing was said" describe the same set of in-flight
   requests, and the minimum of the two would enforce a ceiling that provider
   never stated for that model. Several model-scoped rules collapse to their
   `min`, since each is a separate true statement about the same traffic.
2. otherwise a `default` count rule supplies the ceiling, and the plan records
   the `count_basis` that says where the number came from.
3. either way, a **non-default** count rule at a looser scope (`credential`,
   `credential_model`, `provider`) keeps its own counter and stacks, exactly as
   it did before defaults existed; a `default` never opens one, which is why it
   must be able to resolve without a host counter of its own.

Subjects never merge across providers: `_key_for()` prefixes every subject with
its provider id, so two providers each holding a rule of one kind still get two
counters, and a selection spanning providers is charged each of them separately.
A `default` is a harness choice wearing a provider file's clothes, so the label
it resolves under has to say which; `providers/nrp/provider.toml` carries one and
it is not an NRP number.

This taxonomy is deliberately not NRP-shaped. NRP publishes no per-token
monetary ceiling and no hard requests-per-minute; a provider that does is
expressed by adding a `limit` kind, not by restructuring the resolver.
Conversely, a provider with *no* concurrency rule at all is legitimate — see
"greedy resolution" below.

**Provider safety margins** exist
because request cost is estimated before it is known. Each is a percentage of
the provider's own threshold, applied by the resolver — never a number
subtracted by hand in `.env`. `_percent()` accepts only an integer from 1 to 100,
so a margin cannot *be* an overcommitment; and the context budget takes
`min(threshold, threshold × margin)` on top of that, so the derived figure is
never above the published rule even if the bound were ever relaxed. The rate
ledger takes `threshold × margin` directly, capped at the same 100, and clamped
to a floor of 1 so a provider that publishes a tiny allowance still admits one
request rather than deadlocking. At the shipped provider the two derived numbers
are 332,500 in-flight tokens (95% of 350,000) and 180,000 booked output tokens
per minute (90% of 200,000).

## Managed local execution and capacity

A version 2 provider declares `[execution]` with `kind = "local"` and
`engine = "llama_cpp"` plus a single `execution.profile`. A version 3 provider
replaces that with one or more named `[execution.profiles.<id>]` tables and an
optional `[execution.memory]`, and a version 3 model selects exactly one of them
with `[serving] profile`. Both kinds declare a measured host capability in
`requires`. Values are provider-owned facts under `providers/`; there are no
capacity environment variables or separate model-capacity registry.

Named profiles exist because a provider can legitimately serve two models with
two engines at once - which is exactly the migration state this provider is in -
and a single provider-wide profile cannot describe that. Every profile declares
its own `engine`, and a profile's settings are validated against that engine's
own table, so a key belonging to one engine cannot silently name a different
setting on another. Unknown keys, unknown settings, missing profiles, artifacts
the selected engine cannot load, and ambiguous implicit defaults are all
refused. A provider declaring more than one profile declares **no** default: a
model that omits `[serving]` fails to parse rather than receiving whichever
profile sorted first.

| Table | Required fields | Meaning |
| --- | --- | --- |
| `execution.profiles.<id>` | `engine` | `llama_cpp` or `mlx_lm`; selects which settings table the rest of the profile is validated against |
| any profile | `context_granularity`, `max_sequences` | Positive integer alignment and engine capability bounds. Per-profile, not per-provider: these are statements about the engine, and the shipped `sequence_slots` is checked against the profile each model actually named |
| `llama_cpp` profile | `batch_tokens`, `microbatch_tokens` | Microbatch cannot exceed batch, and the sequence ceiling cannot exceed microbatch |
| `llama_cpp` profile | `cache_type_k`, `cache_type_v` | `"f16"` only; the adapter does not change cache precision automatically |
| `llama_cpp` profile | `flash_attention`, `load_mode`, `lazy_mode`, `gpu_layers` | Normalised to `"auto"` for the first three and `"all"` for GPU layers |
| `llama_cpp` profile | `kv_unified`, `cache_ram_mib`, `context_checkpoints` | Independent slots require `false`; extra RAM prompt cache and checkpoints require zero |
| `mlx_lm` profile | `kv_allocation` | `"growing"`: cache storage is committed as tokens are consumed, not at the native maximum |
| `mlx_lm` profile | `cache_type_k`, `cache_type_v` | `"f16"` or `"bf16"`, subject to native qualification; a quantised cache is not offered |
| `mlx_lm` profile | `prefill_chunk_tokens`, `prefill_chunk_candidates` | The selected chunk must be one of the declared candidates, so a tuned value cannot arrive without the benchmark that justified it |
| `mlx_lm` profile | `allocator_cache_bytes` | Free device buffers retained by the allocator. Zero is meaningful and declarable: it retains none |
| `mlx_lm` profile | `prefix_cache` | `"continuation_only"` or `"none"` |
| `execution.memory` | `strategy`, `target`, `distribution`, `unknown_measurements` | How deliberately-wired bytes are chosen. Bounds residency only; it is never an admission test and cannot resize a window or a slot count. `unknown_measurements = "unwired"` means an unavailable reading disables deliberate wiring rather than inventing a size |
| `capacity` | `strategy` | `"native_context"`: full model-native windows without available-memory fitting |
| `capacity` | `sequence_slots` | Explicit positive slot count, bounded by the `max_sequences` of the profile each model named |
| `capacity` | `validation_status`, `validation_evidence` | `"pending_native_validation"` by default or `"validated"`; validated status requires evidence references |

`models/<provider>/<model>/model.toml` at version 3 adds `[serving] profile` and
is otherwise unchanged; the artifact format it installs is declared by its
`weights.lock.json`, not by the model.

Adapters live in `tools/local_engines/` and are dispatched by the declared
`engine` kind, never by provider or model name. An adapter owns seven
operations - provision, identity, artifact validation, launch, status
normalisation, status verification and the compute canary - and owns no policy.
Unsupported backends fail explicitly. A status document is normalised per
engine: llama.cpp answers `/props` and is *not* asked to implement a harness
endpoint, while the harness-written MLX worker answers
`/v1/harness/status`. Both must satisfy one comparison, which is the part the
engine kind selects rather than the part the engine implements.

A managed provider may combine capacity with supported count, rate and context
rules. Version 2 currently rejects `max_context_tokens` and
`max_output_tokens_per_request`: fixed per-request caps cannot silently replace
the declared engine-derived envelope and remaining-context output mode.

### Resolution and launch

The provider explicitly declares `capacity.strategy = "native_context"` and a
`sequence_slots` count. Each engine receives its model's `context.native_tokens`
without shrinking or fitting. The window must be engine-aligned and exceed Kimi's
compaction reserve. Slot counts must fit the declared engine capability, not a
memory estimate. A model serving both roles is deduplicated; distinct models
remain separate processes. The generic remote fan-out fallback is never a local
slot count.

The picker retains backend/platform and quant-specific installed-RAM/disk
eligibility. Models below their declared static floors are filtered out, including
explicit overrides. Unknown installed RAM fails a nonzero RAM floor. Current
available-memory measurements and missing estimators do not disable an otherwise
eligible choice. `Not Installed`
models remain selectable; installation and digest verification complete after
selection and before launch. No unverified weights become `Ready`.

Version 4 engine records declare `capacity_strategy = "native_context"`,
`memory_management = "os_managed"`, `native_context_tokens`, `context_tokens`,
`sequences`, and `total_context_tokens = context_tokens × sequences`, together
with pinned model/build/profile identities and bindings. Version 4 adds the
engine-kind fields the adapter dispatch needs: `engine_kind`, `serving_profile`,
`protocol_version`, `artifact_format`, `cache_allocation`, `runtime_identity`,
the structured `identity` record, and the resolved `wiring` decision.
`build_commit` became optional rather than assumed, because a Python library
resolved from a lock reports a version and a source revision and has no build
number at all. Physical `resources`, projected `allocations`, headroom and
process-overhead admission settings are absent. Stale version 2 and version 3
local plans require a relaunch and are never silently reinterpreted; each is
refused with a message naming its own version. Version 1 provider-rule plans
retain their existing contract and are emitted byte for byte, which
`tools/capture_local_inference_references.py --check` enforces in CI against
complete captured plans and guidance blocks rather than against selected fields.

After static eligibility passes, available RAM, predicted allocations, RSS and
Metal's recommended working set do not gate resolution or startup. `host-caps.json` retains optional
observations for diagnostics; missing available-memory or RSS readings are not fatal. Offline
projection helpers remain available for explicitly requested diagnostics, but
launch does not require or call `llama-fit-params`.

Serving preserves FP16 cache, native context, sampling defaults, independent slots,
zero extra RAM prompt cache and zero context checkpoints. Automatic fitting is
explicitly off; loading remains automatic memory mapping, without memory locking,
OS tuning, CPU fallback, YaRN or automatic precision changes. Paging and allocation
are decided by the OS/backend; removing harness limits cannot make every Metal
allocation pageable or guarantee its success. In-slot reuse remains available;
a changed prefix may require additional prompt processing.

The supervised host engine authenticates readiness and verifies model/build,
per-sequence context, slot count and normalized settings before exposing Kimi.
Memory observations in readiness records do not establish or enforce a RAM bound.
An actual allocation/startup failure stops that launch and is logged. It never
silently changes model, precision or context. Runtime engine failure makes its
routes unavailable; comprehensive OOM recovery and automatic restarts are deferred.

### Request admission

Local `primary` and `subagent` retain separate routes, aliases and reasoning
defaults while binding to their engine's fixed window. `agent_lane` remains a
launch-time designation, and `secondary_model.force = true` remains mandatory.
Each engine contributes an ordinary count counter named
`count:engine:<qualified model id>`, with `scope = "engine"` and
`source = "engine_capacity"`. That identity is separate from provider fair-use
counters. Requests acquire one full slot through the existing execution gates;
excess requests queue with queued-main priority. A shared engine permits children
to use all its slots while the main agent waits.

Preparation is serialized per engine outside execution permits. After model
binding and existing body transformations, the proxy sends the final payload,
including tools, thinking settings and chat-template framing, to the authenticated
`/v1/chat/completions/input_tokens` endpoint. It retains body limits, cancellation
and bounded queues. Unavailable or malformed counts fail closed, without a
bytes-per-token fallback. A prompt at or beyond the hard context boundary is
rejected with a context diagnostic; otherwise the output allowance is the actual
remaining context, bounded by any explicit client maximum. Kimi's earlier
compaction target does not shorten this hard boundary.

Execution permits are released on completion, cancellation and failure. Retry
backoff and rate waits hold no execution permits. Engine failure makes its local
routes unavailable without resizing ongoing conversations or changing independent
NRP enforcement. NRP retains its existing token estimation, guards, reservations,
rate booking, output clamps, exclusivity and aggregate-budget semantics in both
mixed-provider directions.

### Readiness is computation, and completion is not success

Metadata readiness was the original defect: a backend whose Metal allocations had
failed kept answering `/props`, so the launcher published a receipt, Kimi staged
against it, and every later request failed while the session retried forever. Two
rules replace that.

An engine publishes readiness only after an authenticated generation has produced
content. A model declaring `tool_use` must additionally return a tool call for a
forced one, which is what makes the declared capability a checked property rather
than a claim: a server that accepts a `tools` array and parses with its built-in
template answers with prose, and at the transport layer that is a perfectly
successful completion. The canary asserts no specific answer string, because
proving compute works and proving a quant reproduces one particular completion are
different claims and only the first is being made.

`proxy/local_engine_contract.py` defines one vocabulary for what a completion
amounted to — content, tool-only, reasoning-only, truncated, empty, error-object,
framing-failure, unknown — and it is pure standard library because the launcher,
the proxy and the engine worker all read it while only the proxy may have aiohttp.
Authority is split rather than given to one observer, because the recorded failure
was one observer trusted about a signal it could not see. The proxy decides whether
the client received a usable completion; the engine decides whether it is fatally
broken, and only the engine may clear that. A healthy metadata read never resets a
latched failure. Where the two disagree the request is not usable and the engine is
not latched either: two reporters contradicting each other is a contract defect, not
evidence about a device.

A latched engine is refused before the wire, so no further request can pay for a
prefill that cannot complete, and the terminal refusal carries no `Retry-After` —
withholding the retry hint is the one lever available against a client configured
to retry indefinitely. Two bounds exist for a local lane that neither a latch nor a
status code can reach: a progress lease measured on generated content rather than
bytes on the wire (a keepalive is not progress), and a dispatch attempt count. Both
default to `0`, which preserves the previous unbounded behaviour, because no
measurement of worst-case native-context prefill on the affected host exists yet.
They are armed from `tools/local_native_probe.py` and `tools/local_qualification.py`
evidence, never from a guess, and a too-tight value would turn the slowest and
largest requests into failures.

Whether Kimi's own retry loop terminates on that refusal is unresolved (U1) and is
an actual-client measurement, not something this document claims.

### Capacity diagnostics and qualification

The recap and health information report native context, explicit execution slots,
OS-managed memory, pinned identity and validation status. Request-ID logs identify
queue and rejection reasons. The single resolved plan remains authoritative;
there is no separate memory budget, persistent calibration store or policy env var.
Readiness and optional diagnostic records remain instance-scoped.

The local declarations remain `pending_native_validation`. Fixtures do not prove
native generation correctness, paging performance or success beyond physical RAM.
Native evidence may record peak RSS and shutdown allocations; these are observations,
not admission limits, and no process-overhead allowance is calibrated or enforced.

### Operator native evidence command

`tools/local_capacity_smoke.py` collects synthetic evidence from an existing
resolved instance. Run it on the host after a normal launch with Kimi idle; the
test deliberately uses every planned slot on each selected local engine. It
checks the authenticated model/build/context properties and readiness fingerprint,
and verifies that the sampled PID is still the recorded supervisor's child. It
does not install, start, stop or reconfigure a model, and does not change the plan.

```sh
python3 tools/local_capacity_smoke.py run \
  --runtime /path/to/harness/.local/runtime/INSTANCE
```

The tool builds a tokenizer-measured near-window prompt containing tools, Unicode,
code and thinking settings, observes simultaneous slot occupancy through `/slots`,
and makes a preparation call while all those executions remain occupied. It
requires the requested synthetic generation workload to finish and samples only
that owned engine's RSS. An unavailable count, missed occupancy overlap, identity
drift, incomplete generation or failed measurement fails the evidence run. The
CLI output budget and timeout control only the synthetic workload. A larger
output budget may be needed when generation finishes before overlap is observed.

Results are private files under the instance's `capacity-smoke/` directory. They
contain normalized engine, profile and weight-lock identity, usage
counts and measured RSS. They contain no credential,
synthetic request body or generated response text. Failed attempts also retain
a partial record. Distinct engines are stressed sequentially with selected
weights resident; this does not establish a synchronized execution peak across
different engine processes.

After the operator stops the stack through its normal lifecycle, isolate exactly
one `fit_memory_breakdown` JSONL shutdown event for the recorded engine into a file
under the same instance directory. Then attach the allocation observation to that engine's evidence record:

```sh
python3 tools/local_capacity_smoke.py compare-shutdown \
  --runtime /path/to/harness/.local/runtime/INSTANCE \
  --record smoke-RECORD_ID.json --engine provider/model \
  --event /path/to/harness/.local/runtime/INSTANCE/isolated-shutdown.jsonl
```

The comparison records rounded core host/device allocations and the event digest.
Larger allocations do not fail an evidence run or create a RAM admission limit.
A combined log does not prove which engine emitted an unlabelled event: this
step records the attribution as operator-provided and requires one isolated
event. The tool always leaves `native_qualification_complete = false`, including
after a passing comparison. Both pinned quants, the appropriate multi-engine and
maximum-context workloads and shutdown attribution still
require review before declaring the provider profile validated. It never writes
validation status back into a manifest.

### Weights, engine, and `Ready?`

Local engines run on the host because Docker's macOS VM cannot address Metal or
the host's unified memory. Each model has its own origin and port. The listener
must be reachable from the VM, so it is authenticated with a generated session
token shared only with the proxy; `LOCAL_MPS_BIND` can narrow its host interface.
The agent container still has one model egress through `model-proxy`. Prompt
archiving, failure diagnostics and the staged-configuration drift check continue
to apply to local traffic.

**`weights.lock.json`.** One tracked file per local model, beside its manifest:

```json
{
  "schema_version": 1,
  "source": {
    "hub": "huggingface",
    "repo": "unsloth/Qwen3.8-27B-GGUF",
    "revision": "<40-hex commit>",
    "upstream": "Qwen/Qwen3.8-27B",
    "license": "apache-2.0",
    "base": "https://mirror.example"
  },
  "files": [{"path": "one-file.gguf", "sha256": "<64-hex>", "size": 17559178144}]
}
```

Schema 2 exists for artifacts that are a directory rather than a file. It adds one
required `artifact` block and changes nothing else:

```json
{
  "schema_version": 2,
  "artifact": {
    "format": "mlx",
    "variant": "4bit",
    "directory": "mlx-4bit"
  },
  "source": {"repo": "…", "revision": "<40-hex commit>"},
  "files": [
    {"path": "mlx-4bit/config.json", "sha256": "<64-hex>", "size": 1200},
    {"path": "mlx-4bit/model.safetensors.index.json", "sha256": "<64-hex>", "size": 40000},
    {"path": "mlx-4bit/model-00001-of-00003.safetensors", "sha256": "<64-hex>", "size": 4990000000}
  ]
}
```

`artifact.format` is `gguf` or `mlx` and `artifact.variant` names the quantised
build. The block is required, not optional, because the two formats cannot be told
apart from their filenames and the loader that must be used is not otherwise a
derivable fact: an MLX shard is named `model-00001-of-00003.safetensors` and
carries no quant label at all, so the version-1 rule that matched the variant name
inside a path would reject a correctly pinned artifact. `artifact.directory` keeps
a multi-file artifact beneath `weights/` without colliding with a retained GGUF,
and is validated against traversal.

Schema-1 locks keep their exact meaning. An MLX lock pins **every file the load
consumes** — configuration, tokenizer sources, chat template, the weight index and
each shard it names — because a valid shard proves nothing about a config file that
drifted beside it. The index's `weight_map` is checked against the pinned set, so a
shard the lock does not describe is refused rather than discovered by a loader that
would happily read it. Tensor-storage totals come from reading verified shard
headers, never from a declared byte count; they are what the bounded-wiring split is
proportional to, so an invented weight size would invent a residency budget too.

HuggingFace is the source of record and the revision is a commit, never a branch:
`main` would let a publisher change a hundred gigabytes underneath a definition
that claims to describe it, and only the digest would notice - after the
download. `base` is an optional mirror of the same layout; the digest decides
whether a mirror is trusted, and the hub token is never sent to one. The weights
themselves are never versioned: they land in `models/<provider>/<id>/weights/`,
which is git-ignored and `.dockerignore`d, so "delete the model directory" removes
the download with it and `git status` never sees either.

The download is plain HTTPS through the standard library rather than
`huggingface_hub`: that package installs dependencies at launch, follows mutable
refs, and builds its cache from symlinks - which `definitions._walk_safe` refuses
anywhere in the definition tree. The weights subtree is exempt from that walk
because its contents belong to whoever published the checkpoint, but it is exempt
from nothing else: only `tools/local_llm.py` opens it, and it digests every file.

**`Ready?` is a claim about verification.** `weights/install.json` records the
commit, the variant, and the digests that were matched, and it is the only thing
that makes a model report `Ready`. Correctly-sized files with no marker are
reported `Not Installed` until something has read them once - which
`install` does in place, without refetching, so hand-copied weights cost a hash
and not a transfer. The picker's column reads the verification marker and file
sizes; it does not hash the entire checkpoint again for each screen.

**The engine** is a pinned host binary in `dependencies.lock.json` under
`downloads`, fetched from the official release and verified against the digest
the way the Kimi bundle is - the same `sha256:` check
`scripts/select_versions.py` applies - then unpacked under
`.local/runtimes/llama-cpp/<version>/`, outside the project tree.
`LOCAL_MPS_LLAMA_SERVER` points at a build of the operator's and skips the fetch.
Extraction refuses any member that would write outside its directory, and keeps
the versioned dylib symlinks a real macOS release is built from.

**Lifecycle.** After model and module selection, the launcher waits for selected
weights to be installed and verified, then resolves final capacity. Selected
engines must pass authenticated startup validation before Kimi becomes available.
Installation failure, interruption or startup failure leaves no partially ready
local route exposed. The launcher tracks each host process and stops it on
shutdown. A selected model never becomes available through a mid-session plan
rewrite.

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
   declares it. Dropping a lane drops its route, its alias, and its counters. A
   lane's `base_url` is the provider's origin with the model's `[install].port`
   substituted where one is declared - which is what lets two local models be two
   processes - and the proxy checks the provider's origin and the lane's
   separately, because a plan whose halves disagree is drift even when one half
   would still route.
   The selection also designates which of those lanes the *main agent* launches
   on (`agent_lane`, one of `primary` or `long`, defaulting to `long`), which
   decides only the alias rendered into Kimi's `default_model`; designating a
   lane the selected model does not declare is a refusal, not a fallback,
   because a launch that quietly got the smaller window would believe it had the
   larger one. What the launcher *does* clamp is a designation nobody asked for:
   `policy.designated_lane` settles an `agent_lane` a different machine recorded
   onto a model that has no such lane, which is a plan being carried between
   hosts rather than an operator's request. Every route stays open either way, so Kimi's own model picker can
   still move the session between them mid-flight. The two agent lanes are peers
   rather than tiers, which is why `tools/policy.py` keeps one label map —
   `LANE_DISPLAY` — and every human-facing surface reads its entry for the lane
   in play: the picker rows `tools/models.py` builds, the `display_name` in each
   generated `[models.*]` table, the `--resolve` summary, and the lane table in
   the generated workspace guidance. Labels are the only thing that map touches;
   the ids stay `primary`/`long`/`subagent` because they name the route, the
   alias, and the provider-scoped policy behind them.
2. **Fixed lane sizing.** For version 1, per-request caps shrink the declared window and clamp;
   `input_tokens` is then `min(declared, context - clamp)`. Kimi's
   `loop_control.reserved_context_size` (read from `runtime/config.toml`,
   because it describes how Kimi paces its own compaction rather than what the
   model is) must fit inside every lane or resolution fails. Managed lanes
   instead use the already-resolved engine window and remaining-context output
   mode described under managed execution above.
3. **Counters.** One context counter per (provider, model) pair, plus rate and
   count counters per admissible scope, each keyed by the scope table above and
   carrying the list of lanes bound to it. A model serving itself with its own
   key contributes its synthesised credential id to those subjects, so a
   credential-scoped rule follows the key actually in use rather than the
   provider that issued it. A model-scoped concurrency limit is
   folded into that model's context counter rather than duplicated, since both
   describe the same set of in-flight requests. Each managed engine adds its own
   engine-scoped count counter for declared execution slots.
4. **Exclusivity.** A lane is exclusive when its in-flight cost
   (`input + clamp`) reaches the tightest `exclusive_above_context_fraction`
   threshold bound to it.
5. **Feasibility.** A non-exclusive lane whose reservation exceeds its budget
   could never be admitted, so that is a hard error naming the fix. An
   exclusive lane may exceed the budget, because the provider itself permits
   exactly that request to run alone; the proxy admits it only when the counter
   is empty.
6. **Derived limits.** For fixed provider envelopes, `subagent_concurrency` is the largest fan-out the rules
   allow: `1` when the subagent lane is exclusive, otherwise the minimum of the
   count ceilings and every `budget ÷ reservation` quotient. When the selected
   provider states no ceiling at all, the harness publishes its own default of
   8 and labels it as such — an unconstrained provider is a reason to use the
   allowance, not a reason to stall the workspace on 1. For managed children it
   comes from their selected engine's resolved slots, further constrained by any
   applicable provider rules. The main model's provider does not determine child
   concurrency.

The structural cases a selection can fall into:

- **One fixed-envelope model in both lanes** — one `model`-scoped counter, so primary and
  subagent traffic contend for the same permits and the aggregate context
  budget spans both lanes. Fan-out is `budget ÷ subagent reservation`.
- **Two models, one provider** — two model-scoped counters; only
  `credential`, `credential_model` and `provider` rules are shared, so a
  per-key rate limit still couples the lanes while context does not.
- **Two models, two providers** — disjoint key sets. Each provider's rules are
  enforced only against its own traffic, and the derived subagent fan-out comes
  from the subagent's provider alone.
- **One managed model in both lanes** — one engine allocation and one
  engine-capacity counter, shared by primary and subagent requests.
- **Distinct managed engines** — separate processes with full native context and explicit
  engine-scoped slots; no joint memory-admission check.

### Greedy, not conservative

Fixed-envelope limits are the largest the provider's rules allow after the declared
margin. Managed execution uses full native context and declared slots, independently of
physical-memory measurements. In either case,
`./start.sh` also derives Kimi's subagent fan-out from the same plan the proxy
enforces — Kimi fills the envelope exactly instead of hoping two hand-written
numbers agree. `tools/prompt_context.py enabled_guidance()` turns the plan into
audience-scoped blocks - a table of lanes, budgets and ceilings, and an explicit instruction to
run the full allowed number of subagents - and the composed text is what the launcher appends to
the session's system prompt. Blocks are rendered one at a time rather than all at once so that a
block the operator switched off costs nothing and can be measured on its own.
`tools/policy.py render_guidance()` renders the same blocks for a whole audience in one call;
nothing in the launch path uses it, and `tests/test_prompt_context.py` asserts the composed
additions are byte-identical to it, which is what keeps the two from drifting.

## Artifacts and lifetimes

Generated launch artifacts live under the harness root's
`.local/runtime/<instance>/`, never the selected workspace. Private files are
mode 0600. The session sweep removes plans, rendered credentials and readiness
records at shutdown and before the next launch; logs and remembered selections
have their own longer lifetimes.

| File | Written by | Read by | Contents |
| --- | --- | --- | --- |
| `model-selection.json` | `models.py select` | `models.py resolve` | `{"primary": id, "subagent": id, "agent_lane": "primary"\|"long"}` — the two lanes the user answered, and the window the primary screen's row carried |
| `last-model-selection.json` | `start.sh` after a successful launch | `models.py select` | Prior selection, shown as `(last used)` and pre-selected, its `agent_lane` deciding which of a model's two rows was the answer |
| `model-policy.json` | `models.py resolve` | proxy, `render_runtime.py`, `check_services.py`, `local_llm.py serve` | The authoritative plan: lanes, counters, limits, providers, selection and agent lane; version 3 also includes native context, execution slots, OS-managed memory and engine identity |
| `model.env` | `models.py resolve` | `start.sh`, then Compose interpolation | `KIMI_SUBAGENT_CONCURRENCY`, `KIMI_BACKGROUND_TASK_SLOTS` — only the values `compose.yaml` interpolates; lane aliases reach Kimi through the rendered config |
| `credentials/<provider>__<credential>` | `render_runtime.py` | proxy (via Docker secret) | One key value each, where a model-scoped credential id is the synthesised `<credential>__<slug>`; stale files from deselected models are removed |
| `compose/models.json` | `models.py resolve` | Compose (`-f`, appended by `tools/runtime.sh`) | Secrets declaration + the proxy service's secret list |
| `host-caps.json` | `host_caps.py summary`, refreshed by `capacity.prepare` after installation | picker, resolver, launch notes | Platform, disk and accelerator eligibility; optional memory observations do not determine native context or admission |
| `capacity-projections.jsonl` | optional projection diagnostics | the operator | Optional normalized host/device estimates; never required by launch or used for admission |
| `local-engines/<provider>__<model>.json` | supervised `local_llm.py serve` | launcher readiness check | Owned process identities bound to the plan fingerprint and resource snapshot; removed on exit and swept as session state |
| `local-model.log` | `models.py install`, `local_llm.py serve` | the operator | Installation and structured engine output; retained for diagnosing a failed launch |
| `capacity-smoke/smoke-<id>.json` | operator-invoked `local_capacity_smoke.py` | the operator | Synthetic native workload evidence and optional shutdown comparison; retained instance-scoped, with qualification explicitly pending review |

Two model-installation artifacts survive a launch, because deleting either would throw away work
that took hours rather than a session: the weights themselves, at
`models/<provider>/<id>/weights/`, and `weights/install.json` beside them
recording which digests were matched. Both are git-ignored and
`.dockerignore`d, and both are removed by deleting the model's own directory.
`credentials/local_mps__default` does *not* survive, by design: it is the session
token a locally-started engine was given, and a new launch starts a new engine.

The plan is independently versioned; the proxy supports the existing version 1
and native-context version 3 contracts, and refuses unknown versions. Only lanes present in the plan get routes,
so a model without a `long` lane 404s on `/long/v1/*` rather than serving it.

`compose.bootstrap.yaml` must declare every `.env` name the selected definitions
reference — each `credential.env`, each `model.key_env`, each
`endpoint.base_url_env`, and the harness's own `KIMI_BACKGROUND_TASK_SLOTS`.
`key_env` is declared even when the provider-scoped key won, because the name is
a property of the selected model and Compose silently drops undeclared names.
`models.py resolve` checks this because Compose interpolates only names a file
declares, and a missing declaration looks exactly like an operator who left the
key blank.

## How the envelope reaches a prompt

The generated envelope never enters the workspace. `tools/models.py resolve`
writes the plan, and the two prompt documents are composed from it:
`CONTEXT.md` (falling back to `runtime/AGENTS.md`) becomes the all-lane
`AGENTS.md`, and `SYSTEM.md` becomes the main agent's system prompt.
`tools/render_runtime.py` does the composing, in a fixed order per audience:
the document's own text, then the lane-audience blocks for the contract
(usage limits, then module guidance) and the main-audience blocks for the
prompt (the per-model lane table, then parallelism guidance).

Nothing writes into `<workspace>/AGENTS.md`. That file belongs to the project Kimi
is working on, and an operator is entitled to find the harness left it untouched —
which is also why module guidance is staged in the instance runtime directory as
`module-guidance.md` rather than merged into it. Both composed documents are
re-stamped at every launch and neither carries a credential, so neither is session
material that the launcher has to delete on exit.

Which blocks are composed in is a launch-panel choice, recorded in
`prompt-context.json` in the instance runtime directory and changeable with
`./prompts.sh --configure --enable ID --disable ID`. No `.env` variable governs it, and the earlier
`KIMI_SYSTEM_PROMPT_OMIT_ENVELOPE` is retired. Switching the blocks off changes no
limit: `model-proxy` reads the plan from `model-policy.json` and enforces it
whether or not any prompt says so. Guidance text goes through Kimi's template
substitution like the rest of the prompt, which is why every generated block is
written without a single `${...}`.

Each document resolves in two tiers: the operator's file if it exists, otherwise
the harness's default. `SYSTEM.md.example` and `CONTEXT.md.example` document those
conventions and are in no fallback chain. Kimi Code discards a system prompt that
is blank once trimmed and substitutes its own built-in prompt, so an absent
`SYSTEM.md` composes as a `${base_prompt}` wrapper when any add-on is enabled and
stages nothing at all when none is — that is the only route to Kimi's own prompt. A
present-but-empty `SYSTEM.md` is honoured as a decision instead: its add-ons become
the whole prompt, and with every add-on off it stages a lone period so that "no
prompt" survives upstream.

## Adding a model

1. `cp -r models/nrp/qwen3_8_flash_next models/nrp/new_thing` — under the
   provider directory that serves it. Serving one model from a second provider is
   a second directory with the same leaf name, not an edit to the first.
2. Set `label`, `model` (wire name), a unique `slug`, the `provider` and
   `credential` ids, `[context].advertised_tokens`, and the lanes you want.
   `provider` must equal the directory it sits in.
3. Optionally set `key_env` to a variable name of your own — that is what lets
   this model carry its own key in `.env` instead of sharing the one its
   `credential` names. Whatever you choose, add that name to
   `compose.bootstrap.yaml`, alongside the names your provider contributes.
4. `./start.sh` — the new label appears alphabetically in both pickers.

Delete the directory to remove it. Nothing else references model ids.

A **managed local** model uses the version 2 envelope and takes two more steps. Write its `weights.lock.json` with one
pinned commit and the SHA-256 of every file - read them from the hub's own API
(`paths-info` returns the LFS `oid`, which is the file's SHA-256) rather than
guessing, and let `python3 scripts/check_locks.py` refuse the shape if it is
wrong. Declare quant-specific installed-RAM/disk eligibility and backend support.
These static floors do not imply that native context fits available RAM.
`[install].port` must be unique within the provider.

## Adding a provider

1. Create `providers/acme/provider.toml` with `[endpoint]`, one or more
   `[[credential]]` tables, and the `[[rule]]` set you can actually enforce.
2. Add every `credential.env` / `base_url_env` name to
   `compose.bootstrap.yaml`'s bootstrap environment.
3. Point a model at it with `provider = "acme"`, in `models/acme/`.

A model whose provider directory is missing is dropped from both pickers rather
than failing startup, which is how you park a model while its provider is being
rewritten. Removing a provider directory therefore removes its models in one
step - and it is the same mechanism that removes them on a host that cannot
satisfy the provider's `requires`, which is why a local provider needs no
hiding rule of its own.

A managed provider whose models run on the operator's machine uses version 2,
names a capability in `requires`, sets `sends_cache_salt = false`, and gives its
credential `generate = true`. It declares an engine profile, capacity strategy
and explicit provider-rule list. The engine is pinned by a `downloads` entry in
`dependencies.lock.json`; `tools/host_caps.py` measures platform resources, and
the engine adapter verifies authenticated model/build/context readiness.

## The launcher's model screen

Both model questions are a table, because the same model name now appears once
per provider and a label cannot say which is which. The columns are Model,
Provider, Lane, then two unlabelled state columns - `Ready` or `Not Installed`,
and `(last used)` / `(default)` where a row carries a mark - which need no header
because their content already reads as itself. Navigation is unchanged
(arrows, space to select, enter to confirm): `tools/tui/menu.py` grew the columns
beside the existing single-choice list rather than replacing it, so the module and
version menus render exactly as before.

Each row is written twice, as cells and as the older label-plus-hint pair, and a
window too narrow to align every column gets the older rendering rather than a
grid with truncated model names. Below the list the status line re-joins the
marked row's cells into one sentence, and repeats its hint only when the grid is
not being drawn - or when the row cannot be selected, where the hint is the reason
and the grid has no column for it.

Installation readiness comes from `tools/local_llm.py` and retains digest-based
verification. A model with no `[install]` block requires no local installation.
`Not Installed` local choices proceed through the verified installer before serving.
The picker and read-only forecasts do not estimate memory fit. The resolved recap
reports the declared native window and slot count, without a physical-fit promise.

## Kimi's model picker

`render_runtime.py` generates `[providers.*]`, `[models.*]` and
`[secondary_model]` from the plan, so the harness sets the default model and the
subagent model automatically. Each `[models.*]` table carries a `display_name`
built from the model's label and its lane description. Fixed envelopes retain
the Long/queued, Medium/concurrent and Subagent/automatic descriptions. Managed
local capacity is described by its fixed engine window and slots. A provider
name is added only when a plan serves one model label through two providers:
every lane carrying that label gains its provider in brackets, because otherwise
identical names would require guessing. One label across two lanes
of the *same* provider is not that case — those rows already differ by their lane
phrase — so an ordinary launch reads exactly as it did. Each table still binds
its own provider with a `provider =` key beside
`model =`, so the row names the lane while the table says who serves it.

**The subagent row cannot be hidden, and is labelled instead.** Kimi Code's
model list is every configured alias: in the shipped bundle,
`pickerModelsForHost()` (byte `142070433`) filters exactly one entry out —
`SECONDARY_DERIVED_MODEL_ALIAS`, the `__secondary__` value Kimi synthesises
itself from the `[secondary_model]` recipe — and offers the rest, with no
`hidden` flag on a model table to honour. The `qwen3-subagent` alias has to
exist for `secondary_model.force = true` to bind children to it, so hiding it
from `/model` would mean deleting the lane that makes the binding work.
Renaming the alias to `__secondary__` is worse, not better: Kimi strips that
name from the models view, so the forced binding could fall back to the main
model and every child would silently inherit the main window. The mitigations
are therefore that `secondary_model.force = true` keeps children on the
subagent lane whatever the picker offers, `default_model` is not one of the
user-owned keys the initializer lets an in-session write keep, the label says
"(automatic)", and the proxy re-reads the configuration the launcher staged at
every policy refresh and fails closed if a lane drifts from the plan.

Two names are therefore unavailable as aliases, both discovered in the same
bundle: `__secondary__`, which Kimi filters as above, and `primary`, which
`/secondary-model` refuses because it reserves that word for "bind the caller's
own model". A model slug that produced either would break the picker rather
than the resolver, so this harness keeps lane names in ids and personality in
`display_name`.

What that last check compares against is worth stating precisely, because it
bounds what the harness can possibly enforce. The file it reads is the rendered
snapshot under `/policy/`, which is read-only and outside Kimi's own home;
`/home/agent/.kimi-code/config.toml`, where an in-session `/model` writes, is
never opened. A model switch is therefore not policed by name — and could not
be, since the proxy has no view of it — but it is policed by *price*: whichever
alias the request arrives under, the proxy resolves that lane's own reservation.
For fixed provider envelopes it re-prices the permit from the request's estimated
size; managed routes instead enforce exact engine token counts and acquire a full
slot. A session that switches aliases is still bounded by the chosen route's
policy and fixed engine allocation. The
converse is also deliberate: the designations cannot be *enforced* to agree, so
`agent_lane` is a launch-time statement of which window the session opened in,
which is why the generated envelope publishes it — an agent cannot otherwise tell
which lane it is being metered on.
