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

## Extension and custom-node trust

Review executable project extensions with `./extensions.sh list`, then approve
their exact content with `./extensions.sh approve`. Approved snapshots are
mounted read-only. A content change requires review, approval, and restart.
Ordinary `AGENTS.md` guidance remains writable and does not enter this approval
boundary.

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
