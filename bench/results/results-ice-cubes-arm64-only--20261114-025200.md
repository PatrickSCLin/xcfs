# Benchmark: Ice Cubes (arm64 only), 3 parallel checkouts

MacBookPro18,4 · Apple M1 Max, 10 cores, 64 GB · macOS 27.2 · Xcode 27.2 Build version 27B52 · source 98098b9

https://github.com/Dimillian/IceCubesApp, a SwiftUI Mastodon client: an .xcodeproj, 13 local Swift packages and remote packages, built for the iOS Simulator. Plain worktrees are set up as the project's README says and built with xcodebuild into the default DerivedData, as an agent would; xcfs is measured twice: slots driven by xcodebuild, and slots driven by the persistent build session. Swift packages are resolved by each side on its own (Xcode's global package cache is shared by both). This variant builds one architecture (ARCHS=arm64) on every side — what an agent needs for an Apple-silicon simulator — repeats the edit loop twice to show run-to-run noise, and also times `xcfs check` of the edited file.

## Summary

| | plain worktrees | xcfs (xcodebuild) | xcfs (session) |
|---|---|---|---|
| an agent waits for a new checkout (ready + first build, mean) | 75.1 s | 6.4 s | 2.0 s |
| background work before any agent asks | - | 214.3 s | 216.9 s |
| disk for 3 built checkouts (volume free space used) | 4.6 GB | 5.6 GB (incl. warm-up) | 5.6 GB (incl. warm-up) |
| resident memory of their processes after building | 0 MB | 0 MB | 71 MB |
| left behind after removing them (plain: removal; xcfs: retiring the pool) | 4.4 GB, 0 process(es) | 0 MB, 0 process(es) | 0 MB, 0 process(es) |

## Edit loop in the first checkout

| | plain worktrees | xcfs (xcodebuild) | xcfs (session) |
|---|---|---|---|
| no-op build | 5.9 s | 5.2 s | 0.9 s |
| edit a function body in a leaf package (Timeline) | 7.9 s | 5.4 s | 3.1 s |
| edit a function body in a package 12 others depend on (Models) | 8.4 s | 5.2 s | 3.7 s |
| add a public declaration to that package | 30.0 s | 5.4 s | 24.8 s |
| edit a function body in a leaf package (Timeline) (repeat 2) | 7.5 s | 5.3 s | 3.1 s |
| edit a function body in a package 12 others depend on (Models) (repeat 2) | 8.2 s | 5.2 s | 3.5 s |
| add a public declaration to that package (repeat 2) | 30.3 s | 5.4 s | 24.3 s |
| edit a function body in a leaf package (Timeline) — xcfs check of the file | - | 8.1 s | 2.5 s |
| edit a function body in a package 12 others depend on (Models) — xcfs check of the file | - | 8.6 s | 1.2 s |
| add a public declaration to that package — xcfs check of the file | - | 30.4 s | 1.7 s |
| edit a function body in a leaf package (Timeline) (repeat 2) — xcfs check of the file | - | 8.0 s | 1.5 s |
| edit a function body in a package 12 others depend on (Models) (repeat 2) — xcfs check of the file | - | 8.5 s | 1.2 s |
| add a public declaration to that package (repeat 2) — xcfs check of the file | - | 30.5 s | 1.9 s |

## Per checkout

| side | agent | ready to build | first build | disk (real) | checkout (du) | build output (du) | resident memory |
|---|---|---|---|---|---|---|---|
| plain worktrees | 1 | 0.4 s | 80.3 s | 1.5 GB | 85 MB | 1.5 GB | 0 MB (0 proc) |
| plain worktrees | 2 | 0.3 s | 72.0 s | 1.5 GB | 85 MB | 1.5 GB | 0 MB (0 proc) |
| plain worktrees | 3 | 0.3 s | 71.9 s | 1.5 GB | 85 MB | 1.5 GB | 0 MB (0 proc) |
| xcfs (xcodebuild) | 1 | 0.2 s | 6.3 s | 0 MB | 85 MB | 2.0 GB | 0 MB (0 proc) |
| xcfs (xcodebuild) | 2 | 0.2 s | 6.2 s | -3 MB | 85 MB | 2.0 GB | 0 MB (0 proc) |
| xcfs (xcodebuild) | 3 | 0.2 s | 6.1 s | -1 MB | 85 MB | 2.0 GB | 0 MB (0 proc) |
| xcfs (session) | 1 | 0.2 s | 1.9 s | 0 MB | 85 MB | 2.0 GB | 24 MB (1 proc) |
| xcfs (session) | 2 | 0.2 s | 1.7 s | 1 MB | 85 MB | 2.0 GB | 24 MB (1 proc) |
| xcfs (session) | 3 | 0.2 s | 1.7 s | 0 MB | 85 MB | 2.0 GB | 23 MB (1 proc) |

## Removal

| side | agent | time | left behind | processes still running |
|---|---|---|---|---|
| plain | 1 | 0.1 s | 1 cache(s), 1.5 GB | 0 (0 MB) |
| plain | 2 | 0.1 s | 1 cache(s), 1.5 GB | 0 (0 MB) |
| plain | 3 | 0.1 s | 1 cache(s), 1.5 GB | 0 (0 MB) |
| xcfs (xcodebuild) release | 1 | 0.8 s | (slot kept for the next lease) | 0 (0 MB) |
| xcfs (xcodebuild) release | 2 | 0.8 s | (slot kept for the next lease) | 0 (0 MB) |
| xcfs (xcodebuild) release | 3 | 0.8 s | (slot kept for the next lease) | 0 (0 MB) |
| xcfs (xcodebuild) pool resize 0 | all | 0.7 s | 0 cache(s), 0 MB | 0 (0 MB) |
| xcfs (session) release | 1 | 0.9 s | (slot kept for the next lease) | 0 (0 MB) |
| xcfs (session) release | 2 | 0.9 s | (slot kept for the next lease) | 0 (0 MB) |
| xcfs (session) release | 3 | 0.9 s | (slot kept for the next lease) | 0 (0 MB) |
| xcfs (session) pool resize 0 | all | 0.7 s | 0 cache(s), 0 MB | 0 (0 MB) |
