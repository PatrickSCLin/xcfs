# Benchmarks: parallel checkouts of open-source iOS apps

What does it cost to give three agents their own checkout of a real iOS app — the plain way (`git worktree` plus the
project's own setup and build) and through xcfs (a pool of slots)? Every project below is public, pinned to a commit,
and measured with [bench/bench.py](../bench/bench.py) and a profile in [bench/profiles](../bench/profiles), so the runs
can be repeated.

**Machine:** MacBook Pro, Apple M1 Max, 10 cores, 64 GB, macOS 27.2, Xcode 27.2. Edit loops were run twice where
shown; the repeats agree within ~1 s, so differences of a few seconds between sides are real.

## Three agents, real tasks, at the same time

The measure closest to what xcfs is for: three agents start at once on one app, each with a small task defined in
advance and judged by a build and the package's own tests. On [Ice Cubes](#ice-cubes) (one architecture): a public
helper added to `Models` (12 packages depend on it), a computed property added to `Timeline`'s filter enum, a helper
added to `Env`. Each task first writes code that does not compile (a typo, a non-exhaustive switch, an optional not
unwrapped), fixes it, adds a unit test, runs the package's tests and builds the app
([profile](../bench/profiles/icecubes-tasks.json), `bench.py --phases tasks`).

- **Plain:** `git worktree add` + the README's setup, `xcodebuild` into the default DerivedData; a "does it compile"
  is a build, tests run on the simulator named in the command (`name=iPhone 18 Pro`), as an agent would write it.
- **xcfs:** a pool of three slots, `xcfs check` / `xcfs build` / `xcfs test` (each checkout its own simulator). The
  pool is warmed first, and that time is shown, not hidden: a fresh pool (the first batch of agents ever), and the
  next three agents on the same pool after the first released their slots (the slots are warmed again first, timed).

Ranges over clean runs (plain 2, each xcfs cell 2–3; nothing else ran on the machine):

| | plain worktrees | xcfs, defaults | xcfs, without the experimental Swift driver |
|---|---|---|---|
| warm-up before the agents (a fresh pool) | — | 280–301 s | 214–226 s |
| the agents' wall time, warm-up excluded | 284–302 s | 107–139 s | 140–149 s |
| **a fresh pool, warm-up included** | **284–302 s** | **408–419 s** (slower) | **354–376 s** (slower) |
| **the next three agents** (warm-up of the released slots included) | **284–302 s** (every batch builds cold) | **111–121 s** | **138–151 s** |
| no warm-up at all: the agents ask an empty pool (one run) | — | 451 s (slower still) | — |
| peak memory while the agents work | 23.9–25.4 GB | 7.9–11.8 GB | 8.3–11.8 GB |
| peak memory during the warm-up | — | 22.6–25.2 GB | 24.1–24.9 GB |
| left on disk after removal | 1.5 GB of global DerivedData per worktree | nothing (pool retired) | nothing |
| builds or tests with an unexpected result | 0 in these runs; 1 of 6 test runs in an earlier run (below) | 0 | 0 |

What it says:

- **From the second batch on, xcfs is 2–2.7× faster**, and agents work in a third of the memory: the slots are built,
  each build is incremental, and a check compiles one file.
- **A fresh pool's first batch is slower than plain worktrees** (25–45 %): warming three slots (three cold builds
  side by side, and each slot's simulator booted once) takes longer than three plain cold builds, because each warm-up
  build is bounded to keep memory near plain's (see the experimental driver below). The pool pays once; plain
  worktrees pay on every batch.
- **Starting without a warm-up is the slowest way in** (451 s, one run): each agent's first build is then a full
  build in a slot (266–267 s, three at once), and its first test also builds the test targets and boots the slot's
  simulator (124–161 s); peak memory 20.8 GB. So the quick start warms the pool first, in the background, before the
  agents need it.
- **The experimental Swift driver** makes the next batches faster (111–121 vs 138–151 s; the ranges do not overlap)
  and the fresh pool slower (408–419 vs 354–376 s). With the warm-up excluded, the difference is within the noise of
  this task mix (107–139 vs 140–149 s).
- **Shared simulator:** in an earlier run of the same tasks, one of six plain test runs failed with "Test crashed with
  signal kill while preparing to run tests": three agents testing on the one simulator they all named, one test run
  killed another's. Each xcfs checkout has its own simulator (≈535 MB on disk once booted, created and booted once
  during the warm-up).

What changed to get there (each found by these runs, each with a test): `xcfs test` had the build session's tests
built by `xcodebuild` from nothing in a DerivedData of its own (first test 45.6 s; now the session builds them, 0.4–1 s,
and `xcodebuild test-without-building` runs them); Swift Testing results were counted as zero; a warm-up took an
already-current slot away from an agent asking for one; a slot's simulator was created and first booted during the
agent's first test (~30 s idle, far more under load); the slots of a fresh pool were warmed one after another
(301 s); and once side by side, the experimental driver's compile jobs were unbounded (59 GB at the peak; now a
gate of four places per core, 40 on the 10-core machine, keeps it at 22–25 GB, for a warm-up of 280–301 instead of
217 s).

### Wikipedia iOS

The same design on a larger app with Swift and Objective-C ([profile](../bench/profiles/wikipedia-ios-tasks.json)):
a wikitext helper and a URL helper added to `WMFData` (imported by ~300 app files), a layout helper added to
`WMFComponents`; each task runs its own new test class (`-only-testing`, `xcfs test --only`). Two runs each:

| | plain worktrees | xcfs, defaults |
|---|---|---|
| warm-up before the agents (a fresh pool) | — | 278–279 s |
| the agents' wall time, warm-up excluded | 375–387 s | 241–259 s |
| **a fresh pool, warm-up included** | **375–387 s** | **520–537 s** (slower) |
| **the next three agents** (warm-up of the released slots included) | **375–387 s** | **187–203 s** |
| peak memory while the agents work | 15.3–15.7 GB | 7.1–8.2 GB |
| peak memory during the warm-up | — | 10.2–10.3 GB |
| left on disk after removal | 8.8–8.9 GB of global DerivedData | nothing |
| builds or tests with an unexpected result | 2 of 6 test runs ("Test crashed with signal kill while preparing to run tests": the shared simulator) | 0 |

