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
xcfs new . feature/x
xcodebuild -workspace ../sample-wt-feature-x/GenApp.xcworkspace -scheme GenApp \
  -destination 'generic/platform=iOS Simulator' -derivedDataPath ../sample-wt-feature-x/DerivedData \
  CODE_SIGNING_ALLOWED=NO build
xcfs rm ../sample-wt-feature-x --apply --prune-branch
```

[tests/sample_e2e.sh](../../tests/sample_e2e.sh) does the same in a throw-away directory and checks the result: the
worktree builds, `git status` stays clean, nothing in it points back into the source checkout, and `xcfs reap` removes
it with its DerivedData and its empty branch.
