# Benchmark: Signal iOS (arm64, zero-config), 3 parallel checkouts

MacBookPro18,4 · Apple M1 Max, 10 cores, 64 GB · macOS 27.2 · Xcode 27.2 Build version 27B52 · source a690a26

https://github.com/signalapp/Signal-iOS: a workspace with a large app target and frameworks (SignalServiceKit, SignalUI) in Swift and Objective-C, plus Swift packages; not seen while xcfs was developed.

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
| xcfs (defaults) | **101.2 s** | checkout 0.4 s, first build 2.3 s, edit 1 + check 1.7 s, edit 2 + check 17.9 s, edit 3 + check 24.6 s, edit 4 + check 1.0 s, edit 5 + check 16.5 s, final build 35.7 s, release 0.9 s | 0 MB |
| xcfs (defaults-without-driver-patch) | **109.6 s** | checkout 0.3 s, first build 2.2 s, edit 1 + check 1.6 s, edit 2 + check 19.5 s, edit 3 + check 18.1 s, edit 4 + check 0.9 s, edit 5 + check 18.1 s, final build 47.8 s, release 1.0 s | 0 MB |

## Edit loop in the first checkout

| |  |
|---|

## Per checkout

| side | agent | ready to build | first build | disk (real) | checkout (du) | build output (du) | resident memory |
|---|---|---|---|---|---|---|---|

## Removal

| side | agent | time | left behind | processes still running |
|---|---|---|---|---|