### A pool at rest

Three Ice Cubes slots warmed, then left alone ([bench/pool-idle.py](../bench/pool-idle.py)):

| minutes idle | processes of the pool | resident memory | CPU used since the previous sample |
|---|---|---|---|
| 0, 5, 25 | 9: per slot the build service (≈730 MB), `swbuild` (20 MB), the xcfs session (23 MB) | 2.3 GB | 0 s |
| 35 | none: a session quits after 30 minutes without a build (`sessionIdleMinutes`), its `swbuild` and build service with it | 0 | 0 s |

Disk: 6.0 GB taken on the volume by the three slots (free space before and after, so blocks shared by clones count
once); per slot, logically, 0.08 GB of checkout, 1.5 GB of build output and 0.25–0.36 GB for its simulator.

For a machine with less memory (not measured on one): the warm-up peak scales with the slots built at once and with
the experimental driver's frontend places; on Ice Cubes, three slots at once peaked at 22 GB (four places per core,
the default) or 17 GB (two per core, `XCFS_FRONTEND_JOBS`), at 276 and 327 s; `"warmJobs": 1` builds one slot at a
time.

### Agents at the same time: the round of work

The same comparison with the round of work above (no tests), three agents at once. Ranges over two runs:

| | plain worktrees | xcfs, defaults: warm-up excluded | a fresh pool, warm-up included | the next three agents (re-warm included) | peak memory: agents / warm-up |
|---|---|---|---|---|---|
| Ice Cubes | 288–289 s, 21–23 GB | 19.4–19.7 s | 228–239 s | 24.3–24.6 s | 3.6–4.6 GB / 56–59 GB* |
| Wikipedia iOS | 381–429 s, 12–16 GB | 92.3–92.5 s | 334–343 s | 115.5–115.6 s | 5.4–5.7 GB / 10–14 GB |

\* measured before the frontend gate; with it, the Ice Cubes warm-up peaks at 22 GB and takes 276 s.

One agent working beside a warm-up of two more slots (the pool growing while it is used): Ice Cubes 34–56 s (alone:
19 s), Wikipedia 81 s (alone: 92 s).

## Overview

For the two Xcode projects the main numbers build **one architecture** (`ARCHS=arm64`), which is what an agent needs
for an Apple-silicon simulator; xcfs is measured twice — slots driven by `xcodebuild` (the pool alone), and slots
driven by the persistent build session. Each cell reads plain → xcfs (xcodebuild) → xcfs (session).

