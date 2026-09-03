#!/bin/bash

docker compose -f compose.agent.yaml run --rm \
  --service-ports \
  kimi-agent \
  kimi --model primary web \
    --network \
    --port 5494 \
    --no-open \
    --auth-token "$KIMI_WEB_TOKEN"
