# Xcode versions

xcfs has three parts that depend on the installed Xcode in different ways. Each decides on the machine, for the Xcode
that is selected, whether it can be used; when it cannot, builds go the plain way and nothing else changes.

| part | depends on | when it does not fit |
|---|---|---|
| worktrees, the pool, cleanup, `xcodebuild` builds | `xcodebuild` and `simctl` as documented | — |
| the persistent build session | the `swbuild` tool inside Xcode, which Apple ships as a debugging tool without a stability promise | a regression check runs once per Xcode version (`xcfs doctor --session-check`, or the first build); if it fails, builds use `xcodebuild` |
| the experimental Swift driver | the swift-driver release of the installed Swift; the patch exists for the 6.4 line | builds use Xcode's own driver; `xcfs doctor` says why |

## What was run on which version

| Xcode | Swift | pool and `xcodebuild` | build session | experimental driver |
|---|---|---|---|---|
| 27.2 | 6.4.2 | every benchmark in [benchmark.md](benchmark.md) | regression check passes; every benchmark | built, self-test passes; incremental builds equal clean ones on two apps |
| 27.1 | 6.4.1 | the correctness comparison on one app | used in that comparison | built for it, self-test passes; incremental builds equal clean ones on one app |
| 27.0 | 6.4.0 | the sample project, while v0.2 was developed | regression check passed then | the release tag is known; not measured |
| earlier | — | not run | decided by the regression check | not applied |
| a new release line (Swift 6.5 and later) | — | expected to work | decided by the regression check | not applied until the patch is ported |

"Not run" means nobody has tried it yet, not that it fails: a report from another version is welcome
([CONTRIBUTING.md](../CONTRIBUTING.md)).

## When a new Xcode (or beta) appears

1. Select it (`xcode-select` or `DEVELOPER_DIR`) and run `xcfs doctor --session-check`: the build session's
   regression check on that version (a cold build, a no-op, an edit, a compile error and its location, a one-file
   check, recovery).
2. `tests/sample_e2e.sh`: the sample project end to end — worktree, build, check, test, run, pool, session.
3. `xcfs doctor` in a project: the state of the experimental driver. A patch release of a known line is built and
   self-tested by itself (the patch must apply with `git apply --check`; a release tag is pinned to its commit once
   measured, in `lib/xcfs/adapters/swift_driver.py`). A new release line gets no driver until the patch is ported:
   port it, record the dependency pins it was built with, and run `lib/xcfs/adapters/swiftdriver/selftest.py`.
4. `bench/oracle.py` on a real app: every object file of each incremental build against a clean build of the same
   sources.
5. One benchmark round on an app already in [bench/results](../bench/results/README.md), to compare with the
   previous Xcode.
6. Add the row above, and a line in [CHANGELOG.md](../CHANGELOG.md).

The patch itself, and what upstream swift-driver would need for it not to be a patch, is described in
[incremental-swift.md](incremental-swift.md).
