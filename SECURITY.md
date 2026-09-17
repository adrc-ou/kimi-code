# Security model

This harness treats the workspace, project-supplied agent extensions, model
responses, upstream HTTP responses, downloaded media, and module application extensions
as untrusted input. Host credentials and generated service credentials remain
outside the workspace. Services receive only the credentials and networks they
need.

## Supported deployment boundary

The core supports macOS and Linux/WSL2 on arm64 or x86-64. Modules enforce their
own platform requirements. The
Compose services are a local single-user development environment; do not expose
their ports on a shared or untrusted network.

The launcher creates an instance identity from the canonical project root,
canonical workspace, and platform. It prevents concurrent ownership of that
instance, records bind-path identity before Compose starts, and verifies it
again immediately before launch. Generated secrets and rendered configuration
are stored with restrictive permissions below `.local/runtime/INSTANCE_ID` and
removed when the launcher exits.

## Agent filesystem permissions

Kimi keeps its configuration, sessions, and OAuth state in its home directory
and writes its settings file with a temporary-file rename. That directory
therefore has to be writable, and a `:ro` bind over the settings file makes the
rename fail with `EBUSY`, which the UI reports only as an unrecognized I/O
error. The harness resolves this with staged volumes rather than a permissive
mount:

- the agent's home, Serena cache, and runtime-asset mirror are Docker named
  volumes, and the agent sees no host path behind them except the workspace;
- a root-only, network-isolated one-shot initializer runs before the agent. It
  owns the staging content, repairs ownership to the agent UID/GID, and is the
  only service with `FOWNER`/`LINUX_IMMUTABLE`;
- staged content is root-owned, readable only through the agent's primary group,
  and never writable by the agent. The tree in the runtime-asset mirror volume
  and the empty placeholder volumes for user-scoped agents, skills, and plugins
  are protected by that ownership and mode, because their parent directories are
  root-owned;
- the three operator files that live inside the agent-owned Kimi home
  (`AGENTS.md`, `SYSTEM.md`, `mcp.json`) additionally carry the ext4 immutable
  flag, set by the initializer after it writes them. Ownership and mode cannot
  protect them: `may_delete()` lets a process that owns a directory remove any
  entry in it, and the sticky bit is precisely the exception for callers who do
  *not* own the directory. The flag survives the container and outlives the
  session, and the agent cannot clear it because it does not hold
  `LINUX_IMMUTABLE`. Staging fails closed if the volume filesystem does not
  honour the flag;
- policy that the agent is allowed to change is confined to the user-owned keys
  listed in `runtime/config-policy.json`. Everything else in `config.toml` is
  re-rendered from `runtime/config.toml` at each launch, so an in-session edit
  of model, context, concurrency, or provider settings is reverted on restart
  instead of being trusted.

`no-new-privileges` and dropped capabilities stay in force, and the agent
remains non-root on a read-only container filesystem. The state volumes are
mounted read-write, and Compose offers no mount-option syntax for adding
`noexec` to a named volume, so the boundary here is content, ownership, and
file flags rather than mount options. Do not replace the immutable staging with
a `:ro` bind over a file the agent must write, and do not grant the agent
container `LINUX_IMMUTABLE`, `FOWNER`, or write access to the initializer's
staging inputs.

## Host information disclosure

Bind sources are listed verbatim in the world-readable
`/proc/self/mountinfo` inside the container, and Docker gives no mechanism to
hide a mount that the container needs. The same applies to named-volume
sources, which appear as `/docker/volumes/<project>_<name>/_data`. The agent can
therefore always learn the exact host path of its workspace and the Compose
project name, and it can learn the harness checkout path whenever the workspace
has approved project-extension snapshots.

The workspace path normally sits below the operator's home directory, so the
account name is part of what the agent can see. Keep account names and other
sensitive path components out of the workspace location, out of the harness
checkout location, and out of `COMPOSE_PROJECT_NAME`. Everything else in the
harness root, including the instance directory, rendered configuration, and
generated secret filenames, is no longer visible in the agent's mount table.

Treat anything the agent can read in `/workspace` as disclosure-capable: it has
network egress, and no mount flag prevents exfiltration of content it can read.

## Extension and custom-node trust

Review executable project extensions with `./extensions.sh list`, then approve
their exact content with `./extensions.sh approve`. Approved snapshots are
mounted read-only. A content change requires review, approval, and restart.
Ordinary `AGENTS.md` guidance remains writable and does not enter this approval
boundary. Links are refused at every level of a privileged path, not only at its
leaf: an ancestor link would otherwise let workspace-authored content redirect the
scan, the approval digest, and the snapshot copy into a host directory the agent
cannot otherwise read.

Modules under `modules/` are operator-trusted host code, like the launcher itself.
Never load modules from the writable workspace. Stop the stack before installing,
editing or removing modules. Selected runtime contributions are copied into
private snapshots and mounted read-only; conflicting assets fail closed.
Modules cannot replace core model policy through the manifest interface.
Module hooks and Compose overlays must receive the same review as core changes.
Native module applications run with the host user's permissions; see each
module's documentation for its application-specific trust boundary.

## Dependency and vulnerability maintenance

All container bases use immutable digests. Application source uses immutable
commits or verified release checksums. Python and npm dependencies use checked-in
locks, and CI verifies the lock metadata. Run `scripts/generate_sbom.sh` for each
built image and review the Grype result. A vulnerability exception must name an
owner, explain the decision, and have a future expiry date in
`vulnerability-exceptions.json`.

## Reporting

Do not include API keys, generated tokens, private keys, workspace content, or
full upstream response bodies in a report. Report security issues privately to
the repository maintainers with the affected commit, reproduction steps, and a
description of impact.
