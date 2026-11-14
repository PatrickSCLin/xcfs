# Incremental Swift builds: the patch, what upstream would need, and the compilation cache

Notes for deciding where the experimental Swift driver ([lib/xcfs/adapters/swiftdriver](../lib/xcfs/adapters/swiftdriver/README.md))
should go next. Nothing here is implemented beyond what that directory ships.

## What the patch changes, and where

The patch adds ~770 lines to swift-driver 6.4; ~570 of them are one new file (`APIDigests.swift`: extraction,
digests, the store). What it needs from the rest of the driver is small:

| hook | where | lines | what it does |
|---|---|---|---|
| a module was emitted | `EmitModuleJob`, `Driver` | ~25 | start the background extraction of the module's symbol graph and write its digests |
| an imported module changed | `ModuleDependencyGraph` integrator | ~65 | before invalidating every use of the module's declarations, ask the digests which keys really changed; invalidate only those |
| a file of this module changed its interface | incremental protected state | ~55 | wait for the module's own emit job, then filter the same way before scheduling the second wave |
| identity | `DriverVersion`, `main` | ~10 | the version line Xcode's build service reads; a flag for the digest store |

Everything else — what a digest covers, how it is computed, where it is stored — sits behind those three hooks.

## Getting it upstream: two routes

**A filter hook in the driver (small, keeps the digests outside).** swift-driver could expose one optional
predicate: "given a dependency key of an external module (or of this module) whose interface hash changed, has this
key's interface really changed?" — consulted where the integrator and the second wave invalidate, with today's
behavior when no predicate is installed. Plus a notification that a module was emitted. With that, the digest code
could live in a plugin or a separate tool and follow its own release cadence; porting to a new swift-driver release
would mean porting nothing. The cost of being wrong stays the same as today's patch (a key judged unchanged that did
change is a missed recompile), so upstream would want the self-test's oracle as part of its tests.

**Finer fingerprints in the frontend (larger, removes the need).** The cause is that the frontend fingerprints only
the bodies of nominal types; a top-level function, an extension member or a type's header shares the interface hash
of its whole file, and a `.swiftmodule` is one "file". If the frontend emitted a fingerprint per declaration — top
level, extension members, type headers — into `.swiftdeps` and into the module's dependency information, the
existing driver logic would narrow rebuilds by itself, with no extraction step and no store. This is the change
that fits Swift's design; it touches the frontend's dependency emission and the serialized module format, so it
needs a proposal on the Swift forums and would ship with a toolchain, not with xcfs.

The digest approach is a working measurement of what the second route would gain (on the apps measured, adding a
public declaration to a widely imported module: 25 → 6 s, 35 → 15 s); that is the argument to bring to either
discussion.

## Compilation caching (CAS) with prefix mapping, compared

Xcode's compilation cache (`COMPILATION_CACHE_ENABLE_CACHING`) keys each compile job by the content of its inputs
and replays its outputs on a hit; prefix mapping (`SWIFT_/CLANG_ENABLE_PREFIX_MAPPING`) rewrites the paths in those
keys so two checkouts at different paths can share results.

| | API digests (the patch) | compilation cache + prefix mapping |
|---|---|---|
| what it saves | jobs that would run although nothing they use changed | jobs whose exact inputs were compiled before |
| a public declaration added to a widely imported module | most downstream files are not recompiled | every downstream job has a new input (the changed module), so every one misses and runs |
| a new slot, a clean build, switching back to a branch | nothing | jobs another checkout already ran are replayed |
| who maintains it | xcfs, per swift-driver release | Apple, in Xcode |
| correctness risk | a key judged unchanged that changed (guarded by the self-test and the oracle) | a key that misses an input (Apple's responsibility) |
| cost | background extraction per module version, a digest store | the cache's disk, explicit modules |

They address different builds and do not exclude each other. Measured so far: with the cache and both prefix-mapping
settings, warming slots 2 and 3 of Ice Cubes took 62–63 s instead of 65 s, because the keys still contained each
slot's own output paths ([benchmark notes](benchmark.md#where-the-time-goes)). Making cross-slot hits real would need
those paths out of the keys (or every slot's build directory at one path, which a pool of slots at fixed paths
cannot give); that is where the cache would help xcfs most — warm-up and the first build of a slot — while the
digests keep helping the edit loop.

## Order of work, if any

1. Keep the patch, with the oracle and the self-test as its gate; port per release.
2. Propose the filter hook upstream, with the measurements and the oracle cases; if accepted, move the digest code
   out of the patch.
3. Raise per-declaration fingerprints on the forums as the long-term fix.
4. Separately, try the compilation cache again for warm-up once its keys can exclude output paths.
