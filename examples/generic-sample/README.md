# generic-sample

A small iOS app that exercises the project shapes `xcfs` has to cope with, in one workspace:

- an XcodeGen-generated `.xcodeproj` (`project.yml`) inside a CocoaPods `.xcworkspace`;
- a local pod (`LocalPod`) linked as a static framework, with no explicit dependency from the app target on the Pods
  project (the build only works with implicit dependencies);
- a synchronized folder (`Synced/`), whose file list Xcode expands when the project is loaded;
- Swift and Objective-C in one target through a bridging header;
- a Run Script phase and a unit-test target.

The generated project, the workspace and `Pods/` are git-ignored; [.xcfs.json](.xcfs.json) clones them into every new
worktree, so a worktree builds without running `xcodegen` or `pod install` again.

## Try it

```
cp -R examples/generic-sample ~/src/sample && cd ~/src/sample
git init -q -b main && xcodegen generate && pod install && git add -A && git commit -qm init
xcfs checkout . feature/x            # creates a slot on first use; prints its path
cd "$(xcfs pool status . --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["slots"][0]["worktree"])')"
xcfs build && xcfs test && xcfs run
xcfs release --prune-branch
```

Set `"driver": "session"` in `.xcfs.json` (or `XCFS_DRIVER=session`) to build through the persistent session.

[tests/sample_e2e.sh](../../tests/sample_e2e.sh) does all of this in a throw-away directory and checks the results:
a worktree that builds, tests and runs on its own simulator and stays clean; a pool slot that is handed out warm and
follows its baseline; session builds, single-file checks and a new file in the synchronized folder; and that
everything — checkouts, build output, simulators, sessions, branches — is gone afterwards.
