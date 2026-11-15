# Using xcfs from an AI agent

## The contract

What an agent (or the tool around it) does, in order, and what it reads back. Every command prints one JSON object
with `--json`; the fields named here are the stable ones ([stability.md](stability.md)).

| step | command | read | when it fails |
|---|---|---|---|
| before starting agents (an orchestrator) | `xcfs pool status <repo> --json` | `summary.<repo>`: `ready` (built at the baseline), `free`, `leased`, `warming`, `next_checkout`, `next_ready_in_seconds` | — |
| get a checkout | `xcfs checkout <repo> <branch> --session ID --json` | `worktree` (work there), `warm`, `hint` | 10: no free slot (`--wait 600`, or `xcfs new`); 3: config not trusted |
| or a plain worktree | `xcfs new <repo> <branch> --session ID --json` | `worktree` | 4: another volume; 3 |
| check while editing | `xcfs check <file>... --json` | `ok`, `diagnostics`, `against` | 8: fix `diagnostics` |
| build | `xcfs build [path] --json` | `ok`, `diagnostics`, `seconds` | 8: fix `diagnostics`; 9: timed out |
| test | `xcfs test [path] [--scheme S] [--only T/C/m] --json` | `ok`, `tests`, `failures`, `failed` | 8: fix `failed` |
| finish a task | commit, then `xcfs release --prune-branch --json` (slot) or `xcfs rm <wt> --apply --prune-branch --json` | `ok` | 5: uncommitted work; 7: changed carried files; 6: in use |
| the session ends | `xcfs reap --session ID --apply --prune-branch --json` | `left` (kept: unsaved work) | never fails (exit 0) |

On every failure, `error` says what happened and `next` what to do. A refusal (5, 7) means xcfs would otherwise have
lost something: commit or ask, never force or delete by hand.

## Instructions to paste

[examples/agent-instructions](../examples/agent-instructions/README.md) has a block for any tool's instruction file
(`CLAUDE.md`, `AGENTS.md`, `GEMINI.md`, an Aider conventions file) and says which file each tool reads. A shorter one,
to adjust:

```markdown
## Checkouts and builds

Work that needs its own checkout goes through xcfs, never through plain `git worktree add` or a copy:

- Start: `xcfs checkout <repo> <branch> --json` and work in the `worktree` path it prints (a slot that is already
  built). Without a pool, `xcfs new <repo> <branch> --json` makes a plain worktree. The session id is taken from
  `$XCFS_SESSION` (or `$CLAUDE_CODE_SESSION_ID`); pass `--session ID` if neither is set.
- Build with `xcfs build --json` from inside the checkout; check single files with `xcfs check <file>... --json`;
  run tests with `xcfs test --json`; launch with `xcfs run --json`. Read `diagnostics` (file, line, column,
  severity, message) instead of scraping logs. A passing `check` means "these files compile against the last build";
  run `xcfs build` before saying everything compiles.
- See what is open: `xcfs ls --session <id> --status --json` and `xcfs pool status --json`.
- Done with a task: commit, then `xcfs release --prune-branch --json` (slot) or
  `xcfs rm <worktree> --apply --prune-branch --json` (worktree).
- Never delete a checkout, DerivedData or a build directory with `rm -rf`. xcfs refuses when something would be lost;
  read the error and commit or ask, do not work around it.
- Build output never goes into a non-hidden directory inside a local Swift package.
```

## Worktrees an agent tool creates by itself

Agent tools that isolate work in a worktree of their own — Claude Code with `claude --worktree`, a subagent with
`isolation: "worktree"`, the `EnterWorktree` tool, background sessions — can get it from xcfs instead, with no change
to how anyone works: install two hooks, and every such worktree is a pool slot that is already built (when the
repository has a free one at its baseline) or a new xcfs worktree with the dependency layer cloned in, its build
output outside the source tree and handed to the session, so the session-end hook below cleans it up.

```
xcfs hook worktree-create    # stdin: {"name", "cwd", "session_id", ...}; stdout: the worktree's path, nothing else
xcfs hook worktree-remove    # stdin: {"worktree_path", ...}; exit 0 when it is gone
```

- The branch is `worktree-<name>`, from the remote's default branch (`origin/HEAD`), as Claude Code's own default;
  `XCFS_HOOK_BASE=head` starts from the current `HEAD` instead. The repository is the one `cwd` is in (its main
  checkout, when `cwd` is inside a worktree).
- Removal is `xcfs release --prune-branch` for a slot and `xcfs rm --apply --prune-branch` for a worktree. Both refuse
  when something would be lost (uncommitted changes, changed carried files); the hook then exits non-zero and the
  tool keeps the worktree. Commits stay on their branch: the branch is deleted only when xcfs created it and it never
  got a commit of its own. A worktree xcfs did not make is removed the way `git worktree remove` would,
  never forced.
