# Benchmark: Wikipedia iOS (arm64, three agents, real tasks), 3 parallel checkouts

MacBookPro18,4 · Apple M1 Max, 10 cores, 64 GB · macOS 27.2 · Xcode 27.2 Build version 27B52 · source 0919f3e

https://github.com/wikimedia/wikipedia-ios: Swift and Objective-C, local packages WMFData (imported by ~300 app files) and WMFComponents. Three agents at the same time, each with a small task judged by a build and its own new unit test: a wikitext helper and a URL helper in WMFData, a layout helper in WMFComponents. Each task first writes code that does not compile, fixes it, adds a test class, runs that class and builds the app. Plain: git worktree + scripts/setup_bundle_id ci, xcodebuild into the default DerivedData, tests on the simulator named in the command. xcfs: a pool warmed first (timed), then xcfs check / build / test. One architecture.

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
| plain | - | 387.0 s | 387.0 s | 381 / 379 s, 387 / 383 s, 380 / 374 s | 2 | 15.7 GB | 0 MB | 8.9 GB |
| xcfs (defaults) | 278.5 s | 241.1 s | 519.6 s | 224 / 222 s, 225 / 223 s, 241 / 239 s | 0 | 7.4 GB | 4.4 GB | 0 MB |
| xcfs (defaults), the next 3 agents on the same pool | 39.7 s | 147.1 s | 186.8 s | 143 / 141 s, 147 / 144 s, 147 / 145 s | 0 | 8.2 GB | 369 MB | 0 MB |

## Edit loop in the first checkout

| |  |
|---|

## Per checkout

| side | agent | ready to build | first build | disk (real) | checkout (du) | build output (du) | resident memory |
|---|---|---|---|---|---|---|---|

## Removal

| side | agent | time | left behind | processes still running |
|---|---|---|---|---|
