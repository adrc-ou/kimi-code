# Module authoring contract (schema version 1)

A module is an operator-installed folder immediately below `modules/`. The
folder name is its stable identifier: `[a-z][a-z0-9_]*`. No central registry or
core edit is needed. A folder without `module.json` is ignored. Module folders
and contents must be real files/directories, never symlinks or devices.

```text
modules/example/
├── module.json              # required: metadata and declarative setup
├── module.sh                # required: host compatibility and lifecycle hooks
├── AGENTS.md                # optional: guidance appended to the session prompt
├── README.md                # recommended: requirements, security and operation
├── runtime/
│   ├── mcp.json             # optional: {"mcpServers": {...}}
│   ├── skills/<name>/SKILL.md
│   ├── agents/<name>.md
│   └── tools/<name>         # optional: helpers and service probes
├── scripts/                 # private host-side implementation
├── dependencies.lock.json   # module-owned immutable dependency metadata
├── check_locks.py           # optional: discovered by core lock checks
└── tests/
    ├── test_*.py
    ├── compose-config.sh    # optional: called by core Compose checks
    └── build.sh             # optional: called by CI
```

## Manifest

```json
{
  "schema_version": 1,
  "label": "Example application",
  "workspace_directories": ["example/user", "example/output"],
  "environment": [
    {
      "name": "EXAMPLE_TOKEN",
      "prompt": "Token for your Example account",
      "secret": true,
      "agent": false
    }
  ],
  "images": ["example-local-image"]
}
```

`schema_version` and `label` are required. All other fields default to empty.
Labels must be nonempty and contain no terminal control characters. Directories
are relative to the writable workspace and cannot contain `..`. Initialization
creates missing directories without replacing existing data or following links.

Every `environment` entry is a required single-line variable. `name` must match
`[A-Z][A-Z0-9_]*`; `prompt` is required. Values defined in the harness `.env` are
resolved by Compose. Missing/empty values are requested after version selection.
`secret` defaults to true (hidden input); `agent` defaults to false (host only).
Only `agent: true` values are explicitly added to the agent's Compose environment.
Optional settings should have module defaults and be documented separately.
Never declare model-provider keys as agent variables.

Session values go to mode-0600 files below `.local/runtime/<instance>/`, never
`.env`, workspace files, selection history or version state. They are removed
on launcher exit. Non-interactive startup refuses missing required values.

`images` lists module-owned local Docker image repositories, without tags. The
module builds them with `${HARNESS_IMAGE_SUFFIX}` as the tag. On a later launch,
removing this module folder removes those instance tags and its private
`module-data/<id>/` installation. In-use Docker images cannot be removed; startup stops so you can stop
the owning session and retry. Stop all sessions before uninstalling. Shared Docker build cache is not pruned.
Persistent workspace directories and named data volumes are never deleted.

## Selection and ordering

`module.sh compatible` must exit 0 for a supported host or 1 for an unsupported
host. Other exit codes and timeouts fail startup. The probe must be read-only,
noninteractive, bounded, and must not install software. Detect host GPU/platform
requirements here. The core only detects architecture for the Kimi Linux asset.

Compatible modules are shown by label, alphabetically within two groups: last
successful setup's enabled modules (checked), then all others (unchecked).
Up/Down changes focus, Space toggles, Enter accepts, and Ctrl-C cancels with the
terminal restored. An empty list skips the menu. State is scoped to the harness,
workspace and host platform. `HARNESS_MODULES=a,b` overrides the menu;
`HARNESS_MODULES=` selects none. `--non-interactive` otherwise reuses the prior
compatible selection.

## Lifecycle hooks

`module.sh` is sourced by Bash 3.2+ on the host. Define optional functions below;
missing hooks are no-ops. At the bottom, dispatch the compatibility probe:

```bash
module_compatible() { ...; }
if [[ "${1:-}" == compatible ]]; then module_compatible; fi
```

The launcher calls hooks for each selected module in selection order:

| Hook | Responsibility |
| --- | --- |
| `module_configure` | Export module defaults/backend configuration; no installation or prompts. |
| `module_select_version` | After the Kimi menu, present the module's version menu and append validated version/commit values to `HARNESS_SESSION_FILE`. |
| `module_prepare` | After declarative directory/runtime setup and project extension approval, generate session secrets/certificates and record bind identities. |
| `module_compose` | Append trusted overlays to `HARNESS_COMPOSE_FILES`. Also used by `shell.sh`; no installation or secrets generation. |
| `module_verify` | Recheck module bind identities before install/start and when opening a shell. |
| `module_install` | Install immutable, verified application/dependency artifacts outside the workspace. |
| `module_check_build` | Verify built services/backend after the core Compose build. |
| `module_start` | Start any native services and wait for readiness; Compose starts afterward. |

