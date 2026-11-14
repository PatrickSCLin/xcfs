# Raw results behind docs/benchmark.md

Each file is what [bench.py](../bench.py) wrote for one run (`results-<profile>-<start time>.json` and `.md`; paths
shortened to placeholders). A run never overwrites another's files. When a run takes one side from an earlier run
(`--reuse-from`), its JSON names that run in `reused_from`.

| table in docs/benchmark.md | file(s) |
|---|---|
| Ice Cubes, one architecture | plain side: `…ice-cubes-arm64-only--20261114-025200`; xcfs sides: `…ice-cubes-arm64-only--20261114-062541` (a rerun of the xcfs sides with `--reuse-from` the first) |
| Wikipedia iOS, one architecture | plain side: `…wikipedia-ios-arm64-only--20261114-031103`; xcfs sides: `…wikipedia-ios-arm64-only--20261114-063751` (as above) |
| One agent's round of work | `…ice-cubes-arm64-only-agent-round--20261114-102021`, `…wikipedia-ios-arm64-only-agent-round--20261114-102452` |
| the same with the experimental Swift driver (superseded) | `…ice-cubes-arm64-only-agent-round--20261114-134936`, `…wikipedia-ios-arm64-only-agent-round--20261114-135601` |
| Zero configuration | per project, the plain round and the xcfs rounds (the last run, after the fixes the table's text lists): Kickstarter `…150920` / `…183610`, Element X `…152658` / `…182835`, Signal `…153216` / `…184224`, Home Assistant `…153836` / `…185000`, Ice Cubes `…154957` / `…190207` (and a later repeat `…205259`), Wikipedia `…155808` / `…190515` |

The xcfs sides of `…150920`, `…154957` and `…155808` were measured before those fixes and are not used.

In the one-architecture runs, the xcodebuild column's per-edit times in the docs are those of `xcfs check`: with the
`xcodebuild` driver a check is a full build, and the build that follows it is a no-op (≈5.3 s on Ice Cubes, ≈15.5 s
on Wikipedia).

Runs of the two-architecture tables and of Telegram-iOS were not kept.
