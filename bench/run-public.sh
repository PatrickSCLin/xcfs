#!/bin/bash
# Rerun a published benchmark on your machine: clone the public app at the pinned commit, set it up, run bench.py.
#
#   bench/run-public.sh <app> [phases] [--print]
#     app      icecubes | wikipedia
#     phases   bench.py --phases (default: tasks — three agents with real tasks, plain worktrees against xcfs)
#     --print  show the commands, run nothing
#
# Work goes to ${XCFS_BENCH_DIR:-$HOME/xcfs-bench}: the clone, the runs, their logs. The results land in
# <work>/results-*.json and .md, in the format of bench/results (compare with the files named in
# bench/results/README.md). Nothing outside the work directory is changed, except what the plain side does as an
# agent would (Xcode's global DerivedData, which the harness measures and then removes).
# The plain side runs its tests on the simulator the profile names ("iPhone 18 Pro"); change the profile's "test"
# commands if your Xcode has another one.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
app=${1:-}; phases=tasks; dry=""
shift || true
for a in "$@"; do
  case "$a" in --print) dry=1 ;; *) phases=$a ;; esac
done
case "$app" in
  icecubes)
    url=https://github.com/Dimillian/IceCubesApp.git; commit=98098b9; dir=IceCubesApp
    profile=icecubes-tasks.json
    setup="cp IceCubesApp.xcconfig.template IceCubesApp.xcconfig && sed -i '' 's/^DEVELOPMENT_TEAM = .*/DEVELOPMENT_TEAM =/' IceCubesApp.xcconfig" ;;
  wikipedia)
    url=https://github.com/wikimedia/wikipedia-ios.git; commit=0919f3e; dir=wikipedia-ios
    profile=wikipedia-ios-tasks.json
    setup="scripts/setup_bundle_id ci" ;;
  *) sed -n '2,12p' "$0"; exit 2 ;;
esac
work=${XCFS_BENCH_DIR:-$HOME/xcfs-bench}
run() { echo "+ $*"; [ -n "$dry" ] || eval "$@"; }
run "mkdir -p '$work'"
if [ ! -d "$work/$dir/.git" ]; then
  run "git clone '$url' '$work/$dir'"
fi
run "git -C '$work/$dir' checkout -q $commit"
run "cd '$work/$dir' && $setup"
stamp=$(date +%Y%m%d-%H%M%S)
run "cd '$work' && python3 '$HERE/bench.py' '$HERE/profiles/$profile' --source '$dir' --work '$work/run-$app-$stamp' --phases $phases"
echo "results: $work/run-$app-$stamp/results-*.md"
