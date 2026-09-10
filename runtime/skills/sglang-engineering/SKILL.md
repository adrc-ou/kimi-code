---
name: sglang-engineering
description: SGLang integration, serving, multimodal request, observability, reproducibility, and performance-debugging methodology
type: prompt
whenToUse: When working with SGLang serving, requests, crashes, throughput, memory, multimodal inputs, context handling, or deployment flags
---

Treat current SGLang documentation/source as authoritative for flags.

Before changing performance settings establish:

- exact SGLang revision/version;
- GPU architecture;
- model revision;
- serving command;
- tensor-parallel configuration;
- dtype/quantization;
- context and KV-cache settings;
- concurrency;
- failing or benchmark request.

For crashes or correctness failures, prioritize deterministic replay:
capture the failing request, minimize it, reproduce it, then change one
meaningful variable at a time.

Separate:
1. application/request issue;
2. framework/runtime issue;
3. allocator/memory issue;
4. kernel/hardware issue.

Record reproducible commands and measurements.
