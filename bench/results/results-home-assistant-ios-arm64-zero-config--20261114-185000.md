# Benchmark: Home Assistant iOS (arm64, zero-config), 3 parallel checkouts

MacBookPro18,4 · Apple M1 Max, 10 cores, 64 GB · macOS 27.2 · Xcode 27.2 Build version 27B52 · source f160c75

https://github.com/home-assistant/iOS: an .xcodeproj with a large app target, a shared framework (Shared) ~550 app files import, many extensions and Swift packages, and script phases (SwiftGen and others); not seen while xcfs was developed.

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
| xcfs (defaults) | **98.9 s** | checkout 0.2 s, first build 4.3 s, edit 1 + check 17.4 s, edit 2 + check 8.0 s, edit 3 + check 7.6 s, edit 4 + check 27.5 s, edit 5 + check 5.3 s, final build 27.3 s, release 1.1 s | 0 MB |
| xcfs (defaults-without-driver-patch) | **159.9 s** | checkout 0.2 s, first build 4.3 s, edit 1 + check 19.0 s, edit 2 + check 8.4 s, edit 3 + check 6.9 s, edit 4 + check 53.3 s, edit 5 + check 7.2 s, final build 59.4 s, release 1.1 s | 0 MB |

## Edit loop in the first checkout

| |  |
|---|

## Per checkout

| side | agent | ready to build | first build | disk (real) | checkout (du) | build output (du) | resident memory |
|---|---|---|---|---|---|---|---|

## Removal

| side | agent | time | left behind | processes still running |
|---|---|---|---|---|
