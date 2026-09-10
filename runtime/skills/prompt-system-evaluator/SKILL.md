---
name: prompt-system-evaluator
description: Evaluates LLM prompt-generation and multimodal-context systems with repeatable cases and structured failure analysis
type: prompt
whenToUse: When developing LLM-based prompt generation, rewriting, multimodal context optimization, or prompt-conditioning logic
---

Do not judge prompt-system quality from one attractive example.

Maintain representative evaluation cases covering:
- normal requests;
- terse/underspecified requests;
- long context;
- conflicting context;
- multimodal inputs;
- model-specific terminology;
- negative constraints;
- adversarial or malformed input.

Separate:
- instruction-following;
- semantic preservation;
- useful enrichment;
- hallucination;
- verbosity;
- structural validity;
- downstream image/video-model usefulness.

When changing prompt logic, compare before/after outputs over the same fixture set
and record meaningful regressions.
