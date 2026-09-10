---
name: nvidia-gpu-forensics
description: Evidence-driven diagnosis of NVIDIA GPU memory, CUDA, kernel, architecture, precision, and throughput problems
type: prompt
whenToUse: When diagnosing CUDA errors, OOMs, GPU hangs, precision issues, memory pressure, or performance bottlenecks
---

Do not jump directly to kernel-level explanations.

Classify the failure first:

1. algorithm/model memory;
2. tensor lifetime or unnecessary copies;
3. framework allocator behavior;
4. host/device transfer;
5. synchronization;
6. kernel implementation;
7. GPU architecture limitation.

Record GPU model, driver/runtime versions, dtype, tensor dimensions, workload,
concurrency, and a minimal reproduction.

Use current NVIDIA documentation for hardware-specific facts.

Benchmark before and after any optimization and change one major variable at a time.
