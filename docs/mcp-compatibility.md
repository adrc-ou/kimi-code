# MCP compatibility at startup

Every launch checks the reviewed interfaces of enabled hosted harness MCP
servers. The quick service pass compares the tool catalog it already fetches;
there is no scheduler, extra container, additional discovery request, or new
launch question. The delayed full pass compares again before approved functional
calls. Source changes take effect on the operator's next launch.

The check establishes that an interface matched when discovery ran. It is a
**diagnostic, not a permission gate**: Kimi may already have loaded its tools, and
hosted services can change during the session. Failures leave the stack running.
No result automatically rewrites an allowlist, accepts a baseline or changes
Kimi's protected configuration.

## Contracts and ownership

Core contracts live in `runtime/mcp-contracts.json`. Modules own equivalent
`modules/<id>/runtime/mcp-contracts.json` files beside their MCP declarations.
`tools/modules.py` validates ownership and coverage and merges selected contracts
into `assets/mcp-contracts.json`. The existing initializer stages that file into
the read-only runtime asset volume at `/opt/kimi-runtime/mcp-contracts.json`.
No additional host bind reaches the agent. Deselection removes staged module
contracts without deleting persistent user data.

A manifest is a JSON object with `schema_version: 1` and `servers`, a map keyed by
names declared in its owning `mcp.json`. Each server entry has exactly:

| Field | Meaning |
| --- | --- |
| `reviewed_at` | ISO review date, not an expiry or automatic refresh trigger. |
| `auth_profile` | `anonymous`, `configured-bearer`, or `configured-headers`, matching the configuration. No credentials or credential hashes. |
| `catalog_names` | Complete reviewed catalog name set, including excluded tools. |
| `tools` | Fingerprints for the tools exposed by this configuration. |

Each `tools` entry has `input_schema_sha256`, `output_schema_sha256` and
`metadata_sha256`. Digests are 64 lowercase hexadecimal characters; an absent
output schema is represented by JSON `null`. Input/output schema digests include
the complete schema. Metadata covers top-level title, description, annotations
and execution declarations. Tool names must satisfy the MCP name character/length
convention. Unsupported fields, duplicate catalog names, invalid fingerprints,
authentication-profile mismatches and incomplete selection coverage are rejected.

`enabledTools` and `disabledTools` remain the tool-selection policy. Contracts
grant no permissions. With no explicit allowlist, every effective catalog tool
needs a fingerprint; changes to that exposed set require review. Local pinned
stdio servers retain discovery and existing functional checks; absent contracts
are reported as `not_enrolled`. Disabled and unsupported OAuth connections remain
`SKIP` and `MANUAL`.

The initial hosted contracts cover DeepWiki, Context7 and ComfyUI's Hugging Face
`hf_fs` tool. The interfaces were reviewed against public discovery on
2026-10-23. Hugging Face's initial selected schema was observed anonymously;
its contract expects the module's configured bearer authentication, which must
still be verified in each authenticated launch. An empty `HF_TOKEN` is `SETUP`
before any network request, never an anonymous compatibility pass. Different
authentication scopes can produce different catalogs; a mismatch alone does not
prove an upstream rename.

## Comparison and results

`tools/check_services.py` uses its staged MCP configuration, configured headers
or bearer token, and the agent's DNS/TLS/network environment. The same initialized
session supplies both catalog comparison and any approved smoke call. Only
complete catalogs are compared; duplicate tool names, repeated pagination cursors,
malformed schemas and exceeded limits invalidate discovery.

| Observation | Result |
| --- | --- |
| Selected names and fingerprints match | `PASS`, compatibility `matched`. |
| Added/removed tools outside an explicit allowlist | `PASS` with an unselected-catalog-change notice. |
| Configured tool absent | `FAIL`, compatibility `incompatible`; missing and added names are separate findings. |
| Selected input/output schema changes | `FAIL`, compatibility `review`; change is not necessarily a proven breaking change. |
| Selected description/behavior metadata changes | `SETUP`, compatibility `review`. |
| Effective exposed tool set changes | `FAIL`, compatibility `review`. |
| Hosted server has no reviewed contract | `SETUP`, compatibility `unbaselined`. |
| Network, authentication or catalog validation fails | Failure/setup result; compatibility remains `unverified` until complete discovery. |
| Local server has no contract | Existing discovery result, compatibility `not_enrolled`. |

