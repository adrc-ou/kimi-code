# syntax=docker/dockerfile:1

ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.8.22

FROM ${UV_IMAGE} AS uv-bin

# ------------------------------------------------------------
# Build the official GitHub MCP server as a native executable.
# ------------------------------------------------------------

FROM golang:1.27-bookworm AS github-mcp-builder

ARG GITHUB_MCP_VERSION=v1.12.1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates \
       git \
    && rm -rf /var/lib/apt/lists/*

RUN git clone \
      --depth 1 \
      --branch "${GITHUB_MCP_VERSION}" \
      https://github.com/github/github-mcp-server.git \
      /src/github-mcp-server

WORKDIR /src/github-mcp-server

RUN CGO_ENABLED=0 go build \
      -trimpath \
      -o /out/github-mcp-server \
      ./cmd/github-mcp-server


# ------------------------------------------------------------
# Agent image
# ------------------------------------------------------------

FROM node:22-bookworm-slim

ARG KIMI_CODE_VERSION
ARG KIMI_CODE_ASSET_URL
ARG KIMI_CODE_ASSET_SHA256
ARG CHROME_DEVTOOLS_MCP_VERSION=1.9.0
ARG PLAYWRIGHT_CLI_VERSION=0.1.19
ARG SERENA_VERSION=1.7.0
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

# Local browser tooling still requires Node.js.
RUN npm install -g \
      "chrome-devtools-mcp@${CHROME_DEVTOOLS_MCP_VERSION}" \
      "@playwright/cli@${PLAYWRIGHT_CLI_VERSION}"

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

ENV UV_TOOL_DIR=/opt/uv-tools
ENV UV_TOOL_BIN_DIR=/usr/local/bin

RUN uv tool install \
      --python 3.11 \
      "serena-agent==${SERENA_VERSION}"

COPY --from=github-mcp-builder \
     /out/github-mcp-server \
     /usr/local/bin/github-mcp-server

# Never run Kimi as root. Replace the base image's node user so bind-mount
# ownership follows the invoking Unix user on Linux/WSL.
RUN userdel --remove node \
    && groupdel node \
    && groupadd --gid "${AGENT_GID}" agent \
    && useradd \
      --create-home \
      --shell /bin/bash \
      --uid "${AGENT_UID}" \
      --gid "${AGENT_GID}" \
      agent \
    && install -d \
      -o agent \
      -g agent \
      -m 700 \
      /home/agent/.kimi-code \
      /home/agent/.serena \
      /workspace

USER agent

ENV HOME=/home/agent
ENV KIMI_CODE_HOME=/home/agent/.kimi-code
ENV CHROME_DEVTOOLS_MCP_NO_USAGE_STATISTICS=1

WORKDIR /workspace

LABEL org.opencontainers.image.version="${KIMI_CODE_VERSION}"

# Install Microsoft's Playwright Agent Skill in the generic skill path.
RUN playwright-cli install --skills=agents --global

CMD ["bash"]
