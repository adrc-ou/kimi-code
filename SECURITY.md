# Security model

This harness treats the workspace, project-supplied agent extensions, model
responses, upstream HTTP responses, downloaded media, and custom ComfyUI nodes
as untrusted input. Host credentials and generated service credentials remain
outside the workspace. Services receive only the credentials and networks they
need.

## Supported deployment boundary

The supported hosts are Apple Silicon macOS with Docker Desktop and native MPS,
or x86-64 WSL2 with Docker Desktop, NVIDIA Container Toolkit, and CUDA. The
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

Custom nodes are executable code. Inspect their source and dependency metadata
before placing them in the custom-node directory. Add required packages as
exact pins to `comfy/requirements-custom.txt`, regenerate the hash-checked lock,
update recorded digests, and recertify both supported backends before release.
Runtime dependency installation is intentionally unsupported.

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
