# xcfs

**Several AI agents developing one iOS app at the same time, without waiting on cold builds and without filling the
disk.** Each agent gets its own checkout of the Xcode project: a slot of a pool that is already built, an incremental
build session, a one-file compile check, its own simulator, and nothing left behind when it is done.

```
xcfs checkout ~/src/app feature/x      # lease a slot that is already built at the baseline; prints its path
xcfs build                             # incremental build; diagnostics as JSON with --json
xcfs check Sources/Feature/View.swift  # compile-check one file
xcfs test && xcfs run                  # on the slot's own simulator
xcfs release                           # back to the pool; rebuilt at the baseline in the background
```

With Claude Code, two hooks make every worktree it creates (`claude --worktree`, subagents with worktree isolation)
an xcfs slot, with no change to how anyone works: see [docs/agents.md](docs/agents.md).

`xcfs` is the name of the tool, not a file system: it uses git worktrees and APFS clones, needs no root, installs no
kernel extension and does not modify Xcode.

**Status: v0.2.** Python 3 (standard library only, 3.9 or later). Everything below is implemented and tested; see
[docs/design.md](docs/design.md) for what is measured, what is planned and what is not verified.

## What it is worth

Three agents start at once on [Ice Cubes](https://github.com/Dimillian/IceCubesApp), each with a real task judged by
a build and the package's tests (add a function, get a compile error, fix it, add a unit test, run the tests, build the
app). Plain: `git worktree add` and `xcodebuild`, as an agent would. Ranges over clean repeated runs
([details](docs/benchmark.md#three-agents-real-tasks-at-the-same-time)):

| | plain worktrees | xcfs |
|---|---|---|
| the next three agents on the same pool (warm-up of the released slots included) | 284–302 s | **111–121 s** |
| the first three agents on a fresh pool, warm-up included | 284–302 s | 408–419 s — **slower** |
| memory while the agents work | 24–25 GB | 8–12 GB |
| left on disk afterwards | 1.5 GB of DerivedData per worktree | nothing |
| tests run at the same time | one simulator for all; a test run was killed by another's once in six | one simulator per checkout |

The pool is warmed once and reused: from the second batch of agents on, xcfs is 2.3–2.7× faster; the very first batch on a
fresh pool pays for the warm-up (280–301 s for three slots) and is slower than plain worktrees. On four more apps
xcfs was not developed on, one agent's round of work (checkout, build, five edits each checked, a final build) took
3–9× less time with the default configuration
([zero-configuration table](docs/benchmark.md#zero-configuration-on-projects-xcfs-was-not-developed-on)).

**Where it does not help much:**

- **One checkout, day to day.** Without parallel checkouts there is no pool to reuse; what remains is the build
  session (a no-op build 5.9 → 0.9 s, a small edit 8 → 3–4 s on Ice Cubes) and `xcfs check`.
- **CI.** Fresh machines and one build per job: no pool survives between jobs, and remote build caches are out of
  scope ([docs/build-systems.md](docs/build-systems.md)).
- **The first batch on a fresh pool**, as above.
- **Bazel projects:** incremental builds are Bazel's own; xcfs helps with the wait for a checkout and with cleanup.

**Optional acceleration: an experimental Swift driver.** On Xcode 27.x (Swift 6.4), xcfs builds a patched
swift-driver in the background and uses it once it passes a correctness self-test: after a public declaration is
added to a widely imported module, it recompiles only the files that use what changed (Ice Cubes 25 → 6 s, Wikipedia
35 → 15 s). It is on by default where it applies and off everywhere else; `"swiftDriver": "native"` turns it off. In the
three-agent tasks it made the next batches faster (111–121 vs 138–151 s) and the fresh pool's warm-up slower (408–419
vs 354–376 s, warm-up included). Incremental builds with it are checked against clean builds on real apps
([oracle](docs/benchmark.md#is-an-incremental-build-the-same-as-a-clean-one)). See
[its notes](lib/xcfs/adapters/swiftdriver/README.md) and [docs/incremental-swift.md](docs/incremental-swift.md).

## Why

Running several agents on one repository means several checkouts. With an Xcode project each checkout brings its own
dependency layer (gigabytes), its own DerivedData bound to the checkout's absolute path, a cold build (minutes on a
large app), and an `xcodebuild` fixed overhead on every incremental build (≈35 s before the first compile task on a
large app). Cleanup scripts then guess — by path prefix, by directory mtime, by "is any build running anywhere".

`xcfs` addresses each part:

| | how |
|---|---|
| dependency layer | cloned with APFS clonefile (shares blocks), from a snapshot that matches the lock files |
| cold builds | a **pool** of slots at fixed paths, kept built at the baseline; handing one out costs only the difference |
| fixed overhead | a **persistent build session** that keeps the build service and the build description alive |
| build output | outside the source tree, one directory per checkout and configuration, removed with the checkout |
| cleanup | a registry of what xcfs created; nothing is deleted that holds unsaved work or is not provably its own |

Measured on a large app (≈1,800 targets) with prototypes of the same mechanisms: a no-op build 34–36 s → ≈4 s, a
function-body edit 37–39 s → 6–7 s, the first build of a new checkout ≈285 s → 4–50 s from an up-to-date slot. On the
small [sample](examples/generic-sample): cold build 9 s, no-op 0.15 s, an edit 0.9 s, a single-file check 0.5 s
through the session; a slot handed out in 0.3 s whose first build takes 1.2 s.

## Requirements

- macOS on APFS, git, `python3` 3.9 or later (the one in the Xcode command-line tools works).
- Xcode for the Xcode adapter. The persistent session uses the `swbuild` tool inside Xcode; see
  [Build session](#build-session).
- A repository, its worktrees and its pool on one APFS volume; `xcfs` refuses rather than silently copying in full.

## Install

```
git clone <this repository> ~/src/xcfs
ln -s ~/src/xcfs/bin/xcfs ~/.local/bin/xcfs     # any directory on PATH
xcfs --version
```

`bin/xcfs` finds its code in `../lib` (or `../libexec`). To install one file instead (a dotfiles manager, a package
manager), build it: `scripts/build-single-file.sh` writes `dist/xcfs`, the whole tool as a Python zip application.
A Homebrew tap is planned.

What runs on its own: on the first build with an Xcode of the Swift 6.4 line, xcfs builds a patched
[swift-driver](https://github.com/swiftlang/swift-driver) in the background (`git clone` of the release tag — for the
releases the patch was made on, only if the tag points at the known commit; `git apply`; `swift build`; a correctness self-test; one to two
minutes, about 400 MB in `~/Library/Caches/xcfs/swift-driver`, plus the per-module digests it keeps there, about
1 GB after the benchmarks below), and uses it for Swift compilation once the self-test
has passed. Until then, and whenever anything does not fit, builds use Xcode's own driver. `"swiftDriver": "native"`
in a configuration (or `XCFS_SWIFT_DRIVER=native`) turns it off; `xcfs doctor` shows its state. See
[its notes](lib/xcfs/adapters/swiftdriver/README.md).

## Commands

| command | what it does |
|---|---|
| **worktrees** | |
| `xcfs new <repo> <branch> [dest] [--from REF] [--session ID]` | A worktree of the repository set (and the branch, if it does not exist), with the dependency layer cloned in, carried files copied, `afterSeed` commands run. Fails without leaving anything behind. |
| `xcfs rm <worktree> [--apply] [--discard-ignored] [--prune-branch]` | Remove a worktree with its build output, global DerivedData and simulator. Refuses if anything would be lost. |
| `xcfs reap (--session ID \| --older-than DAYS \| --all) [--apply] [--prune-branch]` | Remove the clean worktrees and return the clean slots of a session (or of that age); report the rest. |
| `xcfs adopt <worktree> [--session ID \| --release]` | Hand a worktree to a session, take a plain `git worktree add` under management, or exempt one from `reap`. |
| `xcfs ls [--session ID] [--status] [--sizes]` | The worktrees xcfs manages. |
| **pool** | |
| `xcfs checkout <repo> <branch> [--from REF] [--session ID] [--wait S] [--build]` | Lease the best free slot (built at the current baseline with the current toolchain first; a new one if the pool is not full), switch it to the branch, print its path. Exit 10 when every slot is taken. |
| `xcfs release [path] [--prune-branch] [--discard-ignored] [--no-warm]` | Return the slot: refuses if anything would be lost, goes back to the baseline, rebuilds there in the background. |
| `xcfs pool status [repo]` | Slots, their state, leases (stale ones flagged), whether they are built at the current baseline. |
| `xcfs pool warm <repo> [--slots N] [--slot PATH] [--clean] [--fetch] [--background]` | Create missing slots; bring idle ones to the baseline and build them. |
| `xcfs pool resize <repo> N` | Change the number of slots; idle extras are retired now, leased ones when returned. |
| **building** (in a worktree, a slot, or any checkout) | |
| `xcfs build [path] [--config NAME] [--toolchain NAME] [--timeout S]` | Incremental build. |
| `xcfs check <file>... [--config NAME]` | Compile-check these files (see [Build session](#build-session) for what a pass means). |
| `xcfs test [path] [--config NAME] [--scheme NAME] [--only TARGET/CLASS[/METHOD]]...` | Run tests on the checkout's own simulator; `--scheme` names another scheme (a package's tests). With the session driver the session builds the tests (only what changed since the last build) and xcodebuild runs them without building. |
| `xcfs run [path] [--config NAME]` | Build, install on the checkout's simulator, launch. |
| **agent tools** | |
| `xcfs hook worktree-create \| worktree-remove` | For a tool's own worktree hooks (Claude Code's `WorktreeCreate`/`WorktreeRemove`): JSON on stdin; create prints the path of a built slot or a new xcfs worktree. See [docs/agents.md](docs/agents.md#worktrees-an-agent-tool-creates-by-itself). |
| **maintenance** | |
| `xcfs gc [--apply] [--foreign] [--stale-days N]` | Repair interrupted operations, return clean slots whose lease went stale, drop unused dependency snapshots and the build output of vanished checkouts. `--foreign` also reports (with `--apply`, removes) orphaned or stale global DerivedData xcfs did not create. |
| `xcfs doctor [path] [--session-check]` | Environment, hygiene and toolchain checks; `--session-check` runs the build-session regression check. |
| `xcfs init [repo] [--write] [--force]` | Suggest a `.xcfs.json` from what the adapters detect. |
| `xcfs trust <repo>` | Approve the commands a repository's config runs. |

Every command takes `--json` and then prints exactly one JSON object. **Nothing is deleted without `--apply`**
(`release` and `pool resize` only return or retire slots, which hold nothing that is not rebuildable; they refuse
when a slot holds unsaved work).

### Exit codes

| code | meaning |
|---|---|
| 0 | ok |
| 1 | error |
| 2 | usage error, or an operation the toolchain does not support |
| 3 | the config wants to run commands that have not been approved with `xcfs trust` |
| 4 | source and destination are not on one APFS volume |
| 5 | uncommitted work |
| 6 | in use (a process has its cwd inside, or the path on its command line) |
| 7 | ignored files carried into the checkout have changed and exist nowhere else |
| 8 | the build, the check or the tests failed |
| 9 | timed out |
| 10 | no free slot (wait with `--wait`, release one, or grow the pool) |
| 11 | not enough free disk space for a slot |

## Configuration

What differs between projects lives in a config, never in the tool. `xcfs` reads `<repo>/.xcfs.json`, else
`$XDG_CONFIG_HOME/xcfs/<repo directory name>.json` (default `~/.config/xcfs/`). A repository without a config works
too: a plain worktree, and the toolchain is detected.

```json
{
  "version": 1,
  "seed": ["Pods", ".spm_cache"],
  "seedKey": ["Podfile.lock", "Package.resolved"],
  "carry": ["local.xcconfig"],
  "afterSeed": ["mkdir -p build && ln -sfn \"$PWD/.spm_cache\" build/SourcePackages"],
  "repos": [
    { "path": ".", "baseline": { "branch": "main" } },
    { "path": "modules/core", "baseline": { "command": "scripts/pinned-version core" } }
  ],
  "prepare": "xcodegen generate",
  "pool": { "slots": 2 },
  "toolchains": [
    {
      "adapter": "xcode",
      "workspace": "App.xcworkspace",
      "scheme": "App",
      "configurations": {
        "sim-debug": { "configuration": "Debug", "destination": "generic/platform=iOS Simulator" }
      },
      "buildSettings": { "CODE_SIGNING_ALLOWED": "NO" },
      "driver": "auto"
    }
  ]
}
```

| key | meaning |
|---|---|
| `seed` | Directories cloned into every new worktree or slot with APFS clonefile. Absolute paths SwiftPM records in `workspace-state.json` are rewritten. A seed that git does not ignore makes the checkout dirty from birth; xcfs warns. |
| `seedKey` | Lock files whose content keys a snapshot of the seed directories. A checkout of a revision whose lock files differ from the source checkout's gets the matching snapshot, or a warning when there is none. |
| `carry` | Ignored files (local settings, secrets) copied in. A later change to them — invisible to `git status` — blocks removal (exit 7) unless `--discard-ignored`. |
| `afterSeed` | Shell commands run in a new checkout (`XCFS_WORKTREE`, `XCFS_SOURCE` set). Failure rolls the whole creation back. |
| `repos` | The repository set: nested repositories (git-ignored by the primary one) checked out together in every worktree and slot. `baseline` is where an idle slot sits: `{"branch"}` (the remote-tracking branch if there is one), `{"ref"}`, or `{"command"}` printing a ref. Default: the primary repository's default branch. |
| `prepare` | Shell command run after a slot switched revision, before a build. |
| `pool` | `{"slots": N}`: the pool size for this project (overridden by `xcfs pool resize`). |
| `toolchains` | The adapters this project uses, each with its own keys. Without it, built-in adapters detect the checkout. |

Commands in `afterSeed`, `prepare` and baseline commands come from the repository, so they run only after
`xcfs trust <repo>`; changing them asks again.

Xcode toolchain keys: one of `workspace` / `project` / `package`; `scheme` (default: the scheme named like the
container); `target` (default: from the scheme); `configurations` (name → `configuration`, `destination`, optional
`testDestination`, `buildSettings`); `default`; `warm` (configurations a slot keeps built; default the first);
`simulator` (base device name or UDID; default the newest iPhone); `buildSettings`; `packagesDir`
(`-clonedSourcePackagesDirPath`, relative); `xcodebuildArgs`; `reexportWhenChanged` (extra files that change the
PIF); `driver` (`auto` by default, `xcodebuild`, `session`); `pinDerivedData`; `sharedXcodeCaches` (default true: builds use the
module, compilation and SDK stat caches Xcode shares between projects); `skipScripts` (session driver only; `auto` by default: shell-script phases that only run a linter — no declared
outputs, `swiftlint` or `swiftformat --lint` without a fix mode, nothing written to a file, every other line plain
shell plumbing — are left out, since they cannot change what is built; a list names exactly the phases to leave out,
for instance a project's localization sync, and `[]` keeps them all; xcfs removes them from a copy of the project
description, the project itself is untouched, and each build's JSON lists what it skipped and why); `swiftDriver` (`auto` by default: on an Xcode of the Swift 6.4 line, build with a patched Swift driver that recompiles only the files a
changed interface really affects, built in the background on first use — see [its notes](lib/xcfs/adapters/swiftdriver/README.md);
`native` turns it off); `compileCache` (`off` by default; experimental: Xcode's compilation cache shared by every
checkout, with each checkout's own paths kept out of its keys so a slot replays what another compiled. `"dependencies"`
keeps the checkout's sources at their real paths and shares what lies outside them (packages, Clang modules);
`"all"` maps the whole checkout, so every compile job is shared, but `#filePath` at run time reads `/^checkout/…`,
which breaks tests that find files next to their source (fixtures, snapshot tests), and lldb needs a source map,
which each build's JSON gives (`lldb_source_map`); xcfs maps test failure locations back. Either uses Xcode's own Swift driver, and a fresh pool then warms one slot first and the others from the cache.
Measured on Ice Cubes with `"all"`: a second slot's warm-up 84–88 → 35–38 s; a new pool the next day 312–321 → 173 s
and a third of the memory; three slots following the baseline 15–20 % faster; incremental builds within 0–4 s of no
cache; but three agents' first batch on a fresh pool still 352–355 s against 284–302 s for plain worktrees. See
[docs/benchmark.md](docs/benchmark.md#a-shared-compilation-cache)).

Bazel toolchain keys: `build`, `test`, `run`, `warm` (shell commands run in the checkout; `--config` appends
arguments), `env`, `outputUserRoot`, `stopServerOnRelease`. The adapter owns each checkout's Bazel output base — kept
by Bazel outside the checkout, at `<output user root>/<md5 of the workspace path>`, with a resident server — and
removes it, server stopped, with the checkout. A slot at a fixed path keeps its output base, and so its incremental
state, from one lease to the next; returning a slot keeps its server (and the analysis cache in it) unless
`stopServerOnRelease` is set — Bazel stops an idle server by itself after three hours.

[examples/generic-sample](examples/generic-sample) is a small XcodeGen + CocoaPods workspace with a config.

### Machine settings and environment

`$XDG_CONFIG_HOME/xcfs/settings.json`:

| setting | default | |
|---|---|---|
| `poolRoot` | `~/Library/Developer/xcfs/pools` | one `<repo>-<hash>.noindex` directory per project; must be on the repositories' volume |
| `slots` | 2 | default pool size per project; each slot holds one full build (several GB on a large app) |
| `slotDiskGB` | 8 | free space required before a slot is created or warmed (exit 11 otherwise) |
| `leaseHours` | 24 | a leased slot idle this long is flagged; `gc --apply` returns it if clean |
| `warmOnRelease` | true | rebuild a returned slot at the baseline in the background |
| `buildTimeout` | 3600 | seconds before a build is killed (exit 9) |
| `warmJobs` | 0 | slots one warm-up builds side by side; 0 means one per 16 GB of memory (a 64 GB machine warms 3 at a time) |

| variable | |
|---|---|
| `XCFS_STATE` | registry, locks, approvals (default `$XDG_STATE_HOME/xcfs`, else `~/.local/state/xcfs`) |
| `XCFS_CACHE` | build output of worktrees and checkouts, dependency snapshots (default `~/Library/Caches/xcfs`) |
| `XCFS_POOL_ROOT` | overrides `poolRoot` |
| `XCFS_DD_ROOT` | where `rm` and `gc` look for global DerivedData (default `~/Library/Developer/Xcode/DerivedData`) |
| `XCFS_DRIVER` | overrides the Xcode `driver` (`xcodebuild`, `session`, `auto`) |
| `XCFS_SESSION`, `CLAUDE_CODE_SESSION_ID` | session id when `--session` is not given |

## The pool

A slot is the repository set checked out **detached at its baseline** at a fixed path, with the dependency layer
seeded and the build done. Build state is bound to absolute paths, so a slot never moves.

- `xcfs checkout` leases a slot and switches it to the branch (created from the baseline, or from `--from`). Nested
  repositories switch too when they have the branch, and stay at their baseline otherwise. A branch can be checked
  out in one place at a time; xcfs says where it is.
- After every switch the adapters clear what the switch invalidates. For Xcode that is the explicit precompiled
  modules, which otherwise fail the build with "has been modified since the module file was built" after a binary
  dependency changed — and retrying does not heal it.
- `xcfs release` refuses when anything would be lost, keeps detached commits as `refs/xcfs/rescue/…`, switches back to
  the baseline, stops the slot's session and simulator, and warms it again in the background.
- `xcfs pool warm` (run it from a timer, or let releases do it) brings idle slots to the current baseline — after a
  fetch with `--fetch`; a failed fetch keeps the slot as it is, marked behind — and builds them. One warm-up per
  project at a time, at a lower priority, and it waits while an agent's build runs in another slot.
- A slot built with another toolchain version is treated as cold: its old output is discarded before the next build.
- Git submodules come along in every worktree and slot, cloned from the source checkout's copies (no network, objects
  hard-linked), and follow every switch. A submodule commit that exists only in a checkout blocks its removal.
- Pool directories are named `.noindex` (no Spotlight) and excluded from Time Machine.

## Build session

By default (`"driver": "auto"`) `xcfs build` uses a build session when the installed Xcode passes the regression check
below, and `xcodebuild` otherwise; `"driver": "xcodebuild"` or `"session"` forces one. In a session the Xcode adapter keeps one
`swbuild` process — the build service client inside Xcode — alive per checkout, reuses the build description while
the project definition is unchanged, and sends `buildFiles` requests for `xcfs check`. On a large app this is the
difference between ≈35 s and ≈4 s for a no-op build.

Apple positions `swbuild` as a debugging tool without a stability promise, so a session is used only after a
regression check has passed on the installed Xcode version: `xcfs doctor --session-check` (or automatically on the
first build with `auto` or `session`) builds a throw-away Swift package through it — cold, no-op, an edit, a compile error
and its location, `buildFiles`, recovery. If it fails, or `swbuild` is missing, builds go through `xcodebuild` and
nothing else changes. Nothing from Xcode is redistributed.

What the session takes care of:

- The PIF (the project description the build service reads) is exported again when the file list, a project or
  package definition, or the toolchain changed — a new file in a synchronized folder included — and cached otherwise.
- Requests come from the scheme (or the target named like it), honoring its implicit-dependency setting; each session
  has its own request files and its own DerivedData (`xcodebuild` and the session would each rebuild what the other
  built).
- `swbuild` answers a command it cannot run with a line without a newline and then hangs. xcfs reads its output in raw
  chunks, fails the build on such an answer, and kills, restarts and retries once on a timeout.
- A session quits after 30 idle minutes and stops with its checkout.

**What `xcfs check` means.** With the session, it compiles only the given files against the modules of the last
build of that checkout: upstream modules are not rebuilt, so a pass means "these files compile against what was last
built", not "the app builds". The result says so in `against`. With `xcodebuild` it is a full build reported for the
given files. Use `xcfs build` before concluding that everything compiles.

## Using it from an agent

```
xcfs checkout <repo> <branch> --session "$SESSION" --json   # or: xcfs new <repo> <branch> for a plain worktree
xcfs build --json / xcfs check <files> --json / xcfs test --json
xcfs release --prune-branch --json                          # done with the task
xcfs reap --session "$SESSION" --apply --prune-branch --json  # the session ends (from a hook)
```

`reap` removes the session's clean worktrees and returns its clean slots; commits stay on their branches, and with
`--prune-branch` a branch xcfs created that never got a commit is deleted. Anything with uncommitted work is left and
listed under `left`, and the exit code stays 0 so a hook does not fail. [docs/agents.md](docs/agents.md) has
instructions to paste into an agent's instruction file and a session-end hook.

## What `xcfs` refuses to do

Each of these is covered by a test in [tests/xcfs_test.py](tests/xcfs_test.py):

- **Remove or switch away from uncommitted work** — in any repository of the set (exit 5). It never stashes or forces.
- **Lose commits.** A detached HEAD with commits on no branch is kept under `refs/xcfs/rescue/` first; `--prune-branch`
  deletes only a branch xcfs created that holds nothing of its own.
- **Lose ignored files it carried in** whose content changed (exit 7).
- **Remove something in use** — judged per checkout, after stopping the checkout's own session and simulator; never
  "some build is running somewhere on the machine".
- **Touch someone else's caches.** Global DerivedData is removed only when its recorded workspace is inside the
  checkout, matched on a path boundary; anything else only with `gc --foreign --apply`. Staleness is judged by the
  last build, not by a directory's mtime.
- **Copy instead of clone** (exit 4), or **run repository commands without approval** (exit 3).
- **Leave half-made state.** A failed `new` or slot creation is rolled back; one that was killed is repaired by
  `gc --apply`.
- **Race with itself or with git.** Registry changes happen under a file lock and are written atomically; git
  worktree operations are serialized per repository; one build at a time per checkout; parallel `checkout` calls
  never get the same slot.
- **Remove a main checkout** — only worktrees and slots.

## Hygiene

`xcfs doctor <path>` also checks that no **non-hidden build output sits inside a local Swift package directory**
(`build/`, `DerivedData*` next to a `Package.swift`). Xcode walks every non-hidden file under a local package each
time it resolves the package graph; one stray DerivedData of 105,000 files made resolution take 24 s instead of 12 s
on every build of a large app. `xcfs` itself never puts build output inside a source tree.

## Adapters

The core knows no toolchain (a test enforces it). Toolchains plug in through adapters: the built-in `xcode` and
`bazel` adapters, or any executable named `xcfs-adapter-<name>` on `PATH` that answers JSON over stdin/stdout — the protocol is
documented at the top of [lib/xcfs/adapter.py](lib/xcfs/adapter.py). A project lists the ones it uses under
`toolchains`; a Flutter or React Native app could combine `xcode` with its own.

## Tests

```
python3 tests/xcfs_test.py                     # 67 end-to-end tests, ~3 min; --only t03,t36 to pick
/usr/bin/python3 tests/xcfs_test.py --python /usr/bin/python3
tests/sample_e2e.sh                            # the sample: worktree, build/check/test/run, pool, session
```

The tests create throw-away repositories in a temporary directory whose path contains a space and non-ASCII
characters, with their own state, cache, pool and DerivedData roots. They use a toy external adapter instead of
Xcode, a fake `swbuild` for the session's failure modes, and mount a small disk image for the different-volume check.
`sample_e2e.sh` needs Xcode with an iOS Simulator runtime, `xcodegen` and CocoaPods; it creates and deletes a
simulator.

## Benchmark

`bench/bench.py <profile> --source <clone>` sets up N parallel checkouts of a real project twice — as plain
`git worktree`s with the project's own setup, and as xcfs slots — and measures the time until an agent can build, the
first build, disk, resident memory, an edit loop, and what removal leaves behind. A profile (`bench/profiles/`) says
how the project is set up, built and edited; the harness knows no project.

Measured with the defaults alone — no option set — on six public apps, four of them never built while xcfs was
developed. One agent's round of work (a checkout, a build, five edits each checked, a final build, cleanup): Kickstarter
312.0 → 33.2 s, Element X 299.2 → 57.4 s, Signal 323.8 → 101.2 s, Home Assistant 537.8 → 98.9 s, Ice Cubes 161.8 →
17.1 s, Wikipedia iOS 255.6 → 82.0 s (plain worktree → xcfs). The build session and the experimental Swift driver took
effect on every project and no build failed; plain worktrees left 1.5–6 GB behind each, xcfs nothing. Builds that recompile many modules
still take the compiler's time, and on a Bazel app (Telegram-iOS) incremental builds are Bazel's. Details and caveats: [docs/benchmark.md](docs/benchmark.md).

## Design

[docs/design.md](docs/design.md): the measurements behind the approach, the architecture and adapter interface, the
slot lifecycle, the edge cases and how each is handled, and what is not verified.
[docs/build-systems.md](docs/build-systems.md): what Bazel, Buck2, Gradle, Tuist, Nx and compiler caches do, which of
those problems the measured apps have, what each is worth in seconds, and why xcfs is not rewritten in a faster language.
[docs/incremental-swift.md](docs/incremental-swift.md): the experimental Swift driver's patch — what upstream would need
(a filter hook in the driver, or finer fingerprints in the frontend) — and how it compares with Xcode's compilation cache.

## License

MIT; see [LICENSE](LICENSE).
