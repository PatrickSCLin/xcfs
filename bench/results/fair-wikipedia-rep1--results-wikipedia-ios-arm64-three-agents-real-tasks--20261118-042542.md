# Benchmark: Wikipedia iOS (arm64, three agents, real tasks), 3 parallel checkouts

MacBookPro18,4 · Apple M1 Max, 10 cores, 64 GB · macOS 27.2 · Xcode 27.2 Build version 27B52 · source 0919f3eafd

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
| xcfs (defaults) | 245.9 s | 320.9 s | 566.8 s | 321 / 289 s, 298 / 266 s, 321 / 291 s | 0 | 11.0 GB | 6.2 GB | 0 MB |
| xcfs (defaults), the next 3 agents on the same pool | 72.9 s | 154.4 s | 227.3 s | 154 / 152 s, 148 / 146 s, 136 / 132 s | 0 | 9.0 GB | 3.2 GB | 0 MB |
| plain, kept worktrees | - | 473.6 s | 473.6 s | 300 / 298 s, 474 / 470 s, 299 / 293 s | 0 | 14.9 GB | 0 MB | 0 MB |
| plain, kept worktrees, the next 3 agents | - | 186.2 s | 186.2 s | 186 / 186 s, 184 / 184 s, 184 / 184 s | 0 | 14.4 GB | 128 MB | 8.9 GB |
| plain, kept worktrees, built at the earlier baseline (208 commits back; not compared) | - | 174.7 s | 174.7 s | 158 / 157 s, 173 / 170 s, 175 / 170 s | 0 | 14.0 GB | 8.3 GB | 0 MB |
| plain, kept worktrees, after the baseline moved 208 commits | - | 254.1 s | 254.1 s | 238 / 238 s, 254 / 253 s, 238 / 237 s | 0 | 17.0 GB | 648 MB | 8.9 GB |
| xcfs (defaults), after the baseline moved 208 commits | 267.7 s | 189.2 s | 456.9 s | 156 / 153 s, 152 / 148 s, 189 / 186 s | 0 | 12.1 GB | 3.8 GB | 0 MB |

## Edit loop in the first checkout

| |  |
|---|

## Per checkout

| side | agent | ready to build | first build | disk (real) | checkout (du) | build output (du) | resident memory |
|---|---|---|---|---|---|---|---|

## Removal

| side | agent | time | left behind | processes still running |
|---|---|---|---|---|
