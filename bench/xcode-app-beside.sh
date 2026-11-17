#!/bin/bash
# Xcode.app beside xcfs on one slot, headless: an xcodebuild without -derivedDataPath builds the way Xcode.app does
# (into the global DerivedData, keyed by the project's path).
set -u
W=${1:?usage: bench/xcode-app-beside.sh <work dir> [IceCubesApp clone]}; SRC=${2:-$W/IceCubesApp}
X=$(cd "$(dirname "$0")/.." && pwd)/bin/xcfs; P=$(cd "$(dirname "$0")" && pwd)/profiles
mkdir -p "$W/config/xcfs"
[ -d "$SRC/.git" ] || git clone -q https://github.com/Dimillian/IceCubesApp.git "$SRC"
git -C "$SRC" checkout -q 98098b9
(cd "$SRC" && cp IceCubesApp.xcconfig.template IceCubesApp.xcconfig && sed -i '' 's/^DEVELOPMENT_TEAM = .*/DEVELOPMENT_TEAM =/' IceCubesApp.xcconfig)
python3 -c "
import json; c=json.load(open('$P/icecubes-zero-config.json'))['xcfs']['config']
json.dump(c, open('$W/config/xcfs/IceCubesApp.json','w'))"
export XCFS_STATE=$W/state XCFS_CACHE=$W/cache XCFS_POOL_ROOT=$W/pool XDG_CONFIG_HOME=$W/config
t() { python3 -c 'import time;print(time.time())'; }
j() { python3 -c "import json,sys; d=json.loads(sys.stdin.read().strip().splitlines()[-1]); print($1)"; }
APP() {  # like Xcode.app: no -derivedDataPath; simulator, no signing
  (cd "$S" && xcodebuild build -project IceCubesApp.xcodeproj -scheme IceCubesApp -destination 'generic/platform=iOS Simulator' \
    CODE_SIGNING_ALLOWED=NO ARCHS=arm64 > "$W/app-$1.log" 2>&1); echo $?; }
echo "== warm one slot";        a=$(t); $X pool warm "$SRC" --slots 1 --json | j 'd["ok"]'; b=$(t); python3 -c "print('warm %.1fs' % ($b-$a))"
S=$($X checkout "$SRC" xcodeapp-probe --json | j 'd["worktree"]'); echo "slot: $S"
echo "== Xcode.app-style build, cold";   a=$(t); c=$(APP cold); b=$(t); python3 -c "print('exit $c, %.1fs' % ($b-$a))"
find $HOME/Library/Developer/Xcode/DerivedData -maxdepth 1 -name "IceCubesApp-*" | while read d; do /usr/libexec/PlistBuddy -c 'Print :WorkspacePath' "$d/info.plist" 2>/dev/null | grep -q "$S" && echo "bucket: $d $(du -sh "$d" | cut -f1)"; done
echo "== xcfs build after it";            $X build "$S" --json | j '"ok %s %.1fs driver %s" % (d["ok"], d["seconds"], d.get("driver"))'
echo "== Xcode.app-style build again";    a=$(t); c=$(APP again); b=$(t); python3 -c "print('exit $c, %.1fs' % ($b-$a))"
echo "== an edit, both building at once"
F=$(git -C "$S" ls-files 'Packages/Models/Sources/*.swift' | head -1); echo "edit: $F"; printf '\npublic func xcodeAppProbe() -> Int { 1 }\n' >> "$S/$F"
a=$(t); ( $X build "$S" --json | j '"xcfs: ok %s %.1fs" % (d["ok"], d["seconds"])' ) & ( c=$(APP both); echo "xcode.app-style: exit $c" ) & wait; b=$(t); python3 -c "print('both done in %.1fs' % ($b-$a))"
git -C "$S" checkout -q -- .
echo "== doctor and gc";                  $X doctor "$S" 2>&1 | grep -i 'session\|simulator' | head -3; $X gc 2>&1 | tail -3
echo "== release";                        $X release "$S" --no-warm --prune-branch --json | j '"released %s" % d.get("ok")'
find $HOME/Library/Developer/Xcode/DerivedData -maxdepth 1 -name "IceCubesApp-*" | while read d; do /usr/libexec/PlistBuddy -c 'Print :WorkspacePath' "$d/info.plist" 2>/dev/null | grep -q "$W/pool" && echo "after release, bucket kept: $d"; done
echo "== retire the pool";                $X pool resize "$SRC" 0 --json | j '"retired %s" % d.get("retired")'
sleep 5
echo "buckets of the slot left after retire: $(find $HOME/Library/Developer/Xcode/DerivedData -maxdepth 1 -name "IceCubesApp-*" | while read d; do /usr/libexec/PlistBuddy -c 'Print :WorkspacePath' "$d/info.plist" 2>/dev/null | grep "$W/pool"; done | wc -l)"
