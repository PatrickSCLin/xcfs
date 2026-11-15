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
| plain | - | 374.8 s | 374.8 s | 356 / 354 s, 375 / 372 s, 356 / 352 s | 0 | 15.3 GB | 8.4 GB | 8.8 GB |
| xcfs (defaults) | 277.8 s | 259.1 s | 536.9 s | 242 / 239 s, 247 / 244 s, 259 / 257 s | 0 | 7.1 GB | 4.7 GB | 0 MB |
| xcfs (defaults), the next 3 agents on the same pool | 40.5 s | 162.5 s | 203.0 s | 159 / 157 s, 162 / 160 s, 148 / 146 s | 0 | 5.4 GB | 345 MB | 0 MB |

## Edit loop in the first checkout

| |  |
|---|

## Per checkout

| side | agent | ready to build | first build | disk (real) | checkout (du) | build output (du) | resident memory |
|---|---|---|---|---|---|---|---|

## Removal

| side | agent | time | left behind | processes still running |
|---|---|---|---|---|
