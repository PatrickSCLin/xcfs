# Changelog

What changed for people and scripts that use xcfs. The interface that is kept stable is listed in
[docs/stability.md](docs/stability.md).

## Unreleased

### Fixed

- `xcfs test` boots the checkout's own simulator itself and keeps it booted: when `xcodebuild` booted it, it shut it
  down again after the run, so every test paid a boot and a first launch (on one Ice Cubes slot a first test after a
  boot took 61 s, a test on a booted, settled simulator 5 s). The simulator now stays booted for
  `simulatorIdleMinutes` (30) after its last use — warm-up, lease, test, run, release — and a watcher shuts it down
  then; a booted simulator holds about 3.4 GB. A lease starts booting it in the background if it is not (adapter op
  `lease`). A plain checkout's tests run on the base device, booted by `xcodebuild` as before.
- A slot's warm-up waits for its new simulator to settle after the first boot (it keeps 4–9 cores busy for about four
  minutes on an M1 Max), so the agents do not start on a busy machine: a fresh Ice Cubes pool's warm-up 216–243 →
  501–509 s, its first batch of three agents 213–231 → 94–118 s (the two together 429–474 → 595–627 s).

### Added

- Benchmarks: the fair comparison (2026-11-18, two runs per app): worktrees kept between batches and switched by a
  script, and the next batch after main moved 30 (Ice Cubes) and 208 (Wikipedia) commits, plus two more runs of xcfs
  after the settle change. Against kept worktrees the agents wait 46–97 against 161 s on Ice Cubes and 130–185 against
  185–186 s on Wikipedia; after main moved, 77 against
  257–279 s and 174–189 against 217–254 s. The README, the charts, the benchmark page, the comparison page, the index
  and llms.txt lead with these; the earlier runs stay, dated.
- Benchmarks: three agents on Wikipedia iOS with xcfs's default (no experimental driver), two runs with 0.3.1 beside
  plain worktrees of the same day: the next batch 248–264 s against 347–363 s, 11–13 GB against 15–16 GB, and 0 of 6
  test runs killed against 3 of 6. The README, the charts and the benchmark page use them; Wikipedia no longer has
  numbers with the driver only.
- Benchmarks: Xcode.app beside xcfs on one slot, measured headless (`bench/xcode-app-beside.sh`): Xcode.app builds
  its own copy once, both build side by side, and retiring the slot removes its DerivedData.
- Builds through `xcodebuild` report where their time went too (`breakdown`: starting xcodebuild, preparing —
  packages, dependency graph, build description —, the build's tasks, the rest), from when its lines arrived.
- `xcfs doctor` shows the experimental driver's frontend gate (places, and how many are in use) when the driver is on.
- `docs/agents.md`: how a worktree manager or orchestrator hands worktree creation and removal to xcfs.

## 0.3.1 — 2026-11-16

### Security

- The experimental driver's wrapper (only written with `"swiftDriver": "patched"`) put the frontend's path inside
  double quotes: a backquote or `$(...)` in the selected Xcode's path ran as a shell command on every Swift compile,
  and a quote broke the wrapper. The path is now quoted for the shell. Spaces and non-ASCII already worked, and Xcode
  takes such a `SWIFT_EXEC` too (checked).

### Changed

- README: the quick start installs the Claude Code hooks; the intro names the plugin and the other agent tools; the
  warm-up memory figures say which side they come from; `XCFS_SWIFT_DRIVER` in the environment table.
- Test t65 judges the frontend gate by when the jobs started, not by total time (it failed once on a slow CI runner).

## 0.3.0 — 2026-11-16

### Changed

- **The experimental Swift driver is opt-in.** By default builds use Xcode's own Swift driver, and xcfs no longer
  clones or builds swift-driver on its own. Turn the patched driver on per project with `"swiftDriver": "patched"`
  (or `XCFS_SWIFT_DRIVER=patched`); `"api-digests"`, its earlier name, still works. A config that says `"auto"` (the
  default before 0.3) now means `"native"`, and `xcfs doctor` says so. A patched driver already built in
  `~/Library/Caches/xcfs/swift-driver` is left there unused until a project asks for it; `xcfs uninstall` removes it.
- The README and the benchmark pages show xcfs without the driver as the main numbers (on Ice Cubes the next batch of
  three agents takes 138–151 s instead of 284–302 s with plain worktrees), and the driver as an option (111–121 s).
  Numbers are relabelled, not measured again; Wikipedia's three-agent runs exist with the driver only and say so.
- The SessionEnd hook has a 60 s timeout (Claude Code gives SessionEnd hooks 1.5 s in all by default);
  `xcfs hook install` updates an installed one.

### Added

- **Agent tools:** `xcfs hook install | uninstall | status [repo] [--apply]` for the Claude Code hooks (other hooks
  kept, nothing written without `--apply`); `xcfs hook session-end` (no `jq` needed); a Claude Code plugin
  (`plugins/xcfs`, marketplace `.claude-plugin/marketplace.json`: `claude plugin marketplace add PatrickSCLin/xcfs`,
  `claude plugin install xcfs@xcfs`) with the hooks and a skill; `examples/agent-instructions` for any tool's
  instruction file (`AGENTS.md`, `GEMINI.md`, …); `docs/agents.md` opens with the contract (steps, fields, exit codes);
  `xcfs pool status --json` has a `summary` per project for orchestrators.
- **First use:** `xcfs checkout` of a never-built slot says how to keep slots built (`hint`); `xcfs doctor` advises
  slot counts and parallel warm-ups for the machine's memory (`advice`); `xcfs init` recognizes XcodeGen and Tuist
  projects, suggests `seedKey` from tracked lock files and notes macOS, watchOS, visionOS and tvOS targets;
  `xcfs pool status --watch` shows a warm-up's step, elapsed time and estimate.
- **Diagnosis:** every refusal and failure carries `next`, what to do (by exit code, and for known Xcode build
  failures: full disk, out-of-date precompiled module, simulator, signing, package or CocoaPods resolution, the build
  session); session builds report where their time went (`breakdown`); `xcfs doctor <checkout>` reports its build
  session, project description and simulator; `xcfs stats [--days N]` from a local history (nothing is sent anywhere).
- **Resources:** `minFreeGB` (keep space free by retiring other projects' idle slots, least recently used first),
  `maxSessions` (at most this many build sessions alive, the longest idle stops first), `xcfs gc --sizes`.
- **Single checkout:** `xcfs session start [path]`: the build session and its project description ready, nothing
  built.
- **Leaving:** `xcfs uninstall [--apply]` lists everything xcfs made on the machine and removes it; what holds unsaved
  work is kept.
- **Benchmarks and docs:** charts drawn from the raw results (`scripts/charts.py`); `docs/why-not-plain-worktrees.md`
  with a new measurement (two worktrees sharing one DerivedData recompile everything each time the other one built);
  three agents without a warm-up (slower than plain worktrees and than warming first); the cost of exporting the
  project description; `bench/run-public.sh` to rerun the three-agent benchmark on a public app;
  `CONTRIBUTING.md`, `SECURITY.md`, this file, `docs/stability.md`, `docs/xcode-versions.md`; a GitHub Pages site and
  `llms.txt` in `docs/`.
- Tests run in CI on macOS with the current Python 3 and with 3.9.

### Fixed

- The remove and session-end hooks no longer refuse because the agent tool's own processes still have their working
  directory in the worktree (found running the plugin in a live session).

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