- Claude Code runs these hooks instead of its own `git worktree add`, so `.worktreeinclude` is not read: files a
  worktree needs that git ignores belong in the project's `carry` list.
- Claude Code does not run the remove hook for a subagent's worktree; those are returned by the session-end hook
  (`xcfs reap --session`) or by `xcfs reap --older-than`.

Checked with Claude Code 2.1.325: `claude -w <name>` in a repository with these hooks starts the session in the xcfs
worktree on `worktree-<name>`; the hook's `cwd` is the directory Claude Code was started in. A non-interactive run
(`claude -p`) leaves its worktree in place when it exits; the session-end hook returns it. Checked again with the
plugin (`claude -p --plugin-dir plugins/xcfs -w <name>`): the worktree came from xcfs and the session's end removed it
and its branch. When Claude Code runs the remove or session-end hook, its own processes (and anything it started)
still have their working directory in the worktree; xcfs leaves those out when it decides whether the worktree is in
use (the tool asked for the removal), and still refuses for any other process and for unsaved work.

Install for Claude Code: `xcfs hook install --apply` adds both worktree hooks and the session-end hook to
`~/.claude/settings.json` (every project); `xcfs hook install <repo> --apply` to a repository's
`.claude/settings.json` (one project). Without `--apply` it only shows what it would add. Hooks that are not xcfs's
are kept; when another tool's worktree hook is installed it refuses, since the agent tool takes a worktree from one
command only. `xcfs hook uninstall --apply` takes xcfs's hooks out again, `xcfs hook status` shows what is installed.
Or install them as a Claude Code plugin, which adds a skill with the instructions below:
`claude plugin marketplace add PatrickSCLin/xcfs` and `claude plugin install xcfs@xcfs`
([plugins/xcfs](../plugins/xcfs/README.md)); not both, since Claude Code runs every matching hook. The session-end hook
has a 60 s timeout: without one, Claude Code gives SessionEnd hooks 1.5 s in all.
What `xcfs hook install` writes is [examples/agent-hooks/claude-code-settings.json](../examples/agent-hooks/claude-code-settings.json),
which can be merged by hand as well. `xcfs` must be on the `PATH` the hooks run with. Without Claude Code, any tool or
script can call the same commands with the same JSON.

For people and agents that run `git worktree add` themselves, `xcfs adopt <worktree>` takes such a worktree under
management afterwards (cleanup, build output outside the tree), and `xcfs checkout <repo> <branch>` prints a built
slot's path, so a shell function can replace the habit:

```sh
xwt() { cd "$(xcfs checkout "$(git rev-parse --show-toplevel)" "$1" --json | jq -r .worktree)"; }
```

## Session-end hook

When the session ends, remove every worktree it opened and return every slot it leased, as long as they hold no
uncommitted work. The ones with uncommitted work
are kept and listed under `left` in the result; the command still exits 0.

For Claude Code this is the `SessionEnd` hook `xcfs hook session-end`, installed by `xcfs hook install` with the two
above. It reads the session id from the JSON on stdin, runs `xcfs reap --session <id> --apply --prune-branch`, writes
the result to `last-reap.json` in xcfs's state directory (`~/.local/state/xcfs`), and exits 0 whatever happened, so
that a session can always end.

A session-start hook can show `last-reap.json` once, so the next session learns which worktrees were left behind and
why.

For other agents, run the same `reap` command with the agent's own session id when it exits. Agents without a
session-end hook, and sessions that crash before it runs, are covered by a timer:

```
xcfs reap --older-than 3 --apply --prune-branch
```

## Reading the results

Every command accepts `--json` and then prints exactly one JSON object with `"ok": true|false`; on failure it has an
`error` string, usually a `next` sentence saying what to do (for a failed build, a specific one when the failure is a
known kind: a full disk, an out-of-date precompiled module, a simulator that does not boot, code signing, package or
CocoaPods resolution), and the exit code says why (see the README). An agent should branch on the exit code:

| code | what to do |
|---|---|
| 3 | the repository's config wants to run commands: ask the user to review them and run `xcfs trust <repo>` |
| 4 | the destination is on another volume: pick a destination next to the repository |
| 5 | uncommitted work: commit it (or ask), then retry |
| 6 | something is still running in the worktree: stop it, then retry |
| 7 | carried ignored files changed: ask before passing `--discard-ignored` |
| 8 | the build, check or tests failed: fix what `diagnostics` (or `failed`, for tests) lists |
| 9 | the build timed out: retry once; if it happens again, report it |
| 10 | every slot is taken: retry with `--wait 600`, or fall back to `xcfs new` |
| 11 | not enough disk space for a slot: report it; do not delete anything to make room |