| project | one agent's round of work (5 edits) | an agent waits for a new checkout | no-op build | edit a function body in a widely used package | add a public declaration to it | the same, checked with `xcfs check` (session) | left behind after removal |
|---|---|---|---|---|---|---|---|
| [Ice Cubes](#ice-cubes) | 142.7 → **37.4 s** | 75.1 → 6.4 → **1.9 s** | 5.9 → 5.2 → **0.9 s** | 8.3 → 8.5 → **3.7 s** | 30.2 → 30.7 → 24.7 s | **1.2–1.9 s** | 4.4 GB → **0** |
| [Wikipedia iOS](#wikipedia-ios) | 254.4 → 130.4 → **92.9 s** (`skipScripts`) | 92.0 → 16.3 → **12.4 s** | 16.0 → 15.6 → 11.6 s | 18.0 → 18.6 → 14.2 s | 45.8 → 52.4 → 46.4 s | **2.5–3.6 s** | 8.4 GB → **0** |
| [Telegram-iOS](#telegram-ios) (Bazel) | — | 59.3 → **8.2 s** (≈200 → 9 s on an incomplete cache) | 7.3 → 6.9 s | 12.1 → 12.2 s | 536.6 → 526.5 s | — | 11.7–24.3 GB + 3 Bazel servers → **0** |

What the projects show:

- **The pool removes the wait for a new checkout**: 7–40× on every project (to 2–16 s). The build happens in a
  background warm-up that no agent waits for; the warm-up costs about what the plain builds cost.
- **The build session removes the fixed cost of a build**, ≈5 s on Ice Cubes and ≈4 s on Wikipedia, so a no-op or
  a small edit gets 1.3–7× faster. It cannot make the compiler's work smaller: when an edit recompiles many modules,
  the time is the compilation, on every side.
- **`xcfs check` is the fast path for "does my change compile"**: it compiles only the edited file against the last
  build — 1.2–3.6 s for a file in a package where a build takes 15–50 s (17–18 s for a file in Wikipedia's app
  target, which carries the project's script phases). It does not rebuild what depends on the file; run `xcfs build` before
  concluding that everything compiles.
- **Plain removal always leaks**: 1.5–4 GB of global DerivedData per Xcode worktree, 0.7–11 GB of Bazel output base
  and a resident Bazel server per Telegram worktree. Retiring the xcfs pool left nothing.
- **Building one architecture is the largest single saving**, for everyone: `xcodebuild` with a generic simulator
  destination builds arm64 and x86_64. On Ice Cubes, adding a public declaration took 54 s for two architectures and
  30 s for one; a no-op is unaffected.
- **For Bazel, incremental builds are Bazel's**: the same on both sides; xcfs helps with the wait and with cleanup.

## One agent's round of work

The measure that matters most to an agent: get a checkout, build, make five edits — each followed by "does it
compile?" — build once more, hand the checkout back. Plain: worktree + project setup, a first build, every edit
verified by a build (`xcodebuild` cannot compile one file), removal. xcfs: checkout of a slot warmed beforehand, a
first build, every edit verified with `xcfs check`, one final build, release. One architecture; edits cycle through the
profile's three (a function body in a leaf, a function body in a widely used package, a public declaration in it).

| project | plain worktrees | xcfs (session) | xcfs (session + `skipScripts`) |
|---|---|---|---|
| Ice Cubes | 142.7 s, 1.5 GB left behind | **37.4 s** (3.8×), nothing left | — |
| Wikipedia iOS | 254.4 s, 2.8 GB left behind | 130.4 s (2.0×), nothing left | **92.9 s** (2.7×), nothing left |

With the experimental Swift driver ([the driver's notes](../lib/xcfs/adapters/swiftdriver/README.md),
`"swiftDriver": "api-digests"`), measured in one later run against the same sides (Wikipedia with `skipScripts` in
both xcfs columns):

| project | plain worktrees | xcfs (session) | xcfs (session + experimental driver) |
|---|---|---|---|
| Ice Cubes | 164.4 s | 38.0 s | **16.4 s** (10×) |
| Wikipedia iOS | 255.4 s | 90.4 s | **42.4 s** (6×) |

These two rows were measured before the driver had its version line and features file (commit 8c2d298); both
projects built correctly in that state, but the [zero-configuration table](#zero-configuration-on-projects-xcfs-was-not-developed-on)
below measures the driver as it is now and supersedes them.

Where it went (Wikipedia, session → with the driver): the check after the public declaration 33.9 → 10.2 s, the final
build 34.2 → 11.5 s; the other steps within 0.5 s. On Ice Cubes: that check 8.6 → 2.1 s, the final build 19.1 → 5.1 s.
Edits measured alone on fresh checkouts (3 repeats, no check; same session driver with and without the experimental
one): adding a public declaration to `Models` 25.3–25.6 → **6.0–6.2 s** on Ice Cubes, to `WMFData` 34.9–37.6 →
**14.7–15.0 s** on Wikipedia; function bodies and the first build unchanged (Ice Cubes 77.9 → 78.7 s, Wikipedia
86.8 → 86.9 s).

Step by step (Wikipedia, xcfs session + `skipScripts`): checkout 0.3 s, first build 3.4 s, checks 8.6 / 2.6 / 2.9 /
34.7 / 3.0 s, final build 36.1 s, release 1.2 s. The 34.7 s check is the one after a public declaration was added to
`WMFData`: `xcfs check` compiles only the edited file, so the downstream work of an interface change is paid by the next
check of a file that depends on it — once, not on every check.

## Zero configuration, on projects xcfs was not developed on

Does it carry over? Four apps never built while xcfs was written, plus Ice Cubes and Wikipedia, each with an `.xcfs.json`
that only names the project, the scheme and the destination (plus the arm64-only and no-signing settings both sides
use): no `skipScripts`, no `driver`, no `swiftDriver` — every optimization decides by itself. One agent's round
(checkout, first build, five edits each checked, a final build, release), the edits being a function body in the app
target, a function body in a widely imported module, and a public declaration added to that module. Plain worktrees
measured the same day on the same machine, three times each (the runs differ by 3–13 %, so a difference of a few
percent between sides means nothing; the ones below are 2.5–10×). The xcfs columns are the last run after the fixes
listed below; repeats of it on Ice Cubes (15.8–17.1 s) and Element X (57.0–57.4 s) differed by less than 5 %.

| project | kind | plain worktree (mean of 3, range) | xcfs, defaults | xcfs, defaults without the experimental Swift driver |
|---|---|---|---|---|
| [Kickstarter](https://github.com/kickstarter/ios-oss) | `.xcodeproj` + local Swift packages (KsApi: ~800 importing files) | 301.3 s (279–313) | **33.2 s** | 73.9 s |
| [Element X](https://github.com/element-hq/element-x-ios) | XcodeGen-generated, one large app target, Swift packages, a prebuilt Rust SDK | 287.3 s (263–299) | **57.4 s** | 93.5 s |
| [Signal](https://github.com/signalapp/Signal-iOS) | workspace, Swift and Objective-C frameworks (SignalServiceKit: ~800 importing files) | 322.3 s (317–326) | **101.2 s** | 109.6 s |
| [Home Assistant](https://github.com/home-assistant/iOS) | large app target, a shared framework (~550 importing files), many extensions, SwiftGen and SwiftLint phases | 518.7 s (492–538) | **98.9 s** | 159.9 s |
| Ice Cubes (seen) | `.xcodeproj` + 13 local Swift packages | 155.1 s (143–162) | **17.1 s** | 39.1 s |
| Wikipedia iOS (seen) | Swift and Objective-C, local packages | 251.4 s (246–256) | **82.0 s** | 132.3 s |

What each automatic decision did, over all six rounds:

| optimization | default | took effect | fell back / failed |
|---|---|---|---|
| pool slot instead of a new worktree + first build | always | 6 of 6 (checkout 0.2–0.3 s) | — |
| build session (`"driver": "auto"`) | after a one-time regression check per Xcode | 6 of 6 | 0 |
| experimental Swift driver (`"swiftDriver": "auto"`) | when the selected Xcode is of the Swift 6.4 line (built and self-tested per Xcode) | 6 of 6; digests extracted for 282 of 283 module versions (one module had no symbols to describe and kept the stock behavior) | 0 build failures |
| leave out lint-only script phases (`"skipScripts": "auto"`) | phases that only run a linter | Element X (SwiftLint, SwiftFormat in lint mode), Home Assistant (two SwiftLint phases) | the other projects have none: Wikipedia's SwiftLint phase runs `swiftlint --fix`, which rewrites sources, and is kept |
| `xcfs check` | always available | every edit of every round | — |

Running the defaults on these apps found five defects that Ice Cubes and Wikipedia had not shown, all fixed before
these numbers: a private submodule the source checkout never had made a checkout fail; the session check did not
recognize the compile tasks of an external Swift driver, so "auto" fell back to `xcodebuild` everywhere; a failed
check was trusted forever; the patched driver lacked the version line and the features file through which the build
service learns what a compiler takes (packages using `package` access did not build); and extensions of
`Optional`-like types were not matched to their digests, which on Home Assistant turned one added function into 1,271
recompiled files instead of 10.

Where the experimental driver gains least: Signal (101.2 vs 109.6 s). Its widely imported framework mixes Swift and
Objective-C; we have not yet traced which part of that rebuild the digests leave in place — a likely candidate is the
generated `-Swift.h` header and the Objective-C files that import it, which no Swift-level digest can narrow.

Not included: NetNewsWire (its build needs a generated secrets file from a script with credentials), DuckDuckGo
(its sources did not compile against the Xcode 27.2 SDK), a CocoaPods app (the candidates checked have all moved to
Swift packages; CocoaPods is covered only by `examples/generic-sample`), and a Tuist project (it needs the Tuist binary).
Profiles: `bench/profiles/{kickstarter,element-x,signal,home-assistant}.json`.

## A shared compilation cache

`"compileCache"` (experimental, off by default) gives every checkout one Xcode compilation cache, so a slot can
replay what another slot compiled. Xcode's own prefix mapping was not enough: on a second Ice Cubes slot 306 of 308
Swift compiles missed. Logging `swiftc`'s arguments in two slots showed why: Xcode maps a target's sources and
intermediates, but the compiler's working directory (the checkout's project file) and the checkout's build directory
still differ between slots. xcfs maps those too; then every compile job of a second and third slot hit (Swift
308/308, emit-module 36/36, C 35/35, Clang modules all), and their warm-up went from 84–88 to 35–38 s.

Two modes, because mapping paths changes what the program sees at run time:

- `"all"` maps the whole checkout to `/^checkout`. Every compile job is shared, but `#filePath` and `#file` read
  `/^checkout/…` at run time: a test reading a fixture next to its source failed (file not found) and one writing a
  snapshot there failed (read-only root), as tests built on swift-snapshot-testing would. `#fileID` is unchanged,
  compile errors keep their real paths, and xcfs maps test failure locations back.
- `"dependencies"` keeps the checkout's sources at their real paths and shares what lies outside them (packages,
  Clang modules): those tests pass, and fewer jobs are shared (Swift 165/308 on a second slot).

Checked on the sample project, mode by mode:

| | off | `"dependencies"` | `"all"` |
|---|---|---|---|
| a test reading a fixture next to its source, or writing a snapshot there | passes | passes | fails |
| compile errors (`xcfs build`, `xcfs check`) | real paths | real paths | real paths |
| a test failure's location | real path | real path | mapped back by xcfs |
| lldb: a breakpoint by real path (as Xcode sets them), showing the source | works | works | needs `settings set target.source-map /^checkout <checkout>`; xcfs prints it in each build's JSON (`lldb_source_map`) |
| `#fileID` (and so `fatalError`, `assert` messages in Swift 6 modules) | real | real | unchanged |
| `#file` in a Swift 5 module (messages, logs), source paths in a symbolicated crash | real | real | `/^checkout/…` |
| the project's own `OTHER_SWIFT_FLAGS` | kept | kept | kept (xcfs's flags follow `$(inherited)`) |

Either needs Xcode's own Swift driver: the experimental driver's compile jobs are not cached at all, and switching a
cache-warmed slot to it recompiled everything (1,582 tasks, 53.7 s), so the two do not combine.

Correctness: the Ice Cubes oracle with `"all"` (the incremental side on the shared cache, each clean build on a new,
empty one): 9 steps, no difference. Incremental builds on one slot, two runs each, off / `"dependencies"` / `"all"`:
a function body 4.0–4.3 / 4.3–4.4 / 4.3–4.4 s, a public declaration in `Models` 26.6–27.0 / 29.2–30.7 / 30.2–30.6 s
(37 more compile jobs and a longer dependency scan with the cache), a no-op 0.8–1.9 s throughout. The first edit after
a slot was replayed recompiles that module once: swift-build #1810 fixes it, merged into `main` on 2026-10-02 and not
in any 6.4 release branch, so not in Xcode 27.2.

Where it pays, Ice Cubes, three slots, two runs each:

| | defaults (experimental driver) | `"all"` | `"dependencies"` |
|---|---|---|---|
| the baseline moves on 15 commits (1.5 days of history): three slots follow it | 115–116 s, 12.8–13.5 GB | **93–98 s**, 8.6–8.9 GB | 117–123 s, 11.6–11.7 GB |
| the baseline moves on 20 commits (5 days) | 105–117 s, 13.3–13.7 GB | **89–98 s**, 8.5–9.2 GB | 114–128 s, 11.6–11.7 GB |
| a new pool the next day (the cache holds the previous baseline only) | 312–321 s, 19.6–23.2 GB | **173 s**, 6.3–7.3 GB | 198–199 s, 10.8–11.0 GB |
| three agents' real tasks, a fresh pool, warm-up included | 408–419 s | 352–355 s | 387–398 s* |
| the next three agents on that pool | 111–121 s | 117–153 s | 141–153 s* |

\* before the simulator's first boot overlapped a cached build. Plain worktrees: 284–302 s for every batch.

So the cache makes the pool cheaper to keep and to recreate (a new pool 45 % faster and with a third of the memory
once the cache has seen the project; following the baseline 15–20 % faster), but it does not make the very first
batch on a fresh machine beat plain worktrees, and the agents' own edits lose the experimental driver's narrowing.
It stays off by default. In these runs the warm-up with the cache peaked at 8–10 GB against 21–23 GB without; the
cache grew by ~200 MB per slot (1.3 GB after three slots of Ice Cubes); it lives in xcfs's cache directory.

## Exporting the project description

The build session reads the project as a PIF, exported by `xcodebuild` and cached by a key of the file list, the
project and package definitions and the toolchain; a content edit keeps it, a new file exports it again. Measured with
`xcfs session start` in a new worktree (Xcode 27.2, one run each):

| | first export (packages resolved) | nothing changed | after a new Swift file in the app target |
|---|---|---|---|
| Ice Cubes | 27.0 s | 0.09 s | 2.9 s |
| Wikipedia iOS | 46.9 s | 0.12 s | 3.2 s |

So on these apps an agent that adds a file pays about 3 s once, and patching the PIF instead of exporting it again (a
planned refinement) would save about that much; the first export belongs to the first build, or to
`xcfs session start` before it.

## Is an incremental build the same as a clean one?

Speed is worth nothing if an incremental build can leave something stale. [bench/oracle.py](../bench/oracle.py)
checks it on real apps with xcfs's defaults (build session, experimental Swift driver): one worktree takes a list of
edits one after another and is built incrementally after each; for every step a fresh worktree gets the same edits
and is built from nothing, and every object file of the clean build is compared with the incremental one (code and
symbol names; a build that fails must fail on both sides). The edits are the kind a dependency graph can get wrong:

| project | edits (cumulative; the breaking ones are undone after their step) | steps | objects per step | differences |
|---|---|---|---|---|
| Ice Cubes, Xcode 27.2 | a stored property added to an `@Observable` class 15 files use (macro expansion, class layout); a default argument added; an argument label changed and a setter made `private(set)` (callers break); a private stored property added to a struct other packages pass by value; an enum case added (switches break); a function body in a leaf package and in `Models`; a public declaration added to `Models` | 9 | 3,230 | **0** |
| Ice Cubes, Xcode 27.1 | the same, with the driver xcfs built for that Xcode | 9 | 3,230 | **0** |
| Wikipedia iOS, Xcode 27.2 | an `@objc` property added to a Swift class Objective-C uses (generated `-Swift.h`); a property added to an Objective-C class Swift uses (Clang module, Objective-C layout); an `@objc` name changed (an Objective-C caller breaks); a function body in the app target and in `WMFData`; a public declaration added to `WMFData` | 6 | 3,218 | **0** |

Raw results: `oracle-*.json` in [bench/results](../bench/results/README.md).

What had to be taken out of the comparison, because it differs between two correct builds: paths of different
lengths move compiled-in strings (`#file`, assertions) and the code that refers to them, so both checkouts get paths
of one length; labels of string literals contain the string, including source paths; an object named after a hash
of its source path. And one case is checked again rather than excluded: on Ice Cubes, three objects of `StatusKit`
(`StatusRowView.o`, `AccessoryView.o` and the package's merged object) come out of a clean build with different code
than the same file compiled alone — the incremental build's object equals the one compiled alone in the clean
checkout, which has nothing stale, so those count as equal. The oracle recompiles a differing object's sources in
the clean checkout and compares once more before it reports a difference.

The self-test every experimental driver passes before xcfs uses it applies the same rule, byte for byte, to two
small modules and 19 steps (`lib/xcfs/adapters/swiftdriver/selftest.py`).

## Where the time goes

Why larger projects gain less, and what was tried — each row measured before and after, repeated:

| | finding |
|---|---|
| fixed cost per build | `xcodebuild` spends ≈4 s before the first task on Wikipedia (package graph, target graph, build description); on Ice Cubes a no-op takes 5.9 s with `xcodebuild` and 0.9 s with the session. The session starts work at once (planning 0 s, first task at 0.14 s on Wikipedia). |
| a project's own scripts | Wikipedia's "Update Localizations" phase compiles a command-line tool from source on every build (8.6 s) and rewrites localization files, so copies, validations and some compilation follow. With `"skipScripts": ["Update Localizations", "SwiftLint"]` (session driver; xcfs removes them from a copy of the PIF) on fresh checkouts, 3 repeats each: **no-op 12 → 1 s, a function body in the app target 19.3 → 8.4 s, in `WMFData` 14.1 → 4.0 s, a public declaration 46.5 → 34–37 s.** The project is not changed. |
| a public declaration recompiles everything downstream | asked the Swift driver why (`-driver-show-incremental`): the frontend links every declaration of a file to the file's interface hash, and a `.swiftmodule` describes the whole module as one file, so any interface change marks every declaration in `WMFData` as changed and all 1,115 importing files recompile (inside the package, the edited file's interface invalidates every declaration in it the same way). `-enable-incremental-imports`, merge-module mode, implicit modules and build settings do not change that. **An experimental patched Swift driver** ([the driver's notes](../lib/xcfs/adapters/swiftdriver/README.md), `"swiftDriver": "api-digests"`) compares per-declaration digests extracted from the module instead. Fresh checkouts, `skipScripts`, 3 repeats, no check: **public declaration 33.1 / 33.9 / 35.8 s → 15.0 / 14.9 / 14.8 s**; a function body in the app target 8.0 → 8.3 s, in `WMFData` 4.0 → 4.1 s (same driver with the feature off vs on); Xcode's own integrated driver gave 35.8 / 37.5 / 36.5 s. The first build of a checkout pays one extraction per module (in a background process: the first build of a fresh checkout took 86.8 s without and 86.9 s with it; once per machine, tens of seconds more to build the Clang modules the extraction uses). Of the remaining 15 s, ≈4 s is the app target re-emitting its module without compiling a file (a target with a bridging header always re-runs its emit-module job), the rest copies, links and app-extension validation that follow any change of a module's product. |
| build settings for agents | on fresh checkouts with `skipScripts`, adding implicit modules, no index store and no debug dylib saved ≈3 % (public declaration 35.7 → 34.5 s; other edits within 0.3 s). An earlier sweep on one reused checkout had suggested 12–25 %; that came from artifacts the previous variants left behind. No preset is shipped. |
| sharing compilation results between slots | Xcode's compilation cache (`COMPILATION_CACHE_ENABLE_CACHING`), also with `SWIFT_/CLANG_ENABLE_PREFIX_MAPPING` and `SWIFT_/CLANG_ENABLE_PROJECT_PREFIX_MAPPING`: warming slots 2 and 3 of Ice Cubes took 62–63 s instead of 65 s. Cache keys still contain each slot's own output paths; cross-slot hits would need the build service changes the design notes list for the optional cache stage. |
| xcfs (xcodebuild) slower than plain on Wikipedia's public declaration (≈52 s vs ≈46 s) | the same `xcodebuild` command run by hand in the slot takes the same time, and the same edit built into the default DerivedData in the same slot takes 45 s. It follows the choice of a DerivedData location of its own; sharing Xcode's module, compilation and SDK stat caches (now the default) did not remove it in a fresh pool. Not explained yet; the session driver does not show it. |
| Telegram: one process left per slot after release | the slot's Bazel server, kept on purpose: the next lease's first build is 9 s instead of 20 s, at 1.5–3.4 GB of memory per idle slot until Bazel's 3-hour idle timeout (`"stopServerOnRelease": true` stops it on release). |
| Telegram: 526 s for one public declaration | Bazel's Swift rules compile whole modules: a changed interface recompiles every dependent module completely. Outside xcfs's reach. |

A bug found on the way: the build session reused a build description whenever the PIF was unchanged, so a change of
build settings, configuration or destination was ignored until the project itself changed. Reuse is now keyed on the
whole request; earlier benchmark runs were unaffected (each used fresh slots with fixed settings).

## Ice Cubes

[Ice Cubes](https://github.com/Dimillian/IceCubesApp) at `98098b9`: a SwiftUI Mastodon client — an `.xcodeproj`, 13
local Swift packages and remote packages; simulator build. Profiles:
[icecubes-arm64.json](../bench/profiles/icecubes-arm64.json) (one architecture) and
[icecubes.json](../bench/profiles/icecubes.json) (generic destination, two architectures).

- **Plain:** `git worktree add`, then the README's setup (copy `IceCubesApp.xcconfig.template` to the git-ignored
  `IceCubesApp.xcconfig`), then `xcodebuild` into the default DerivedData, as an agent would.
- **xcfs:** the config file is carried into every slot (`"carry": ["IceCubesApp.xcconfig"]`).
- Edits: a function body in `Timeline` (nothing else depends on it); a function body in `Models` (12 packages depend on
  it); a public declaration added to `Models`. Each edit loop ran twice; both runs are shown.

One architecture:

| | plain worktrees | xcfs, xcodebuild | xcfs, session |
|---|---|---|---|
| **an agent waits for a new checkout** (mean of 3) | **75.1 s** | **6.4 s** | **1.9 s** |
| background warm-up of 3 slots | — | 190.9 s | 206.1 s |
| disk for 3 built checkouts | 4.6 GB | 5.0 GB | 5.0 GB |
| left behind after removal | 4.4 GB of global DerivedData | nothing | nothing |
| no-op build | 5.9 s | 5.2 s | 0.9 s |
| function body, leaf package | 7.9 / 7.5 s | 8.2 / 8.0 s | 3.1 / 3.1 s |
| function body, `Models` | 8.4 / 8.2 s | 8.5 / 8.5 s | 3.7 / 3.8 s |
| public declaration added to `Models` | 30.0 / 30.3 s | 30.7 / 30.6 s | 24.6 / 24.8 s |
| `xcfs check` of the edited file, per edit above | — | (a full build) | 2.5 / 1.5, 1.2 / 1.4, 1.7 / 1.9 s |

Raw results: [bench/results](../bench/results/README.md), plain side from the run of 02:52, xcfs sides from its rerun
of 06:25. In the xcodebuild column an edit's time is that of `xcfs check`, which with the `xcodebuild` driver is a full
build (the build after it is a no-op, ≈5.3 s).

Two architectures (generic destination; the session side was rerun after it was made to build both architectures
like `xcodebuild` — the first session run built one and is not shown):

| | plain worktrees | xcfs, xcodebuild | xcfs, session |
|---|---|---|---|
| an agent waits for a new checkout | 108.6 s | 6.7 s | 2.4 s |
| no-op build | 6.2 s | 5.5 s | 0.9 s |
| function body, leaf package / `Models` | 10.0 / 10.3 s | 10.5 / 10.6 s | 5.9 / 6.0 s |
| public declaration added to `Models` | 54.3 s | 54.9 s | 50.3 s |
| left behind after removal | 9.2 GB | nothing | nothing |

## Wikipedia iOS

[Wikipedia iOS](https://github.com/wikimedia/wikipedia-ios) at `0919f3e`: an `.xcodeproj` with Swift and Objective-C,
app extensions, local Swift packages (`WMFData`, `WMFComponents`, …) and remote packages; simulator build. Profiles:
[wikipedia-ios-arm64.json](../bench/profiles/wikipedia-ios-arm64.json) and
[wikipedia-ios.json](../bench/profiles/wikipedia-ios.json).

- **Plain:** `git worktree add`, then the repository's `scripts/setup_bundle_id ci` (writes the git-ignored signing
  configuration, as its CI does), then `xcodebuild` into the default DerivedData.
- **xcfs:** the generated configuration is carried into every slot
  (`"carry": ["Configurations/OpenSourceDebug.xcconfig"]`).
- Edits: a function body in the app target; a function body in `WMFData` (≈300 files import it); a public declaration
  added to `WMFData`.

One architecture:

| | plain worktrees | xcfs, xcodebuild | xcfs, session |
|---|---|---|---|
| **an agent waits for a new checkout** (mean of 3) | **92.0 s** | **16.3 s** | **12.4 s** |
| background warm-up of 3 slots | — | 285.7 s | 282.4 s |
| disk for 3 built checkouts | 8.3 GB | 9.0 GB | 8.7 GB |
| left behind after removal | 8.4 GB of global DerivedData | nothing | nothing |
| no-op build | 16.0 s | 15.6 s | 11.6 s |
| function body, app target | 22.9 / 22.9 s | 24.0 / 23.4 s | 18.8 / 18.7 s |
| function body, `WMFData` | 18.1 / 17.9 s | 18.4 / 18.8 s | 14.3 / 14.1 s |
| public declaration added to `WMFData` | 45.2 / 46.4 s | 53.3 / 51.4 s | 47.9 / 44.9 s |
| `xcfs check` of the edited file, per edit above | — | (a full build) | 18.0 / 16.9, 2.5 / 2.5, 3.2 / 3.6 s |

Raw results: [bench/results](../bench/results/README.md), plain side from the run of 03:11, xcfs sides from its rerun
of 06:37; the xcodebuild column times `xcfs check` as above (the build after it ≈15.5 s).

A check of a file in the app target takes 17–18 s: the app target is the module the project's script phases belong
to. Checks in a package take 2.5–3.6 s.

Two architectures (generic destination; session side rerun as above):

| | plain worktrees | xcfs, xcodebuild | xcfs, session |
|---|---|---|---|
| an agent waits for a new checkout | 126.8 s | 16.8 s | 12.9 s |
| no-op build | 15.9 s | 15.7 s | 11.8 s |
| function body, app target / `WMFData` | 30.5 / 19.3 s | 31.6 / 20.9 s | 27.7 / 16.7 s |
| public declaration added to `WMFData` | 75.9 s | 90.8 s | 84.4 s |
| left behind after removal | 12.4 GB | nothing | nothing |

## Telegram-iOS

[Telegram-iOS](https://github.com/TelegramMessenger/Telegram-iOS) at `24638bc6` — built with Bazel through its own
`build-system/Make/Make.py`, 12 git submodules, ≈8,200 build actions for the simulator app (`debug_sim_arm64`);
Bazel 9.2.0, `--overrideXcodeVersion` (the project asks for Xcode 27.1). Profile:
[bench/profiles/telegram-ios.json](../bench/profiles/telegram-ios.json).

### Method

Both sides use the same build command and share one Bazel disk cache (`--cacheDir`), as the project's README
recommends; the cache was filled by building the source checkout first. So this measures what parallel checkouts cost
**on top of a warm cache**, not a cold build (the source checkout's own build took ≈15 min of actions after ≈10 min of
analysis).

| | plain worktrees | xcfs |
|---|---|---|
| a new checkout | `git worktree add` + `git submodule update --init --recursive --depth 1` (network) | `xcfs checkout`: a slot from a pool warmed beforehand (`xcfs pool warm --slots 3`, in the background); submodules cloned from the source checkout |
| build | `Make.py … build` (Bazel; each checkout gets its own output base and Bazel server) | the same command, through xcfs's Bazel adapter |
| edits, in the first checkout | no-op; a function body in a leaf module; a function body in a module 48 others import; a public declaration added to that module — each built in turn | the same edits (different text on each side and run, so neither side hits the other's cache) |
| removal | `rm -rf` + `git worktree prune` (`git worktree remove` refuses worktrees with submodules) | `xcfs release`, then `xcfs pool resize 0` |

Two workarounds apply to both sides: the repository's fake provisioning profiles expired on 2026-10-30, so the build
adds `--//Telegram:disableProvisioningProfiles` (the simulator needs none) through
[bench/helpers/bazel-extra-args](../bench/helpers/bazel-extra-args), because `Make.py` forwards no extra Bazel flags; and
Xcode's Metal toolchain had to be installed (`xcodebuild -downloadComponent MetalToolchain`).

### Results

The two runs differ in one thing: in the first, the shared cache lacked 15 long-running actions (the source
checkout's build had not stored them), so every new checkout ran them itself — a 139 s critical path. The second run,
an hour later, found them cached. A cache in real use sits somewhere between the two.

#### Run 2 — the shared cache has everything

| | plain worktrees | xcfs |
|---|---|---|
| **an agent waits for a new checkout** (ready + first build, mean of 3) | **59.3 s** (40 s checkout + submodule download, 19 s build) | **8.2 s** (0.6 s checkout, 7.6 s build) |
| background work before any agent asks | — | 92 s to warm 3 slots |
| disk for 3 built checkouts | 3.2 GB | 3.0 GB |
| left behind after removing them | **11.7 GB** of Bazel output bases, **3 Bazel servers** still running (6.4 GB resident) | nothing |
| time to remove one | 4.0–4.1 s | release 1.7–2.0 s; retiring all three 1.6 s |

| edit loop in the first checkout | plain | xcfs |
|---|---|---|
| no-op build | 7.3 s | 6.9 s |
| function body, leaf module | 16.5 s | 16.2 s |
| function body, module 48 others import | 12.1 s | 12.2 s |
| public declaration added to that module | 536.6 s | 526.5 s |

#### Run 1 — the shared cache misses a few long actions

| | plain worktrees | xcfs |
|---|---|---|
| **an agent waits for a new checkout** (mean of 3) | **≈200 s** (41–46 s checkout + submodules, 155–159 s build) | **≈9 s** (0.6–0.7 s checkout, 7.7–8.5 s build) |
| background work before any agent asks | — | 402 s to warm 3 slots |
| disk for 3 built checkouts | 23.0 GB | 21.9 GB |
| left behind after removing them | **24.3 GB** of Bazel output bases, 3 Bazel servers | — (see below) |

The xcfs side of run 1 could not be completed as measured: returning a slot was refused because the slot's own Bazel
server — kept on purpose between leases — counted as "something else using the checkout", and a background delete
could not empty Bazel's read-only directories. Both were fixed (adapters now name their own helper processes; deletes
make directories writable first) and run 2 measured the fixed version. Run 1's edit loop is not shown: both sides
wrote the same edit text, so the second side's builds were cache hits.

### What this shows

- **The wait for a new checkout is where xcfs pays off: 59 s → 8 s with a complete cache, ≈200 s → 9 s without one.**
  The work moves to a background warm-up that no agent waits for. Without a pool, every agent pays the submodule
  download (≈40 s here) and whatever the cache lacks, every time.
- **Removal is where plain worktrees leak.** Bazel keeps each checkout's output base and server outside the
  checkout, so deleting a worktree leaves 0.7–11 GB and a resident JVM per checkout behind, on every removal. xcfs
  owns them: retiring the pool left nothing.
- **Incremental builds are the same on both sides.** For a Bazel project, speed after an edit is Bazel's: xcfs adds
  nothing there (its persistent-session speed-up is for `xcodebuild` projects and does not apply). The 9-minute rebuild
  after adding one public declaration to a widely imported module is the cost of the build graph, on either side.
- **Disk is about equal with a shared cache.** With Bazel's remote and disk caches, an output base only holds what is
  materialized; xcfs's disk savings come from bounding the number of checkouts (slots are reused) and from removing
  what plain removal leaves behind, not from a smaller checkout.
- **The kept server is worth it:** a no-op build of the source checkout took 20 s right after the server started
  (re-analysis of 1,381 packages) and 9 s with the server alive (3.4 GB resident).
- **Memory:** each checkout's Bazel server holds 2–4 GB while it lives. Plain worktrees keep theirs until Bazel's idle
  timeout (3 h) even after the worktree is gone; xcfs keeps a slot's server between leases (so the next lease does not
  re-analyze) and stops it when the slot is retired, or on release with `"stopServerOnRelease": true`.


## Firefox for iOS (not measured)

[Firefox for iOS](https://github.com/mozilla-mobile/firefox-ios) would be the best-known candidate, but its
`bootstrap.sh` must run first: it downloads and runs a script from another Mozilla repository, runs `npm install` and
`npm run build`, and copies git hooks. The benchmark's automation declined to run third-party code on its own; without
the bootstrap the build stops at the "Nimbus Feature Manifest Generator Script" phase (`firefox-ios/bin/nimbus-fml.sh:
No such file or directory`). To measure it, run `sh ./bootstrap.sh` yourself in the clone, then give xcfs the generated
dependency layer — git-ignored outputs the bootstrap leaves in the source checkout — through `"seed"` (for example
`node_modules`, `firefox-ios/bin`) so that every slot gets them by clone instead of running the bootstrap again.

## What the projects needed from xcfs

Each of these was found by running a project above, fixed in general terms (no project is named in xcfs), and covered
by a test or by the sample end-to-end run:

| found on | problem | change |
|---|---|---|
| Telegram-iOS | git submodules: a worktree has none until they are fetched again from the network | submodules come along in every worktree and slot, cloned from the source checkout's copies, and follow every switch; a commit that exists only in a checkout's submodule blocks its removal |
| Telegram-iOS | Bazel keeps each checkout's build state and a resident server outside the checkout | a built-in Bazel adapter owns the output base (`<user root>/<md5 of the workspace path>`), removes it with the checkout and stops the server |
| Telegram-iOS | returning a slot was refused: the slot's own Bazel server (kept on purpose between leases) counted as "in use" | adapters name a checkout's own helper processes (`ownProcesses`), which never count as other users |
| Telegram-iOS | a background delete could not empty Bazel's read-only directories | deletes make directories writable first |
| Ice Cubes | `xcodebuild` leaves `ibtoold` (Interface Builder's helper) running with its working directory inside the slot, which blocked the return | the Xcode adapter treats Xcode's own helper daemons in a checkout as its own and stops them on release |
| Ice Cubes | the session's PIF export resolved packages into a global DerivedData folder no checkout owned | the export (and scheme listing) writes into the checkout's own build directory; the sample run now fails if any folder appears in the global DerivedData |
| Ice Cubes | `xcodebuild -list -json` output carries log lines with braces before the JSON | the first complete JSON object is found instead |
| Ice Cubes | the session built one architecture where `xcodebuild` builds two for a generic destination, which made it look faster than it was | the session builds what `xcodebuild` builds; the sample run compares the two builds' slices |
| Wikipedia | why builds take what they take was invisible | session results carry a timing breakdown (planning, task time by kind, slowest tasks) |
| Wikipedia | a project script that runs on every build dominated every build an agent ran | `"skipScripts"`: the session driver leaves named script phases out of a copy of the PIF |
| Wikipedia (settings sweep) | the session reused a build description across changed build settings | reuse is keyed on the whole request |
| Ice Cubes, Wikipedia | each project needs a git-ignored configuration file in every checkout | no change: the existing `carry` key copies it from the source checkout |

## Reproduce

```
# one agent's round of work: bench.py <profile> --source <clone> --phases round   (profiles *-round.json)
# what build settings cost on one checkout: bench/knobs.py <profile> --source <clone> [--only knob,…]
# an Xcode project, e.g. Ice Cubes (icecubes-arm64.json for one architecture)
git clone --depth 1 https://github.com/Dimillian/IceCubesApp.git && cd IceCubesApp && git checkout 98098b9
cp IceCubesApp.xcconfig.template IceCubesApp.xcconfig    # and empty DEVELOPMENT_TEAM for a simulator build
cd .. && python3 <xcfs>/bench/bench.py <xcfs>/bench/profiles/icecubes.json --source IceCubesApp --agents 3

# Wikipedia iOS: clone, `scripts/setup_bundle_id ci`, then the same with bench/profiles/wikipedia-ios.json
```

The harness writes a report like the tables above and a JSON file with every measurement into its work directory.
Plain worktrees build into the default global DerivedData, as an agent would; the harness removes what they leave
behind after measuring it.

Telegram-iOS:

```
git clone --recursive -j8 --depth 1 --shallow-submodules https://github.com/TelegramMessenger/Telegram-iOS.git
cd Telegram-iOS && python3 build-system/Make/Make.py --overrideXcodeVersion --cacheDir=../bazel-cache build \
  --configurationPath=build-system/appstore-configuration.json \
  --codesigningInformationPath=build-system/fake-codesigning --configuration=debug_sim_arm64 --buildNumber=1
#   (with expired profiles: add --bazel <xcfs>/bench/helpers/bazel-extra-args and the XCFS_BENCH_BAZEL* variables
#    from the profile's "env")
cd .. && python3 <xcfs>/bench/bench.py <xcfs>/bench/profiles/telegram-ios.json --source Telegram-iOS \
  --cache bazel-cache --agents 3
```

The harness writes a report like the tables above and a JSON file with every measurement into its work directory.
