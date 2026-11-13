# Benchmark: parallel checkouts of Telegram-iOS

What does it cost to give three agents their own checkout of a large iOS app, the plain way and through xcfs?

- **Project:** [Telegram-iOS](https://github.com/TelegramMessenger/Telegram-iOS) at `24638bc6` — built with Bazel
  through its own `build-system/Make/Make.py`, 12 git submodules, ≈8,200 build actions for the simulator app
  (`debug_sim_arm64`).
- **Machine:** MacBook Pro, Apple M1 Max, 10 cores, 64 GB, macOS 27.2, Xcode 27.2 (the project asks for 27.1;
  `--overrideXcodeVersion`), Bazel 9.2.0.
- **Harness:** [bench/bench.py](../bench/bench.py) with [bench/profiles/telegram-ios.json](../bench/profiles/telegram-ios.json),
  three checkouts per side, one run per side and scenario. Single runs: treat differences under ~10 % as noise.

## Method

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

## Results

The two runs differ in one thing: in the first, the shared cache lacked 15 long-running actions (the source
checkout's build had not stored them), so every new checkout ran them itself — a 139 s critical path. The second run,
an hour later, found them cached. A cache in real use sits somewhere between the two.

### Run 2 — the shared cache has everything

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

### Run 1 — the shared cache misses a few long actions

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

## What this shows

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
- **Memory:** each checkout's Bazel server holds 2–4 GB while it lives. Plain worktrees keep theirs until Bazel's idle
  timeout (3 h) even after the worktree is gone; xcfs keeps a slot's server between leases (so the next lease does not
  re-analyze) and stops it when the slot is retired, or on release with `"stopServerOnRelease": true`.

## Reproduce

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
