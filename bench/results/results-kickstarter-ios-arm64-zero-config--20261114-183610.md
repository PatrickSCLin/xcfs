# Benchmark: Kickstarter iOS (arm64, zero-config), 3 parallel checkouts

MacBookPro18,4 · Apple M1 Max, 10 cores, 64 GB · macOS 27.2 · Xcode 27.2 Build version 27B52 · source 8a9f360

https://github.com/kickstarter/ios-oss: an .xcodeproj with local Swift packages (KsApi, Library, ...), not seen while xcfs was developed; setup copies the repository's Secrets.swift.example as its Makefile does.

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
| xcfs (defaults) | **33.2 s** | checkout 0.3 s, first build 3.3 s, edit 1 + check 6.0 s, edit 2 + check 3.6 s, edit 3 + check 3.8 s, edit 4 + check 5.4 s, edit 5 + check 3.1 s, final build 6.3 s, release 1.3 s | 0 MB |
| xcfs (defaults-without-driver-patch) | **73.9 s** | checkout 0.3 s, first build 3.4 s, edit 1 + check 6.8 s, edit 2 + check 3.8 s, edit 3 + check 3.2 s, edit 4 + check 22.7 s, edit 5 + check 3.3 s, final build 29.3 s, release 1.1 s | 0 MB |

## Edit loop in the first checkout

| |  |
|---|

## Per checkout

| side | agent | ready to build | first build | disk (real) | checkout (du) | build output (du) | resident memory |
|---|---|---|---|---|---|---|---|

## Removal

| side | agent | time | left behind | processes still running |
|---|---|---|---|---|
