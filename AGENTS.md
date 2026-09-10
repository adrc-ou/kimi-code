# Harness maintenance instructions

This repository defines the Kimi Code sandbox and policy harness.
It is not the writable development workspace used by the contained agent.

`runtime/AGENTS.md` is the authoritative runtime operating contract.

When changing NRP model, concurrency, context, or proxy behavior:
- preserve NRP Fair Use compliance;
- keep primary and subagent lanes mutually exclusive;
- keep `secondary_model.force = true`;
- verify proxy policy against the actual runtime Kimi configuration;
- never place real model credentials in the kimi-agent container.

Do not reintroduce legacy `.agent/`, `.agent-container/`, `SAFE_CONTEXT`,
or `KIMI_MODEL_*` configuration.
