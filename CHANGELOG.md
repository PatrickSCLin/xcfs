# Changelog

What changed for people and scripts that use xcfs. The interface that is kept stable is listed in
[docs/stability.md](docs/stability.md).

## Unreleased

- Benchmarks: three agents on an empty pool without a warm-up (`bench.py --phases tasks-cold`): slower than plain
  worktrees and than warming first; the quick start keeps the warm-up.
- `xcfs stats [--days N]`: checkouts handed out built, the full builds not waited for, builds and checks, from a
  local history (`history.jsonl` in the state directory; nothing is sent anywhere).
- Refusals and failures carry `next`: what to do, by exit code, and for known Xcode build failures (full disk,
  out-of-date precompiled module, simulator, signing, package or CocoaPods resolution, the build session).
- `xcfs pool status --watch`: a warm-up's step, elapsed time and an estimate from the last one, until it is done.
- `xcfs init` recognizes XcodeGen and Tuist projects (the generated project is seeded, keyed by its spec) and suggests
  `seedKey` from the tracked lock files.
- `xcfs checkout` of a slot that was never built says how to keep slots built (`hint` in the JSON).
- `xcfs doctor` advises slot counts and parallel warm-ups for the machine's memory (`advice` in the JSON).
- `docs/index.md`, `docs/llms.txt` and `docs/_config.yml`: a GitHub Pages site from `docs/`, and an index for
  language models.
- `xcfs uninstall [--apply]`: list everything xcfs made on the machine and remove it; what holds unsaved work is
  kept.
- `xcfs hook install | uninstall | status [repo] [--apply]`: put the Claude Code hooks in the user's or a
  repository's settings, or take them out; other hooks are kept, nothing is written without `--apply`.
- `xcfs hook session-end`: the session-end hook as a command (no `jq` needed); the earlier `xcfs reap` line in a
  settings file is replaced by `xcfs hook install`.
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
