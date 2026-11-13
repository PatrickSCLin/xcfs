# xcfs

Worktrees for parallel development on macOS — built for AI agents that each need their own checkout of a large Xcode
project, without paying for a full copy of the dependencies or leaving tens of gigabytes of build output behind.

```
xcfs new ~/src/app feature/x          # a git worktree + the dependency layer cloned in (APFS clonefile)
cd ~/src/app-wt-feature-x && …        # build, test, commit
xcfs rm ~/src/app-wt-feature-x --apply  # the worktree and every cache it owns, deleted in the background
```

`xcfs` is the name of the tool, not a file system: it uses git worktrees and APFS clones, needs no root, installs no
kernel extension and does not modify Xcode.

**Status: v0.1.** A single Python 3 file (standard library only, 3.9 or later) that manages the worktree lifecycle:
create, seed, register, reclaim. The planned next stages — a pool of pre-built worktrees and a persistent build
session — are described in [docs/design.md](docs/design.md); they are not implemented yet.

## Why

Running several agents on one repository means several checkouts. With an Xcode project each checkout normally brings:

- **its own dependency layer** (`.spm_cache`, `Pods`, `node_modules`, vendored SDKs) — gigabytes per copy;
- **its own DerivedData**, which is tied to the checkout's absolute path and is rarely cleaned up;
- **cleanup scripts that guess**: by path prefix, by directory mtime, by "is any build running anywhere".

`xcfs` keeps a registry of what it created, clones the dependency layer instead of copying it, and only ever deletes
what it can prove is its own and holds no unsaved work.

Measured on a large app repository:

| | xcfs |
|---|---|
| new worktree with a 1.3 GB / 6,761-file dependency layer | 1.6 s, 11 MB more on disk |
| new worktree with a 5.1 GB / 49,075-file dependency layer | 10.7–11.6 s (almost all of it creating the clones) |
| new worktree with a 300 MB dependency layer (test suite) | 72–132 KB more on disk |
| remove a worktree | 0.56 s, the delete runs in the background (a cleanup script that sized all of DerivedData on every removal took 65 s on the same repository) |

## Requirements

- macOS on APFS (clonefile), git, `python3` 3.9 or later (the one that ships with the Xcode command-line tools works).
- The repository and its worktrees must be on one APFS volume; `xcfs` refuses rather than silently copying in full.

## Install

```
install -m 755 bin/xcfs ~/.local/bin/xcfs    # any directory on PATH
xcfs --version
```

A Homebrew tap is planned.

## Commands

| command | what it does |
|---|---|
| `xcfs new <repo> <branch> [dest] [--from REF] [--session ID]` | Create a worktree (and the branch, if it does not exist), clone the config's `seed` directories into it, copy its `carry` files, run its `afterSeed` commands. Default destination: `<repo>-wt-<branch>` next to the repository. Fails without leaving anything behind. |
| `xcfs rm <worktree> [--apply] [--discard-ignored] [--prune-branch]` | Remove a worktree and the global DerivedData that belongs to it. Refuses if anything would be lost. |
| `xcfs reap (--session ID \| --older-than DAYS \| --all) [--apply] [--prune-branch]` | Remove every *clean* worktree of a session (or of that age); report the ones it had to leave. |
| `xcfs adopt <worktree> [--session ID \| --release]` | Hand a worktree to another session, take one made with plain `git worktree add` under management, or exempt one from `reap`. |
| `xcfs gc [--apply] [--foreign] [--stale-days N]` | Clean up after interrupted runs: registry rows that point nowhere, half-created worktrees, unfinished background deletes. `--foreign` also reports (and with `--apply` removes) orphaned or stale buckets in global DerivedData that `xcfs` did not create. |
| `xcfs ls [--session ID] [--status] [--sizes]` | The worktrees `xcfs` manages; `--status` adds uncommitted files, unpushed commits and age. |
| `xcfs doctor [path]` | Environment and hygiene checks (see below). |
| `xcfs trust <repo>` | Approve the commands a repository's config wants to run. |

Every command takes `--json` and then prints exactly one JSON object. **Nothing is deleted without `--apply`**;
without it every destructive command is a dry run that says what it would do.

### Exit codes

| code | meaning |
|---|---|
| 0 | ok |
| 1 | error |
| 2 | usage error (unknown command or flag, missing argument, `reap` without a selector) |
| 3 | the config wants to run commands that have not been approved with `xcfs trust` |
| 4 | source and destination are not on one APFS volume |
| 5 | uncommitted work in the worktree |
| 6 | the worktree is in use (a process has its cwd in it or its path on its command line) |
| 7 | ignored files that were carried into the worktree have changed and exist nowhere else |

## Configuration

What differs between projects lives in a config, never in the tool. `xcfs` reads `<repo>/.xcfs.json`, else
`$XDG_CONFIG_HOME/xcfs/<repo directory name>.json` (default `~/.config/xcfs/`), so a repository you cannot add files to
can still have one. A repository without a config works too: it just gets a plain worktree.

```json
{
  "version": 1,
  "seed": [".spm_cache", "Pods"],
  "carry": ["local.xcconfig"],
  "afterSeed": ["mkdir -p build && ln -sfn \"$PWD/.spm_cache\" build/SourcePackages"],
  "pinDerivedData": true
}
```

