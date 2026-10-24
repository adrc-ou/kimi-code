# Model and provider definition contract (schema version 1)

Two operator-owned directories hold every fact the harness needs about *what
model serves traffic* and *what the provider permits*. Nothing in `.env`
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
ways. `Qwen3.8-Flash-Next` from NRP is a 1M-token window metered against
published fair-use terms; the same model from `local_mps` is a 128K slice of the
operator's own memory with a 111 GB download in front of it. Those are two
definitions, not one file with a switch in it, and the model's identifier is the
qualified `local_mps/qwen3_8_flash_next` so a selection document can say which
serving it recorded. The directory half of that name is also what makes removal
atomic: dropping a provider directory, or a host that cannot satisfy its
`requires`, retires every model it served in one step.

A `model.toml` one level too shallow is refused with the move to make, rather
than ignored: a discovery rule that quietly skipped an operator's copied
directory would look like a model that had disappeared.

Definitions are host-trusted operator configuration, **not a sandbox**. They are
still read strictly, because these files decide which endpoint receives an API
key: a manifest that is a symlink, a definition directory that is a symlink, and
any non-regular file inside either tree are refused outright.

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
`subagent`. All three lane numbers are positive integers, and a model must
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

`label`, `[endpoint]`, at least one `[[credential]]` and at least one `[[rule]]`
are required. An empty `[[rule]]` list would mean "this provider has no terms",
which is a claim worth refusing rather than assuming.

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

**Safety margins** are the only tunables a provider file carries, and they exist
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

## Local serving: weights, engine, and `Ready?`

A provider that names a host capability is served by a process on that machine,
and that adds three things the resolver and the proxy do not otherwise have to
know about: weights that must be fetched and verified, an engine that must be
running before a lane answers, and a per-lane origin, because one engine process
holds one model.

**Nothing about the request path changes.** The proxy already takes its upstream
URL from the plan per lane and speaks one wire format, so a local model is a new
`base_url`, not a new code path. Prompt archiving, failure dumps, lane policy and
the config-drift check apply to local traffic exactly as they do to cloud traffic,
and the agent container still has exactly one model egress. The engine therefore
has to be an OpenAI-compatible chat-completions server - which `llama-server`,
Ollama, LM Studio, vLLM and SGLang all are - and `local_mps` and a future
`local_cuda` differ in their engine, not in their contract.

Image eviction is the one policy knob whose *reason* is cloud-shaped: its
documented motives are staying inside what a metered gateway accepts, keeping a
shared cache prefix stable, and not paying tokens for pixels twice. None of those
apply to a loopback server, and eviction remains off unless the operator turns it
on - where it still helps, for a different reason, because a re-sent image is KV
cache the host cannot use for anything else.

**A local model declares one agent lane, and that is deliberate.** The `primary`
and `long` pair exists on a metered provider because the two windows trade context
against in-flight overlap: the narrow reservation is what leaves the provider's
shared pool room for other requests. Locally the pool is the operator's memory, the
provider's own rule allows one or two concurrent requests, and there is nothing left
to trade — so the honest shape is the widest window the smallest eligible host can
hold, declared once as `primary`. A model with a single agent lane offers one row on
the main screen and launches on it, which is the case this document already
documents for a model that omits `[lane.long]`. Adding a `long` lane to a local
definition would not widen anything; it would put a second, larger KV reservation in
front of the same memory and ask the operator to choose between two promises the
host cannot both keep.

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
and not a transfer. The digest is never recomputed at launch: the picker's column
is marker plus sizes, because hashing 111 GB per screen would be the last thing a
launch did.

**The engine** is a pinned host binary in `dependencies.lock.json` under
`downloads`, fetched from the official release and verified against the digest
the way the Kimi bundle is - the same `sha256:` check
`scripts/select_versions.py` applies - then unpacked under
`.local/runtimes/llama-cpp/<version>/`, outside the project tree.
`LOCAL_MPS_LLAMA_SERVER` points at a build of the operator's and skips the fetch.
Extraction refuses any member that would write outside its directory, and keeps
the versioned dylib symlinks a real macOS release is built from.

**Lifecycle, and why nothing blocks the operator.** The launcher measures the host
before the questions, so the picker shows only what this machine can run. After
`./start.sh` commits the answers, a selected model that is not installed is
provisioned in the background and its lane goes live mid-session - the questions
are never waiting on a download. A model that *is* installed has its engine
started before `docker compose up`, so the first request finds a server. Each is
a host process tracked so shutdown stops it: a model server cannot run in a
container on macOS, because Docker's VM has no access to the Metal device or to
unified memory, which is the same reason the ComfyUI MPS backend runs on the
host. That listener is reachable from the VM (loopback is not - the gateway
address arrives at the Mac on another interface), so it is bound broadly and
authenticated by the session token; `LOCAL_MPS_BIND` narrows it.

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
numbers agree. `tools/prompt_context.py enabled_guidance()` turns the plan into
audience-scoped blocks - a table of lanes, budgets and ceilings, and an explicit instruction to
run the full allowed number of subagents - and the composed text is what the launcher appends to
the session's system prompt. Blocks are rendered one at a time rather than all at once so that a
block the operator switched off costs nothing and can be measured on its own.
`tools/policy.py render_guidance()` renders the same blocks for a whole audience in one call;
nothing in the launch path uses it, and `tests/test_prompt_context.py` asserts the composed
additions are byte-identical to it, which is what keeps the two from drifting.

