#!/usr/bin/env python3
"""Correctness self-test of a freshly built experimental Swift driver, before xcfs uses it.

    selftest.py <swiftc wrapper> <result.json>

Builds two modules with the driver directly (no Xcode): A, and B importing A, both incremental. Then edits A and
checks which files of B the driver recompiles:

  1. a new public function nobody uses        -> no file of B
  2. a typealias behind a private stored property of a struct B uses changes its type (the struct's layout changes,
     no token of the struct does)             -> the file of B that uses the struct
  3. a member of an `extension Optional` changes its signature
                                              -> the file of B that calls it (which then fails to compile)
  4. a public function B calls changes its return type
                                              -> the file of B that calls it (which then fails to compile)

Writes {"ok": bool, "cases": [...], "error": ...} to result.json. Python 3.9, standard library only.
"""
import json, os, re, shutil, subprocess, sys, tempfile, time

WRAPPER, RESULT = sys.argv[1], sys.argv[2]
REMARK = re.compile(r"(?:Queuing|will queue)[^{\n]*\{compile: (\w+)\.o <= ")

A_FILES = {
    "Base.swift": "public typealias ID = Int\npublic func helper() -> Int { 1 }\n",
    "Layout.swift": "public struct P { private var hidden: ID = 0; public init() {} }\n",
    "Opt.swift": "extension Optional { public func xcfsOr(_ d: Wrapped) -> Wrapped { self ?? d } }\n",
    "Other.swift": "public struct Q { public init() {}; public func q() -> Int { 2 } }\n",
}
B_FILES = {
    "UsesHelper.swift": "import A\npublic func useHelper() -> Int { helper() }\n",
    "UsesLayout.swift": "import A\npublic func useLayout() -> Int { MemoryLayout<P>.size }\n",
    "UsesOpt.swift": "import A\npublic func useOpt() -> Int { Optional<Int>.none.xcfsOr(1) }\n",
    "UsesOther.swift": "import A\npublic func useOther() -> Int { Q().q() }\n",
}


def sdk():
    return subprocess.run(["xcrun", "--show-sdk-path", "--sdk", "macosx"], capture_output=True, text=True).stdout.strip()


def write_module(root, name, files):
    src = os.path.join(root, name, "src")
    out = os.path.join(root, name, "out")
    os.makedirs(src, exist_ok=True)
    os.makedirs(out, exist_ok=True)
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
    src = os.path.join(root, name, "src")
    out = os.path.join(root, name, "out")
    files = sorted(os.path.join(src, f) for f in os.listdir(src))
    cmd = [WRAPPER, "-module-name", name, "-parse-as-library", "-incremental", "-output-file-map",
           os.path.join(root, name, "ofm.json"), "-emit-module", "-emit-module-path", os.path.join(out, name + ".swiftmodule"),
           "-c", "-sdk", sdk_path, "-target", "arm64-apple-macosx14.0", "-I", os.path.join(root, "A", "out"),
           "-driver-show-incremental", "-j", "4", *files]
    env = dict(os.environ, SWIFT_DRIVER_API_DIGEST_DIR=store)
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=os.path.join(root, name), env=env, timeout=600)
    return r.returncode == 0, set(REMARK.findall(r.stderr)), r.stderr[-2000:]


def wait_for_digests(store, timeout=120):
    """The digests of a module are extracted in the background after its build."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        pending = [os.path.join(d, f) for d, _, fs in os.walk(store) for f in fs if f.endswith(".pending")]
        if not pending:
            return
        time.sleep(0.2)


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
    result = {"ok": False, "cases": [], "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    try:
        sdk_path = sdk()
        write_module(root, "A", A_FILES)
        write_module(root, "B", B_FILES)
        for name in ("A", "B"):
            ok, _, err = build(root, name, sdk_path, store)
            if not ok:
                raise RuntimeError("initial build of %s failed: %s" % (name, err))
            wait_for_digests(store)
        cases = [
            ("unused public function", "Base.swift", None, "public func unusedXcfs() -> Int { 3 }\n", set(), True),
            ("private stored property layout through a typealias", "Base.swift", "typealias ID = Int\n",
             "typealias ID = Int8\n", {"UsesLayout"}, True),
            ("Optional extension member signature", "Opt.swift", "-> Wrapped { self ?? d }",
             "-> Wrapped? { self ?? d }", {"UsesOpt"}, False),
            ("return type of a called function", "Base.swift", "func helper() -> Int { 1 }",
             "func helper() -> Int8 { 1 }", {"UsesHelper"}, False),
        ]
        all_ok = True
        for label, rel, find, replace, must, b_builds in cases:
            edit(root, rel, find, replace)
            ok_a, _, err_a = build(root, "A", sdk_path, store)
            wait_for_digests(store)
            ok_b, compiled, err_b = build(root, "B", sdk_path, store)
            passed = ok_a and must <= compiled and (bool(must) or not compiled) and ok_b == b_builds
            result["cases"].append({"case": label, "ok": passed, "recompiled": sorted(compiled),
                                    "expected": sorted(must), "b_built": ok_b})
            all_ok = all_ok and passed
            if not ok_b:   # undo the breaking edit so the next case starts from a module B compiles against
                edit(root, rel, replace, find)
                build(root, "A", sdk_path, store)
                wait_for_digests(store)
                build(root, "B", sdk_path, store)
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
