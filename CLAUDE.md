# xcfs — rules for coding agents

xcfs is an open-source tool. Everything tracked in this repository is public.

## Hard rules

1. **Nothing private, anywhere it can be published.** No name of a private project, company, product, internal
   repository, host, ticket, person, or home directory may appear in code, comments, documentation, tests,
   fixtures, examples, file names, commit messages, branch names, or pull-request text. Use neutral names
   (`MyApp`, `core`, `feature/x`, `example.com`, `~/src/app`).
   - The concrete list of forbidden terms is in `plan/PRIVATE_TERMS.txt`, which is git-ignored on purpose:
     a public file cannot name what must not be named. Read it before writing anything.
   - Run `scripts/check-private-terms.sh` before every commit. The local `pre-commit` and `commit-msg` hooks run
     it too; if they are missing (fresh clone), reinstall them:
     `printf '#!/bin/sh\nexec "$(git rev-parse --show-toplevel)/scripts/check-private-terms.sh" --staged\n' > .git/hooks/pre-commit`
     `printf '#!/bin/sh\nexec "$(git rev-parse --show-toplevel)/scripts/check-private-terms.sh" --message "$1"\n' > .git/hooks/commit-msg`
     `chmod +x .git/hooks/pre-commit .git/hooks/commit-msg`
   - Never bypass the check (`--no-verify`, editing the term list to make a commit pass, `git add -f`).

2. **`plan/` is private and git-ignored.** It holds the design history, measurements, and prototypes this tool grew
   out of, and it names private things freely. Never commit it, never force-add anything under it, and never copy
   text out of it into a tracked file without rewriting it in neutral terms. Measurements may be cited as numbers
   ("a 5 GB dependency layer"), not by whose project they came from.

3. **The tool knows no project.** No project name, directory name, scheme, or path is hard-coded in the tool. What
   differs between projects goes in the project's config (`.xcfs.json`); what differs between toolchains goes in
   an adapter. Tests use throw-away repositories they create themselves.

4. **English** for code, comments, documentation, and commit messages.

5. **Do not commit or push unless asked.** Nothing is deleted from a user's machine without `--apply`; keep that
   property in every new command.

## Layout

- `bin/xcfs` — the tool (v0.1): one Python 3 file, standard library only, must keep running on Python 3.9.
- `tests/xcfs_test.py` — end-to-end tests in a throw-away sandbox; run with both `python3` and `/usr/bin/python3`.
  `tests/sample_e2e.sh` — the sample project end to end (needs Xcode, xcodegen, CocoaPods).
- `examples/generic-sample/` — a small XcodeGen + CocoaPods workspace and its `.xcfs.json`; the fixture for real builds.
- `docs/` — public design notes and agent usage, written in neutral terms (not copied from `plan/`).
- `plan/` — private notes (ignored): design history, measurements, prototypes.
- `scripts/check-private-terms.sh` — the gate described above.

Every change to `bin/xcfs` comes with a test; run the whole suite before committing.
