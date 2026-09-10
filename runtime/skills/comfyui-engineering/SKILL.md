---
name: comfyui-engineering
description: Engineering methodology for ComfyUI custom nodes, execution behavior, server APIs, frontend extensions, and workflow integration
type: prompt
whenToUse: When implementing, debugging, reviewing, or designing ComfyUI custom nodes, workflows, server integrations, or frontend extensions
---

Treat the currently installed/upstream ComfyUI source as authoritative.

Before changing a custom node:
1. inspect related upstream node implementations;
2. identify INPUT_TYPES, RETURN_TYPES, RETURN_NAMES, FUNCTION, CATEGORY and
   execution/caching behavior;
3. distinguish ComfyUI IMAGE layout from model-native layouts;
4. preserve compatibility with current execution semantics;
5. verify both backend execution and workflow serialization when relevant.

For frontend work, inspect the current ComfyUI frontend APIs rather than relying
on remembered extension APIs.

Prefer minimal adapters at ComfyUI boundaries. Keep model-specific logic outside
generic node/UI plumbing where practical.

For workflow execution debugging, prefer machine-readable ComfyUI server APIs
over visually guessing what happened.
