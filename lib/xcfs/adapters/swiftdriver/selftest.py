#!/usr/bin/env python3
"""Correctness self-test of a freshly built experimental Swift driver, before xcfs uses it.

    selftest.py <swiftc wrapper> <result.json>

Builds two modules with the driver directly (no Xcode): A, and B importing A, both incremental, Swift 6 language mode,
one package name. Then applies a sequence of edits to A. After each one:

  - an incremental build of A and then of B with the driver, as a client would;
  - the oracle: the same sources built from nothing at the same paths. Every object file of A and of B must be
    byte-for-byte what the incremental build left, and each module must build (or fail) the same way. Swift's
    output is reproducible for the same input at the same path, so any difference is something the incremental
    build missed.

The edits cover what a client can depend on: new and changed declarations, a typealias behind a private stored
property (a layout change no token of the struct shows), an `Optional` extension, default argument values,
availability, actor isolation, `package` access, an enum case, a protocol requirement with a default implementation,
`let` to `var`, `@discardableResult`, `throws`. Edits that break B are undone afterwards (and that undo is checked
the same way). One more check makes sure the driver actually narrows rebuilds: a new public function nobody uses must
recompile no file of B.

Writes {"ok": bool, "steps": [...], "error": ...} to result.json. Python 3.9, standard library only.
"""
import filecmp, json, os, re, shutil, subprocess, sys, tempfile, time

VERSION = 2   # xcfs reruns the self-test of an installed driver whose result has another version
WRAPPER, RESULT = (sys.argv[1], sys.argv[2]) if len(sys.argv) == 3 else (None, None)
REMARK = re.compile(r"(?:Queuing|will queue)[^{\n]*\{compile: (\w+)\.o <= ")

A_FILES = {
    "Base.swift": "public typealias ID = Int\npublic func helper() -> Int { 1 }\n"
                  "public func withDefault(_ x: Int = 1) -> Int { x }\n"
                  "public func available() -> Int { 1 }\npublic func isolated() -> Int { 1 }\n"
                  "@discardableResult public func discardable() -> Int { 1 }\n"
                  "public func throwing() throws -> Int { 1 }\npackage func packaged() -> Int { 1 }\n",
    "Layout.swift": "public struct P { private var hidden: ID = 0; public init() {} }\n"
                    "public struct R { public let x: Int; public init() { x = 1 } }\n",
    "Opt.swift": "extension Optional { public func xcfsOr(_ d: Wrapped) -> Wrapped { self ?? d } }\n",
    "Kinds.swift": "public enum E { case a, b }\npublic protocol PR { func r() -> Int }\n"
                   "extension PR { public func r2() -> Int { 2 } }\n",
    "Other.swift": "public struct Q { public init() {}; public func q() -> Int { 2 } }\n",
}
B_FILES = {
    "UsesHelper.swift": "import A\npublic func useHelper() -> Int { helper() }\n",
    "UsesLayout.swift": "import A\npublic func useLayout() -> Int { MemoryLayout<P>.size }\n",
    "UsesOpt.swift": "import A\npublic func useOpt() -> Int { Optional<Int>.none.xcfsOr(1) }\n",
    "UsesDefault.swift": "import A\npublic func useDefault() -> Int { withDefault() }\n",
    "UsesAttributes.swift": "import A\npublic func useAvailable() -> Int { available() }\n"
                            "public func useIsolated() -> Int { isolated() }\n"
                            "public func useDiscardable() { discardable() }\n"
                            "public func useThrowing() -> Int { (try? throwing()) ?? 0 }\n",
    "UsesPackage.swift": "import A\npublic func usePackage() -> Int { packaged() }\n",
    "UsesKinds.swift": "import A\npublic func useEnum(_ e: E) -> Int { switch e { case .a: return 1; case .b: return 2 } }\n"
                       "public struct BS: PR { public init() {}; public func r() -> Int { 1 } }\n"
                       "public func useR() -> Int { var r = R(); _ = r; return r.x }\n",
    "UsesOther.swift": "import A\npublic func useOther() -> Int { Q().q() }\n",
}
# (label, file, find (None: append), replace, B still builds)
EDITS = [
    ("a new public function nobody uses", "Base.swift", None, "public func unusedXcfs() -> Int { 3 }\n", True),
    ("a typealias behind a private stored property changes (layout)", "Base.swift", "typealias ID = Int\n",
     "typealias ID = Int8\n", True),
    ("an Optional extension member changes its signature", "Opt.swift", "-> Wrapped { self ?? d }",
     "-> Wrapped? { self ?? d }", False),
    ("a called function changes its return type", "Base.swift", "func helper() -> Int { 1 }",
     "func helper() -> Int8 { 1 }", False),
    ("a default argument value changes", "Base.swift", "withDefault(_ x: Int = 1)", "withDefault(_ x: Int = 2)", True),
    ("a function becomes unavailable", "Base.swift", "public func available()",
     "@available(macOS 99, *) public func available()", False),
    ("a function becomes main-actor isolated", "Base.swift", "public func isolated()",
     "@MainActor public func isolated()", False),
    ("a package function changes its return type", "Base.swift", "package func packaged() -> Int { 1 }",
     "package func packaged() -> Int8 { 1 }", False),
    ("an enum gains a case", "Kinds.swift", "case a, b }", "case a, b, c }", False),
    ("a protocol gains a requirement with a default implementation", "Kinds.swift",
     "func r() -> Int }\n", "func r() -> Int; func r3() -> Int }\nextension PR { public func r3() -> Int { 3 } }\n", True),
    ("a stored property changes from let to var", "Layout.swift", "public let x: Int", "public var x: Int", True),
    ("@discardableResult is removed", "Base.swift", "@discardableResult public func discardable()",
     "public func discardable()", True),
    ("a throwing function stops throwing", "Base.swift", "func throwing() throws -> Int", "func throwing() -> Int", True),
]


