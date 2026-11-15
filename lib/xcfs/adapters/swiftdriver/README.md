# Experimental: a Swift driver that rebuilds only what an API change touches

**Status: experimental, on by default where it applies.** For the Swift 6.4 release line (Xcode 27.x); with an Xcode
of another line, and until it is built, xcfs builds with Xcode's own driver and says so.

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
(`swift-symbolgraph-extract`, from the binary module, in a background process so the build does not wait; a client
that needs the digests before they are ready waits for them) and stores one digest per dependency key — a top-level name, a
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
change an interface cost the same, and so does the first build of a checkout. On Ice Cubes the same edit took
25.3–25.6 s and 6.0–6.2 s. A whole agent round (checkout, build, five edits each checked, final build) with xcfs's
defaults, with and without this driver: 39.1 → 17.1 s on Ice Cubes, 132.3 → 82.0 s on Wikipedia, and on four apps
xcfs was not developed on 73.9 → 33.2 s, 93.5 → 57.4 s, 109.6 → 101.2 s and 159.9 → 98.9 s (`docs/benchmark.md`,
zero-configuration table). Whether it should move upstream, and how it compares with Xcode's compilation cache:
`docs/incremental-swift.md`.

## Use

Nothing to configure. With the default `"swiftDriver": "auto"`, xcfs reads the selected Xcode's `swiftc --version`
(it follows `xcode-select` and `DEVELOPER_DIR`), derives the swift-driver release tag of that Swift
(`swift-6.4.2-RELEASE`) and the patch of its release line, and on the first build with that Xcode starts building
the driver in the background: clone the tag, `git apply --check` (a patch that does not apply means Xcode's own
driver — nothing is applied fuzzily), apply, build with the pinned dependencies, then a correctness self-test
(`selftest.py`): two modules, A and B importing A, built incrementally with the new driver; thirteen edits to A
(a new function, a layout change through a typealias behind a private stored property, an `Optional` extension
member, a return type, a default argument value, availability, main-actor isolation, `package` access, an enum case,
a protocol requirement with a default implementation, `let` to `var`, `@discardableResult`, `throws`), the breaking
ones undone afterwards. After every step the oracle builds both modules from nothing at the same paths: every object
file must be byte-for-byte what the incremental build left, and each module must build or fail the same way (Swift's
output is reproducible, so any difference is a missed recompile; injecting one — a stale object — is caught). One
more check makes sure the driver narrows rebuilds at all: a new function nobody uses recompiles no file of B. The
stock driver passes the oracle but fails that check. The self-test takes about 30 seconds, the whole build about 1.5
minutes; builds use Xcode's own driver meanwhile, and only a driver that passed is used.

Each Xcode gets its own install, `~/Library/Caches/xcfs/swift-driver/<swift>-<swift build>-<hash of the Xcode's
path>/` (or under `$XCFS_CACHE`), because the driver uses that Xcode's frontend and links its `lib/` and `share/`.
Switching Xcode picks the matching install, or starts building it. A failed build or self-test is recorded and
retried after a day. Each build's JSON says which driver it used (`swift_driver`, with the reason when it is Xcode's
own), and `xcfs doctor` reports the state.

Overrides, in the toolchain entry of the config: `"swiftDriver": "native"` never uses it; `"api-digests"` warns on
every build that cannot use it. `XCFS_SWIFT_DRIVER=native|auto|api-digests` overrides the config. To build by hand:
`build.sh <install dir> <tag> <patch> <Package.resolved> <driver version> [<commit>]` (see its header).

Patches by release line (`KITS` in `../swift_driver.py`): 6.4 — `api-digests-swift-6.4.2.patch` with
`Package-swift-6.4.resolved`. The `swift-6.4.0`, `6.4.1` and `6.4.2-RELEASE` tags are the same swift-driver commit,
and they are pinned to it: a tag that points elsewhere is not built. Another release of the line is built from its
tag and self-tested; the commit built is recorded in `toolchain.json`.

## Costs and limits

- Each emitted module is extracted once per version, in the background: 0.2–2.5 s of CPU on the modules measured,
  plus a one-time build of the Clang modules the extraction needs (tens of seconds the first time on a new machine or
  SDK). A build that changes an interface waits for the extraction of the modules it needs.
- `SWIFT_USE_INTEGRATED_DRIVER=NO` makes Xcode run `swiftc` as a process per target instead of planning compile jobs
  in its own build service. One build costs the same. But each `swiftc` starts as many compile jobs as there are
  cores, so several targets compiling at once, and several cold builds at once (a fresh pool warming three slots side
  by side), ran far more frontends than Xcode's own driver would: 59 GB of frontends at the peak on Ice Cubes, against
  24 GB without this driver. Its frontends therefore start through a gate (`frontend-gate.py`, installed as
  `frontend-bin/swift-frontend`, with the toolchain's other tools linked next to it, where the driver looks for
  them): compile jobs (`-c`) wait for one of four places per core (`os.cpu_count()`; 40 places on the 10-core
  machine measured below), shared by every build that uses the install (`XCFS_FRONTEND_JOBS` sets another number); emitting a module, scans and queries never wait. Measured on that fresh
  pool: 22 GB at the peak and 276 s, against 59 GB and 217 s ungated (two places per core: 17 GB, 327 s). xcfs
  writes the wrapper and the gate itself, so an install made before them gets them on its next use.
- `@inlinable`, `@_transparent` and `@_alwaysEmitIntoClient` bodies are not part of a digest. Measured: when only such
  a body changes, neither the stock driver nor this one recompiles the clients that use it (the module's dependency
  graph does not change).
- A changed source file still recompiles; only the files that use it are spared.
- **Maintenance**: the patch is tied to one swift-driver release. Each Xcode that ships a new Swift needs the patch
  ported to that release's tag (the touched code — the incremental dependency graph's integrator, the first-wave and
  second-wave scheduling, the emit-module job — changes between releases), its dependency pins refreshed, and the
  correctness cases rerun; until then that Xcode builds with its own driver. The gain depends on modules: a target
  that imports nothing changed gets nothing, and a single-target app only gets the within-module narrowing.

## Files

- `api-digests-swift-6.4.2.patch` — the patch (Apache-2.0, a derivative of swift-driver; see [NOTICE.md](NOTICE.md))
- `Package-swift-6.4.resolved` — the dependency revisions the 6.4 line is built with
- `build.sh` — clone, check and apply the patch, build, install, self-test
- `selftest.py` — the correctness self-test a driver must pass before xcfs uses it
- `LICENSE.txt` — swift-driver's license
