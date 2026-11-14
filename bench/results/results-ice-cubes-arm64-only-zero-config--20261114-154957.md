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
| plain | **161.8 s** | create 0.4 s, create 0.0 s, first build 77.8 s, edit 1 + build 8.2 s, edit 2 + build 8.5 s, edit 3 + build 30.3 s, edit 4 + build 6.1 s, edit 5 + build 30.3 s, remove 0.1 s | 1.5 GB |
| xcfs (defaults) | **56.9 s** | checkout 0.2 s, first build 6.3 s, edit 1 + check 7.8 s, edit 2 + check 8.4 s, edit 3 + check 11.0 s, edit 4 + check 6.2 s, edit 5 + check 10.6 s, final build 5.6 s, release 0.9 s | 0 MB |
| xcfs (defaults-without-driver-patch) | **96.8 s** | checkout 0.2 s, first build 6.2 s, edit 1 + check 8.2 s, edit 2 + check 8.6 s, edit 3 + check 30.7 s, edit 4 + check 6.3 s, edit 5 + check 30.3 s, final build 5.4 s, release 0.8 s | 0 MB |

## Edit loop in the first checkout

| |  |
|---|

## Per checkout

| side | agent | ready to build | first build | disk (real) | checkout (du) | build output (du) | resident memory |
|---|---|---|---|---|---|---|---|

## Removal

| side | agent | time | left behind | processes still running |
|---|---|---|---|---|
