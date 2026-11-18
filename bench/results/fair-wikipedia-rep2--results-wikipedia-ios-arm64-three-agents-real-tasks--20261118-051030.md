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
| xcfs (defaults) | 291.8 s | 298.9 s | 590.7 s | 299 / 276 s, 282 / 258 s, 293 / 269 s | 0 | 10.1 GB | 6.2 GB | 0 MB |
| xcfs (defaults), the next 3 agents on the same pool | 71.9 s | 184.9 s | 256.8 s | 185 / 181 s, 176 / 172 s, 184 / 181 s | 0 | 11.4 GB | 2.5 GB | 0 MB |
| plain, kept worktrees | - | 343.0 s | 343.0 s | 320 / 317 s, 343 / 339 s, 318 / 311 s | 0 | 14.3 GB | 0 MB | 0 MB |
| plain, kept worktrees, the next 3 agents | - | 185.3 s | 185.3 s | 185 / 185 s, 184 / 183 s, 185 / 185 s | 0 | 16.0 GB | 112 MB | 8.9 GB |
| plain, kept worktrees, built at the earlier baseline (208 commits back; not compared) | - | 171.9 s | 171.9 s | 168 / 166 s, 172 / 169 s, 144 / 138 s | 0 | 11.0 GB | 8.2 GB | 0 MB |
| plain, kept worktrees, after the baseline moved 208 commits | - | 217.3 s | 217.3 s | 217 / 217 s, 211 / 210 s, 215 / 214 s | 0 | 17.5 GB | 595 MB | 8.9 GB |
| xcfs (defaults), after the baseline moved 208 commits | 257.7 s | 173.7 s | 431.4 s | 171 / 169 s, 160 / 156 s, 174 / 171 s | 0 | 11.9 GB | 3.1 GB | 0 MB |

## Edit loop in the first checkout

| |  |
|---|

## Per checkout

| side | agent | ready to build | first build | disk (real) | checkout (du) | build output (du) | resident memory |
|---|---|---|---|---|---|---|---|

## Removal

| side | agent | time | left behind | processes still running |
|---|---|---|---|---|