All version and required-environment questions complete before installation.
Version hooks can reuse `scripts/select_versions.py` utilities: `choose`,
`load_state`, `write_environment`, release fetching and checksum validation.
Keep application catalogs and backend-specific compatibility rules in the module.
Only version metadata belongs in `HARNESS_SESSION_FILE`; it becomes persistent
`state.env`. Never write tokens or paths supplied as secrets there.

Available context:

- `HARNESS_ROOT`, `MODULE_DIR`, `HARNESS_WORKSPACE`, `HARNESS_RUNTIME_DIR`;
- `HARNESS_INSTANCE_ID`, `HARNESS_IMAGE_SUFFIX`, `HARNESS_PLATFORM`;
- `HARNESS_STATE_FILE`, `HARNESS_SESSION_FILE`, `HARNESS_RESOLVED_BOOTSTRAP`;
- `MODULE_NON_INTERACTIVE` (`true` or `false`);
- `harness_compose`, `state_value KEY`, and `wait_for_url URL [TOKEN] [CA] [TRIES]`.

Use module-prefixed exported variables and function-local scratch variables.
The shell's core `root` and `workspace` variables are available during startup,
but use `HARNESS_*` names for reusable hooks. Append native background PIDs to
`MODULE_PIDS` immediately after spawning; the launcher monitors, terminates and
waits for them. Add relative generated secret filenames to `MODULE_SESSION_FILES`
for cleanup after containers stop. Persist Compose-required exports in private
`runtime.env` with `printf 'export NAME=%q\n' "$value"` so `shell.sh` can use them.

Generated artifacts and replaceable native installations belong under
`HARNESS_RUNTIME_DIR/module-data/<id>/`. Never write real credentials to the
workspace or tracked files. Compose overlay paths resolve relative to the core
`compose.yaml`; use root-relative build paths and explicit validated bind sources
with `create_host_path: false`.

## Runtime assembly and security

Core and selected module skills, agents, tools and MCP entries are copied into
`<runtime>/assets/` and are otherwise unreachable to the agent. The initializer
copies that tree into the agent's runtime mirror volume as root-owned,
group-readable content, so `<runtime>/assets/` is the only path through which
module runtime content reaches `/opt/kimi-runtime`. Duplicate names fail closed.
Module content cannot override core runtime assets or the core Kimi config and
NRP provider policy, which remain authoritative; only the initializer writes
policy files. Module guidance is staged in the instance runtime directory and
appended to the session's system prompt under a heading naming the module. The
workspace's own `AGENTS.md` belongs to the project being worked on and is never
written, so no marker pair and no managed region exists there.

For the same reason, a module Compose overlay must not add host binds to
`kimi-agent`. `tools/compose_hygiene.py` runs against the fully resolved launch
configuration, and `tests/compose-config.sh` runs it again over the core files,
the generated approved-extension fragment, and each module overlay; only the
workspace bind and staged extension-snapshot binds are accepted. Every source
path a bind names is disclosed in the agent's mount table, and each extra mount
is one more thing a reviewer has to trace, so assets belong in
`<runtime>/assets/` and per-instance state belongs in declared workspace
directories or named volumes. A module that must hand the agent a file the
launcher generated stages it into a named volume with a network-less root-only
one-shot, which is what `modules/comfyui/compose.mps.yaml` does for the MPS
bridge CA certificate at `/run/comfy-bridge/ca.crt`. The same script requires
every published port to sit on `127.0.0.1` and every container to keep a
read-only root filesystem.

Optional `runtime/tools/service_<id>.py` files define `probe(full)` and return a
short success description. The doctor discovers only selected probes and runs
them with its bounded subprocess wrapper. Do not return response bodies,
credentials or sensitive URLs in diagnostics.

Modules are trusted host programs, **not a sandbox for third-party plugins**.
Review them as carefully as the launcher. Never source module hooks from the
workspace, and never bypass `extensions.sh` for workspace-supplied executable
extensions. Preserve non-root containers, read-only filesystems, capability
restrictions, browser sandboxing, network segmentation and secret isolation.

Stop sessions before modifying/removing modules. Removing a module takes effect
on the next launch: it disappears from selection, its assets/guidance are not
assembled, and its recorded disposable installation is reclaimed. Unchecking a
module retains its installation for later reuse. In both cases user data stays.
