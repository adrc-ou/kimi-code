# syntax=docker/dockerfile:1

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

ARG KIMI_CODE_VERSION=0.41.0
ARG CHROME_DEVTOOLS_MCP_VERSION=1.9.0
ARG PLAYWRIGHT_CLI_VERSION=0.1.19
ARG SERENA_VERSION=1.7.0

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

# Current Kimi Code + local browser tooling.
RUN npm install -g \
      "@moonshot-ai/kimi-code@${KIMI_CODE_VERSION}" \
      "chrome-devtools-mcp@${CHROME_DEVTOOLS_MCP_VERSION}" \
      "@playwright/cli@${PLAYWRIGHT_CLI_VERSION}"

# Install browser and Linux dependencies at image-build time.
RUN playwright-cli install-browser --with-deps

# uv, used only to install Serena.
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && install -m 0755 /root/.local/bin/uv /usr/local/bin/uv \
    && install -m 0755 /root/.local/bin/uvx /usr/local/bin/uvx

ENV UV_TOOL_DIR=/opt/uv-tools
ENV UV_TOOL_BIN_DIR=/usr/local/bin

RUN uv tool install \
      --python 3.11 \
      "serena-agent==${SERENA_VERSION}"

COPY --from=github-mcp-builder \
     /out/github-mcp-server \
     /usr/local/bin/github-mcp-server

# Never run Kimi as root.
RUN useradd \
      --create-home \
      --shell /bin/bash \
      --uid 1001 \
      agent \
    && install -d \
      -o agent \
      -g agent \
      -m 700 \
      /home/agent/.kimi-code \
      /home/agent/.serena \
      /workspace

COPY container/entrypoint.sh /usr/local/bin/kimi-entrypoint.sh

RUN chmod 0755 /usr/local/bin/kimi-entrypoint.sh

USER agent

ENV HOME=/home/agent
ENV KIMI_CODE_HOME=/home/agent/.kimi-code
ENV CHROME_DEVTOOLS_MCP_NO_USAGE_STATISTICS=1

WORKDIR /workspace

# Install Microsoft's Playwright Agent Skill in the generic skill path.
RUN playwright-cli install --skills=agents --global

ENTRYPOINT ["/usr/local/bin/kimi-entrypoint.sh"]

CMD ["bash"]