## Artifacts and lifetimes

All generated files live under `.local/runtime/<instance>/` (mode 0600, created
atomically) and are deleted when the launcher exits:

| File | Written by | Read by | Contents |
| --- | --- | --- | --- |
| `model-selection.json` | `models.py select` | `models.py resolve` | `{"primary": id, "subagent": id, "agent_lane": "primary"\|"long"}` — the two lanes the user answered, and the window the primary screen's row carried |
| `last-model-selection.json` | `start.sh` after a successful launch | `models.py select` | Prior selection, shown as `(last used)` and pre-selected, its `agent_lane` deciding which of a model's two rows was the answer |
| `model-policy.json` | `models.py resolve` | proxy, `render_runtime.py`, `check_services.py` | The plan: lanes, counters, limits, providers, selection, designated agent lane |
| `model.env` | `models.py resolve` | `start.sh`, then Compose interpolation | `KIMI_SUBAGENT_CONCURRENCY`, `KIMI_BACKGROUND_TASK_SLOTS` — only the values `compose.yaml` interpolates; lane aliases reach Kimi through the rendered config |
| `credentials/<provider>__<credential>` | `render_runtime.py` | proxy (via Docker secret) | One key value each, where a model-scoped credential id is the synthesised `<credential>__<slug>`; stale files from deselected models are removed |
| `compose/models.json` | `models.py resolve` | Compose (`-f`, appended by `tools/runtime.sh`) | Secrets declaration + the proxy service's secret list |
| `host-caps.json` | `host_caps.py summary`, from `start.sh` | `host_caps.caps()` via `models.py` and `flow_survey.py` | This machine's platform, memory, free disk and accelerator capabilities; the one probe the picker, the resolver and the launch notes all read |
| `local-model.log` | `local_llm.py serve` / `provision` | the operator | One line per download and the engine's own output. Deliberately not deleted with the rest: it is where a failed 111 GB fetch is explained after the session that started it has ended |

Two artifacts survive a launch, because deleting either would throw away work
that took hours rather than a session: the weights themselves, at
`models/<provider>/<id>/weights/`, and `weights/install.json` beside them
recording which digests were matched. Both are git-ignored and
`.dockerignore`d, and both are removed by deleting the model's own directory.
`credentials/local_mps__default` does *not* survive, by design: it is the session
token a locally-started engine was given, and a new launch starts a new engine.

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

A **local** model takes two more steps. Write its `weights.lock.json` with one
pinned commit and the SHA-256 of every file - read them from the hub's own API
(`paths-info` returns the LFS `oid`, which is the file's SHA-256) rather than
guessing, and let `python3 scripts/check_locks.py` refuse the shape if it is
wrong. Then declare `[requires]` from the arithmetic, not from the hardware in
front of you: the memory floor is the pinned quant's resident size plus KV for
the widest lane plus what the harness's own container VM takes from the same
pool, and a floor that has been lowered to fit a machine is a promise the engine
will break at first use. `[install].port` must be unique within the provider.

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

A provider whose models run on the operator's machine additionally names a
capability in `requires`, sets `sends_cache_salt = false`, gives its credential
`generate = true`, and publishes real capacity rules (`max_concurrent_requests`
per model, plus a provider default for the models nobody transcribed yet). The
engine it needs is a `downloads` entry in `dependencies.lock.json`, and
`tools/host_caps.py` is where a new capability gets its measurement.

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

`Ready?` is a fact about the host, not a guess: it comes from
`tools/local_llm.py`, and a model with no `[install]` block is always ready
because no code here installs anything for it. The column is what the automatic
download is offered against - selecting a `Not Installed` model proceeds, and
provisioning happens after the questions end.

## Kimi's model picker

`render_runtime.py` generates `[providers.*]`, `[models.*]` and
`[secondary_model]` from the plan, so the harness sets the default model and the
subagent model automatically. Each `[models.*]` table carries a `display_name`
built from the model's label and the lane's `LANE_DISPLAY` phrase — for the
shipped plan, `Qwen3.8-Flash-Next - Long (queued)`,
`... - Medium (concurrent)` and `... - Subagent (automatic)` — and no provider
name, with one exception: when a plan serves one model label through two
providers, every lane carrying that label gains the provider in brackets
(`Qwen3.8-Flash-Next [Local (MPS)] - Long (queued)`), because two rows of an
identical name would otherwise be chosen by guessing. One label across two lanes
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
alias the request arrives under, the proxy resolves that lane's own reservation
and re-prices the permit from the request's measured size, so a session that
switched itself onto a wider window still cannot spend more than that window's
lane is permitted. A wrong turn loses a request, not fair-use compliance. The
converse is also deliberate: the designations cannot be *enforced* to agree, so
`agent_lane` is a launch-time statement of which window the session opened in,
which is why the generated envelope publishes it — an agent cannot otherwise tell
which lane it is being metered on.
