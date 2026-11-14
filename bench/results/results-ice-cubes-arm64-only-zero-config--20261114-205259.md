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

## One agent's round of work

Plain: worktree + setup, first build, 5 edits each verified by a build, removal. xcfs: checkout of a slot warmed beforehand, first build, the same edits each verified by `xcfs check`, one final build, release.

| side | total | steps | left behind |
|---|---|---|---|
| xcfs (defaults) | **15.8 s** | checkout 0.2 s, first build 1.7 s, edit 1 + check 2.1 s, edit 2 + check 1.3 s, edit 3 + check 1.3 s, edit 4 + check 2.0 s, edit 5 + check 1.0 s, final build 5.1 s, release 0.9 s | 0 MB |
| xcfs (defaults-without-driver-patch) | **38.1 s** | checkout 0.2 s, first build 1.8 s, edit 1 + check 2.4 s, edit 2 + check 1.4 s, edit 3 + check 1.5 s, edit 4 + check 8.8 s, edit 5 + check 1.8 s, final build 19.3 s, release 0.9 s | 0 MB |

## Edit loop in the first checkout

| |  |
|---|

## Per checkout

| side | agent | ready to build | first build | disk (real) | checkout (du) | build output (du) | resident memory |
|---|---|---|---|---|---|---|---|

## Removal

| side | agent | time | left behind | processes still running |
|---|---|---|---|---|
