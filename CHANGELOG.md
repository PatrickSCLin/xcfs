# Changelog

What changed for people and scripts that use xcfs. The interface that is kept stable is listed in
[docs/stability.md](docs/stability.md).

## Unreleased

- README: what xcfs solves for parallel agents, with benchmark charts drawn from `bench/results`
  (`scripts/charts.py`).
- `CONTRIBUTING.md`, `SECURITY.md`, this file; `docs/stability.md` (what is stable); `docs/xcode-versions.md` (which
  Xcode versions each part was run on, and the checklist for a new one).
- Tests run in CI on macOS with the current Python 3 and with 3.9.

## 0.2.0 — 2026-11-15

The first tagged release.

- **Worktrees:** `new`, `rm`, `reap`, `adopt`, `ls` — the dependency layer cloned with APFS clonefile, build output
  outside the source tree, a registry of what xcfs created; nothing removed that holds unsaved work.
- **Pool:** `checkout`, `release`, `pool status | warm | resize` — slots kept built at the baseline, warmed in the
  background, one simulator per checkout.
- **Building:** `build`, `check`, `test`, `run` through `xcodebuild` or a persistent build session (`"driver": "auto"`
  uses the session once the installed Xcode passes the regression check); a Bazel adapter; external adapters.
- **Experimental Swift driver** for the Swift 6.4 line, built and self-tested in the background, with a gate on how
  many compiler frontends run at once; `"swiftDriver": "native"` turns it off.
- **Settings:** `compileCache` (`off`, `dependencies`, `all`), `warmJobs`, `sessionIdleMinutes`.
- **Agents:** `xcfs hook worktree-create | worktree-remove` for Claude Code's worktree hooks, `--session`, `--json` on
  every command.
- **Setup:** `xcfs init` detects the project, scheme, platform and files to carry (`--write`, `--user`);
  `xcfs doctor`; `xcfs trust`; a Homebrew formula.
