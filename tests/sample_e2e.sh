#!/bin/bash
# End-to-end check of xcfs on a real Xcode project: examples/generic-sample (XcodeGen project + CocoaPods workspace,
# a local pod, a synchronized folder, Swift/ObjC in one target, a unit-test target).
#
#   tests/sample_e2e.sh [--keep]
#
# Copies the sample into a throw-away git repository whose path contains a space, then walks the agent flow:
#   xcfs new (session set) -> xcfs build / check / test / run -> checks -> xcfs reap --session --prune-branch
# Needs Xcode with an iOS Simulator SDK, xcodegen and CocoaPods on PATH. Uses its own xcfs state directory.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd -P)
XCFS="${XCFS:-${ROOT}/bin/xcfs}"
KEEP=0; [ "${1:-}" = "--keep" ] && KEEP=1
for t in xcodebuild xcodegen pod git python3; do
  command -v "$t" >/dev/null || { echo "sample_e2e: $t not found on PATH" >&2; exit 2; }
done

work=$(mktemp -d "${TMPDIR:-/tmp}/xcfs-e2e.XXXXXX"); work=$(cd "${work}" && pwd -P)
export XCFS_STATE="${work}/state" XCFS_DD_ROOT="${work}/dd" XDG_CONFIG_HOME="${work}/config" XCFS_SESSION="e2e-session" \
  XCFS_CACHE="${work}/cache" XCFS_POOL_ROOT="${work}/pool" XCFS_DRIVER=xcodebuild \
  XCFS_SWIFT_DRIVER=native   # the experimental Swift driver is not built in this sandbox
unset CLAUDE_CODE_SESSION_ID
mkdir -p "${XCFS_DD_ROOT}"
finish() {
  local code=$?
  if [ "${KEEP}" = 1 ] || [ "${code}" != 0 ]; then echo "sandbox kept: ${work}"; else rm -rf "${work}"; fi
  [ "${code}" = 0 ] && echo "sample_e2e: ok" || echo "sample_e2e: FAILED" >&2
}
trap finish EXIT
# xcfs must never leave folders in the global DerivedData: every build it runs has its own.
GLOBAL_DD=~/Library/Developer/Xcode/DerivedData
# Xcode's own shared caches are allowed: every default build uses them too
dd_before=$( (ls "${GLOBAL_DD}" 2>/dev/null; printf '%s\n' ModuleCache.noindex CompilationCache.noindex SDKStatCaches.noindex) | sort -u)
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