def sdk():
    return subprocess.run(["xcrun", "--show-sdk-path", "--sdk", "macosx"], capture_output=True, text=True).stdout.strip()


def write_module(root, name, files):
    src = os.path.join(root, name, "src")
    os.makedirs(src, exist_ok=True)
    out = os.path.join(root, name, "out")
    ofm = {"": {"swift-dependencies": os.path.join(out, name + "-master.swiftdeps")}}
    for f, text in files.items():
        p = os.path.join(src, f)
        with open(p, "w") as fh:
            fh.write(text)
        stem = f[:-len(".swift")]
        ofm[p] = {"object": os.path.join(out, stem + ".o"), "swift-dependencies": os.path.join(out, stem + ".swiftdeps")}
    with open(os.path.join(root, name, "ofm.json"), "w") as fh:
        json.dump(ofm, fh)


def build(root, name, sdk_path, store):
    """(ok, files compiled, stderr tail)."""
    src = os.path.join(root, name, "src")
    out = os.path.join(root, name, "out")
    os.makedirs(out, exist_ok=True)
    files = sorted(os.path.join(src, f) for f in os.listdir(src))
    cmd = [WRAPPER, "-module-name", name, "-package-name", "xcfsselftest", "-swift-version", "6", "-parse-as-library",
           "-incremental", "-output-file-map", os.path.join(root, name, "ofm.json"), "-emit-module",
           "-emit-module-path", os.path.join(out, name + ".swiftmodule"), "-c", "-sdk", sdk_path,
           "-target", "arm64-apple-macosx14.0", "-I", os.path.join(root, "A", "out"), "-driver-show-incremental",
           "-j", "4", *files]
    env = dict(os.environ, SWIFT_DRIVER_API_DIGEST_DIR=store)
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=os.path.join(root, name), env=env, timeout=600)
    return r.returncode == 0, set(REMARK.findall(r.stderr)), r.stderr[-1500:]


