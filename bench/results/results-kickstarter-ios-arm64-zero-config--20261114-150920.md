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
| plain | **312.0 s** | create 1.5 s, create 0.0 s, first build 167.1 s, edit 1 + build 18.4 s, edit 2 + build 18.9 s, edit 3 + build 45.1 s, edit 4 + build 16.8 s, edit 5 + build 43.7 s, remove 0.5 s | 5.7 GB |
| xcfs (defaults) | **163.9 s** | checkout 0.3 s, first build 17.1 s, edit 1 + check 18.2 s, edit 2 + check 18.9 s, edit 3 + check 37.7 s, edit 4 + check 17.1 s, edit 5 + check 37.2 s, final build 16.1 s, release 1.2 s | 0 MB |
| xcfs (defaults-without-driver-patch) | **184.0 s** | checkout 0.3 s, first build 17.3 s, edit 1 + check 19.1 s, edit 2 + check 19.7 s, edit 3 + check 47.4 s, edit 4 + check 17.6 s, edit 5 + check 44.9 s, final build 16.4 s, release 1.1 s | 0 MB |

## Edit loop in the first checkout

| |  |
|---|

## Per checkout

| side | agent | ready to build | first build | disk (real) | checkout (du) | build output (du) | resident memory |
|---|---|---|---|---|---|---|---|

## Removal

| side | agent | time | left behind | processes still running |
|---|---|---|---|---|
