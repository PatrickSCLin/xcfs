#!/bin/sh
# Build the experimental swift-driver with per-name API digests and install it where xcfs looks for it.
#
#   toolchains/swift-driver/build.sh [--force]
#
# The patch is made for one swift-driver release; the selected Xcode must ship that Swift version, or the script
# stops. It clones swift-driver at the release tag, applies the patch, builds the driver with Xcode's SwiftPM and
# installs it into ${XCFS_CACHE:-~/Library/Caches/xcfs}/swift-driver/<swift version>/:
#   bin/swift-driver   the patched driver
#   bin/swiftc         the SWIFT_EXEC wrapper xcfs points Xcode at
#   toolchain.json     the `swiftc --version` of the Xcode it was built for (xcfs compares it before every build)
#   store/             API digests and the module cache used to extract them (created on use)
# Nothing outside that directory is touched.
set -eu

SWIFT_VERSION=6.4.2
TAG=swift-6.4.2-RELEASE
HERE=$(cd "$(dirname "$0")" && pwd)
PATCH="$HERE/api-digests-swift-$SWIFT_VERSION.patch"
ROOT="${XCFS_CACHE:-$HOME/Library/Caches/xcfs}/swift-driver/$SWIFT_VERSION"
SRC="$ROOT/src"
FORCE=${1:-}

VERSION_LINE=$(/usr/bin/xcrun swiftc --version 2>&1 | head -1)
case "$VERSION_LINE" in
  *"Apple Swift version $SWIFT_VERSION "*) ;;
  *) echo "build.sh: this patch is for Swift $SWIFT_VERSION; the selected Xcode has: $VERSION_LINE" >&2; exit 1 ;;
esac
FRONTEND=$(/usr/bin/xcrun -f swift-frontend)

if [ -x "$ROOT/bin/swift-driver" ] && [ "$FORCE" != "--force" ]; then
  echo "already built: $ROOT/bin/swift-driver (pass --force to rebuild)"
else
  rm -rf "$SRC"
  mkdir -p "$ROOT"
  git clone -q --depth 1 --branch "$TAG" https://github.com/swiftlang/swift-driver.git "$SRC"
  git -C "$SRC" apply "$PATCH"
  # The release's own Package.resolved is out of date (it pins llbuild's main branch while Package.swift asks for
  # release/6.4.x); use the pins this patch was built and measured with, and do not let SwiftPM move them.
  cp "$HERE/Package.resolved" "$SRC/Package.resolved"
  (cd "$SRC" && /usr/bin/xcrun swift build -c release --product swift-driver --force-resolved-versions)
  mkdir -p "$ROOT/bin"
  cp "$SRC/.build/release/swift-driver" "$ROOT/bin/swift-driver.new"
  mv "$ROOT/bin/swift-driver.new" "$ROOT/bin/swift-driver"
fi

cat > "$ROOT/bin/swiftc.new" <<EOF
#!/bin/sh
# SWIFT_EXEC for Xcode (written by xcfs toolchains/swift-driver/build.sh): the patched swift-driver, run against
# the frontend of the Xcode it was built for.
D=\$(cd "\$(dirname "\$0")" && pwd)
export SWIFT_DRIVER_SWIFT_FRONTEND_EXEC="$FRONTEND"
export SWIFT_DRIVER_API_DIGEST_DIR="\${SWIFT_DRIVER_API_DIGEST_DIR:-\$(cd "\$D/.." && pwd -P)/store}"
exec "\$D/swift-driver" --driver-mode=swiftc "\$@"
EOF
chmod +x "$ROOT/bin/swiftc.new"
mv "$ROOT/bin/swiftc.new" "$ROOT/bin/swiftc"

python3 - "$ROOT/toolchain.json" "$VERSION_LINE" "$FRONTEND" "$TAG" <<'EOF'
import json, sys
path, version, frontend, tag = sys.argv[1:]
json.dump({"swiftVersion": version, "frontend": frontend, "swiftDriverTag": tag, "patch": "api-digests"},
          open(path, "w"), indent=1)
EOF
echo "installed: $ROOT/bin/swiftc"
