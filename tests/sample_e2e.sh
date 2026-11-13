#!/bin/bash
# End-to-end check of xcfs on a real Xcode project: examples/generic-sample (XcodeGen project + CocoaPods workspace,
# a local pod, a synchronized folder, Swift/ObjC in one target, a unit-test target).
#
#   tests/sample_e2e.sh [--keep]
#
# Copies the sample into a throw-away git repository whose path contains a space, then walks the agent flow:
#   xcfs new (session set)  ->  xcodebuild inside the worktree  ->  checks  ->  xcfs reap --session --prune-branch
# Needs Xcode with an iOS Simulator SDK, xcodegen and CocoaPods on PATH. Uses its own xcfs state directory.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd -P)
XCFS="${XCFS:-${ROOT}/bin/xcfs}"
KEEP=0; [ "${1:-}" = "--keep" ] && KEEP=1
for t in xcodebuild xcodegen pod git python3; do
  command -v "$t" >/dev/null || { echo "sample_e2e: $t not found on PATH" >&2; exit 2; }
done

work=$(mktemp -d "${TMPDIR:-/tmp}/xcfs-e2e.XXXXXX"); work=$(cd "${work}" && pwd -P)
export XCFS_STATE="${work}/state" XCFS_DD_ROOT="${work}/dd" XDG_CONFIG_HOME="${work}/config" XCFS_SESSION="e2e-session"
unset CLAUDE_CODE_SESSION_ID
mkdir -p "${XCFS_DD_ROOT}"
finish() {
  local code=$?
  if [ "${KEEP}" = 1 ] || [ "${code}" != 0 ]; then echo "sandbox kept: ${work}"; else rm -rf "${work}"; fi
  [ "${code}" = 0 ] && echo "sample_e2e: ok" || echo "sample_e2e: FAILED" >&2
}
trap finish EXIT
step() { printf '\n== %s\n' "$*"; }
fail() { echo "sample_e2e: $*" >&2; exit 1; }
json() { python3 -c 'import json, sys; print(json.loads(sys.argv[1])[sys.argv[2]])' "$1" "$2"; }
secs() { python3 -c 'import time; print("%.1f" % (time.time() - float(__import__("sys").argv[1])))' "$1"; }
now() { python3 -c 'import time; print(time.time())'; }

src="${work}/sample app"
step "prepare a repository from examples/generic-sample"
cp -R "${ROOT}/examples/generic-sample" "${src}"
rm -rf "${src}/Pods" "${src}/GenApp.xcodeproj" "${src}/GenApp.xcworkspace" "${src}/DerivedData" "${src}/Podfile.lock"
cd "${src}"
git init -q -b main
xcodegen generate --quiet
pod install --silent
git add -A
git -c user.name=t -c user.email=t@t.invalid commit -q -m init
[ -z "$(git status --porcelain)" ] || fail "the prepared repository is not clean"

step "xcfs new"
out=$("${XCFS}" new "${src}" feature/x --json)
wt=$(json "${out}" worktree)
echo "worktree: ${wt}  ($(json "${out}" seconds)s, seeded: $(json "${out}" seeded))"
[ -d "${wt}/Pods" ] && [ -d "${wt}/GenApp.xcworkspace" ] || fail "seed directories missing in the worktree"

step "build inside the worktree"
build() {
  xcodebuild -workspace "${wt}/GenApp.xcworkspace" -scheme GenApp -destination 'generic/platform=iOS Simulator' \
    -derivedDataPath "${wt}/DerivedData" CODE_SIGNING_ALLOWED=NO build -quiet >"${work}/build.log" 2>&1 \
    || { tail -30 "${work}/build.log"; fail "xcodebuild failed in the worktree"; }
}
t=$(now); build; echo "cold build: $(secs "${t}")s"
t=$(now); build; echo "no-op build: $(secs "${t}")s"
xcodebuild -workspace "${wt}/GenApp.xcworkspace" -scheme GenApp -destination 'generic/platform=iOS Simulator' \
  -derivedDataPath "${wt}/DerivedData" CODE_SIGNING_ALLOWED=NO build-for-testing -quiet >"${work}/test-build.log" 2>&1 \
  || { tail -30 "${work}/test-build.log"; fail "the unit-test target did not build"; }
echo "unit-test target builds"

step "the worktree is clean and self-contained"
[ -z "$(git -C "${wt}" status --porcelain)" ] || { git -C "${wt}" status --short; fail "git status is not clean after a build"; }
# The worktree path starts with the source path ("…/sample app" vs "…/sample app-wt-…"): only "<source>/" points back.
back=$(grep -rIl -F "${src}/" "${wt}" --exclude-dir=DerivedData --exclude=.git || true)
[ -z "${back}" ] || { echo "${back}"; fail "files in the worktree point back into the source checkout"; }
echo "git status clean; no path points back into the source checkout"

step "xcfs ls --status"
out=$("${XCFS}" ls --session "${XCFS_SESSION}" --status --json)
python3 - "${out}" "${wt}" <<'EOF'
import json, sys
rows = json.loads(sys.argv[1])["worktrees"]
assert [r["worktree"] for r in rows] == [sys.argv[2]] and rows[0]["dirty"] == 0, rows
print("1 worktree, dirty=0")
EOF

step "session end: xcfs reap --session --apply --prune-branch (run from inside the worktree, as a hook may be)"
out=$(cd "${wt}" && "${XCFS}" reap --apply --prune-branch --json)
[ "$(json "${out}" removed)" = 1 ] || fail "reap did not remove the worktree: ${out}"
[ ! -e "${wt}" ] || fail "the worktree directory is still there"
[ "$(git -C "${src}" worktree list | wc -l | tr -d ' ')" = 1 ] || fail "git still lists the worktree"
! git -C "${src}" show-ref --verify --quiet refs/heads/feature/x || fail "the branch xcfs created (no commits) was kept"
[ "$("${XCFS}" ls --json)" = '{"worktrees": [], "ok": true}' ] || fail "the registry is not empty"
echo "worktree, its DerivedData and its empty branch are gone; registry empty"
