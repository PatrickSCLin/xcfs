# Notice

This directory holds a patch to **swift-driver**, part of the Swift open source project:

- Source: https://github.com/swiftlang/swift-driver, tag `swift-6.4.2-RELEASE` (swift-driver 1.168.6, the Swift
  driver of Xcode 27.2).
- Copyright (c) 2014 - 2025 Apple Inc. and the Swift project authors.
- Licensed under the Apache License, Version 2.0, with the Runtime Library Exception; the full text is in
  [LICENSE.txt](LICENSE.txt), copied unchanged from that repository. See CONTRIBUTORS.txt there for the list of
  Swift project authors.

`api-digests-swift-6.4.2.patch` is a derivative work of swift-driver and is distributed under the same license
(Apache-2.0), not under the MIT license that covers the rest of xcfs. It adds one file,
`Sources/SwiftDriver/IncrementalCompilation/APIDigests.swift`, and changes six others:

- `Sources/SwiftDriver/Driver/Driver.swift`
- `Sources/SwiftDriver/Jobs/EmitModuleJob.swift`
- `Sources/SwiftDriver/IncrementalCompilation/ModuleDependencyGraph.swift`
- `Sources/SwiftDriver/IncrementalCompilation/ModuleDependencyGraphParts/Integrator.swift`
- `Sources/SwiftDriver/IncrementalCompilation/IncrementalCompilationProtectedState.swift`
- `Sources/swift-driver/main.swift`

Each changed file carries the line "Modified by the xcfs project" below its license header, and every original
copyright and license header is kept.

`Package-swift-6.4.resolved` records the dependency revisions the patched driver was built and measured with (swift-llbuild
and swift-tools-support-core on their `release/6.4.x` branches, swift-argument-parser 1.6.2,
swift-toolchain-sqlite 1.0.7); those packages keep their own licenses.

`build.sh`, `selftest.py` and `README.md` are part of xcfs (MIT).
