# CLAUDE.md

## Working on tickets

- **Work in a dedicated worktree.** Never make changes in the main checkout or commit to `main`. Before touching any file, create a new worktree on a descriptively named branch off an up-to-date `main`, and do all edits, test runs and commits there:
  ```bash
  git fetch origin && git worktree add -b feat/<ticket>-short-summary ../groceries-scraper-<short-name> origin/main
  ```
  Use `feat/` or `fix/` prefixes as appropriate. If a worktree for the ticket already exists (`git worktree list`), reuse it. Subagents get the worktree path explicitly and work only inside it. Remove the worktree (`git worktree remove`) only after its branch is merged, and only when asked.
- **Keep docblocks and comments concise.** Explain *why*, not *what* the code already says. One line where possible; no restating signatures, types or obvious behaviour.
