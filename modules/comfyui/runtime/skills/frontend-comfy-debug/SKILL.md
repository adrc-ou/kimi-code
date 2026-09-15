---
name: frontend-comfy-debug
description: Debugs ComfyUI JavaScript frontend extensions using current source, Chrome DevTools, and token-efficient Playwright CLI verification
type: prompt
whenToUse: When implementing or debugging ComfyUI frontend JavaScript, widgets, DOM behavior, extension lifecycle, serialization, console errors, or browser regressions
---

Use current ComfyUI frontend source/API as authority.

Use Chrome DevTools MCP for exploratory diagnosis:
- console exceptions;
- network activity;
- runtime state;
- DOM/widget behavior;
- performance traces.

Use `playwright-cli` for deterministic regression verification.

Prefer:
1. reproduce;
2. inspect console/network;
3. identify lifecycle or state boundary;
4. make minimal change;
5. verify with repeatable browser steps.

Avoid using browser automation as a substitute for backend API testing.
