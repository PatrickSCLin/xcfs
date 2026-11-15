# Benchmark: Ice Cubes (arm64, three agents, real tasks), 3 parallel checkouts

MacBookPro18,4 · Apple M1 Max, 10 cores, 64 GB · macOS 27.2 · Xcode 27.2 Build version 27B52 · source 98098b9

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
| plain | - | 286.2 s | 286.2 s | 268 / 268 s, 286 / 285 s, 275 / 274 s | 0 | 23.4 GB | 3.3 GB | 4.6 GB |
| xcfs (defaults) | 208.7 s | 144.4 s | 353.1 s | 142 / 140 s, 143 / 141 s, 144 / 143 s | 0 | 7.3 GB | 2.0 GB | 0 MB |
| xcfs (defaults), the next 3 agents on the same pool | 37.9 s | 115.7 s | 153.6 s | 115 / 113 s, 113 / 111 s, 116 / 114 s | 0 | 9.3 GB | 679 MB | 0 MB |
| xcfs (defaults-without-driver-patch) | 219.1 s | 150.7 s | 369.8 s | 140 / 138 s, 140 / 138 s, 151 / 149 s | 0 | 11.3 GB | 1.9 GB | 0 MB |
| xcfs (defaults-without-driver-patch), the next 3 agents on the same pool | 43.7 s | 113.3 s | 157.0 s | 108 / 106 s, 92 / 90 s, 113 / 112 s | 0 | 6.5 GB | 717 MB | 0 MB |

## Edit loop in the first checkout

| |  |
|---|

## Per checkout

| side | agent | ready to build | first build | disk (real) | checkout (du) | build output (du) | resident memory |
|---|---|---|---|---|---|---|---|

## Removal

| side | agent | time | left behind | processes still running |
|---|---|---|---|---|
