# Security

## Reporting a problem

Report a vulnerability privately through GitHub: the repository's **Security** tab, **Report a vulnerability**.
Please do not open a public issue for it. Fixes are made on `main` and released as a new version; only the latest
release is supported.

## What xcfs runs and touches

xcfs runs as the user who starts it: no root, no kernel extension, no telemetry, and no network access of its own
beyond the two `git` operations below. The only processes it leaves running are a background warm-up of a pool and a
checkout's build session, which quits after `sessionIdleMinutes` (30) unused.

- **Commands from a repository's config.** `.xcfs.json` may name commands (`afterSeed`, a baseline `command`, a
  toolchain's setup). They run only after `xcfs trust <repo>`, and the approval is bound to a hash of the commands: a
  changed config has to be approved again (exit 3 until then).
- **Tools it starts:** `git`, and for the Xcode adapter `xcodebuild`, `xcrun simctl`, the `swbuild` tool inside Xcode
  and `swiftc`; for the Bazel adapter the project's `bazel`. An external adapter is an executable named
  `xcfs-adapter-<name>` on `PATH`, run only when a project's config lists it.
- **Network:** `git fetch` in the repositories it manages (to bring idle slots to the baseline), and, for the
  experimental Swift driver, a `git clone` of `https://github.com/swiftlang/swift-driver` at the release tag of the
  installed Swift. The tools it starts use the network as they would without xcfs (package resolution, for one).
- **The experimental Swift driver** is a patched build of that clone, used for Swift compilation on the Xcode
  releases it applies to once its self-test has passed. For the releases the patch was made on, the clone is used
  only if the tag points at the commit recorded in xcfs; the patch is applied with `git apply`, never fuzzily. It is
  built in the user's cache directory and nothing in Xcode is modified. `"swiftDriver": "native"` in a configuration,
  or `XCFS_SWIFT_DRIVER=native`, turns it off; `xcfs doctor` shows whether it is in use.
- **What it writes:** its state (`~/.local/state/xcfs`), its caches (`~/Library/Caches/xcfs`), its pools
  (`~/Library/Developer/xcfs/pools`), the worktrees it creates, and simulators it creates for its checkouts.
- **What it deletes:** only what its registry says it created, only with `--apply`, and never something that holds
  unsaved work. Global DerivedData it did not create is removed only with `gc --foreign --apply`.