step "xcfs build: cold, then no-op"
out=$("${XCFS}" build "${wt}" --json) || fail "xcfs build failed: ${out}"
echo "cold build: $(json "${out}" seconds)s"
out=$("${XCFS}" build "${wt}" --json) || fail "no-op build failed: ${out}"
echo "no-op build: $(json "${out}" seconds)s; output in $(json "${out}" derived_data)"
case "$(json "${out}" derived_data)" in "${wt}"/*) fail "build output landed inside the worktree";; esac

step "xcfs check reports an error in the file it was asked about"
echo 'enum Broken { static func f() -> Int { return "x" } }' >> "${wt}/App/Other.swift"
set +e; out=$("${XCFS}" check "${wt}/App/Other.swift" --json); code=$?; set -e
[ "${code}" = 8 ] || fail "check of a broken file exited ${code}: ${out}"
python3 - "${out}" "${wt}/App/Other.swift" <<'EOF2'
import json, sys
d = json.loads(sys.argv[1])
assert d["diagnostics"] and d["diagnostics"][0]["file"] == sys.argv[2] and d["diagnostics"][0]["line"] == 2, d["diagnostics"]
print("exit 8, error at %s:%d" % (d["diagnostics"][0]["file"].rsplit("/", 1)[-1], d["diagnostics"][0]["line"]))
EOF2
git -C "${wt}" checkout -q -- App/Other.swift
"${XCFS}" check "${wt}/App/Other.swift" --json >/dev/null || fail "check after the fix failed"

step "xcfs test on the worktree's own simulator"
out=$("${XCFS}" test "${wt}" --json) || fail "xcfs test failed: ${out}"
echo "tests=$(json "${out}" tests) failures=$(json "${out}" failures) in $(json "${out}" seconds)s"

step "xcfs run: install and launch"
out=$("${XCFS}" run "${wt}" --json) || fail "xcfs run failed: ${out}"
sim=$(json "${out}" simulator)
echo "launched $(json "${out}" bundle_id) as pid $(json "${out}" pid) on ${sim}"

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
sleep 2
! xcrun simctl list devices | grep -q "${sim}" || fail "the worktree's simulator was not deleted"
echo "worktree, its build output, its simulator and its empty branch are gone; registry empty"

step "nothing in the global DerivedData (worktree part)"
dd_new=$(comm -13 <(echo "${dd_before}") <(ls "${GLOBAL_DD}" 2>/dev/null | sort))
[ -z "${dd_new}" ] || fail "new folders in the global DerivedData: ${dd_new}"
echo "none"

step "pool: warm one slot (a full build at the baseline, as the background would do)"
out=$("${XCFS}" pool warm "${src}" --slots 1 --json) || fail "pool warm failed: ${out}"
python3 - "${out}" <<'EOF2'
import json, sys
s = json.loads(sys.argv[1])["slots"]
assert len(s) == 1 and s[0]["ok"], s
assert s[0].get("simulator_first_boot") is not None, "the slot's simulator was not prepared: %s" % s
print("slot warmed in %.1fs; its simulator created and booted once in %.1fs" % (s[0]["seconds"], s[0]["simulator_first_boot"]))
EOF2

step "pool: check out a branch, build it (the slot is already built at the baseline)"
t=$(now); out=$("${XCFS}" checkout "${src}" feature/pool --json) || fail "checkout failed: ${out}"
slotwt=$(json "${out}" worktree)
[ "$(json "${out}" warm_at_baseline)" = True ] || fail "the slot was not warm at the baseline: ${out}"
echo "checkout: $(secs "${t}")s -> ${slotwt}"
out=$("${XCFS}" build "${slotwt}" --json) || fail "build in the slot failed: ${out}"
echo "first build after checkout: $(json "${out}" seconds)s"
echo 'enum Added { static func v() -> Int { 1 } }' >> "${slotwt}/App/Other.swift"
out=$("${XCFS}" build "${slotwt}" --json) || fail "incremental build in the slot failed: ${out}"
echo "build after an edit: $(json "${out}" seconds)s"
git -C "${slotwt}" -c user.name=t -c user.email=t@t.invalid commit -qam "work in the slot"

step "pool: the baseline moves while the slot is leased; release; the slot follows the baseline"
echo '// baseline change' >> "${src}/App/App.swift"
git -C "${src}" -c user.name=t -c user.email=t@t.invalid commit -qam "baseline moves"
out=$("${XCFS}" release "${slotwt}" --no-warm --json) || fail "release failed: ${out}"
git -C "${src}" show-ref --verify --quiet refs/heads/feature/pool || fail "the branch with a commit was lost"
out=$("${XCFS}" pool warm "${src}" --json) || fail "warm after release failed: ${out}"
[ "$(git -C "${slotwt}" rev-parse HEAD)" = "$(git -C "${src}" rev-parse main)" ] || fail "the slot is not at the new baseline"
python3 - "${out}" <<'EOF2'
import json, sys
s = json.loads(sys.argv[1])["slots"][0]
assert s["ok"], s
print("idle update to the new baseline: %.1fs" % s["seconds"])
EOF2
t=$(now); out=$("${XCFS}" checkout "${src}" feature/pool --json) || fail "second checkout failed: ${out}"
out=$("${XCFS}" build "${slotwt}" --json) || fail "build of the existing branch failed: ${out}"
echo "checkout of an existing branch + build: $(secs "${t}")s"
"${XCFS}" release "${slotwt}" --no-warm --json >/dev/null || fail "second release failed"

step "pool: retire"
out=$("${XCFS}" pool resize "${src}" 0 --json) || fail "resize failed: ${out}"
slotdir=$(dirname "${slotwt}")
python3 -c 'import os, sys, time
t = time.time()
while os.path.exists(sys.argv[1]) and time.time() - t < 60: time.sleep(0.2)
sys.exit(1 if os.path.exists(sys.argv[1]) else 0)' "${slotdir}" || fail "the slot directory is still there"
[ "$(git -C "${src}" worktree list | wc -l | tr -d ' ')" = 1 ] || fail "git still lists the slot"
echo "slot retired; git worktree list is back to the main checkout"

step "session: regression check on this Xcode"
out=$("${XCFS}" doctor "${src}" --session-check --json) || fail "doctor --session-check failed: ${out}"
python3 - "${out}" <<'EOF2'
import json, sys
print("\n".join(m for m in json.loads(sys.argv[1]).get("problems", [])) or "passed")
EOF2

step "session: build through a persistent swbuild session"
export XCFS_DRIVER=session
out=$("${XCFS}" new "${src}" feature/session --json) || fail "new failed: ${out}"
swt=$(json "${out}" worktree)
report() { python3 -c 'import json, sys
d = json.loads(sys.argv[1]); print("%-34s %5.2fs  compile tasks %-3s description reused %s" % (sys.argv[2], d["seconds"], d.get("compile_tasks"), d.get("reused_description")))
b = d.get("breakdown") or {}
print("%-34s pif %.2fs, session start %.2fs, planning %.2fs, tasks %.2fs, other %.2fs" % ("", b.get("pif_seconds", -1), b.get("session_start_seconds", -1), b.get("planning_seconds", -1), b.get("tasks_seconds", -1), b.get("other_seconds", -1)))
assert d.get("driver") == "session", d
assert b and abs(sum(b[k] for k in ("pif_seconds", "session_start_seconds", "tasks_seconds", "planning_seconds", "other_seconds")) - d["seconds"]) < 0.5, (d["seconds"], b)' "$1" "$2"; }
out=$("${XCFS}" session start "${swt}" --json) || fail "session start failed: ${out}"
[ "$(json "${out}" driver)" = session ] || fail "session start did not start a session: ${out}"
echo "session start: $(json "${out}" seconds)s (PIF and session, nothing built)"
out=$("${XCFS}" build "${swt}" --json) || fail "session cold build failed: ${out}"; report "${out}" "cold build"
[ "$(json "${out}" pif_exported)" = False ] || fail "the cold build after session start exported the PIF again"
out=$("${XCFS}" build "${swt}" --json) || fail "session no-op failed: ${out}"; report "${out}" "no-op"
[ "$(json "${out}" compile_tasks)" = 0 ] || fail "a no-op compiled something"
echo 'enum Added { static func v() -> Int { 1 } }' >> "${swt}/App/Other.swift"
out=$("${XCFS}" build "${swt}" --json) || fail "session edit build failed: ${out}"; report "${out}" "edit a file"
echo 'enum Broken { static func f() -> Int { return "x" } }' >> "${swt}/App/Other.swift"
set +e; out=$("${XCFS}" check "${swt}/App/Other.swift" --json); code=$?; set -e
[ "${code}" = 8 ] || fail "session check of a broken file exited ${code}: ${out}"
report "${out}" "check (buildFiles), broken file"
git -C "${swt}" checkout -q -- App/Other.swift
out=$("${XCFS}" check "${swt}/App/Other.swift" --json) || fail "session check failed: ${out}"; report "${out}" "check (buildFiles), fixed"
echo 'enum Fresh { static func v() -> Int { 2 } }' > "${swt}/Synced/Fresh.swift"
out=$("${XCFS}" build "${swt}" --json) || fail "a new file in a synchronized folder failed: ${out}"
report "${out}" "new file in a synchronized folder"
[ "$(json "${out}" pif_exported)" = True ] || fail "the PIF was not re-exported for a new file"
rm "${swt}/Synced/Fresh.swift"
"${XCFS}" doctor "${swt}" | grep -E "build session: running|simulator " \
  || fail "doctor does not report the checkout's build session"
# The session must build what xcodebuild builds: a generic destination means every supported architecture.
sw_archs=$(lipo -archs "$(json "${out}" derived_data)/Build/Products/Debug-iphonesimulator/GenApp.app/GenApp")
xc_out=$(XCFS_DRIVER=xcodebuild "${XCFS}" build "${swt}" --json) || fail "xcodebuild build for comparison failed"
xc_archs=$(lipo -archs "$(json "${xc_out}" derived_data)/Build/Products/Debug-iphonesimulator/GenApp.app/GenApp")
[ "${xc_archs}" = "${sw_archs}" ] || fail "session built ${sw_archs}, xcodebuild built ${xc_archs}"
echo "same architectures as xcodebuild: ${sw_archs}"
out=$("${XCFS}" rm "${swt}" --apply --prune-branch --json) || fail "rm with a running session failed: ${out}"
sleep 1
! pgrep -f "xcode_session.*$(basename "${swt}")" >/dev/null || fail "the session outlived its worktree"
echo "session stopped with its worktree"
export XCFS_DRIVER=xcodebuild

step "nothing in the global DerivedData (whole run)"
dd_new=$(comm -13 <(echo "${dd_before}") <(ls "${GLOBAL_DD}" 2>/dev/null | sort))
[ -z "${dd_new}" ] || fail "new folders in the global DerivedData: ${dd_new}"
echo "none"
