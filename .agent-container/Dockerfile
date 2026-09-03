FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# Generic coding-agent foundation.
# Add your project's required runtime/toolchain versions here.
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    build-essential \
    ca-certificates \
    curl \
    git \
    jq \
    nodejs \
    npm \
    python3 \
    python3-pip \
    python3-venv \
    ripgrep \
    && rm -rf /var/lib/apt/lists/*

# Never run the autonomous agent as container root.
RUN useradd \
    --create-home \
    --shell /bin/bash \
    --uid 1001 \
    agent

# Prepare the mount point.
RUN install \
    --directory \
    --owner=agent \
    --group=agent \
    --mode=700 \
    /home/agent/.kimi

USER agent
ENV HOME=/home/agent
ENV PATH="/home/agent/.local/bin:${PATH}"

# Official Kimi Code instillation path.
RUN curl -LsSf https://code.kimi.com/install.sh | bash

WORKDIR /workspace

COPY --chown=agent:agent entrypoint.sh /home/agent/entrypoint.sh

ENTRYPOINT ["/home/agent/entrypoint.sh"]
CMD ["bash"]
