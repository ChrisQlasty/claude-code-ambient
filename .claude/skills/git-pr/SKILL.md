---
name: git-pr
description: Git branch/commit naming, commit and PR behaviour rules, and the PR body template for claude-code-ambient. Use before creating branches, commits, or pull requests in this repo.
---

# Git and pull requests

## Naming (Conventional Commits)

- **Branches:** `<type>/<short-kebab-slug>`, e.g. `feat/nanoleaf-driver`, `fix/restore-when-off`, `chore/ci`.
- **Commits:** `<type>(<optional scope>): <imperative summary>`, lowercase, no trailing period, ≤ 72 chars.
  Examples: `feat(config): add pydantic models`, `test(effects): cover pulse timeline`.
- **PR titles:** same format as commits, describing the whole change, e.g. `feat: project skeleton`.
- **Types:** `feat`, `fix`, `refactor`, `test`, `docs`, `chore`, `build`, `ci`, `perf`. Use `!` for breaking changes
  (`feat(config)!: ...`).
- **Scopes:** `cli`, `config`, `effects`, `engine`, `hooks`, `daemon`, `web`, or a driver name (`nanoleaf`, `nuphy`, ...).

## Behaviour

- **Never commit to `main`.** Start each task on a new branch named as above.
- **Local commits on a feature branch are fine**, in small logical steps, each passing all checks. Don't amend or
  rewrite commits that have already been pushed.
- **Never push, open a PR, merge, or delete branches unless explicitly asked** in the current conversation.
  Approval for one PR does not carry over to the next.
- **Never force-push**, skip hooks (`--no-verify`), or change git config.
- Never commit `PRs/`, `CLAUDE.local.md`, secrets, device tokens, or personal config files.
- When asked to open a PR: use `gh pr create` against `main`. Keep one phase or concern per PR, and keep the body
  concise, using this template:

  ```markdown
  <One or two sentences: what this PR does.>

  ## Purpose
  <Why it's needed: the problem, goal, or plan phase it serves.>

  ## Solution
  <How it works: key design decisions and trade-offs, as short bullets. Not a file-by-file list.>

  ## Testing
  <Commands run and manual checks, including any hardware tested.>
  ```

- At the end of a task development, report what was committed (branch and commit list) and what was left uncommitted.