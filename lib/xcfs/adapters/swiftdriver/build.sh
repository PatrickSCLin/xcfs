#!/bin/sh
# Build the experimental swift-driver for the selected Xcode, check it, and install it where xcfs looks for it.
#
#   build.sh <install dir> <swift-driver tag> <patch> <Package.resolved> <driver version>
#
# xcfs runs this by itself (in the background, on the first build with an Xcode that has no driver yet) and picks
# the arguments for the selected Xcode: the swift-driver release tag of its Swift, the patch made for that release
# line, the dependency pins it was built with, and the version its own driver reports. By hand, the same arguments,
# e.g. build.sh ~/Library/Caches/xcfs/swift-driver/6.4.2-<build>-<xcode> swift-6.4.2-RELEASE \
#        api-digests-swift-6.4.2.patch Package-swift-6.4.resolved 1.168.6
#
# Into the install directory: bin/swift-driver (the patched driver), bin/swiftc (the SWIFT_EXEC wrapper),
# toolchain.json (what it was built for), selftest.json (the correctness self-test; xcfs uses the driver only when it
# passed), lib/ and include/ (links to the toolchain), share/ (the toolchain's, with a features file for this driver),
# store/ (API digests, created on use). Nothing outside that directory is touched.
#
# Exit codes: 3 the patch does not apply to that release (no fuzzy applying), 4 the self-test failed.
set -eu

[ $# -eq 5 ] || { echo "usage: build.sh <install dir> <tag> <patch> <Package.resolved> <driver version>" >&2; exit 2; }
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$1
TAG=$2
case "$3" in /*) PATCH=$3 ;; *) PATCH="$HERE/$3" ;; esac
case "$4" in /*) RESOLVED=$4 ;; *) RESOLVED="$HERE/$4" ;; esac
DRIVER_VERSION=$5
SRC="$ROOT/src"
VERSION_LINE=$(/usr/bin/xcrun swiftc --version 2>&1 | head -1)
FRONTEND=$(/usr/bin/xcrun -f swift-frontend)

rm -rf "$SRC"
mkdir -p "$ROOT"
git clone -q --depth 1 --branch "$TAG" https://github.com/swiftlang/swift-driver.git "$SRC"
if ! git -C "$SRC" apply --check "$PATCH"; then
  echo "build.sh: $(basename "$PATCH") does not apply to $TAG" >&2
  exit 3
fi
git -C "$SRC" apply "$PATCH"
# Report the version of the driver this release is, as Xcode's own does (the build service reads that line).
sed -i '' "s/driverSourceVersion: String = \"1.168.6\"/driverSourceVersion: String = \"$DRIVER_VERSION\"/" \
  "$SRC/Sources/SwiftDriver/Driver/DriverVersion.swift"
{
  # A release's own Package.resolved can be out of date (6.4's pins llbuild's main branch while Package.swift asks
  # for release/6.4.x); use the pins this patch line was built and measured with, and do not let SwiftPM move them.
  cp "$RESOLVED" "$SRC/Package.resolved"
  (cd "$SRC" && /usr/bin/xcrun swift build -c release --product swift-driver --force-resolved-versions)
  mkdir -p "$ROOT/bin"
  cp "$SRC/.build/release/swift-driver" "$ROOT/bin/swift-driver.new"
  mv "$ROOT/bin/swift-driver.new" "$ROOT/bin/swift-driver"
}

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

python3 - "$ROOT/toolchain.json" "$VERSION_LINE" "$FRONTEND" "$TAG" "$(basename "$PATCH")" "$DRIVER_VERSION" <<'EOF'
import json, sys
path, version, frontend, tag, patch, driver = sys.argv[1:]
json.dump({"swiftVersion": version, "frontend": frontend, "swiftDriverTag": tag, "patch": patch,
           "driverVersion": driver}, open(path, "w"), indent=1)
EOF

# Correctness self-test with this driver; xcfs uses it only if this passes.
rm -f "$ROOT/selftest.json"
if ! python3 "$HERE/selftest.py" "$ROOT/bin/swiftc" "$ROOT/selftest.json"; then
  echo "build.sh: the self-test failed; see $ROOT/selftest.json" >&2
  exit 4
fi
echo "installed and self-tested: $ROOT/bin/swiftc"
