# xcfs design

This document explains what `xcfs` is for, what has been measured, how the tool is meant to grow, and which edge cases
it handles. What exists today (v0.1) is the worktree lifecycle described in the [README](../README.md); everything
marked *planned* is design backed by measurements of hand-driven prototypes, not shipped code.

## Goal

Several AI agents working on one large Xcode project in parallel run into two problems:

1. **Disk.** Every checkout carries its own dependency layer and its own DerivedData. Eight worktrees of one large app
   that had each been built once held 140.7 GB of build output.
2. **Build time.** A fresh worktree pays a full cold build (≈285 s on that app), and even a no-op incremental build
   with `xcodebuild` costs ≈35 s of fixed overhead before the first compile task starts.

## Approach

No custom file system. Three things layered on git worktrees and APFS clones, with compile caching as an option:

- **Hygiene rules** that keep Xcode from doing needless work (build output never in a non-hidden directory inside a
  local package; one DerivedData per checkout and per driver).
- **A pool of pre-built worktrees** at fixed paths (*planned*): build state is bound to absolute paths, so a slot never
  moves; idle slots follow the integration branch in the background, and handing one out costs only the difference.
- **A persistent build session** (*planned*): keep the build service alive, reuse the build description, and ask for
  exactly the files that changed, instead of starting `xcodebuild` from zero each time.

### Measured on one large app

An `.xcodeproj` with local Swift packages, about 100 remote packages, binary dependencies and Metal shaders —
1,823 targets. 10-core machine, Xcode 27.0, iOS Simulator debug build. Most rows measured 2–10 times.

| situation | `xcodebuild` today | pool + persistent session | |
|---|---|---|---|
| nothing changed | 34–36 s | 3.9–4.1 s | ≈9× |
| edit a function body | 37–39 s | 6.1–6.8 s | ≈6× |
| add or remove a declaration (feature module) | 45 s | 10.7–12.5 s | ≈4× |
| add or remove a declaration (low-level module) | 115 s | 42–51 s | ≈2.5× |
| check that one file compiles | as above | 3.7–4.2 s | ≈9× |
| first build of a new worktree, slot up to date | ≈285 s | the edit itself, 4–50 s | ≥6× |
| same, slot one dependency release behind (32 files) | ≈285 s | ≈52 s | ≈5.5× |
| same, slot one day behind (1,556 files) | ≈285 s | 138–141 s | ≈2× |
| build output on disk | 8 worktrees: 140.7 GB | N slots × ≈8 GB | ≈4× at N = 4 |

Without a pool, a persistent session does not help the very first build of a brand-new path (72 s PIF export + 221 s
cold build). The other way round, the pool does not need the persistent session: driven by `xcodebuild`, a handed-out
slot's first build is estimated at 40–135 s (fixed overhead plus the edit; not measured separately yet).

Incremental results were checked against a clean cold build after 21 mixed edits: of 1.58 million symbols only three
linker-synthesized names differed.

The gain grows with the project. On a small CocoaPods workspace (the [sample](../examples/generic-sample)),
`xcodebuild`'s fixed overhead is about one second, so the persistent session saves little absolute time there; what a
small project gets from `xcfs` is mostly the hygiene and the reclaiming.

### Directions measured and ruled out

| direction | measured limit | verdict |
|---|---|---|
| A custom file system (FSKit, FUSE, NFS), a layered FS, macOS VMs | FSKit has no clone operation and no caller pid; layered FS mounts are Linux-only; two VMs per host | not done |
| Compile-cache stack (CAS, path prefix mapping, patched build service, dedup) | pool warm-up 221 → 105 s, 1.2–1.9 GB saved per slot; no help for day-to-day edits; needs a self-built build service | optional, late |
| Pre-built binaries for first-party modules | cold build 214–221 → 168 s, DerivedData 8.7 → 5.8 GB; edit times unchanged; packaging ≈26 min | only if CI produces them |
| Restoring mtimes when switching back to a baseline | 19.1 s vs 19.9 s | no effect |
| Moving private members into a `private extension` to avoid downstream rebuilds | same 11.6 s, same 4 modules | no effect without library evolution |
| Several agents sharing one slot | rotating: 6–19 s per switch; overlaying: one agent's compile error fails everyone's build | optional, for small machines |

## Architecture

