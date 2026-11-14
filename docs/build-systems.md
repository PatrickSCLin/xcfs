# What other build systems do, and what xcfs could take from them

Each row: the problem a system solves, whether the projects measured here have it, how many seconds it is worth in
our measurements, and what it would cost to bring into xcfs. **Measured** means a number from `docs/benchmark.md` or
from a profile of xcfs itself; **estimated** means a bound derived from measured parts, not a run.

All numbers: Apple M1 Max, Xcode 27.2, one simulator architecture, the six apps of the zero-configuration evaluation.

## The comparison

| idea (where it comes from) | the problem | do we have it? | worth, in our measurements | status in xcfs / cost to adopt |
|---|---|---|---|---|
| **Persistent build process** (Bazel server, Gradle daemon, Bazel persistent workers for compilers) | each build pays for starting the build system and loading the project | yes: `xcodebuild` spends ≈4–5 s before the first task | **measured**: no-op 12 → 1 s (Wikipedia), 5.9 → 0.9 s (Ice Cubes) | done: the build session keeps `swbuild` per checkout (`driver: auto`) |
| **Configuration cache** (Gradle), **analysis cache** (Bazel) | re-planning an unchanged project every build | yes: PIF export 11–25 s, build-description creation | **measured**: PIF reused while its inputs hash the same; build description reused while the request is the same (a no-op plans in 0 s) | done |
| **Fine-grained invalidation** (Buck2's DICE, Bazel's per-action keys; Swift's own per-declaration fingerprints inside a file) | an interface change recompiles everything downstream | yes, and it was the largest single cost after the session | **measured**: a public declaration 33–36 → 15 s (Wikipedia), 25 → 6 s (Ice Cubes); agent rounds 22–61 s shorter on five of six apps (8 s on Signal) | done for the Swift 6.4 line as a patched driver (`swiftDriver: auto`), built and self-tested per Xcode; porting cost per Swift release line (see the driver's notes) |
| **Content-addressed action cache shared between checkouts** (Bazel disk/remote cache, sccache, ccache, Xcode's compilation cache) | the same compile is redone in every checkout | yes: every worktree's first build compiles everything (78–300 s plain) | **measured**: Xcode's CAS with prefix mapping saved ≈3 s of 65 s per extra slot — its keys still contain each slot's output paths. **Estimated**: with path-independent keys, a new worktree off a recent main could hit most actions; the pool already gives agents a built checkout in 0.2–0.3 s, so the gain is in pool warm-up (background) and in `xcfs new` worktrees | not done: needs the build service to key compiles without absolute output paths; a large change outside xcfs |
| **Shared dependency resolution** (Bazel's repository cache, Tuist's dependency cache, Gradle's dependency cache) | each checkout resolves and checks out its Swift packages | yes: the first build of a new `xcfs new` worktree of Home Assistant spent 141 s of 394 s outside the build — exporting the PIF, which resolves and checks out its packages (plus the one-time session check) | **measured** 141 s once per new worktree (Home Assistant); 0 for pool slots after warm-up | partly done: `seed` clones listed directories into a new worktree; **next step**: seed the resolved package checkouts automatically |
| **Prebuilt module cache** (Tuist binary cache, Bazel remote cache for third-party targets) | third-party packages are compiled in every checkout | yes: part of every first build | **estimated**: the share of third-party targets in a first build (not measured separately); agents in pool slots never pay it | not done; same path-independence problem as the action cache |
| **Affected-target analysis** (Nx, Turborepo, Tuist selective testing, Bazel `rdeps` queries) | running every test after a small change | likely, for `xcfs test` | not measured (the benchmarks time builds, not test suites) | not done; the Swift driver's dependency graph and digests already know which files an edit invalidated — the input for picking tests |
| **Hermetic actions** (Bazel sandboxing) | build steps that read or write outside their declared inputs and outputs | yes: a script phase that compiles a tool from source on every build (8.6 s on Wikipedia), another that runs `swiftlint --fix` and rewrites sources | **measured**: skipping the localization script: no-op 12 → 1 s on Wikipedia (manual setting) | partly done: lint-only phases are skipped automatically; anything that writes is kept unless configured — xcfs cannot make a project's scripts hermetic |
| **Remote execution** (Bazel RBE, Buck2) | one machine's cores | not for the single-machine agent setups measured | — | out of scope |

## Rewriting parts of xcfs in Rust

Measured first, as the question asked:

| part | xcfs's own time | notes |
|---|---|---|
| process start (Python, imports) | 0.05 s per command | `xcfs --version` |
| a build or check through the session, before this measurement | 0.15–0.40 s per command | profiled on a large app: mostly other processes (`xcrun swiftc --version` 0.14 s), a recursive search for the scheme file (0.10 s), a second parse of the project description |
| the same, after caching those answers per Xcode and per project description | **0.12 s** per command | left: hashing the file list for the project-description key (`git ls-files`, 0.06 s), parsing the project description (0.04 s), building the request (0.02 s) |
| checkout of a pool slot | 0.2–0.3 s | mostly `git checkout` |
| release | 0.9–1.2 s | mostly `git` and the background cleanup handoff |

One agent round makes nine xcfs calls, so xcfs's own time is ≈1.5 s of rounds that took 17–101 s. A Rust
rewrite could remove the interpreter start and some parsing — at most ≈0.1 s per call, ≈1 s per round (1–6 %). The
remaining time is in `swbuild`, `swift-frontend`, `git` and the linker, which a rewrite does not touch. The profile
also showed that the larger part of xcfs's own time was avoidable work, not slow code: caching it in Python took a
no-op's overhead from 0.40 to 0.12 s.

**Recommendation: no rewrite.** Revisit only if a measured per-call overhead above ≈0.3 s comes back that caching
cannot remove.

## What to do next, by measured value

1. **Seed resolved Swift packages into new worktrees** — 141 s once per `xcfs new` worktree on Home Assistant
   (measured); small cost: xcfs already clones seed directories with APFS clonefile.
2. **Port the Swift driver patch to each new Swift release** as Xcode ships it — it carries the largest per-edit gain
   (measured above); cost: the porting work in the driver's notes.
3. **Affected tests for `xcfs test`** — not measured; the driver's dependency information is the input.
4. **Path-independent compilation caching** — large potential for warm-ups and new worktrees, but it needs the build
   service's keys to change; watch Xcode's compilation cache rather than build it in xcfs.
