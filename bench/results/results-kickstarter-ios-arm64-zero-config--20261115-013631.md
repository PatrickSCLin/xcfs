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
| plain | **278.7 s** | create 1.2 s, create 0.0 s, first build 133.7 s, edit 1 + build 18.4 s, edit 2 + build 18.9 s, edit 3 + build 44.3 s, edit 4 + build 16.9 s, edit 5 + build 44.0 s, remove 0.5 s | 5.7 GB |

## Edit loop in the first checkout

| |  |
|---|

## Per checkout

| side | agent | ready to build | first build | disk (real) | checkout (du) | build output (du) | resident memory |
|---|---|---|---|---|---|---|---|

## Removal

| side | agent | time | left behind | processes still running |
|---|---|---|---|---|
