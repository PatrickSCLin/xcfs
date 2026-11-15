---
name: xcfs
description: How to get a checkout, build, compile-check files, run tests and finish a task in an Xcode project managed by xcfs. Use when working in an iOS or macOS Xcode project in a worktree, or before building, checking or testing one.
---

## Checkouts and builds (xcfs)

This repository is an Xcode project that several agents work on at once. Every agent works in a checkout of its own,
and every checkout, build and test goes through `xcfs`. Never use plain `git worktree add`, a copy, `xcodebuild` into
the default DerivedData, or `rm -rf` on a checkout or build directory.

1. **Get a checkout.** `xcfs checkout <repo> <branch> --session "$SESSION" --json`, then work in the `worktree` path
   it returns: a slot that is already built. Exit 10 means every slot is taken: retry with `--wait 600`, or run
   `xcfs new <repo> <branch> --session "$SESSION" --json` for a plain worktree.
2. **Check while editing.** `xcfs check <file>... --json` compiles just those files against the last build (1–4 s).
   A pass means "these files compile against what was last built", not "the app builds".
3. **Build.** `xcfs build --json` inside the checkout. Read `diagnostics` (file, line, column, severity, message);
   do not scrape logs.
4. **Test.** `xcfs test --json` (or `--scheme <package>Tests`, `--only Target/Class/method`) on the checkout's own
   simulator; failed tests are listed in `failed`.
5. **Finish.** Commit, then `xcfs release --prune-branch --json` (a slot) or
   `xcfs rm <worktree> --apply --prune-branch --json` (a worktree).

On any non-zero exit code read `error` and `next` in the JSON, and do what `next` says. Exit 5 (uncommitted work) and
exit 7 (changed carried files) mean xcfs refused to lose something: commit or ask, never force. Exit 3 means the
repository's config wants to run commands: ask the user to review them and run `xcfs trust <repo>`. Exit 11 means the
disk is short: report it, do not delete files to make room.
