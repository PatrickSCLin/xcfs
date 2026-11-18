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
| xcfs (defaults) | 501.0 s | 93.7 s | 594.7 s | 86 / 84 s, 72 / 68 s, 94 / 86 s | 0 | 8.9 GB | 1.9 GB | 0 MB |
| xcfs (defaults), the next 3 agents on the same pool | 82.4 s | 77.1 s | 159.5 s | 77 / 75 s, 73 / 70 s, 74 / 71 s | 0 | 8.8 GB | 528 MB | 0 MB |
| xcfs (experimental-driver) | 572.3 s | 89.2 s | 661.5 s | 89 / 81 s, 77 / 72 s, 77 / 73 s | 0 | 9.2 GB | 1.7 GB | 0 MB |
| xcfs (experimental-driver), the next 3 agents on the same pool | 56.6 s | 80.1 s | 136.7 s | 80 / 78 s, 74 / 69 s, 62 / 54 s | 0 | 7.2 GB | 732 MB | 0 MB |

## Edit loop in the first checkout

| |  |
|---|

## Per checkout

| side | agent | ready to build | first build | disk (real) | checkout (du) | build output (du) | resident memory |
|---|---|---|---|---|---|---|---|

## Removal

| side | agent | time | left behind | processes still running |
|---|---|---|---|---|