| key | meaning |
|---|---|
| `seed` | Directories cloned into a new worktree with APFS clonefile: they share disk blocks with the source until either side changes them. Absolute paths that SwiftPM records in `workspace-state.json` are rewritten to the new location. A seed directory that git does not ignore makes the worktree dirty from birth; `xcfs` warns about it. |
| `carry` | Ignored files (local settings, secrets) copied into a new worktree. `xcfs` remembers their hash, so a later change to them — invisible to `git status` — blocks removal (exit 7) unless `--discard-ignored` is given. |
| `afterSeed` | Shell commands run in the new worktree (environment: `XCFS_WORKTREE`, `XCFS_SOURCE`). A config comes from the repository, so these run only after `xcfs trust <repo>`; changing them asks again. If one fails, the whole `new` is rolled back. |
| `pinDerivedData` | Make Xcode.app keep this worktree's DerivedData inside the worktree (for `*.xcworkspace` at the worktree root). |

[examples/generic-sample](examples/generic-sample) is a small XcodeGen + CocoaPods workspace with a config.

### Environment

| variable | default | |
|---|---|---|
| `XCFS_STATE` | `$XDG_STATE_HOME/xcfs`, else `~/.local/state/xcfs` | registry, locks, approvals |
| `XCFS_DD_ROOT` | `~/Library/Developer/Xcode/DerivedData` | where `rm` and `gc` look for global DerivedData |
| `XCFS_SESSION` | — | session id for `new`, `reap`, `adopt` when `--session` is not given |
| `CLAUDE_CODE_SESSION_ID` | — | used when neither of the above is set |

## Using it from an agent

Each agent session opens worktrees under its session id and closes them all when it ends:

```
xcfs new <repo> <branch> --session "$SESSION" --json        # start of a task
xcfs ls --session "$SESSION" --status --json                # what is still open, and is anything unsaved
xcfs rm <worktree> --apply --prune-branch --json            # a task is done
xcfs reap --session "$SESSION" --apply --prune-branch --json  # the session ends (e.g. from a hook)
```

`reap` removes every worktree of the session that holds no uncommitted work. Commits stay on their branches; with
`--prune-branch`, a branch that `xcfs` itself created and that never got a commit of its own is deleted as well.
Worktrees with uncommitted work are left in place and listed under `left`, and the exit code is still 0, so a hook does
not fail because of them. For agents without a session-end hook, run `xcfs reap --older-than 3 --apply --prune-branch`
on a timer.

[docs/agents.md](docs/agents.md) has a snippet to paste into an agent's instructions and a session-end hook example.

## What `xcfs` refuses to do

Each of these is covered by a test in [tests/xcfs_test.py](tests/xcfs_test.py):

- **Remove uncommitted work.** Modified or untracked files block `rm` and `reap` (exit 5). It never stashes or forces.
- **Lose commits.** A detached HEAD with commits on no branch is kept as `refs/xcfs/rescue/<name>-<time>` first.
- **Lose ignored files it carried in** whose content changed (exit 7).
- **Remove a worktree that is in use** — judged per worktree (a process with its cwd inside it, or its path on the
  command line), not by "some build is running somewhere on the machine".
- **Touch someone else's caches.** `rm` deletes only DerivedData whose recorded workspace path is inside the worktree,
  matched on a path boundary (`app-wt-foo` never owns `app-wt-foobar`). `gc` touches global DerivedData only with
  `--foreign`, and judges staleness by the last build, not by the bucket directory's mtime (which never moves).
- **Copy instead of clone.** Different volume or not APFS → exit 4.
- **Run repository commands without approval** (exit 3).
- **Leave half-made state.** A failed `new` removes the worktree, the branch it created and its registry row; one that
  was killed is cleaned up by `gc --apply`.
- **Race with itself or with git.** Registry changes happen under a file lock and are written atomically; `git worktree
  add` / `prune` on one repository are serialized (git's own bookkeeping fails now and then under concurrency).
- **Confuse logical and physical paths.** Every path is resolved through symlinks first, as Xcode and git record them.
- **Remove the main checkout** — only worktrees.

## Hygiene check

`xcfs doctor <path>` checks that git is available, that the state directory and the path are on APFS, that the
registry matches the disk, and that no **non-hidden build output sits inside a local Swift package directory**
(`build/`, `DerivedData*` next to a `Package.swift`). Xcode walks every non-hidden file under a local package
directory each time it resolves the package graph, and hidden directories are skipped. On a large app, one stray
DerivedData of 105,000 files inside a local package made package resolution take 24 s instead of 12 s on every build;
the same files under a hidden directory cost nothing. Put such output in a directory whose name starts with `.`, or
outside the package.

## Tests

```
python3 tests/xcfs_test.py                 # 27 end-to-end tests, ~1.5 min; --only t03,t07 to pick
/usr/bin/python3 tests/xcfs_test.py --python /usr/bin/python3
tests/sample_e2e.sh                        # the sample project: new, xcodebuild in the worktree, reap
```

The tests create their own throw-away repositories in a temporary directory whose path contains a space and non-ASCII
characters, with their own state directory and DerivedData root; nothing outside it is touched. One test mounts a small
disk image to check the different-volume refusal. `sample_e2e.sh` needs Xcode with an iOS Simulator SDK, `xcodegen`
and CocoaPods.

## Design and roadmap

See [docs/design.md](docs/design.md): why worktrees and not a file system, the layers a project declares, the
planned pool of pre-built worktrees and persistent build session, and the measurements behind them.
