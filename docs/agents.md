# Using xcfs from an AI agent

## Instructions to paste

Put something like this into the agent's instruction file (`CLAUDE.md`, `AGENTS.md`, …) of a repository, adjusting
the paths:

```markdown
## Worktrees

Work that needs its own checkout goes into a worktree made with xcfs, never with plain `git worktree add` or a copy:

- Start: `xcfs new <repo> <branch> --json` and work in the `worktree` path it prints. The session id is taken from
  `$XCFS_SESSION` (or `$CLAUDE_CODE_SESSION_ID`); pass `--session ID` if neither is set.
- Check what is open: `xcfs ls --session <id> --status --json` (uncommitted files, unpushed commits).
- Done with a task: commit, then `xcfs rm <worktree> --apply --prune-branch --json`.
- Never delete a worktree or DerivedData with `rm -rf`. xcfs refuses when something would be lost; read the error and
  commit or ask, do not work around it.
- Build output (DerivedData, `build/`) never goes into a non-hidden directory inside a local Swift package.
```

## Session-end hook

When the session ends, remove every worktree it opened that holds no uncommitted work. The ones with uncommitted work
are kept and listed under `left` in the result; the command still exits 0.

For Claude Code, in `settings.json` (the hook receives the session id as JSON on stdin):

```json
{
  "hooks": {
    "SessionEnd": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "jq -r '.session_id // empty' | { read -r sid; [ -n \"$sid\" ] && command -v xcfs >/dev/null && mkdir -p \"$HOME/.local/state/xcfs\" && xcfs reap --session \"$sid\" --apply --prune-branch --json > \"$HOME/.local/state/xcfs/last-reap.json\" 2>&1; true; }"
          }
        ]
      }
    ]
  }
}
```

A session-start hook can show `last-reap.json` once, so the next session learns which worktrees were left behind and
why.

For other agents, run the same `reap` command with the agent's own session id when it exits. Agents without a
session-end hook, and sessions that crash before it runs, are covered by a timer:

```
xcfs reap --older-than 3 --apply --prune-branch
```

## Reading the results

Every command accepts `--json` and then prints exactly one JSON object with `"ok": true|false`; on failure it has an
`error` string and the exit code says why (see the README). An agent should branch on the exit code:

| code | what to do |
|---|---|
| 3 | the repository's config wants to run commands: ask the user to review them and run `xcfs trust <repo>` |
| 4 | the destination is on another volume: pick a destination next to the repository |
| 5 | uncommitted work: commit it (or ask), then retry |
| 6 | something is still running in the worktree: stop it, then retry |
| 7 | carried ignored files changed: ask before passing `--discard-ignored` |
