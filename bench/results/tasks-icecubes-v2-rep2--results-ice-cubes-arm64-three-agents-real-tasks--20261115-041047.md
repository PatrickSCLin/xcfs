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
| plain | - | 284.7 s | 284.7 s | 265 / 264 s, 285 / 284 s, 284 / 282 s | 1 | 25.7 GB | 0 MB | 4.6 GB |
| xcfs (defaults) | 211.2 s | 141.6 s | 352.8 s | 140 / 138 s, 140 / 138 s, 142 / 140 s | 0 | 8.7 GB | 1.8 GB | 0 MB |
| xcfs (defaults), the next 3 agents on the same pool | 36.8 s | 126.8 s | 163.6 s | 120 / 118 s, 127 / 125 s, 126 / 124 s | 0 | 8.0 GB | 1.1 GB | 0 MB |
| xcfs (defaults-without-driver-patch) | 220.2 s | 165.7 s | 385.9 s | 159 / 156 s, 159 / 156 s, 166 / 164 s | 0 | 11.9 GB | 2.2 GB | 0 MB |
| xcfs (defaults-without-driver-patch), the next 3 agents on the same pool | 44.1 s | 102.0 s | 146.1 s | 96 / 93 s, 80 / 79 s, 102 / 100 s | 0 | 6.4 GB | 355 MB | 0 MB |

## Edit loop in the first checkout

| |  |
|---|

## Per checkout

| side | agent | ready to build | first build | disk (real) | checkout (du) | build output (du) | resident memory |
|---|---|---|---|---|---|---|---|

## Removal

| side | agent | time | left behind | processes still running |
|---|---|---|---|---|
