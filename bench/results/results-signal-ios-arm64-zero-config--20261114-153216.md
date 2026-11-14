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
| plain | **323.8 s** | create 0.7 s, first build 126.2 s, edit 1 + build 25.0 s, edit 2 + build 26.8 s, edit 3 + build 60.7 s, edit 4 + build 23.7 s, edit 5 + build 60.3 s, remove 0.4 s | 6.1 GB |

## Edit loop in the first checkout

| |  |
|---|

## Per checkout

| side | agent | ready to build | first build | disk (real) | checkout (du) | build output (du) | resident memory |
|---|---|---|---|---|---|---|---|

## Removal

| side | agent | time | left behind | processes still running |
|---|---|---|---|---|
