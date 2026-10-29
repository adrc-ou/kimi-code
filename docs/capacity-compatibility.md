# Native local inference compatibility evidence

Implementation verification recorded on 2026-10-24 and extended on 2026-10-29 with
the local completion semantics below. The pre-refactor reference is
commit `0cc5d51aad49c7e201c2eb1c641ed85b59611bba`. This record covers repository
verification, not native generation correctness or paging performance.

## NRP compatibility

The NRP provider and model manifests are unchanged. Complete NRP-only plans
compare equal to the captured baselines for both supported main-lane designations,
including lanes, counters, margins, exclusivity, rates, fan-out, routes and
credential scopes. Generated main and lane guidance also compare equal to the
pre-refactor implementation. Comparisons used source definitions and synthetic
inputs, without operator credentials or running-instance state.

Mixed-provider fixtures cover both directions. Local execution counters remain
engine-scoped and distinct from NRP fair-use counters. Engine failures do not
alter independent remote routes or provider enforcement.

## Native-context contract

Both local manifests retain their original installed-RAM eligibility floors.
The picker filters out models below those floors; overrides cannot bypass them.
Their provider explicitly
selects `native_context` and one sequence slot. Resolved local plans use version 3,
with model-native windows, explicit slots, OS-managed memory and pinned identities.
They have no physical resource budgets or memory projection requirement. NRP-only
plans retain version 1. Version 2 local plans require a relaunch.

Fixtures exercise shared and distinct engines, low/high/zero/unknown available memory,
unavailable measurements and absent estimators. They verify that no projection
is called during resolution or selection, and that memory does not change a
window or slot count after static eligibility passes. High or unmeasurable startup
RSS does not reject an otherwise ready engine. Installed-RAM/disk/backend eligibility
and digest-based readiness remain intact;
uninstalled models can be selected and verified by the existing installer.

Resolver and proxy fixtures retain native-context, total/per-slot, engine-binding,
profile and staged-Kimi drift checks. Local request tests cover tool/Unicode/code
payloads, exact token boundaries, remaining-context generation, explicit output
maxima, counting failure, preparation serialization and cancellation. Existing
execution-gate tests cover queue priority and permit release. Supervisor fixtures
cover occupied listeners, startup failure cleanup and identity readiness; proxy
fixtures keep failed local routes unavailable without changing NRP enforcement.

## Local completion semantics

Readiness and failure are now decided by computation rather than by metadata. A
local engine publishes its readiness receipt only after an authenticated
generation has produced content, and a model that declares `tool_use` must also
return a tool call for a forced one, so the capability is proved at launch rather
than assumed. An engine that answers metadata and cannot compute fails the launch
instead of serving a session that retries forever.

`proxy/local_engine_contract.py` holds the one vocabulary for this and is pure
standard library, because the launcher, the proxy and an engine worker all read
it and only the proxy has aiohttp. Authority is split rather than centralised: the
proxy decides whether the client received a usable completion, the engine decides
whether it is fatally broken, and a healthy metadata read never clears a latched
failure. A disagreement between the two is reported as unusable and does not latch,
because two reporters contradicting each other is a contract defect and not
evidence about the device.

A latched engine is refused before the wire, so no further request can pay for a
prefill that cannot complete. The terminal refusal carries no `Retry-After`, which
is the only lever the proxy controls against a client configured to retry
indefinitely; whether that ends the client's loop is an actual-client question and
is not claimed here. Two knobs bound a local request that neither a latch nor a
status code can reach: a progress lease measured on generated content rather than
bytes on the wire, and a dispatch attempt bound. Both default to zero, which
preserves the previous unbounded behaviour, because no measurement of worst-case
native-context prefill on the affected host exists yet; they are armed from native
evidence, not from a guess.

Engine metadata is bounded at 1 MiB rather than 64 KiB in both readers. `/props`
embeds the model's entire chat template, so a large template previously made a
healthy engine report "exceeded metadata byte limit" and refuse traffic.

Fixtures for all of the above run without aiohttp where they describe the shared
vocabulary, so the launcher side is not silently skipped on a host that has only
the proxy's dependencies.

The operator evidence helper records synthetic usage, owned-process peak RSS and
attributed shutdown allocations. These observations never enforce a RAM envelope
or qualify hardware automatically. Allocation failures and malformed evidence
still fail explicitly.

## Validation and rollout

Focused policy/picker/host/proxy checks and engine/configuration/rendering/launcher
integration checks passed. Ruff, compilation, lock verification, shell syntax and
compose confinement checks also passed. Compose checks used fixture configuration
and did not start services. Host-specific Linux `/proc` and terminal guarantees
remain CI responsibilities; these checks are not an unqualified full-suite run.

The restored static-eligibility checks passed 195 host/catalog, definition,
picker, native-policy and survey cases. They cover the smaller host excluding
both local models, the intermediate host showing only the smaller local model,
and the larger host showing both. A synthetic below/at-floor picker check also
confirms that overrides cannot bypass eligibility while zero available RAM does
not shrink the native window of an eligible model. NRP baselines still match.

No actual model was started, no generation smoke was performed, no OS settings
were changed and no running stack was restarted or repointed for this change.
Native declarations remain `pending_native_validation`. Removing harness RAM
admission permits oversubscription; it does not guarantee that Metal allocations
are pageable or successful. Comprehensive OOM recovery remains deferred. Changes
take effect on the operator's next launch.
