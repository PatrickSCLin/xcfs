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
| xcfs (defaults) | 215.6 s | 230.9 s | 446.5 s | 229 / 202 s, 216 / 186 s, 231 / 204 s | 0 | 11.1 GB | 3.4 GB | 0 MB |
| xcfs (defaults), the next 3 agents on the same pool | 30.5 s | 46.0 s | 76.5 s | 41 / 39 s, 38 / 35 s, 46 / 43 s | 0 | 9.0 GB | 29 MB | 0 MB |
| xcfs (experimental-driver) | 302.8 s | 187.6 s | 490.4 s | 188 / 160 s, 169 / 152 s, 170 / 153 s | 0 | 8.8 GB | 3.7 GB | 0 MB |
| xcfs (experimental-driver), the next 3 agents on the same pool | 49.7 s | 53.9 s | 103.6 s | 54 / 52 s, 39 / 34 s, 52 / 48 s | 0 | 7.2 GB | 121 MB | 0 MB |
| plain, kept worktrees | - | 304.7 s | 304.7 s | 276 / 275 s, 305 / 303 s, 293 / 289 s | 0 | 24.1 GB | 316 MB | 0 MB |
| plain, kept worktrees, the next 3 agents | - | 160.9 s | 160.9 s | 136 / 136 s, 161 / 161 s, 160 / 160 s | 1 | 8.0 GB | 249 MB | 4.7 GB |
| plain, kept worktrees, built at the earlier baseline (30 commits back; not compared) | - | 135.2 s | 135.2 s | 135 / 134 s, 135 / 134 s, 133 / 130 s | 0 | 19.8 GB | 4.2 GB | 0 MB |
| plain, kept worktrees, after the baseline moved 30 commits | - | 278.9 s | 278.9 s | 251 / 251 s, 279 / 279 s, 264 / 264 s | 0 | 23.7 GB | 1.2 GB | 4.8 GB |
| xcfs (defaults), after the baseline moved 30 commits | 287.4 s | 76.8 s | 364.2 s | 77 / 74 s, 77 / 61 s, 77 / 75 s | 0 | 7.3 GB | 544 MB | 0 MB |
| xcfs (experimental-driver), after the baseline moved 30 commits | 342.4 s | 69.9 s | 412.3 s | 70 / 68 s, 70 / 58 s, 70 / 64 s | 0 | 4.9 GB | 569 MB | 0 MB |

## Edit loop in the first checkout

| |  |
|---|

## Per checkout

| side | agent | ready to build | first build | disk (real) | checkout (du) | build output (du) | resident memory |
|---|---|---|---|---|---|---|---|

## Removal

| side | agent | time | left behind | processes still running |
|---|---|---|---|---|
