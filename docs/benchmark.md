# Benchmarks: parallel checkouts of open-source iOS apps

What does it cost to give three agents their own checkout of a real iOS app — the plain way (`git worktree` plus the
project's own setup and build) and through xcfs (a pool of slots)? Every project below is public, pinned to a commit,
and measured with [bench/bench.py](../bench/bench.py) and a profile in [bench/profiles](../bench/profiles), so the runs
can be repeated.

**Machine:** MacBook Pro, Apple M1 Max, 10 cores, 64 GB, macOS 27.2, Xcode 27.2. Edit loops were run twice where
shown; the repeats agree within ~1 s, so differences of a few seconds between sides are real.

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
