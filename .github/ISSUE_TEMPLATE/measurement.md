---
name: Measurement
about: Numbers from your project — faster, slower, more memory, more disk
labels: measurement
---

**Machine:** model, chip, memory, macOS, Xcode.

**Project**, roughly: targets, Swift files, package manager. A public repository and commit if there is one.

**What I ran** (agents at once, pool size, the settings that differ from the defaults):

**What I saw** — plain worktrees against xcfs: time until the first build finished, an edit and build, peak memory,
disk per checkout, what was left after removal. Where xcfs was slower is as useful as where it was faster.

**Results file**, if `bench/bench.py` produced one.
