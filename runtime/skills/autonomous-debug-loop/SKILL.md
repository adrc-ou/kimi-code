---
name: autonomous-debug-loop
description: Disciplined long-running debugging loop with durable experiment state that survives context compaction and session restarts
type: prompt
whenToUse: When debugging requires multiple experiments, long-running commands, background tasks, or substantial context consumption
---

Use `.agent-state` as external working memory.

STATE.md must always contain:

- objective;
- current failure;
- minimal reproduction;
- last-known-good state/commit when known;
- current evidence;
- active hypothesis;
- eliminated hypotheses;
- important files;
- next three experiments.

Append every substantive experiment to DEBUG_LEDGER.md:

## H<number>
Hypothesis:
Change:
Command:
Expected:
Observed:
Evidence:
Conclusion:
Git state:
Next:

Do not make multiple unrelated changes in one experiment.

Do not repeat an experiment without identifying what changed.

Redirect large logs into `.agent-state/logs/` and summarize them instead of
feeding entire logs repeatedly into context.

Before context compaction, update STATE.md so that a fresh agent could resume
the investigation using only repository state plus `.agent-state`.
