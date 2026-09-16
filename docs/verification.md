# Verify the Kimi workspace and connect optional services

Run commands in the **harness checkout** (the directory containing `start.sh`)
unless a step explicitly says to run them inside Kimi or its container.
This guide matches the configured Kimi Code 0.43-era harness, not the older
Python `kimi-cli` command set. No Homebrew installation is needed.

## 1. Start with the automatic checks

1. Start the stack with `./start.sh`.
2. After Kimi web becomes reachable, the launcher runs a quick service check.
   It probes from inside the agent container, so DNS, networking, TLS and file
   permissions match the agent's environment.
3. Read every result:

   | Result | Meaning |
   | --- | --- |
   | `PASS` | The named check succeeded. Read the description for its scope. |
   | `SKIP` | The MCP server is disabled. It was not tested. |
   | `SETUP` | The server runs but needs configuration, such as Serena project activation. |
   | `MANUAL` | Authentication must be checked in Kimi, such as an OAuth connection. |
   | `FAIL` | The service failed, timed out, rejected credentials, or returned an unexpected tool/schema response. |

4. In a second terminal, rerun either check while the stack stays running:

   ```bash
   ./doctor.sh
   ./doctor.sh --full
   ```

Quick mode checks Kimi web, proxy health, search-adapter health, authenticated
enabled module service probes, and every enabled harness MCP server's initialization and
tool catalog (including configured tool allowlists). It also calls Chrome's
`list_pages` to actually launch Chromium and Serena's `get_current_config` to
detect missing project setup. Probes run concurrently, each bounded to 25 seconds.

Full mode allows 60 seconds per probe and adds a real public web search through
the authenticated search adapter and SearXNG, enabled module functional probes,
and representative read-only MCP calls: Hugging Face file metadata, DeepWiki
repository structure, GitHub identity and Context7 library lookup when enabled.
It does not enable disabled servers. No GPU jobs, model inference, private
repository queries, or arbitrary discovered tools are executed. MCP processes
may create their ordinary local caches/logs; the browser probe uses an isolated
profile, and probe process groups are terminated on completion or timeout.

The standalone command exits `0` for successful checks (disabled servers may be
skipped), `1` for a failure, and `2` for setup/manual work without other failures.
Startup reports these results but keeps the stack running so you can diagnose it.
Raw server responses, credentials and stderr are not printed by the checker.
Use Kimi's `/mcp` and the service logs to investigate a failed connection.

**A green check is not proof that every tool and every credential scope works.**
Discovery proves that tools can be loaded; one read-only call proves that path
works. Editing, private data access, project language servers, model-driven tool
selection, OAuth sessions, and approved project/plugin extensions need the
functional checks below. The checker uses the harness MCP file; it does not
merge project overrides or reuse Kimi's OAuth token store.

## 2. Understand what runs where

| Component | Where it runs | External service / account | Default |
| --- | --- | --- | --- |
| Kimi web, file/shell tools | Agent container | Inference uses OU/NRP via the proxy | On |
| Model policy proxy | Separate local container | OU/NRP LiteLLM credential | On |
| Chrome DevTools MCP | Agent container; launches sandboxed Chromium | No account; browsed sites may require login | On |
| Playwright CLI and skill | Agent container; browser automation | No account; website access is external | Installed; not a separate MCP server |
| Serena MCP | Agent container; code navigation/refactoring and language servers | No hosted account; needs a coding project and its language tooling | On |
| Hugging Face MCP | Hugging Face's servers | Public Hub access; optional HF account/token for authenticated access | When ComfyUI selected; only `hf_fs` exposed |
| DeepWiki MCP | Hosted by Cognition | Public indexed repositories; no account required | On |
| GitHub MCP | Executable in agent container, calling GitHub's API | GitHub account and PAT | Off; read-only toolsets configured |
| Context7 MCP | Hosted by Upstash | Public access with limits; optional account/API key | Off |
| NVIDIA CUDA docs MCP | NVIDIA's servers | NVIDIA Developer sign-in / OAuth | ComfyUI module; off until configured |
| SearXNG | Separate local container | Queries outside search engines; no SearXNG account | On |
| Search adapter | Separate local container | Uses only your SearXNG instance | On |
| ComfyUI and `comfyctl.py` | Native MPS service on Mac; container on CUDA; helper in agent | No account for local execution; individual model downloads may require one | When module selected; REST/WebSocket |
| Harness skills | Read-only instruction files inside the agent | No account of their own; may direct use of the tools above | Installed |

