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
| plain | - | 312.0 s | 312.0 s | 283 / 282 s, 312 / 310 s, 298 / 296 s | 0 | 24.5 GB | 4.9 GB | 4.7 GB |
| xcfs (defaults) | 242.8 s | 212.7 s | 455.5 s | 213 / 189 s, 198 / 174 s, 212 / 190 s | 0 | 8.5 GB | 1.6 GB | 0 MB |
| xcfs (defaults), the next 3 agents on the same pool | 32.1 s | 53.1 s | 85.2 s | 50 / 48 s, 48 / 43 s, 53 / 51 s | 0 | 4.9 GB | 144 MB | 0 MB |
| xcfs (experimental-driver) | 295.6 s | 222.2 s | 517.8 s | 222 / 190 s, 214 / 179 s, 215 / 180 s | 0 | 9.1 GB | 1.8 GB | 0 MB |
| xcfs (experimental-driver), the next 3 agents on the same pool | 26.6 s | 35.9 s | 62.5 s | 36 / 34 s, 31 / 28 s, 32 / 30 s | 0 | 8.0 GB | 45 MB | 0 MB |
| plain, kept worktrees | - | 285.8 s | 285.8 s | 261 / 260 s, 286 / 284 s, 272 / 269 s | 1 | 22.9 GB | 1.2 GB | 0 MB |
| plain, kept worktrees, the next 3 agents | - | 161.1 s | 161.1 s | 134 / 134 s, 160 / 160 s, 161 / 161 s | 1 | 7.4 GB | 281 MB | 4.8 GB |
| plain, kept worktrees, built at the earlier baseline (30 commits back; not compared) | - | 134.5 s | 134.5 s | 134 / 134 s, 133 / 132 s, 133 / 130 s | 0 | 20.4 GB | 4.2 GB | 0 MB |
| plain, kept worktrees, after the baseline moved 30 commits | - | 256.8 s | 256.8 s | 234 / 234 s, 257 / 257 s, 240 / 239 s | 1 | 23.9 GB | 1.2 GB | 4.8 GB |
| xcfs (defaults), after the baseline moved 30 commits | 301.9 s | 76.5 s | 378.4 s | 76 / 74 s, 63 / 58 s, 76 / 74 s | 0 | 10.3 GB | 570 MB | 0 MB |
| xcfs (experimental-driver), after the baseline moved 30 commits | 338.9 s | 70.4 s | 409.3 s | 70 / 68 s, 64 / 60 s, 68 / 66 s | 0 | 9.8 GB | 512 MB | 0 MB |

## Edit loop in the first checkout

| |  |
|---|

## Per checkout

| side | agent | ready to build | first build | disk (real) | checkout (du) | build output (du) | resident memory |
|---|---|---|---|---|---|---|---|

## Removal

| side | agent | time | left behind | processes still running |
|---|---|---|---|---|
