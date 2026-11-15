# What is stable

Agents and scripts depend on xcfs's output, so this is what a release keeps, and what it may change. xcfs follows
`MAJOR.MINOR.PATCH`; before 1.0 a minor release (0.x) may change what is listed under "stable" only with a note in
[CHANGELOG.md](../CHANGELOG.md), and a patch release never does.

## Stable

- **The commands and flags** in the README's command table, and that every command takes `--json`.
- **`--json` output:** exactly one JSON object on stdout, with `"ok": true|false`, and on failure an `error` string.
  Fields are added, not renamed or removed. Of each command's result, these are stable:
  - `checkout`, `new`, `hook worktree-create`: the checkout's path (`worktree`; the hook prints the path alone).
  - `build`, `check`, `test`: `ok`, `seconds`, `diagnostics` (`file`, `line`, `column`, `severity`, `message`);
    for tests also `tests`, `failures` and `failed` (the names of the failed tests); `check` says what it compiled
    against in `against`. A project with several toolchains gets one such object per toolchain under `results`.
  - `reap`: what was left because it holds unsaved work, under `left`.
  - `pool status`: `summary` per project (`ready`, `free`, `leased`, `warming`, `next_checkout`,
    `next_ready_in_seconds`).
  - On a refusal or a failure: `next`, one sentence on what to do (its presence is stable, its wording is not).
- **Exit codes** 0–11, as the README lists them; `reap` exits 0 when it leaves something, so that a hook does not fail.
- **The config** (`.xcfs.json`, `~/.config/xcfs/<repo>.json`): `version: 1` and its keys. A key is not renamed or
  given another meaning within a version; unknown keys are an error, so a config written for a later release fails
  loudly on an earlier one.
- **The promises about deletion:** nothing without `--apply`, nothing that holds unsaved work, nothing xcfs did not
  create.
- **The hook interface:** `xcfs hook worktree-create`, `worktree-remove` and `session-end` read the JSON an agent
  tool sends on stdin (`name`, `cwd`, `session_id`; `worktree_path`; `session_id`).

## Not stable

- Human-readable output (everything without `--json`).
- The layout of the state, cache and pool directories, and the registry's format: use `xcfs ls --json` and
  `xcfs pool status --json`, not the files. A release migrates what an earlier one wrote, or says so.
- Fields of `--json` results not named above (`breakdown` and `timing` among them), `doctor`'s checks and their wording.
- The adapter protocol for external adapters (`xcfs-adapter-<name>`): it has been used by the built-in adapters and a
  test adapter only, and will change when a second real toolchain needs it.
- The experimental Swift driver: which Xcode releases it applies to, its cache, and its settings beyond
  `"swiftDriver": "native"`.
- `bench/` and `scripts/`.
