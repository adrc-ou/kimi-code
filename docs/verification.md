# Verify the Kimi workspace and connect optional services

Run commands in the **harness checkout** (the directory containing `start.sh`)
unless a step explicitly says to run them inside Kimi or its container.
This guide matches the configured Kimi Code 0.43-era harness, not the older
Python `kimi-cli` command set. No Homebrew installation is needed.

This page covers a running stack. Static and unit validation is the
[Validation section of the README](../README.md#validation), which also states its
`aiohttp` prerequisite — without it the policy-enforcement suite skips rather than
runs, and a green result means nothing about fair-use enforcement.

## 1. Start with the automatic checks

1. Start the stack with `./start.sh`.
2. After Kimi web becomes reachable, the launcher prints a quick service check
   and then runs the full check unattended in the background once the stack has
   settled. Both probe from inside the agent container, so DNS, networking, TLS
   and file permissions match the agent's environment.
3. Read every result:

   | Result | Meaning |
   | --- | --- |
   | `PASS` | The named check succeeded. Read the description for its scope. |
   | `SKIP` | The MCP server is disabled. It was not tested. |
   | `SETUP` | The server runs but needs configuration, such as Serena project activation, or it declares a bearer variable the environment leaves empty and Kimi will not mount it. |
   | `MANUAL` | Authentication must be checked in Kimi, such as an OAuth connection. |
   | `FAIL` | The service failed, timed out, rejected credentials, or returned an unexpected tool/schema response. |

4. The background pass writes its own verdict when it lands:

   ```bash
   cat .local/runtime/<instance>/service-check.log
   python3 -m json.tool .local/runtime/<instance>/service-check.json
   ```

   The JSON report is whichever pass ran last, so it stamps itself with
   `generated_at` and `mode` and holds one `{kind, name, status, detail}` entry
   per check plus a `counts` summary and the exit verdict. It survives the launch
   that produced it; the log, which also carries raw container output, does not.

   Nothing has to be rerun to check a configuration change. The agent's MCP file
   is an immutable staged copy, so an edit only takes effect after a restart, and
   a restart checks the stack again by itself. To probe the running stack again
   by hand, `./shell.sh` opens a container shell and
   `/opt/serena/bin/python /opt/kimi-runtime/tools/check_services.py --full`
   repeats the pass there.

Quick mode checks Kimi web, proxy health, search-adapter health, the local
toolchain (every launcher requirement present on `PATH`, and the Playwright CLI able to start),
authenticated enabled module service probes, and every enabled harness MCP server's
initialization and tool catalog (including configured tool allowlists). It also calls Chrome's
`list_pages` to actually launch Chromium and Serena's `get_current_config` to
detect missing project setup. The Serena check only passes when that response
also reports `Language server status: ready`; a server that initialises but whose
language servers failed to start, or were never initialised, is a `FAIL` rather
than a `PASS`, because every symbol tool is dead in that state. Probes run
concurrently, each bounded to 25 seconds. A probe that never answers within its
budget is retried once, because a container that is still busy starting produces a
timeout no checker can attribute to a service; a probe that answers with a failure
is reported immediately, and a result reached on the retry says so in its detail.

Full mode allows 60 seconds per probe and adds a real public web search through
the authenticated search adapter and SearXNG, enabled module functional probes,
and representative read-only MCP calls: Hugging Face file metadata, DeepWiki
repository structure, GitHub identity and Context7 library lookup when enabled.
It does not enable disabled servers. No GPU jobs, model inference, private
repository queries, or arbitrary discovered tools are executed. MCP processes
may create their ordinary local caches/logs; the browser probe uses an isolated
profile, and probe process groups are terminated on completion or timeout.

Each pass exits `0` for successful checks (disabled servers may be skipped), `1`
for a failure, and `2` for setup/manual work without other failures. Startup
records that verdict in the report and keeps the stack running so you can diagnose
it; neither pass can fail a launch. Raw server responses, credentials and stderr
are not printed by the checker, and the report holds only the redacted status
lines. Use Kimi's `/mcp` and the service logs to investigate a failed connection.

**A green check is not proof that every tool and every credential scope works.**
Discovery proves that tools can be loaded; one read-only call proves that path
works. Editing, private data access, symbol queries against a specific project,
model-driven tool selection, OAuth sessions, and approved project/plugin
extensions need the functional checks below. The checker uses the harness MCP
file; it does not merge project overrides or reuse Kimi's OAuth token store.

## 2. Understand what runs where

| Component | Where it runs | External service / account | Default |
| --- | --- | --- | --- |
| Kimi web, file/shell tools | Agent container | Inference uses the selected provider via the proxy | On |
| Model policy proxy | Separate local container | The selected provider's credential | On |
| Chrome DevTools MCP | Agent container; launches sandboxed Chromium | No account; browsed sites may require login | On |
| Playwright CLI and skill | Agent container; browser automation | No account; website access is external | Installed; not a separate MCP server |
| Serena MCP | Agent container; code navigation/refactoring and language servers | No hosted account; needs a coding project; its Python language server is built into the image and pinned by `runtime/serena-config.yml`, so nothing is downloaded at activation | On |
| Hugging Face MCP | Hugging Face's servers | Public Hub access; optional HF account/token for authenticated access | When ComfyUI selected; only `hf_fs` exposed |
| DeepWiki MCP | Hosted by Cognition | Public indexed repositories; no account required | On |
| GitHub MCP | Executable in agent container, calling GitHub's API | GitHub account and PAT | Off; read-only toolsets configured |
| Context7 MCP | Hosted by Upstash | No key needed; anonymous calls work at a reduced rate limit. A key raises limits and is the only route to private repositories | On |
| NVIDIA CUDA docs MCP | NVIDIA's servers | NVIDIA Developer sign-in / OAuth | ComfyUI module; off until configured |
| SearXNG | Separate local container | Queries outside search engines; no SearXNG account | On |
| Search adapter | Separate local container | Uses only your SearXNG instance | On |
| ComfyUI, `comfyctl.py`, and its frontend | Native MPS service on Mac; container on CUDA; helper in agent | No account for local execution; individual model downloads may require one | When module selected; REST/WebSocket plus a browsable UI origin |
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
project MCP file. The credential variables below are forwarded to the agent by
Compose; declaring a variable in `.env` alone does not otherwise make it available
inside a container. Core `compose.yaml` forwards two of them,
`GITHUB_PERSONAL_ACCESS_TOKEN` and `CONTEXT7_API_KEY`. `HF_TOKEN` is a third and is
different: it is forwarded only by the ComfyUI module overlays, because the Hugging
Face MCP entry lives in that module, so it reaches the container only while the
module is selected.

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
8. Restart, which runs the full check by itself; its `get_me` call is what
   verifies authentication. Then ask
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
5. Restart, which runs the full check by itself. Then test any specific private/gated
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

No credential is required. Upstash documents anonymous basic usage on the same remote
endpoint this harness uses, and the core `context7` entry carries no `bearerTokenEnvVar`,
so a fresh launch gets both Context7 tools with nothing to configure. Anonymous calls
land in a reduced shared rate limit that Context7 does not size in its own documentation,
and that is the only thing a key changes: a free key raises the allowance to 1,000 calls
per month, and private repositories need a paid plan.

To authenticate anyway:

1. Sign in at the [Context7 dashboard](https://context7.com/dashboard) and create an API key.
2. Set `CONTEXT7_API_KEY` in `.env`.
3. Add `"bearerTokenEnvVar": "CONTEXT7_API_KEY"` to the `context7` entry in core or module
   `runtime/mcp.json`, then restart. Naming the variable is what sends it as a bearer token,
   and it is also what makes Kimi require a non-empty value. With the variable declared and
   empty, the server fails closed and contributes no tools at all, which is why the keyless
   default omits the line instead of leaving it blank.
4. Ask Kimi to resolve a library and query its documentation. The automatic test covers
   resolution; test `query-docs` separately with the returned library ID.

The quick check reports `SETUP`, not `PASS`, for any server that declares a bearer variable
the environment leaves empty, so the keyless default cannot regress into an unmounted server
without saying so.

No local `npx` installation is needed. See [Context7's official setup](https://github.com/upstash/context7#installation).

### NVIDIA CUDA documentation

1. Create/sign into an [NVIDIA Developer account](https://developer.nvidia.com/).
2. Set `nvidia-cuda-docs.enabled` to `true` in `modules/comfyui/runtime/mcp.json`, then restart.
3. In Kimi's MCP controls, authenticate this server. In the interactive Kimi TUI,
   use `/mcp-config login nvidia-cuda-docs` and follow the browser authorization
   flow. Use `./shell.sh`, then `kimi`, to open the TUI if needed.
4. Check `/mcp` in a fresh session and ask for CUDA documentation with a source link.

Once the server is enabled, the service check reports it as `MANUAL`: its independent MCP
client does not read or copy Kimi's OAuth token store. If browser callback handling
fails across Docker, use the web UI's MCP authentication controls and inspect the
reported flow error; do not put OAuth tokens in tracked JSON or open arbitrary ports
as a workaround.
There is no `NVIDIA_API_KEY` variable wired into this project.
[NVIDIA's connection instructions](https://developer.nvidia.com/nsight-ai) require
Developer sign-in; [Kimi's MCP guide](https://moonshotai.github.io/kimi-code/en/customization/mcp)
documents OAuth login and configuration.

### Model provider access

Each model definition may name a `key_env` variable holding a key for that model alone;
the `[[credential]]` it points at in the provider definition supplies the provider-scoped
fallback variable (`QWEN3_API_KEY` then `NRP_API_KEY` for the shipped model) and a
`key_url` where the administrator issues it. To list the names a checkout defines, run
`grep -rn '^key_env = ' models` and `grep -rn '^env = ' providers`. The value stays in the
model-proxy container; confirm the approved endpoint and model entitlement with that
administrator, because this harness cannot create an institutional account for you.

The proxy enforces whatever rules the selected providers publish, resolved at
launch into one plan — see
[the definition contract](models-providers.md). For NRP that is three rules: a
per-minute output-token allowance per API token *and* model (the only rule the
gateway meters itself, with HTTP 429 — input tokens do not count); one concurrent
request once a request uses at least the policy's fraction of the model context;
and otherwise a bounded number of concurrent requests whose combined context
stays inside that fraction. Nothing about those numbers is baked into the proxy:
each lane's reservation is its input cap plus its output clamp from the resolved
plan, revalidated against Kimi's live rendered configuration on every refresh.

Read the policy that is in force right now:

```bash
./shell.sh
curl -s http://model-proxy:8080/healthz | python3 -m json.tool
```

Expect `policy_enforced: true`; `subagent_limit`; `providers` and `credentials`
for the selection; then per lane its alias, model, endpoint, credential, context
window, input cap, output clamp, computed reservation, whether that reservation
runs alone, and the counter ids it holds. `counters` reports each context gate's
`context_budget` against its provider threshold, and `rates` each rolling
ledger's `capacity` and `unit`. The launcher's quick check condenses the same data to
`policy enforced (3 lanes; 5 subagent permits; context budget 332500; rate 1
counter(s))`; a bare `HTTP ready` or a FAIL means the proxy has stopped
verifying policy, not merely that it is slow.

The same plan is composed into the two prompt documents, so what Kimi was told is
readable without a container shell: the staged copies are
`.local/runtime/<instance>/SYSTEM.md` and `.../AGENTS.md` on the host, and
`/home/agent/.kimi-code/SYSTEM.md` and `.../AGENTS.md` inside the sandbox. Check
that the numbers there match `/healthz`; both come from the one plan, so a mismatch
means a stale launch. The launch panel can switch any add-on off and can put either
document on `on` or `off`, so a missing section is a setting rather than a fault —
the proxy still enforces the plan, and `./prompts.sh --show` prints both halves of
what the next unattended launch will compose: the two documents with their state,
and the nine add-ons with theirs.

With the shipped definitions the model advertises 1,000,000 tokens, of which
262,144 are native and the rest requires YaRN extension upstream. The primary lane
reserves its whole native window and the long lane reserves 965,536, which is at
or above the exclusivity threshold and so runs strictly alone. Only that long lane
runs alone. A primary request plus one subagent reservation does fit the aggregate
budget, which is what the safety margin buys; a primary request plus the full
five-subagent fan-out does not, so the proxy queues the overflow rather than
refusing it. That is the intended shape, not a fault.

Drift fails closed. If the mounted configuration stops describing lanes the
proxy can admit — `secondary_model.force` flipped off, an input allowance that
cannot fit its own window plus the output clamp, or a reservation that could
never be admitted — `/healthz` returns 503 and chat requests get 503 with
`Retry-After` instead of unmeasured traffic. Confirm it once from the host, with
the stack still running:

```bash
rendered=$(sed -n "s/^KIMI_RENDERED_CONFIG=//p" .local/runtime/*/runtime.env | tr -d "'")
sed -i 's/^force = true/force = false/' "$rendered"
docker compose exec -T kimi-agent curl -s -o /dev/null -w '%{http_code}\n' \
  http://model-proxy:8080/healthz    # must print 503
```

Then stop the stack, run `./start.sh` to re-render, and confirm the code is 200
again. Never edit the rendered file as a way to change policy: it is
regenerated from `./models` and `./providers`, and the initializer re-stamps the
agent's copy at every launch.

A proxy health check validates its local policy/configuration, not upstream
credential validity. Send one short prompt in Kimi and confirm a response.
Do not test by bypassing the proxy or launching extra concurrent model sessions.

## 4. Verify tools through Kimi, not just the transport

Open a **fresh Kimi session** after configuration changes. Run `/mcp` and compare
its enabled servers/tools with core or module `runtime/mcp.json`. Project MCP entries override
same-named user entries, so the launcher's check may differ from that
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
metadata; it will not download a language server for Python, because the image
already builds the pinned `pyright-langserver` and `runtime/serena-config.yml`
names it through `ls_specific_settings`. That file is where Serena's global
settings are changed: the initializer re-stages it at every launch, and a
project's `.serena/project.yml` cannot redirect a language server because the
workspace is deliberately untrusted. Confirm the language backend works using
real symbol queries. A separately activated project in a session is not
necessarily the next check's default project.

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

1. In the Kimi web UI, change a user-owned setting (thinking, telemetry,
   background tasks, or experimental). The save must succeed. Open a
   second terminal and confirm it landed in the volume:

   ```bash
   ./shell.sh
   cat /home/agent/.kimi-code/config.toml
   ```

2. Stop and restart the stack, then confirm the value survived and that policy
   keys were re-pinned from the rendered baseline. Provider base URLs, API keys,
   internal proxy tokens, model context ceilings, the default model, and the
   forced subagent model must still match the plan even if you edited them
   in-session — Kimi's own `/model` and `/secondary-model` commands can change
   the live file, and there is no documented way to remove that surface, which is
   why the launch-time choice is re-stamped rather than merely written once.
   `[subagent]` and `[swarm]` are policy keys too: both must come back with
   `timeout_ms = 0`, which is what keeps subagent wall-clock unlimited even
   though the UI can rewrite the file.
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
   must keep working. `container/Dockerfile` never installs `chattr` (the initializer
   talks to the kernel through `fcntl.ioctl` instead), so the binary is only here through the
   base image and could disappear on a rebase; it cannot work at all without
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
6. Confirm the selected prompt documents and the enabled add-on blocks reached the
   model rather than only the volume. The staged files are the input; what Kimi
   actually sent is recorded in the session's `profile.bind` record. Inside
   `./shell.sh`, after at least one turn of a new session:

   ```bash
   python3 - <<'PY'
   import json, pathlib
   root = pathlib.Path("/workspace")
   own = (root / "SYSTEM.md").read_text() if (root / "SYSTEM.md").is_file() else None
   staged = pathlib.Path("/home/agent/.kimi-code/SYSTEM.md")
   contract = pathlib.Path("/home/agent/.kimi-code/AGENTS.md")
   prompt = staged.read_text() if staged.is_file() else ""
   body = contract.read_text() if contract.is_file() else ""
   logs = sorted(pathlib.Path("/home/agent/.kimi-code/sessions").glob(
       "*/*/agents/main/wire.jsonl"), key=lambda p: p.stat().st_mtime)
   for line in logs[-1].open():
       record = json.loads(line)
       if record.get("type") == "profile.bind":
           sent = record["systemPrompt"]
           lines = (own or "").strip().splitlines()
           last = lines[-1].lstrip("# ").strip() if lines else ""
           print("operator SYSTEM.md present:", own is not None)
           print("staged prompt starts with the prompt file:",
                 not (own or "").strip() or prompt.startswith(own.strip("\n")))
           print("amending:", "${base_prompt}" in prompt)
           print("built-in prompt kept:", sent.startswith("You are "))
           print("placeholder consumed:", "${base_prompt}" not in sent)
           print("prompt file kept:", not last or last in sent)
           print("lane table in prompt:", "Model runtime envelope" in sent)
           print("parallel work in prompt:", "## Parallel work" in sent)
           print("usage limits in contract:", "Model usage limits" in body)
           break
   PY
   ```

   The last four lines are the panel's main-audience blocks for the prompt and the
   contract's lane-audience block, so they track the selection rather than a fixed
   expectation: an operator who switches a block off on the launch panel should see
   that line read `False` and nothing else change. With `SYSTEM.md` absent and
   everything left enabled, expect `operator SYSTEM.md present` `False`, then
   `amending`, `built-in prompt kept`, `placeholder consumed`, `lane table in
   prompt`, `parallel work in prompt`, and `usage limits in contract` all `True`. A
   `False` on `built-in prompt kept` while `amending` is `True` is a bug: Kimi
   substitutes every occurrence of the placeholder, so a second one in prose brings
   the built-in prompt in twice.

   A `SYSTEM.md` with no `${base_prompt}` is a supported replacement, not a
   misconfiguration, and `amending` reads `False` for it. Then `built-in prompt
   kept` is expected to read `False` as well, and that is the point — nothing
   supplies the working directory, the applicable `AGENTS.md` files, the skills
   listing, or the plugin sections unless the prompt names those variables
   itself. The generated blocks are the exception: they are composed after your
   text either way, and only switching them off on the panel removes them. Check
   for the rest by reading the recorded `sent` prompt before you accept a
   replacement prompt as working.

   An empty `SYSTEM.md` is a decision, not a missing file: the prompt file
   contributes nothing, and the staged prompt is then exactly the enabled blocks.
   With every block off there is nothing left to say, and since Kimi Code discards
   a prompt that is blank once trimmed, the staged file is a lone period and the
   recorded prompt is that period — `built-in prompt kept` reads `False`, which is
   what a tabula rasa looks like from here. Only an absent `SYSTEM.md` stages
   nothing at all, and that is the case where Kimi supplies its own prompt. Note
   that `usage limits in contract` can still read `True` beside it: the all-lane
   contract is a separate document with its own tier-one file, `CONTEXT.md`, and an
   emptied `SYSTEM.md` says nothing about that one.

   The context step is the operator-visible surface for both documents, and it is
   one screen of the fullscreen modal the whole launch shares: ↑/↓ focus a row,
   `Space` cycles the row under focus, `Enter` accepts the step, `Backspace` returns
   to the previous step, `Ctrl-R` resets the visible choices, and `?` opens the full
   key reference. The footer prints only the keys this step answers, so there is
   nothing to guess. Every row — a document, an add-on, a file on disk — spells its
   answer in the same mark column: `[x]` and `[ ]` are switches you hold, and `-x-`
   and `- -` are the same facts about a row no key here moves. The two documents open
   on a third answer as well, `auto`, which is why the status line under the map
   spells the focused row's state out in words rather than leaving it to the glyph.
   A block whose file is present but empty opens off, which is
   that emptiness being honoured, and moving it on is what makes the check above
   behave as though the file were absent. Neither move edits `SYSTEM.md` or
   `CONTEXT.md`, and the answer is written to `prompt-context.json` only when the
   step is accepted, so a launch you cancel with `Ctrl-C` leaves nothing behind to
   undo.

   Two properties of that modal are worth checking after any change to the flow, and
   both are visible without instrumenting anything. The window never closes between
   steps: `less` the raw transcript of a launch run on a pty and you should find
   exactly one enter and one leave for the whole interactive run, no matter how many
   screens it answered. And the rail total is forecast once, so every screen reads
   `1 of M` through `M of M` and a skipped step is never enumerated. The one honest
   exception is a total that drops by one *after* you make it drop: untick every
   module and the per-module version menu really does vanish. A total that moves for
   any other reason means the forecaster stopped reusing a real predicate and guessed
   instead.
   Everything the launch would have printed while the window held the terminal waits
   in `launch-notes.log` under the instance runtime directory, and the check that it
   arrived is simply that those lines appear after the `leave`, in order, on the
   normal screen.

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
It does write test input/output files, unlike the routine service probes.
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
  agent's mount table contains no harness file paths beyond its workspace and any
  approved project-extension snapshots.
- The quick and full service checks the launcher ran have no unexplained
  failures; every `SETUP` or
  `MANUAL` result has been resolved or explicitly recorded as unused.
- Each relevant tool family has an observed successful call with checked output.
- GitHub/private repository access and OAuth are tested separately from discovery.
- A real model response, search/fetch, browser task, Serena symbol query, and
  enabled module acceptance tests succeed.
- Record Kimi and selected module versions and which optional integrations were enabled.

This is a reproducible acceptance record, not a promise that external services,
token scopes, every future project language, or every model will remain healthy.
