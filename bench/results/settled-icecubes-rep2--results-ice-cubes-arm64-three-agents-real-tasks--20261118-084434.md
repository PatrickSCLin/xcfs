# Benchmark: Ice Cubes (arm64, three agents, real tasks), 3 parallel checkouts

MacBookPro18,4 · Apple M1 Max, 10 cores, 64 GB · macOS 27.2 · Xcode 27.2 Build version 27B52 · source 98098b9c

https://github.com/Dimillian/IceCubesApp. Three agents at the same time, each with a small task defined in advance and judged by a build and the package's own tests: a public helper added to Models (12 packages depend on it), a computed property added to Timeline's filter enum, a helper added to Env (most feature packages depend on it). Each task first writes code that does not compile (a typo, a non-exhaustive switch, an optional not unwrapped), fixes it, adds a unit test, runs the package's tests and builds the app. Plain: git worktree + the README's setup, xcodebuild into the default DerivedData, tests on the simulator named in the command (as an agent would write it). xcfs: slots warmed first (the warm-up is timed and reported), xcfs check / build / test (each checkout its own simulator). One architecture (ARCHS=arm64).

## Summary

| |  |
|---|
| an agent waits for a new checkout (ready + first build, mean) |  |
| background work before any agent asks |  |
| disk for 3 built checkouts (volume free space used) |  |
| resident memory of their processes after building |  |
| left behind after removing them (plain: removal; xcfs: retiring the pool) |  |

## Real tasks, agents at the same time

Each agent: a checkout, a first build, its task's steps (each edit verified: plain by a build or the task's tests; xcfs by `xcfs check`, `xcfs build` or `xcfs test`), removal. Waiting = the sum of the agent's build, check and test times. Unexpected = a verification that passed or failed against the task's definition.

| side | warm-up (before agents) | wall time | wall time incl. warm-up | each agent: total / waiting | unexpected | peak memory | peak disk | left behind |
|---|---|---|---|---|---|---|---|---|
| xcfs (defaults) | 509.0 s | 118.0 s | 627.0 s | 117 / 115 s, 100 / 93 s, 118 / 116 s | 0 | 10.7 GB | 2.7 GB | 0 MB |
| xcfs (defaults), the next 3 agents on the same pool | 36.8 s | 96.7 s | 133.5 s | 93 / 91 s, 89 / 86 s, 97 / 95 s | 0 | 6.5 GB | 771 MB | 0 MB |
| xcfs (experimental-driver) | 571.2 s | 107.4 s | 678.6 s | 107 / 106 s, 101 / 96 s, 101 / 97 s | 0 | 9.5 GB | 2.2 GB | 0 MB |
| xcfs (experimental-driver), the next 3 agents on the same pool | 28.7 s | 75.0 s | 103.7 s | 75 / 72 s, 60 / 56 s, 49 / 45 s | 0 | 8.3 GB | 543 MB | 0 MB |

## Edit loop in the first checkout

| |  |
|---|

## Per checkout

| side | agent | ready to build | first build | disk (real) | checkout (du) | build output (du) | resident memory |
|---|---|---|---|---|---|---|---|

## Removal

| side | agent | time | left behind | processes still running |
|---|---|---|---|---|