```
xcfs (one executable)
├─ core — knows no toolchain and no project
│   ├─ slot pool: create / warm / update / hand out / return / retire; throttling and queueing   (planned)
│   ├─ source layer: git worktrees, a set of repositories switched together, baseline tracking
│   ├─ dependency layer: APFS clonefile
│   ├─ registry, background reclaiming, disk and memory budget
│   ├─ shared resources: simulators and simulator clones, ports                                 (planned)
│   └─ config reading and validation
├─ adapter interface — the only boundary between the core and a toolchain                       (planned)
└─ adapters
    ├─ xcode (built in, first): persistent session, PIF management, precompiled-module cleanup, simctl
    └─ flutter / react-native / gradle … (later; may be written by others)
```

Adapter operations, each with the Xcode mechanism that was measured for it:

| operation | meaning | Xcode adapter |
|---|---|---|
| `detect` | is this a project I handle; suggested config | find `.xcworkspace` / `.xcodeproj` / `Package.swift`, list schemes |
| `layers` | which directories are dependency layer (cloneable), which are build output (path-bound) | `.spm_cache`, `Pods`; DerivedData; no non-hidden output in local packages |
| `prepare` | what to do after handing out or switching, before a build | export or patch the PIF; the config's `prepare` command |
| `warm` | one full cold build in a slot | |
| `build` / `check` | incremental build; compile only the given files | reuse the build description; a `buildFiles` request |
| `afterSwitch` | what to clear after the source set switched | `SwiftExplicitPrecompiledModules`; decide whether the PIF must be re-exported |
| `session` | start, health-check, measure and stop the resident process | idle ≈1.0 GB, ≈1.8–2.0 GB while building |
| `test` / `run` | run tests; install and launch | `simctl spawn … xctest`, `simctl install / launch` |
| `gc` | what is safe to delete and how | move DerivedData aside, delete in the background |
| `regression` | self-check after a toolchain upgrade | a regression script per Xcode version |

External adapters are separate executables (`xcfs-adapter-<name>` on `PATH`) speaking JSON over stdin/stdout, so they
can be written in any language and released on their own. A project can use several at once — Flutter and React Native
apps contain an Xcode workspace and a Gradle project — so the config lists toolchains rather than picking one.

The persistent session will drive the `swbuild` tool that ships inside Xcode. Apple positions it as a debugging tool
with no stability promise, so it is used only after `xcfs doctor` has run the regression checks on the installed Xcode;
otherwise the slot is driven by `xcodebuild` and the pool still works. Nothing from Xcode is redistributed.

## Layers a project declares

The tool contains no project-specific code, directory names or layering. A project describes itself in `.xcfs.json`.

| layer | what the tool does | what the config decides |
|---|---|---|
| dependencies | clone into each worktree; blocks shared on disk | which directories (`.spm_cache`, `Pods`, `node_modules`, vendor SDKs) |
| source | fixed-path slots, switching a set of repositories together, following a baseline | which repositories, where each lives, which baseline each follows *(planned)* |
| build output | one DerivedData per slot and configuration, persistent session, PIF, reclaiming | container, scheme, configurations and destinations *(planned)* |
| pre-built (optional) | swap selected packages for binaries before exporting the PIF | which packages, the project's own commands *(planned)* |

Version 1 of the config has `seed`, `carry`, `afterSeed` and `pinDerivedData`. A sketch of the keys the later stages
will add:

```json
{
  "version": 1,
  "container": { "workspace": "GenApp.xcworkspace" },
  "scheme": "GenApp",
  "configurations": { "sim-debug": { "configuration": "Debug", "destination": "generic/platform=iOS Simulator" } },
  "repos": [ { "path": ".", "baseline": { "branch": "main" } } ],
  "seed": [ "Pods" ],
  "reexportWhenChanged": [ "Podfile.lock", "project.yml" ],
  "prepare": "xcodegen generate && pod install"
}
```

## Slot lifecycle (planned)

| phase | what happens | measured | who waits |
|---|---|---|---|
| create | a worktree per repository in the set; clone the dependency layer; carry ignored files | 17 s | background |
| warm | switch to the baseline; export the PIF; cold build | ≈72 + 221 s | background |
| idle update | when the baseline moves: switch, clear precompiled modules, get a PIF, build | 50–170 s | background |
| hand out | switch to the feature branch, clear precompiled modules, get a PIF, build | 4–50 s up to date; 138–141 s a day behind | the agent |
| work | single-file checks, incremental builds, tests, runs | see the table above | the agent |
| return | refuse if anything is uncommitted; back to idle update | as idle update | background |
| retire | move DerivedData aside and delete in the background; remove worktrees; delete temporary branches | a synchronous delete of 28 GB took 359 s | background |

