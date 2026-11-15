# Contributing to xcfs

Bug reports, measurements from real projects, and patches are welcome.

## Reporting

- **A bug:** open an issue with the command, what happened, what you expected, and the output of
  `xcfs doctor <repo> --json` (it names the Xcode and Swift versions, the build driver in use and the state of the
  experimental Swift driver). Remove anything private from paths and names first.
- **A measurement:** numbers from a project of yours are the most useful contribution — the machine, the size of the
  project (targets, Swift files), what you ran, and the times, memory and disk you saw, including where xcfs was
  slower. `bench/bench.py` produces a results file in the same format as [bench/results](bench/results/README.md).
- **A security problem:** see [SECURITY.md](SECURITY.md).

## Changing the code

```
python3 tests/xcfs_test.py                                     # the whole suite, about 3 minutes
/usr/bin/python3 tests/xcfs_test.py --python /usr/bin/python3  # the same on Python 3.9
python3 tests/xcfs_test.py --only t03,t36                      # some of it
tests/sample_e2e.sh                                            # needs Xcode, xcodegen and CocoaPods
```

- Python 3, standard library only; it has to keep running on 3.9 (the `python3` of the Xcode command-line tools).
- Every change in behavior comes with a test in `tests/xcfs_test.py`. The tests build throw-away repositories in a
  sandbox of their own and need no Xcode.
- The core (`lib/xcfs/*.py`) names no toolchain and no project; a test enforces the first. What differs between
  toolchains goes in an adapter (`lib/xcfs/adapters/`, or an external `xcfs-adapter-<name>`; the protocol is at the
  top of `lib/xcfs/adapter.py`); what differs between projects goes in the project's `.xcfs.json`.
- Nothing is deleted from a user's machine without `--apply`, and nothing that holds unsaved work or that xcfs did not
  create. A new command keeps that property.
- A number in the documentation comes from a results file in `bench/results`, and says where it is slower as well.
- English for code, comments, documentation and commit messages. Examples use neutral names (`MyApp`, `feature/x`,
  `~/src/app`); nothing may name a private project, company, host or person.
- What is stable for scripts and agents — the JSON fields, the exit codes, the config keys — is listed in
  [docs/stability.md](docs/stability.md); changing one of those needs a note in [CHANGELOG.md](CHANGELOG.md).

## Pull requests

One change per pull request, with its test, and the suite passing on both Pythons. Say what you measured if the change
is about speed, memory or disk.
