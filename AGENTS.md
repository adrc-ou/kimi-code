# Project agent operating contract

## Objective

Work autonomously toward the user's requested engineering outcome. Continue until the task is complete, verified, or genuinely blocked by a decision that cannot safely be inferred.

## Repository discipline

Before editing:
- inspect the relevant implementation, tests, configuration, and package metadata;
- follow the project's existing architecture and naming conventions;
- prefer modifying existing abstractions over inventing duplicate ones.

Do not rewrite unrelated code.

Never weaken tests merely to make them pass.

## Python

Follow the repository's existing pyproject.toml/tool configuration.

Prefer:
- explicit, readable Python;
- existing type-hint conventions;
- narrow exception handling;
- pathlib where the project already uses it;
- tests that verify observable behavior rather than implementation trivia.

Run the project's formatter, linter, type checker, and relevant tests when available.

## JavaScript and TypeScript

Follow the repository's package manager, package.json scripts, formatter, linter, tsconfig, and existing module conventions.

Do not introduce a new package when the repository already has an appropriate dependency or platform API.

Preserve the project's existing async, error-handling, and typing patterns.

Run the relevant lint, typecheck, test, and build commands when available.

## Verification

After meaningful changes:
1. inspect the diff;
2. run the narrowest relevant tests;
3. run broader checks when practical;
4. investigate failures rather than assuming they are unrelated.

Before declaring completion, summarize:
- files changed;
- behavioral effect;
- tests/checks run;
- remaining known risks.

## Parallel agents

The root agent counts as one possible model request.

Never arrange more than five simultaneously active background subagents.

Prefer parallel subagents for:
- codebase exploration;
- finding tests/call sites;
- architectural analysis;
- documentation research;
- independent review.

The root agent is normally the sole writer in this shared worktree.

Do not allow several agents to edit overlapping files concurrently.

## Context discipline

This installation intentionally uses a logical model context below the physical NRP context so concurrent requests remain under the permitted context threshold.

Do not attempt to bypass that limit.

Before or after major milestones, update `.agent/SESSION_STATE.md` with:
- current goal;
- important requirements;
- architecture decisions and rationale;
- files substantially changed;
- commands/tests already run and their results;
- unresolved problems;
- next concrete action.

Treat that file as durable working memory across context compactions.

## Security

The workspace is /workspace.

Do not deliberately access paths outside /workspace.

Do not inspect or print:
- API keys;
- environment variables containing credentials;
- SSH keys;
- cloud credentials;
- .env secrets.

Never upload project source, secrets, or private data to arbitrary websites.

Network access is for public technical documentation, dependency retrieval, and the configured model service.

Do not disable security controls or attempt to access the Docker host.

## Git

Never force-push.

Never delete or rewrite user commits.

Do not commit unless the user asks or the current task explicitly calls for a commit.

Use git diff and git status frequently.