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
| plain | - | 289.2 s | 289.2 s | 286.1 s, 286.0 s, 286.2 s | 21.4 GB (67) | - | 6.3 GB |
| xcfs (defaults) | 293.8 s | 19.5 s | 313.3 s | 19.5 s, 19.5 s, 19.5 s | 4.6 GB (34) | 22.9 GB | 2.1 GB |
| xcfs (defaults), the next 3 agents on the same pool | 16.3 s | 17.2 s | 33.5 s | 17.2 s, 17.2 s, 17.1 s | 3.3 GB (29) | 1.3 GB | 1.4 GB |
| xcfs (defaults) beside a warm-up of 2 slots | 112.0 s | 34.4 s | 146.4 s | 34.4 s | 23.7 GB (122) | 22.8 GB | 4.0 GB |

## Edit loop in the first checkout

| |  |
|---|

## Per checkout

| side | agent | ready to build | first build | disk (real) | checkout (du) | build output (du) | resident memory |
|---|---|---|---|---|---|---|---|

## Removal

| side | agent | time | left behind | processes still running |
|---|---|---|---|---|
