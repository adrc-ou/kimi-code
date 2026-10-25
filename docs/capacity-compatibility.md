# Native local inference compatibility evidence

Implementation verification recorded on 2026-10-24. The pre-refactor reference is
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