def wait_for_digests(store, timeout=120):
    """The digests of a module are extracted in the background after its build."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if not [f for _, _, fs in os.walk(store) for f in fs if f.endswith(".pending")]:
            return
        time.sleep(0.2)


def build_both(root, sdk_path, store):
    ok_a, _, err_a = build(root, "A", sdk_path, store)
    wait_for_digests(store)
    ok_b, compiled, err_b = build(root, "B", sdk_path, store)
    wait_for_digests(store)
    return ok_a, ok_b, compiled, err_a if not ok_a else err_b


def objects(root, name):
    out = os.path.join(root, name, "out")
    return {f: os.path.join(out, f) for f in os.listdir(out) if f.endswith(".o")} if os.path.isdir(out) else {}


def oracle(root, sdk_path, store, inc_ok):
    """Build everything from nothing at the same paths and compare with what the incremental build left. Returns a
    list of differences (empty when the incremental build is right). The incremental state is put back afterwards."""
    saved = {}
    for name in ("A", "B"):
        out = os.path.join(root, name, "out")
        saved[name] = out + ".incremental"
        shutil.move(out, saved[name])
    diffs = []
    try:
        clean_a, clean_b, _, _ = build_both(root, sdk_path, store)
        for name, inc, clean in (("A", inc_ok[0], clean_a), ("B", inc_ok[1], clean_b)):
            if inc != clean:
                diffs.append("%s: incremental build %s, clean build %s" % (
                    name, "succeeded" if inc else "failed", "succeeded" if clean else "failed"))
            elif inc:
                fresh = objects(root, name)
                kept = {f: os.path.join(saved[name], f) for f in fresh}
                for f in sorted(fresh):
                    if not os.path.exists(kept[f]):
                        diffs.append("%s/%s: missing from the incremental build" % (name, f))
                    elif not filecmp.cmp(fresh[f], kept[f], shallow=False):
                        diffs.append("%s/%s: differs from a clean build" % (name, f))
    finally:
        for name in ("A", "B"):
            out = os.path.join(root, name, "out")
            shutil.rmtree(out, ignore_errors=True)
            shutil.move(saved[name], out)
    return diffs


def edit(root, rel, find, replace):
    p = os.path.join(root, "A", "src", rel)
    text = open(p).read()
    if find is None:
        text += replace
    else:
        assert find in text, (rel, find)
        text = text.replace(find, replace, 1)
    with open(p, "w") as fh:
        fh.write(text)


def main():
    root = tempfile.mkdtemp(prefix="xcfs-driver-selftest-")
    store = os.path.join(root, "store")
    result = {"ok": False, "version": VERSION, "steps": [], "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    try:
        sdk_path = sdk()
        write_module(root, "A", A_FILES)
        write_module(root, "B", B_FILES)
        ok_a, ok_b, _, err = build_both(root, sdk_path, store)
        if not (ok_a and ok_b):
            raise RuntimeError("the initial build failed: %s" % err)
        all_ok = True

        def step(label, expect_b_builds, narrow=False):
            ok_a, ok_b, compiled, err = build_both(root, sdk_path, store)
            problems = []
            if not ok_a:
                problems.append("A did not build: " + err[-300:])
            if ok_b != expect_b_builds:
                problems.append("B %s, expected it to %s" % ("built" if ok_b else "did not build",
                                                              "build" if expect_b_builds else "fail"))
            if narrow and compiled:
                problems.append("B recompiled %s for a declaration nobody uses (the driver does not narrow rebuilds)"
                                % sorted(compiled))
            problems += oracle(root, sdk_path, store, (ok_a, ok_b))
            result["steps"].append({"step": label, "ok": not problems, "recompiled": sorted(compiled),
                                    "problems": problems})
            return not problems

        for i, (label, rel, find, replace, b_builds) in enumerate(EDITS):
            edit(root, rel, find, replace)
            all_ok &= step(label, b_builds, narrow=(i == 0))
            if not b_builds:
                edit(root, rel, replace, find)
                all_ok &= step("undo: " + label, True)
        result["ok"] = all_ok
    except Exception as e:   # noqa: BLE001 - any failure means the driver is not used
        result["error"] = "%s: %s" % (type(e).__name__, str(e)[:1500])
    finally:
        shutil.rmtree(root, ignore_errors=True)
        with open(RESULT, "w") as fh:
            json.dump(result, fh, indent=1)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