Rules the measurements impose: slot paths never change (build state is bound to absolute paths); one DerivedData is
used by one driver only (`xcodebuild` and the persistent session each rebuild everything the other built); each
session has its own request files; after every switch of the source set, clear
`Build/Intermediates.noindex/SwiftExplicitPrecompiledModules`, or a changed binary dependency fails the build with
"has been modified since the module file was built" and retrying does not heal it.

## Roadmap

| stage | content | status |
|---|---|---|
| S0 skeleton | core and adapter interface, config format and reading | config reading done (v0.1); adapter interface not yet |
| S1 hygiene | registry and reclaiming of every worktree; DerivedData outside package directories; `doctor` checks | registry, reclaiming, `doctor` done (v0.1); `xcfs build` not yet |
| S2 pool | the lifecycle above, driven by `xcodebuild` | planned |
| S3 persistent session | the Xcode adapter around `swbuild`: request generation, description reuse, `buildFiles`, PIF caching and patching, timeouts | planned; off by default, enabled only after the regression checks pass |
| S3b / S4 / S5 | shared slots; compile-cache stack; pre-built first-party modules | optional |

The first public release is meant to contain S0, S1 and S2 only: they depend on git, APFS and `xcodebuild`, not on a
tool Apple does not promise to keep stable.

## Edge cases

Collected from two months of a worktree script in daily use, where every special case had been an incident.
"Test" names the case in [tests/xcfs_test.py](../tests/xcfs_test.py).

| # | case | how xcfs handles it | status |
|---|---|---|---|
| 1 | is a build using this worktree? | ask per worktree (cwd inside it, or its path on a command line), never "is any build running on the machine" | done, t09 |
| 2 | which global DerivedData belongs to a worktree | the workspace path recorded in the bucket, matched on a path boundary | done, t08 |
| 3 | is a DerivedData stale | by its last build, not by the bucket directory's mtime (which never moves) | done, t15 |
| 4 | caches the tool did not create | reported only; removed only with `--foreign --apply` | done, t15 |
| 5 | sizing everything on every operation | nothing is sized unless asked for (`ls --sizes`, `gc --foreign`) | done |
| 6 | symlinks in paths | every path resolved to its physical form first | done, t13 |
| 7 | absolute paths inside the seeded layer | rewritten as JSON values, on a path boundary, never as a regex | done, t02 |
| 8 | where the dependency layer is seeded from | today: the source checkout as it is; planned: snapshots keyed by the lock file's hash | planned |
| 9 | clonefile across volumes | refused (exit 4), never a silent full copy | done, t06 |
| 10 | failure half way through creating | rolled back; a killed run is cleaned by `gc --apply` | done, t05, t16 |
| 11 | concurrent calls | registry under a file lock, atomic writes; git worktree operations serialized per repository | done, t17 |
| 12 | an agent crashes holding a worktree | session id, `reap --older-than`, `adopt`; lease timeouts planned | partly, t14, t23–t25 |
| 13 | unsaved work git does not show | detached commits kept under `refs/xcfs/rescue/`; changed carried files block removal | done, t10, t11 |
| 14 | where metadata lives | in the tool's state directory, never in the worktree or git config | done |
| 15 | Xcode.app keeping DerivedData elsewhere | `pinDerivedData`; later, `xcfs build` chooses the location itself | done |
| 16 | project-specific workarounds | never in the tool: `afterSeed` commands in the project's config | done, t04, t05 |
| 17 | config commands come from the repository | run only after `xcfs trust`, keyed by the commands' hash | done, t04 |
| 18 | the Xcode version changed | record the toolchain per slot; mismatch means cold | planned |
| 19 | backups and indexing | pool directories named `.noindex` and excluded from Time Machine | planned |
| 20 | baseline update needs network | a failed fetch leaves the slot as is, marked behind | planned |
| 21 | submodules, Git LFS | not tested | open |
| 22 | spaces and non-ASCII in paths | the test sandbox path contains both | done |
| 23 | disk full during warm-up or build | check free space first; a slot that failed half way is marked cold | planned |

## Not verified

- The pool's scheduling itself (background updates, throttling, concurrent warm-up): each phase was run by hand.
- A pool driven by `xcodebuild`: hand-out times are estimates.
- `swbuild` on Xcode versions other than 27.0.
- Signed device builds; running the same slot in Xcode.app at the same time (Xcode.app would build its own copy).
- Large CocoaPods monoliths, app extensions, watchOS / macOS / visionOS destinations, Tuist or Bazel generated projects.
- Any toolchain other than Xcode. The adapter interface has been checked against one implementation only and will
  probably change when a second one appears.
