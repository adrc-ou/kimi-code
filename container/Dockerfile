# syntax=docker/dockerfile:1

ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.8.22@sha256:9874eb7afe5ca16c363fe80b294fe700e460df29a55532bbfea234a0f12eddb1

FROM ${UV_IMAGE} AS uv-bin

# ------------------------------------------------------------
# Build the official GitHub MCP server as a native executable.
# ------------------------------------------------------------

FROM golang:1.27-bookworm@sha256:648f440f42a0958804efb24df176f806f9d353b41f1c0627f666428e40310f6b AS github-mcp-builder

# github/github-mcp-server v1.12.1, pinned by commit and mirrored in
# dependencies.lock.json "sources".
ARG GITHUB_MCP_COMMIT=7d13a7ad6f2a17f351a6d77ce280c85ae1821f4d

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates \
       git \
    && rm -rf /var/lib/apt/lists/*

RUN git init /src/github-mcp-server \
    && git -C /src/github-mcp-server remote add origin https://github.com/github/github-mcp-server.git \
    && git -C /src/github-mcp-server fetch --depth 1 origin "${GITHUB_MCP_COMMIT}" \
    && git -C /src/github-mcp-server checkout --detach FETCH_HEAD \
    && test "$(git -C /src/github-mcp-server rev-parse HEAD)" = "${GITHUB_MCP_COMMIT}"

WORKDIR /src/github-mcp-server

RUN CGO_ENABLED=0 go build \
      -trimpath \
      -o /out/github-mcp-server \
      ./cmd/github-mcp-server


# ------------------------------------------------------------
# Agent image
# ------------------------------------------------------------

FROM node:22-bookworm-slim@sha256:83f487e0a63425e5b4d146fb5e5be574bcbe1b7b843d3ebafdd95eaf7767a7e5

ARG KIMI_CODE_VERSION
ARG KIMI_CODE_ASSET_URL
ARG KIMI_CODE_ASSET_SHA256
ARG AGENT_UID=1000
ARG AGENT_GID=1000

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       bash \
       build-essential \
       ca-certificates \
       curl \
       git \
       jq \
       python3 \
       python3-pip \
       python3-venv \
       ripgrep \
       unzip \
    && rm -rf /var/lib/apt/lists/*

# Fetch the selected official Kimi Code native release and verify its digest.
RUN test -n "${KIMI_CODE_VERSION}" \
    && test -n "${KIMI_CODE_ASSET_URL}" \
    && test -n "${KIMI_CODE_ASSET_SHA256}" \
    && curl --fail --location --retry 3 \
      --output /tmp/kimi-code.tar.gz \
      "${KIMI_CODE_ASSET_URL}" \
    && printf '%s  %s\n' \
      "${KIMI_CODE_ASSET_SHA256}" \
      /tmp/kimi-code.tar.gz \
      | sha256sum --check --strict \
    && tar -xzf /tmp/kimi-code.tar.gz -C /usr/local/bin kimi \
    && chmod 0755 /usr/local/bin/kimi \
    && rm /tmp/kimi-code.tar.gz \
    && kimi --version

# Node.js carries the local browser tooling and Serena's Python language server. Both are baked
# in at build time from the reviewed lock: the agent container's root filesystem is read-only and
# $HOME has no writable state directory, so Serena's default of resolving pyright through `uvx`
# at startup can never succeed here. Keep the pin on Serena's own PYRIGHT_VERSION, which
# runtime/serena-config.yml selects through ls_path.
WORKDIR /opt/node-tools
COPY container/package.json container/package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts \
    && ln -s /opt/node-tools/node_modules/.bin/chrome-devtools-mcp /usr/local/bin/chrome-devtools-mcp \
    && ln -s /opt/node-tools/node_modules/.bin/playwright-cli /usr/local/bin/playwright-cli \
    && ln -s /opt/node-tools/node_modules/.bin/pyright-langserver /usr/local/bin/pyright-langserver

ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
ENV PLAYWRIGHT_SKIP_BROWSER_GC=1

RUN mkdir -p /opt/ms-playwright \
    && playwright-cli install-browser --with-deps \
    && chmod -R a+rX /opt/ms-playwright

RUN chrome_path="$(find /opt/ms-playwright \
      -type f -name chrome \
      | head -n 1)" \
    && test -n "${chrome_path}" \
    && ln -s "${chrome_path}" /usr/local/bin/agent-chromium

# uv is used only to install Serena. Fetch it from Astral's versioned official
# image instead of executing a mutable remote installer script.
COPY --from=uv-bin /uv /uvx /usr/local/bin/

COPY container/serena.lock /opt/locks/serena.lock

RUN uv venv --python 3.11 /opt/serena \
    && uv pip sync --python /opt/serena/bin/python --require-hashes /opt/locks/serena.lock \
    && ln -s /opt/serena/bin/serena /usr/local/bin/serena

COPY --from=github-mcp-builder \
     /out/github-mcp-server \
     /usr/local/bin/github-mcp-server

COPY container/ensure-runtime-user.sh /usr/local/sbin/ensure-runtime-user

# Reuse existing numeric identities when host UID/GID values collide with the
# base image. Runtime execution uses the verified numeric identity.
RUN chmod 0755 /usr/local/sbin/ensure-runtime-user \
    && /usr/local/sbin/ensure-runtime-user agent "${AGENT_UID}" "${AGENT_GID}" /home/agent \
    && install -d \
      -o "${AGENT_UID}" \
      -g "${AGENT_GID}" \
      -m 700 \
      /home/agent/.kimi-code \
      /home/agent/.serena \
      /workspace

USER ${AGENT_UID}:${AGENT_GID}

ENV HOME=/home/agent
ENV KIMI_CODE_HOME=/home/agent/.kimi-code
ENV CHROME_DEVTOOLS_MCP_NO_USAGE_STATISTICS=1

# npm defaults its cache to $HOME/.npm, which cannot be created: only the paths above are
# writable under the read-only root filesystem. Point it at the .cache tmpfs instead so that
# in-session npm commands work the way XDG_CONFIG_HOME already does for other tool state.
ENV npm_config_cache=/home/agent/.cache/npm

WORKDIR /workspace

LABEL org.opencontainers.image.version="${KIMI_CODE_VERSION}"

# Install Microsoft's Playwright Agent Skill in the generic skill path.
RUN playwright-cli install --skills=agents --global

CMD ["bash"]
