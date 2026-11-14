# Experimental: a Swift driver that rebuilds only what an API change touches

**Status: experimental.** For Swift 6.4.2 (Xcode 27.2) only. Opt in per toolchain entry; with any other Xcode, or
before it is built, xcfs builds with Xcode's own driver and says so.

## The problem

When a module's interface changes — say a new `public func` is added to a Swift package — Xcode recompiles every
file of every module downstream that uses *any* name from that package, even though nothing uses the new function.
On a large app that is the difference between a 4-second and a 35-second build after a one-line change.

The cause is in how Swift records dependencies, not in Xcode's settings. The frontend links each declaration in a
file to the file's interface hash (type fingerprints only cover type bodies, so when the hash changes, any
declaration in the file might have changed). A `.swiftmodule` describes the whole module as one such "file", so any
change to the module's interface marks every declaration in it as changed, and the driver of a downstream module
recompiles every file that uses one of them. Inside a module the same rule makes a changed file's interface
invalidate every declaration in that file. Top-level functions, extension members and type headers have no
fingerprint of their own, so the dependency graph alone cannot tell which declarations really changed.

## What the patch does

After a module is emitted, the patched driver extracts its symbol graph at every access level
(`swift-symbolgraph-extract`, from the binary module) and stores one digest per dependency key — a top-level name, a
type, a member of a type, a member name — keyed by a hash of the module's whole dependency graph. A digest covers
what users of a declaration depend on: its declaration, its USR (which spells out resolved types), its generic
signature, its conformances, and, transitively, the layout of every same-module type it refers to (stored
properties, including private ones, enum cases, superclass; every member of classes, actors and protocols).

When a downstream driver sees that a module it imports changed, it compares the digests of the version it built
against with those of the new version and invalidates only the declarations whose digest differs. Inside a module,
when a compiled file's interface changed, the driver waits for the module's emit job, then does the same. Whatever
cannot be matched — a key without a digest, a version without stored digests, an emit job that failed — falls back to
what the stock driver does.

Covered by the tests this was developed with (each case recompiles exactly the files that need it): a new
declaration nobody uses (nothing downstream recompiles); a typealias whose underlying type changes (its users
recompile and report the type error); a private stored property whose type changes through another file's typealias
or through an inferred type (users of the type whose layout changed recompile); a member or a conformance added to a
type; a function whose return type changes inside the same module.

Measured on Wikipedia iOS (fresh checkouts, three runs each, see `docs/benchmark.md`): adding a public function to a
package that ~300 app files import took 33–36 s with the stock driver and 15 s with this one; edits that do not
change an interface cost the same; the first build of a checkout takes ~8 s longer (one extraction per module).

## Use

```sh
toolchains/swift-driver/build.sh          # clones swift-driver 6.4.2, applies the patch, builds (~1-2 min)
```

then, in the toolchain entry of `.xcfs.json` (or your config):

```json
{"adapter": "xcode", "swiftDriver": "api-digests", ...}
```

xcfs then builds with `SWIFT_USE_INTEGRATED_DRIVER=NO` and `SWIFT_EXEC` pointing at the installed wrapper. Before
every build it compares the selected Xcode's `swiftc --version` with the one the driver was built for; on any
difference it uses Xcode's own driver and warns once. `xcfs doctor` reports the state.

Everything lives in `~/Library/Caches/xcfs/swift-driver/6.4.2/` (or under `$XCFS_CACHE`): the driver, the wrapper,
and `store/` with the digests (the newest 64 versions per module are kept) and the module cache used to extract them.
Deleting the directory turns the feature off until it is built again.

## Costs and limits

- Each emitted module is extracted once per version: 0.2–2.5 s on the modules measured, plus a one-time build of the
  Clang modules the extraction needs (tens of seconds the first time on a new machine or SDK).
- `SWIFT_USE_INTEGRATED_DRIVER=NO` makes Xcode run `swiftc` as a process per target instead of planning compile jobs
  in its own build service; measured, this costs nothing on the projects above.
- `@inlinable`, `@_transparent` and `@_alwaysEmitIntoClient` bodies are not part of a digest. Measured: when only such
  a body changes, neither the stock driver nor this one recompiles the clients that use it (the module's dependency
  graph does not change).
- A changed source file still recompiles; only the files that use it are spared.

## Files

- `api-digests-swift-6.4.2.patch` — the patch (Apache-2.0, a derivative of swift-driver; see [NOTICE.md](NOTICE.md))
- `Package.resolved` — the dependency revisions it was built with
- `build.sh` — clone, patch, build, install
- `LICENSE.txt` — swift-driver's license
