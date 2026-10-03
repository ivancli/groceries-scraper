# CLAUDE.md

## Working on tickets

- **New branch, new worktree.** Never modify or commit to `main` directly. Before touching any file:
  1. Create a new, descriptively named branch off an up-to-date `main` (`feat/<ticket>-short-summary` or `fix/<ticket>-short-summary`).
  2. Check that branch out in a new worktree beside the repo, and do all edits, test runs and commits there:
     ```bash
     git fetch origin && git worktree add --no-track -b feat/<ticket>-short-summary ../groceries-scraper-<short-name> origin/main
     ```
  If a worktree for the ticket already exists (`git worktree list`), reuse it. Subagents get the worktree path explicitly and work only inside it. Remove the worktree (`git worktree remove`) only after its branch is merged, and only when asked.
- **Keep docblocks and comments concise.** Explain *why*, not *what* the code already says. One line where possible; no restating signatures, types or obvious behaviour.
