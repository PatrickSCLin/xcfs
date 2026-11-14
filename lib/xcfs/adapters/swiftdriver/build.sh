#!/bin/sh
# Build the experimental swift-driver with per-name API digests and install it where xcfs looks for it.
#
#   lib/xcfs/adapters/swiftdriver/build.sh [--force]   (xcfs runs it by itself when needed)
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

# The build service and the driver find the compiler's resources next to `swiftc`: share/swift/features.json says
# which options the compiler takes (without it Xcode leaves out -package-name and others), lib/swift holds the
# macro plugins and runtime libraries. Point both at the Xcode toolchain the driver was built for.
# The features file describes Xcode's own driver, which takes options the open-source release does not; the build
# service checks those against the driver it links, not against this one, so they are left out of this driver's
# copy (the build service then treats them as unsupported, as for an older compiler).
TOOLCHAIN_USR=$(dirname "$(dirname "$FRONTEND")")
for d in lib include; do
  [ -e "$TOOLCHAIN_USR/$d" ] && ln -sfn "$TOOLCHAIN_USR/$d" "$ROOT/$d"
done
rm -rf "$ROOT/share"
mkdir -p "$ROOT/share/swift"
for e in "$TOOLCHAIN_USR"/share/*; do
  [ "$(basename "$e")" = swift ] || ln -sfn "$e" "$ROOT/share/$(basename "$e")"
done
for e in "$TOOLCHAIN_USR"/share/swift/*; do
  [ "$(basename "$e")" = features.json ] || ln -sfn "$e" "$ROOT/share/swift/$(basename "$e")"
done
python3 - "$TOOLCHAIN_USR/share/swift/features.json" "$ROOT/share/swift/features.json" "$SRC/Sources/SwiftOptions/Options.swift" <<'PY'
import json, re, sys
src, dst, options = sys.argv[1:]
known = set(re.findall(r'Option\("(-[^"]+)"', open(options).read()))
# Features with which the build service adds an option for the driver itself.
driver_options = {"emit-localized-strings": "-emit-localized-strings"}
features = json.load(open(src))
features["features"] = [f for f in features["features"]
                        if driver_options.get(f["name"]) is None or driver_options[f["name"]] in known]
json.dump(features, open(dst, "w"), indent=1)
PY

cat > "$ROOT/bin/swiftc.new" <<EOF
#!/bin/sh
# SWIFT_EXEC for Xcode (written by xcfs swiftdriver/build.sh): the patched swift-driver, run against
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