Fingerprints use deterministic JSON object-key ordering, UTF-8 and SHA-256.
Array order and every schema keyword are preserved. This detects changes rather
than attempting general JSON Schema equivalence: even reordered schema arrays
or nested documentation edits can require review. No schema reference is fetched
or executed, and server descriptions/annotations never authorize a tool call.
A selected contract requiring review suppresses that server's automatic smoke
call. Compatibility-only mode calls no tools at all.

The existing parallelism and deadlines are retained: quick workers allow 25
seconds, full workers 60, with one retry only after timeout. Comparison adds
bounded local work and no discovery round trips. An unreachable server can still
consume that existing timeout budget. Catalog limits are 50 pages, 2,048 tools,
8 MiB aggregate tool data, 256 KiB per tool, nesting depth 40 and 100,000 JSON
nodes per canonicalization. Hosted HTTP responses are limited to 8 MiB before
SDK parsing; the probe requests identity encoding and refuses compressed
responses to avoid unbounded decompression. Local stdio still uses the SDK's
transport plus application-level limits and the worker process deadline.

## Reports and focused checks

The existing `service-check.json` keeps status/detail fields and adds a
`compatibility` object for MCP checks. It records the verdict, observed catalog
names and selected fingerprints, reviewed-contract digest, and relevant name,
schema or metadata differences. The report timestamp identifies the observation
pass. Raw descriptions, schemas, tool responses, exception text, headers and
credentials are not printed or archived by the checker.

Quick and full passes share a launch `run_id`. Before probing, the launcher writes
a private `state: pending` report; it also invalidates the previous launch's
verdict before configuration/build. A completed report is accepted only if both
its run identifier and pass mode match. A worker/copy failure leaves a pending
report, not an inherited PASS. The full pass replaces the quick report with a
fresh comparison against the same staged reviewed baseline. Reports never become
future approval baselines.

The container report lives in tmpfs. The launcher copies its redacted result to
`${HARNESS_ROOT}/.local/runtime/<instance>/service-check.json` atomically at mode
0600. This latest verdict intentionally survives shutdown. No compatibility state
is written into the workspace and no raw catalog history accumulates.

To inspect a server without functional calls, open `./shell.sh` and run:

```bash
/opt/serena/bin/python /opt/kimi-runtime/tools/check_services.py \
  --compatibility-only --server deepwiki --report /tmp/mcp-compatibility.json
python3 -m json.tool /tmp/mcp-compatibility.json
```

Omit `--server` to discover every enabled harness MCP server. `--server NAME` also
works with the ordinary quick or full pass. `--full` and `--compatibility-only`
are mutually exclusive. Focused reports stay at the requested container path;
they do not replace the launcher's host verdict automatically. `--config` and
`--contracts` support explicit test/diagnostic inputs; launch checks use the
protected staged files.

## Reviewing an update

1. Read the missing/added names or changed tool and its observed fingerprints.
   Distinguish interface change from incomplete discovery or missing credentials.
2. Review upstream documentation and the current interface. Update source tool
   selection, the owner's contract and any affected smoke recipe together. Review
   is required even when a new name resembles an old one; no automatic aliasing or
   first-launch enrollment exists.
3. Use the focused report as candidate fingerprint data, add the review date,
   and run `python3 scripts/check_mcp_contracts.py` plus relevant tests. Run an
   explicit public/read-only functional check for a changed recipe. Capturing a
   fingerprint alone is not approval.
4. Review, commit and deploy through the normal external workflow. Stop and start
   from the updated launcher clone. Neither source edits nor diagnostic reports
   mutate a running session.

`python3 scripts/check_mcp_contracts.py` runs offline in CI. It checks all module
sources, including currently unselected modules, and requires contracts for
enabled supported hosted servers. Local fixture tests exercise interface drift,
limits, authentication, report freshness and discovery/call counts through the
real MCP SDK; CI does not depend on live vendor availability.

Scope remains **harness core and selected-module configuration**. The checker
does not merge project/plugin overrides or reuse Kimi's OAuth store. Project
extensions still require `extensions.sh` approval. MCP change notifications are
not subscribed to: the probe exits, and Kimi owns the long-lived session client.

References: [MCP tools, schemas and optional change notifications](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)
and [JSON Schema object semantics](https://json-schema.org/understanding-json-schema/reference/object).
