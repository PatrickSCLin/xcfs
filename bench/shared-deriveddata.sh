#!/bin/bash
# Two git worktrees of Ice Cubes building into one shared DerivedData: does the second reuse the first's build, and
# what happens when both build at once?
#
#   bench/shared-deriveddata.sh <IceCubesApp clone at 98098b9, with IceCubesApp.xcconfig> <work dir>
set -u
SRC=$(cd "$1" && pwd); B=$2
mkdir -p "$B"; B=$(cd "$B" && pwd)
for w in a b; do
  git -C "$SRC" worktree add -q -b xcfs-bench/shared-$w "$B/wt-$w" 98098b9
  cp "$SRC/IceCubesApp.xcconfig" "$B/wt-$w/IceCubesApp.xcconfig"
done
b() {  # b <worktree> <label>
  local t0 t1 code n
  t0=$(python3 -c 'import time;print(time.time())')
  (cd "$B/wt-$1" && xcodebuild build -project IceCubesApp.xcodeproj -scheme IceCubesApp \
     -destination 'generic/platform=iOS Simulator' -derivedDataPath "$B/dd" CODE_SIGNING_ALLOWED=NO ARCHS=arm64 \
     > "$B/$2.log" 2>&1); code=$?
  t1=$(python3 -c 'import time;print(time.time())')
  n=$(grep -c '^SwiftCompile \|^CompileSwift ' "$B/$2.log")
  python3 -c "print('%-40s exit %s  %.1f s  swift compile tasks %s' % ('$2', $code, $t1 - $t0, '$n'))"
}
b a "A cold"
b a "A again (no-op)"
b b "B after A (another path)"
b a "A after B"
echo "both at once:"
b a "A concurrent" & b b "B concurrent" & wait
for w in a b; do
  git -C "$SRC" worktree remove --force "$B/wt-$w" && git -C "$SRC" branch -D xcfs-bench/shared-$w >/dev/null
done
