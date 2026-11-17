# Why not just `git worktree`?

Running several AI agents on one Xcode project needs several checkouts, and `git worktree add` makes one in a second.
What follows is what that leaves to an Xcode project, the usual ways around it, and what each was measured to do.
Machine and apps as in [benchmark.md](benchmark.md): MacBook Pro, Apple M1 Max (10 cores), 64 GB, macOS 27.2,
Xcode 27.2, one simulator architecture; [Ice Cubes](https://github.com/Dimillian/IceCubesApp) at `98098b9` and
[Wikipedia iOS](https://github.com/wikimedia/wikipedia-ios) at `0919f3e`.

## Plain worktrees, each with Xcode's default DerivedData

Xcode keys DerivedData by the project's path, so every worktree gets a bucket of its own:

- **Every worktree builds from nothing.** Three agents with a real task each, at once: a batch takes 284–302 s on Ice
  Cubes and 347–387 s on Wikipedia, every time, because every new worktree's first build is cold (160 s on Ice Cubes
  with three building side by side).
- **The buckets outlive the worktrees.** `git worktree remove` deletes the checkout and leaves its DerivedData: 1.5 GB
  per worktree on Ice Cubes, 2.9 GB on Wikipedia, in a folder nothing cleans up.
- **Each worktree resolves its packages and installs its pods** again: dependency directories are git-ignored, so a
  new worktree has none.
- **Agents that test name the same simulator,** and a test run started by one agent kills another's: one of six test
  runs on Ice Cubes, two of six on Wikipedia.
- **Memory:** 24–25 GB (Ice Cubes) and 15–16 GB (Wikipedia) at the peak, for three agents.

## One DerivedData shared by every worktree (`-derivedDataPath`)

The obvious fix for the cold builds. Measured on Ice Cubes, two worktrees at the same commit, one shared DerivedData,
`xcodebuild` with one architecture ([script](../bench/shared-deriveddata.sh),
[output](../bench/results/shared-deriveddata-icecubes-20261115.txt)):

| build | time | Swift compile tasks |
|---|---|---|
| worktree A, cold | 81.3 s | 1,890 |
| A again, nothing changed | 5.9 s | 0 |
| worktree B (same sources, another path) | 62.0 s | 1,890 |
| A again, after B | 59.7 s | 1,890 |
| A and B at once | 5.2 s and 60.4 s | 0 and 1,890 |

Build state is bound to absolute paths, so the second worktree recompiles everything, and then the first does again:
two worktrees sharing a DerivedData take turns invalidating each other, and each switch costs about a cold build.
Both also write the same `Build/Products`, so the app an agent tests or runs is whichever worktree built last.

## Copying a DerivedData into a new worktree

The same paths problem: the copy's build state names the old worktree's paths. xcfs does the opposite — a slot keeps
its path for life, and branches move through it — because slot paths that never change are what lets a build be
reused at all ([design.md](design.md#slot-lifecycle)).

## A few long-lived worktrees, switching branches in them

This is the right idea, and it is what xcfs's pool is. What it takes to make it work unattended, for agents:

- keeping each one built at the baseline in the background, and bringing an idle one along when the baseline moves;
- handing out the best free one (built at the current baseline and toolchain first) to whoever asks, never the same
  one twice, and waiting or failing clearly when all are taken;
- refusing to switch or remove one that holds uncommitted work, commits on no branch, or changed git-ignored files;
- a simulator per checkout, created and booted during the warm-up;
- build output outside the source tree, removed with the checkout, and a record of everything created, so that cleanup
  never guesses;
- the dependency layer cloned into each one with APFS clonefile, so a checkout costs blocks only where it differs;
- an incremental build session instead of `xcodebuild`'s fixed cost (a no-op 5.9 → 0.9 s on Ice Cubes), and a check
  of single files (1–4 s).

With that, the next batch of three agents takes 138–151 s on Ice Cubes instead of 284–302 s and 248–264 s on Wikipedia
instead of 347–363 s, at 8–12 and 11–13 GB instead of 24–25 and 15–16, and removal leaves nothing behind; with the
opt-in experimental Swift driver, 111–121 s and 187–203 s. The first batch on a fresh pool is slower than
plain worktrees (it pays for the warm-up), and starting without a warm-up is slower still (451 s on Ice Cubes, one
run): warm the pool before the agents need it.

## Build caches and other build systems

A content-addressed cache (Bazel's disk or remote cache, Xcode's compilation cache) attacks the same cost from another
side: the same compile redone in every checkout. How much each idea is worth on these apps, and which xcfs uses, is in
[build-systems.md](build-systems.md); Xcode's compilation cache is an option (`compileCache`), measured in
[benchmark.md](benchmark.md#a-shared-compilation-cache).

## What xcfs does not solve

- A cold build is still a cold build: the pool moves it to before the agents need it.
- One checkout, day to day, gets the build session and `xcfs check`, not the pool.
- CI machines that start empty every time.