Remote MCP requests send tool arguments to their provider. Local execution does
not imply offline operation: GitHub MCP calls GitHub, browsers visit websites,
and SearXNG sends queries to search engines.

SearXNG is a **metasearch engine**: it asks multiple search engines for results and
combines them; it does not maintain a complete web index itself. In this harness:

```text
Kimi web-search tool → local authenticated search adapter → local SearXNG
                                                        → external search engines
```

The adapter converts SearXNG's JSON into the format Kimi expects. Its generated
bearer token is separate from `SEARXNG_SECRET`.

`SEARXNG_SECRET` is the local application's cryptographic secret key, used for
signing/protecting application data. **Do not register it anywhere.** Keep the
value you already generated; it is not a search-provider API key. SearXNG's
[`server.secret_key` documentation](https://docs.searxng.org/admin/settings/settings_server.html)
describes how `SEARXNG_SECRET` supplies that setting. The service is not published
on a host port in this harness. Upstream engines can still throttle requests or
return CAPTCHAs; a healthy container cannot rule that out.

## 3. Configure credentials

Stop the stack before changing `.env` or core or module `runtime/mcp.json`, then restart it.
Do not paste credentials into chat, tracked JSON, shell command arguments, or a
project MCP file. The two optional bearer variables below are now forwarded to
the agent by Compose; declaring a variable in `.env` alone does not otherwise
make it available inside a container.

### GitHub MCP: repository, issue and PR tools

1. Sign into GitHub. Open **Settings → Developer settings → Personal access
   tokens → Fine-grained tokens → Generate new token**.
2. Name it for this harness and choose an expiration date.
3. Choose the resource owner and **Only select repositories** for private
   repositories you need. For public-only access, use public repository access.
4. For selected repositories, grant **Contents: Read-only**, **Issues: Read-only**,
   and **Pull requests: Read-only**. Metadata is included automatically. Do not
   grant write or administration permissions for this read-only configuration.
5. If the organization requires approval, wait until the token is approved.
6. Copy the token once into `GITHUB_PERSONAL_ACCESS_TOKEN` in your local `.env`.
7. In core or module `runtime/mcp.json`, change only `github.enabled` to `true`. Keep `--read-only`
   and the limited toolsets.
8. Restart. Run `./doctor.sh --full`; `get_me` verifies authentication. Then ask
   Kimi to read a known file, list issues, and list PRs in each selected repository.
   Identity success alone does not validate repository permissions.

See [GitHub's PAT instructions](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens).
Organizations may restrict token access; a 403 or an unexpected 404 can mean
missing scope, pending approval, or an inaccessible repository.

### GitHub releases: downloading Kimi metadata

`GITHUB_RELEASES_TOKEN` is a **different, host-side credential** used by the
version selector for public release metadata/checksum downloads. It does not
enable GitHub MCP and is not forwarded to the agent.

Leave it empty if version selection works. If GitHub's anonymous API rate limit
blocks setup, create a second fine-grained token using the same settings page,
with public repository access and no additional private-repository permissions.
Put it in `GITHUB_RELEASES_TOKEN`, then rerun `./start.sh` and confirm the release
menus load. The checker inside the agent cannot validate this host-only token.
GitHub documents [public release access and token permissions](https://docs.github.com/en/rest/releases/releases#list-releases).

### Hugging Face

Public metadata checks work without an account in the tested configuration.
For authenticated access:

1. Create/sign into a [Hugging Face account](https://huggingface.co/join).
2. Open [MCP settings](https://huggingface.co/settings/mcp) and review enabled tools.
3. Create a read-only or suitably restricted token under
   [Access Tokens](https://huggingface.co/settings/tokens).
4. Set `HF_TOKEN` in `.env`. The harness MCP entry uses
   `bearerTokenEnvVar: "HF_TOKEN"`; keep `enabledTools: ["hf_fs"]`.
5. Restart and run the full doctor check. Then test any specific private/gated
   repository you need in Kimi. Gated models also require accepting the model's
   access terms; a token alone does not grant access.

Do not enable paid Jobs or community Space execution just to run this test.
See [Hugging Face's MCP guide](https://huggingface.co/docs/hub/agents-mcp).

### DeepWiki

No registration or token is required for the configured public endpoint.
It is already enabled. The full check reads the structure of `python/cpython`.
For your own target, ask Kimi to read its wiki structure; a repository that has
not been indexed may need indexing on [DeepWiki](https://deepwiki.com/).
Private Devin access is a different service and is not configured here.
[DeepWiki MCP documentation](https://docs.devin.ai/work-with-devin/deepwiki-mcp).

### Context7

1. Sign in at the [Context7 dashboard](https://context7.com/dashboard) and create
   an API key if you need authenticated access/higher limits.
2. Set `CONTEXT7_API_KEY` in `.env`; the MCP entry uses that variable as a bearer token.
3. Set `context7.enabled` to `true` in core or module `runtime/mcp.json`.
4. Restart, run `./doctor.sh --full`, then ask Kimi to resolve a library and query
   its documentation. The automatic test covers resolution; test `query-docs`
   separately with the returned library ID.

No local `npx` installation is needed. See [Context7's official setup](https://github.com/upstash/context7#installation).

### NVIDIA CUDA documentation

1. Create/sign into an [NVIDIA Developer account](https://developer.nvidia.com/).
2. Set `nvidia-cuda-docs.enabled` to `true` in `modules/comfyui/runtime/mcp.json`, then restart.
3. In Kimi's MCP controls, authenticate this server. In the interactive Kimi TUI,
   use `/mcp-config login nvidia-cuda-docs` and follow the browser authorization
   flow. Use `./shell.sh`, then `kimi`, to open the TUI if needed.
4. Check `/mcp` in a fresh session and ask for CUDA documentation with a source link.

Once the server is enabled, the doctor reports it as `MANUAL`: its independent MCP
client does not read or copy Kimi's OAuth token store. If browser callback handling
fails across Docker, use the web UI's MCP authentication controls and inspect the
reported flow error; do not put OAuth tokens in tracked JSON or open arbitrary ports
as a workaround.
There is no `NVIDIA_API_KEY` variable wired into this project.
[NVIDIA's connection instructions](https://developer.nvidia.com/nsight-ai) require
Developer sign-in; [Kimi's MCP guide](https://moonshotai.github.io/kimi-code/en/customization/mcp)
documents OAuth login and configuration.

### OU/NRP model access

`LITELLM_API_KEY` is supplied by your OU/NRP service administrator. It stays in
the model-proxy container. Confirm the approved origin/model with that
administrator; this harness cannot create an institutional account for you.
A proxy health check validates its local policy/configuration, not upstream
credential validity. Send one short prompt in Kimi and confirm a response.
Do not test by bypassing the proxy or launching extra concurrent model sessions.

## 4. Verify tools through Kimi, not just the transport

Open a **fresh Kimi session** after configuration changes. Run `/mcp` and compare
its enabled servers/tools with core or module `runtime/mcp.json`. Project MCP entries override
same-named user entries, so the standalone doctor's result may differ from that
session. `/mcp-config` edits to the harness MCP declaration do not persist: the
agent's copy is an immutable staged file, so configure servers on the host and
restart. Section 5 shows how to confirm settings persistence.

Use these tasks one at a time and inspect the actual tool-call cards and results.
An assistant's unsupported statement that a tool works is not evidence.

| Test request to Kimi | What confirms success |
| --- | --- |
| “Create a temporary verification directory, write a small Python function, read it back, grep its name, and run it with Bash.” | Actual write/read/search/shell calls and expected output. Keep the fixture until Serena testing is complete. |
| “Use Chrome DevTools to open `about:blank`, evaluate `1 + 1`, take a screenshot, and close the test page.” | Browser calls return 2 and a viewable screenshot. No logged-in site is necessary. |
| “Use the Playwright CLI skill to open `about:blank`, take a snapshot and screenshot, then close its test browser.” | CLI works independently of Chrome MCP and produces the requested artifacts. |
| “Activate my coding project with Serena, list its files, show the symbols in the verification file, and find references to the test function.” | Correct project path and actual symbol/reference results, not just text search. |
| “Use Hugging Face `hf_fs` to stat and read `hf://models/openai-community/gpt2/README.md`.” | Metadata and model-card text from the public repo. No model weights need downloading. |
| “Use DeepWiki to list `python/cpython` wiki structure, read a relevant page, and answer a small question using `ask_question`.” | All three allowed tools return useful content. |
| “Use GitHub MCP to identify my account, read a known repository file, and list issues and PRs.” | Identity plus each required repository permission succeeds. |
| “Use Context7 to resolve a library and query its docs.” | Both tools succeed with a valid returned library ID. |
| “Use NVIDIA CUDA docs to find the documentation for `cudaMalloc`.” | A real documentation tool call succeeds after OAuth. |
| “Use web search to find Python's official documentation, then fetch the result.” | Search produces links and the fetch tool retrieves page content. |

Serena's current `--project-from-cwd` setting discovers `.git` or
`.serena/project.yml`. A fresh workspace with only state/data folders is not
automatically a code project. Activate the actual repository you intend to edit
(for example a custom-node repository), rather than initializing Git over model
storage merely to silence the check. Activation/onboarding may create project
metadata and download required language servers. Confirm the language backend
works using real symbol queries. A separately activated project in a session is
not necessarily the next doctor's default project.

For write/refactoring tools, use only a disposable source fixture: rename its
function, verify references still run, then undo the edit. For Serena memory
tools, create/read/delete a test-only memory. Do not mass-invoke every advertised
tool: tools that delete files, open external sessions, or execute code need
individual test inputs and expected results.

## 5. Verify settings persistence and mount hygiene

Kimi's settings live in a writable named volume that a root-only initializer
prepares before the agent starts. Check both halves after any change to
`compose.yaml`, `container/initialize-agent-state.py`,
`tools/kimi_config_merge.py`, or `runtime/config*.toml`.

1. In the Kimi web UI, change a user-owned setting (default model, thinking,
   telemetry, background tasks, or experimental). The save must succeed. Open a
   second terminal and confirm it landed in the volume:

   ```bash
   ./shell.sh
   cat /home/agent/.kimi-code/config.toml
   ```

2. Stop and restart the stack, then confirm the value survived and that policy
   keys were re-pinned from `runtime/config.toml`. Provider base URLs, API keys,
   internal proxy tokens, context ceilings, and the subagent concurrency lane
   must still match the rendered baseline even if you edited them in-session.
3. Confirm the staged operator files are protected and the settings file is
   not. Still in `./shell.sh`:

   ```bash
   lsattr /home/agent/.kimi-code/AGENTS.md /home/agent/.kimi-code/SYSTEM.md \
     /home/agent/.kimi-code/mcp.json
   stat -c '%a %U:%G %n' /opt/kimi-runtime /opt/kimi-runtime/skills \
     /home/agent/.kimi-code/agents
   ```

   The three files must show the `i` flag and be mode `440`; the staged volume
   directories must be mode `550`. Both must be owned by `root` with the agent's
   primary group as the reader, which is what keeps them readable while the agent
   cannot write them. Then check both directions of the boundary:

   ```bash
   echo probe >> /home/agent/.kimi-code/AGENTS.md && echo UNPROTECTED
   rm -f /home/agent/.kimi-code/mcp.json && echo UNPROTECTED
   touch /opt/kimi-runtime/skills/probe && echo UNPROTECTED
   chattr -i /home/agent/.kimi-code/AGENTS.md && echo UNPROTECTED
   python3 -c 'import os,pathlib;p=pathlib.Path("/home/agent/.kimi-code/config.toml");t=p.with_suffix(".toml.probe");t.write_bytes(p.read_bytes());os.replace(t,p)'
   ```

   Every `UNPROTECTED` line must be absent, the rename above (the exact mechanism
   the settings UI uses) must succeed, and ordinary file tools in `/workspace`
   must keep working. `chattr` exists in the image but cannot work without
   `LINUX_IMMUTABLE`.
4. Check what the agent can see of the host. Still inside `./shell.sh`:

   ```bash
   awk '$4 != "/" && $4 !~ /^\/(bus|fs|irq|null|zero|sys|proc|sysrq-trigger|dev)/ {
     print $4 " -> " $5 }' /proc/self/mountinfo | sort -u
   ```

   Expected entries are the `/workspace` bind, the `/docker/volumes/...` named
   volumes, Docker's own `/etc/host*`, `/etc/resolv.conf` and `docker-init`
   files, and, only for a workspace with approved project extensions, its
   `.local/runtime/<instance>/extension-snapshot/...` snapshots. No individual
   harness file, generated configuration, or `.local/runtime` secret filename
   may appear. The workspace path itself cannot be hidden; use one whose name is
   not sensitive.
5. A settings save that fails with `storage write failed: unrecognized I/O
   error` means the Kimi home is not a writable volume, or the initializer
   declined to start because the volume filesystem does not honour ext4
   immutable flags. Read the `agent-state-init` service log; it fails closed
   with the path and ioctl that were rejected rather than starting an
   unprotected agent.

## 6. Optional module verification

For an enabled ComfyUI module, run the hardware-appropriate acceptance script:

```bash
bash modules/comfyui/tests/acceptance.sh mps    # Apple Silicon
# or: bash modules/comfyui/tests/acceptance.sh cuda
```

This uploads a tiny fixture, executes an `EmptyImage → SaveImage` workflow,
waits for completion, downloads the result into
`comfyui/output/acceptance-download`, and checks network/credential isolation
and read-only extension mounts. It requires no downloaded model weights.
It does write test input/output files, unlike the routine doctor probes.
Inspect the resulting 64×64 image. This validates workflow execution and file
transfer, but not a particular model's numerical correctness or performance.
Run one small known workflow for each model/custom-node combination you depend on.

Host-approved project agents, skills and MCP declarations need
`./extensions.sh list` and `./extensions.sh approve` after content changes.
Restart afterward. Skills are instructions, not independently listening servers;
verify a representative task using each skill you depend on. Extra project/plugin
MCP servers need their own equivalent connection, read and disposable-write tests.

## Completion checklist

- Every expected enabled server appears in a fresh Kimi `/mcp` view.
- A UI settings change saved without an error and survived a restart, and the
  agent's mount table contains no harness file paths beyond the workspace.
- Quick and full doctor checks have no unexplained failures; every `SETUP` or
  `MANUAL` result has been resolved or explicitly recorded as unused.
- Each relevant tool family has an observed successful call with checked output.
- GitHub/private repository access and OAuth are tested separately from discovery.
- A real model response, search/fetch, browser task, Serena symbol query, and
  enabled module acceptance tests succeed.
- Record Kimi and selected module versions and which optional integrations were enabled.

This is a reproducible acceptance record, not a promise that external services,
token scopes, every future project language, or every model will remain healthy.
