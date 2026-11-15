# Instructions for agent tools

[xcfs.md](xcfs.md) is a block of instructions for any agent tool that reads an instruction file. Copy it into the file
your tool reads, in the repository (or in your user-wide file), and replace `$SESSION` with how the tool names its
session, or set `XCFS_SESSION` in the environment the tool runs commands in:

| tool | file it reads |
|---|---|
| Claude Code | `CLAUDE.md` — or install the hooks instead: `xcfs hook install --apply` (the worktrees Claude Code creates then come from xcfs, and the session id is passed by the hook) |
| Codex CLI, OpenCode, Cursor and others that follow the `AGENTS.md` convention | `AGENTS.md` |
| Gemini CLI | `GEMINI.md` |
| Aider | a conventions file passed with `--read` (for example `CONVENTIONS.md`) |

A tool that runs a command when its session ends should run
`xcfs reap --session "$SESSION" --apply --prune-branch --json` then; without one, a timer does it:
`xcfs reap --older-than 3 --apply --prune-branch`. The steps, the JSON fields and the exit codes are described in
[docs/agents.md](../../docs/agents.md).
