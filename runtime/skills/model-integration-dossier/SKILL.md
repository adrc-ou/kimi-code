---
name: model-integration-dossier
description: Builds an evidence-backed integration dossier for image, video, diffusion, transformer, VLM, or multimodal models
type: prompt
whenToUse: When integrating or evaluating a new model architecture or model family
---

Before implementing an unfamiliar model integration, establish:

- canonical repository and revision;
- model architecture;
- tokenizer/processor behavior;
- text encoders and context limits;
- image/video preprocessing;
- latent or token representation;
- VAE architecture and compression factors;
- channel and patch geometry;
- scheduler, flow, timestep or sampling convention;
- precision expectations;
- device/memory behavior;
- conditioning/context representation;
- reference inference path;
- relevant upstream dependencies.

Prefer model configuration and canonical implementation over third-party summaries.

Write significant findings and exact source locations to
`.agent-state/UPSTREAM_SOURCES.md`.
