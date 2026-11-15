# Benchmark: Ice Cubes (arm64 only, zero-config), 3 parallel checkouts

MacBookPro18,4 · Apple M1 Max, 10 cores, 64 GB · macOS 27.2 · Xcode 27.2 Build version 27B52 · source 98098b9

https://github.com/Dimillian/IceCubesApp, a SwiftUI Mastodon client: an .xcodeproj, 13 local Swift packages and remote packages, built for the iOS Simulator. Plain worktrees are set up as the project's README says and built with xcodebuild into the default DerivedData, as an agent would; xcfs is measured twice: slots driven by xcodebuild, and slots driven by the persistent build session. Swift packages are resolved by each side on its own (Xcode's global package cache is shared by both). This variant builds one architecture (ARCHS=arm64) on every side — what an agent needs for an Apple-silicon simulator — repeats the edit loop twice to show run-to-run noise, and also times `xcfs check` of the edited file.

## Summary

| |  |
|---|
| an agent waits for a new checkout (ready + first build, mean) |  |
| background work before any agent asks |  |
| disk for 3 built checkouts (volume free space used) |  |
| resident memory of their processes after building |  |
| left behind after removing them (plain: removal; xcfs: retiring the pool) |  |

## Agents at the same time

Each agent does the round above, all at once (plain: worktree creation and removal serialized, as git's locks would). Memory: resident memory of the processes the benchmark runs (with build services, compilers and xcfs build sessions), sampled every second.

| side | warm-up before the agents | wall time | wall time incl. warm-up | each agent | peak memory while the agents work (processes) | peak memory during the warm-up | mean memory |
|---|---|---|---|---|---|---|---|
| xcfs (defaults) | 219.3 s | 19.7 s | 239.0 s | 19.7 s, 19.7 s, 19.7 s | 4.6 GB (34) | 59.0 GB | 2.2 GB |
| xcfs (defaults), the next 3 agents on the same pool | 7.5 s | 16.8 s | 24.3 s | 16.8 s, 16.8 s, 16.8 s | 2.5 GB (28) | 4.3 GB | 1.4 GB |

## Edit loop in the first checkout

| |  |
|---|

## Per checkout

| side | agent | ready to build | first build | disk (real) | checkout (du) | build output (du) | resident memory |
|---|---|---|---|---|---|---|---|

## Removal

| side | agent | time | left behind | processes still running |
|---|---|---|---|---|
