"""The persistent build session for the Xcode adapter.

A session is one long-lived `swbuild` process (the build service client that ships inside Xcode) per checkout, kept by
a small daemon that listens on a unix socket. Keeping the service alive, reusing the build description and compiling
only the files asked for (`buildFiles`) is what takes a no-op build of a large app from ~35 s to ~4 s.

Apple positions swbuild as a debugging tool with no stability promise. So a session is used only when the toolchain
entry asks for it ("driver": "session" or "auto") AND a regression check has passed on the installed Xcode version
(`xcfs doctor --session-check`, or automatically on first use with "session"). Otherwise every build goes through
xcodebuild, and nothing else changes.

Rules the measurements impose (each one a failure seen in practice):
- one DerivedData per driver: the session and xcodebuild each rebuild everything the other built;
- every session has its own request files: two sessions sharing one request file build into each other's DerivedData;
- a failed PIF export leaves no file: check the exit code and the file;
- swbuild answers a command it cannot run (unreadable PIF, unknown target) with an error line and then hangs: treat
  such a line as a failure, and give every build a timeout after which the process is killed and restarted.
"""
import glob, hashlib, json, os, plistlib, queue, re, shutil, signal, socket, subprocess, sys, tempfile, threading, time

from ..util import Fail, USAGE, git, phys, read_json, run, say, short_hash, warn, write_json

KEY_FILES = re.compile(r"(^|/)(Package\.swift|Package\.resolved|Podfile\.lock|project\.yml|project\.json|Cartfile\.resolved|"
                       r"[^/]+\.xcconfig|[^/]+\.xcscheme|contents\.xcworkspacedata|project\.pbxproj)$")
IDLE_SECONDS = 1800


# ------------------------------------------------------------------ where things are
def swbuild_path():
    from .xcode import developer_dir
    dev = developer_dir()
    p = os.path.join(os.path.dirname(dev), "SharedFrameworks", "SwiftBuild.framework", "Versions", "A", "Support", "swbuild")
    return p if os.path.isfile(p) else None


def session_dir(ctx):
    return os.path.join(ctx.build_dir, "session")


def socket_path(ctx):
    # unix socket paths are limited to 104 bytes: keep them short and private
    d = os.path.join(tempfile.gettempdir(), "xcfs-%d" % os.getuid())
    os.makedirs(d, mode=0o700, exist_ok=True)
    return os.path.join(d, short_hash(ctx.build_dir, 16) + ".sock")


def check_id():
    """Identifies the code the regression check exercises: a record made by another version of it is not trusted."""
    import pkgutil
    data = b"".join(pkgutil.get_data(__package__, f) or b"" for f in ("xcode_session.py", "xcode.py"))
    return hashlib.sha256(data).hexdigest()[:12]


def regression_current(rec):
    """A recorded check still applies: made by this code, and if it failed, less than a day ago."""
    if not rec or rec.get("check") != check_id():
        return False
    if rec.get("ok"):
        return True
    try:
        made = time.mktime(time.strptime(rec.get("at", ""), "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
    except ValueError:
        return False
    return time.time() - made < 24 * 3600


def regression_file():
    from ..state import state_dir
    return os.path.join(state_dir(), "session-regression.json")


# ------------------------------------------------------------------ driver choice
def choose_driver(adapter, ctx):
    want = os.environ.get("XCFS_DRIVER") or ctx.entry.get("driver", "auto")
    if want == "xcodebuild":
        return "xcodebuild"
    if not swbuild_path():
        warn("no swbuild in this Xcode: building with xcodebuild")
        return "xcodebuild"
    rec = (read_json(regression_file(), {}) or {}).get(adapter.version())
    if not regression_current(rec):
        say("  %s session build on %s: running the session regression check once"
            % ("first" if rec is None else "an updated xcfs or a failed check: another", adapter.version()))
        rec = regression(adapter)
    if rec and rec.get("ok"):
        return "session"
    if want == "session":
        warn("the session regression check failed on %s (%s): building with xcodebuild"
             % (adapter.version(), (rec or {}).get("error", "not run")))
    return "xcodebuild"


# ------------------------------------------------------------------ PIF
def walk_files(root, depth=3):
    """Project files that decide the PIF but are often git-ignored (generated projects, Pods)."""
    out = []
    for dp, dns, fns in os.walk(root):
        rel_depth = dp[len(root):].count(os.sep)
        dns[:] = [d for d in dns if not d.startswith(".") and d not in ("node_modules", "build", "DerivedData")
                  and rel_depth < depth]
        for f in fns:
            if f in ("project.pbxproj", "contents.xcworkspacedata") or f.endswith(".xcscheme"):
                out.append(os.path.join(dp, f))
    return out


def pif_key(adapter, ctx, scheme):
    """Everything that changes the PIF: the file list (a synchronized folder's files are expanded at export), the
    project and package definitions, the toolchain. Content edits to sources do not change it."""
    root = ctx.toolchain_root
    h = hashlib.sha256()
    h.update(("%s\n%s\n%s\n" % (adapter.version(), scheme, json.dumps(adapter.container_args(ctx)[0]))).encode())
    files = run("git", "-C", root, "ls-files", "-co", "--exclude-standard", "-z", check=False).stdout.split("\0")
    files = sorted(f for f in files if f)
    h.update("\0".join(files).encode())
    extra = set(ctx.entry.get("reexportWhenChanged", []))
    keyed = [os.path.join(root, f) for f in files if KEY_FILES.search(f) or f in extra] + walk_files(root)
    for f in sorted(set(keyed)):
        try:
            with open(f, "rb") as fh:
                h.update(f.encode() + b"\0" + hashlib.sha256(fh.read()).digest())
        except OSError:
            pass
    return h.hexdigest()[:24]


def export_pif(adapter, ctx, scheme):
    d = os.path.join(session_dir(ctx), "pif")
    os.makedirs(d, exist_ok=True)
    key = pif_key(adapter, ctx, scheme)
    path = os.path.join(d, key + ".json")
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        return path, False
    args, cwd = adapter.container_args(ctx)
    tmp = path + ".%d.tmp" % os.getpid()
    # Never the global DerivedData: without a location of its own, the export resolves packages into (and logs to) a
    # global folder that no checkout owns and nothing ever removes. -derivedDataPath needs -scheme; the scheme Xcode
    # generates for a package is named after its product or "<package>-Package", so a package tries those in turn.
    own = os.path.join(ctx.build_dir, "swbuild", "pif")
    extra = []
    if ctx.entry.get("packagesDir"):
        extra = ["-clonedSourcePackagesDirPath", os.path.join(ctx.toolchain_root, ctx.entry["packagesDir"])]
    candidates = [scheme] if args else package_schemes(cwd, scheme)
    t0 = time.time()
    for sch in candidates:
        cmd = ["xcodebuild", "-dumpPIF", tmp, *args, "-scheme", sch, "-derivedDataPath", own, *extra]
        r = run(*cmd, cwd=cwd, check=False, timeout=1800)
        if r.returncode == 0 and os.path.isfile(tmp) and os.path.getsize(tmp) > 0:
            break
    if r.returncode != 0 or not os.path.isfile(tmp) or os.path.getsize(tmp) == 0:
        errs = [l for l in (r.stderr + r.stdout).splitlines()
                if re.search(r"\berror\b", l) and "DVTProvisioning" not in l and "result bundle" not in l]
        raise Fail("PIF export failed (%d): %s" % (r.returncode, "\n".join(errs)[:600] or (r.stderr or r.stdout)[-400:]))
    os.replace(tmp, path)
    for old in sorted((f for f in glob.glob(os.path.join(d, "*.json")) if "-skip-" not in f), key=os.path.getmtime)[:-3]:
        stem = old[:-len(".json")]
        for f in [old] + glob.glob(stem + "-skip-*") + [x for x in (stem + ".autoskip", stem + ".scheme")
                                                         if os.path.exists(x)]:
            os.remove(f)
    say("  exported PIF in %.1fs" % (time.time() - t0))
    return path, True


def package_schemes(pkg_dir, scheme):
    """Scheme names Xcode may have generated for a Swift package."""
    name = os.path.basename(pkg_dir)
    try:
        m = re.search(r'Package\s*\(\s*name\s*:\s*"([^"]+)"', open(os.path.join(pkg_dir, "Package.swift")).read())
        if m:
            name = m.group(1)
    except OSError:
        pass
    out = []
    for c in (scheme, name + "-Package", name):
        if c and c not in out:
            out.append(c)
    return out


LINTERS = re.compile(r"(^|[\s/\"'])(swiftlint|swiftformat\b.*--lint\b|periphery\s+scan)")
BENIGN_LINE = re.compile(r"^(export\s+\w+=.*|\w+=[^;&|<>]*|if\b.*|then|else|elif\b.*|fi|.*\bwhich\s+\S+.*|"
                         r"command\s+-v\s+\S+.*|type\s+\S+.*|echo\b[^>]*|printf\b[^>]*|exit(\s+\d+)?|set\s+-\w+|"
                         r"cd\s+\S+|\[\[?\s.*\]\]?(\s*;\s*then)?|test\s.*|true|:)\s*;?$")


def lint_only(phase):
    """True for a shell-script phase that only runs a linter: it declares no outputs, invokes a linter without a fix
    or format mode, writes no file (`>` only to /dev/null), and every other line is plumbing (PATH, if/which, echo).
    Such a phase cannot change what the build produces, so an agent's build can leave it out. Anything else is kept."""
    if phase.get("type") != "com.apple.buildphase.shell-script":
        return False
    if phase.get("outputFilePaths") or phase.get("outputFileListPaths"):
        return False
    script = phase.get("scriptContents") or ""
    lines = [l.strip() for l in script.replace("\\\n", " ").splitlines()]
    lines = [l for l in lines if l and not l.startswith("#")]
    if re.search(r"--fix\b|--autocorrect\b|\bautocorrect\b|--format\b", script):
        return False
    if re.search(r"(?<![0-9&])>\s*(?!/dev/null|&)", script):   # output into a file
        return False
    linted = False
    for l in lines:
        probe = re.sub(r"\$\((command\s+-v|which)\s+[^()]*\)", "X", l)   # `$(which swiftlint)` only looks
        if BENIGN_LINE.match(probe) and not re.search(r"`|\$\(", probe):
            continue
        if LINTERS.search(l):
            if re.search(r"[;&|`]|\$\(", re.sub(r"\|\|\s*(true|:|echo\b[^;&|]*)$", "", probe.split(" 2>")[0])):
                return False   # a linter line that also runs something else
            linted = True
            continue
        return False
    return linted


def scripts_to_skip(pif, setting):
    """(names of shell-script phases to leave out, why). setting: "auto" or absent (lint-only phases), a list of
    names (exactly those; [] keeps every phase)."""
    if isinstance(setting, list):
        return setting, "configured"
    names = sorted({ph.get("name") for e in pif if e.get("type") == "target"
                    for ph in e["contents"].get("buildPhases", []) if lint_only(ph)})
    return names, "auto: linter-only phases"


def patch_pif(pif_path, skip):
    """A copy of the PIF without the shell-script phases named in skip (the project's own scripts an agent's builds
    need not run: a linter, a localization sync). Objects refer to each other by signature, so a changed target gets
    a new signature, and so do the project and workspace that list it. Returns (path, names actually skipped)."""
    if not skip:
        return pif_path, []
    key = short_hash(json.dumps(sorted(skip)), 10)
    out = pif_path[:-len(".json")] + "-skip-" + key + ".json"
    meta = out + ".skipped"
    if os.path.isfile(out) and os.path.isfile(meta):
        return out, json.load(open(meta))
    pif = load_pif(pif_path)
    renamed, skipped = {}, set()
    for e in pif:
        if e.get("type") != "target":
            continue
        phases = e["contents"].get("buildPhases", [])
        kept = [ph for ph in phases if not (ph.get("type") == "com.apple.buildphase.shell-script" and ph.get("name") in skip)]
        if len(kept) != len(phases):
            skipped |= {ph.get("name") for ph in phases if ph not in kept}
            e["contents"]["buildPhases"] = kept
            renamed[e["signature"]] = e["signature"] + "-xcfs-" + key
            e["signature"] = renamed[e["signature"]]
    for kind, field in (("project", "targets"), ("workspace", "projects")):
        for e in pif:
            if e.get("type") == kind and any(x in renamed for x in e["contents"].get(field, [])):
                e["contents"][field] = [renamed.get(x, x) for x in e["contents"][field]]
                renamed[e["signature"]] = e["signature"] + "-xcfs-" + key
                e["signature"] = renamed[e["signature"]]
    write_json(out, pif)
    write_json(meta, sorted(skipped))
    return out, sorted(skipped)


def is_compile_rule(rule):
    """A task that compiles: with Xcode's integrated Swift driver the build service names Swift jobs SwiftCompile /
    SwiftEmitModule; with an external driver (SWIFT_USE_INTEGRATED_DRIVER=NO) it relays them as CompileSwift /
    EmitSwiftModule (CompileSwiftSources, the driver's own task, is not counted)."""
    return rule.startswith(("SwiftCompile ", "SwiftEmitModule ", "CompileC ", "CompileSwift ", "EmitSwiftModule "))


def load_pif(path):
    with open(path) as f:
        return json.load(f)


def file_targets(pif):
    """{absolute source path: [target guid, ...]} from the projects' group trees and the targets' source phases."""
    refs = {}

    def walk(node, base, projdir):
        st, p = node.get("sourceTree", "<group>"), node.get("path", "")
        if st == "<group>":
            here = os.path.normpath(os.path.join(base, p))
        elif st == "SOURCE_ROOT":
            here = os.path.normpath(os.path.join(projdir, p))
        elif st == "<absolute>":
            here = os.path.normpath(p)
        else:
            return
        if node.get("type") == "file":
            refs[node["guid"]] = here
        for c in node.get("children", []):
            walk(c, here, projdir)
    for e in pif:
        c = e.get("contents", {})
        if e.get("type") == "project":
            projdir = phys(c.get("projectDirectory") or os.path.dirname(c.get("path", "/")))
            if c.get("groupTree"):
                walk(c["groupTree"], projdir, projdir)
    out = {}
    for e in pif:
        c = e.get("contents", {})
        if e.get("type") != "target":
            continue
        for ph in c.get("buildPhases", []):
            if ph.get("type") == "com.apple.buildphase.sources":
                for bf in ph.get("buildFiles", []):
                    p = refs.get(bf.get("fileReference"))
                    if p:
                        out.setdefault(p, []).append(c["guid"])
    return out


# ------------------------------------------------------------------ requests
def scheme_info(adapter, ctx, scheme):
    """(build target names, buildImplicitDependencies) from the scheme file, or ([scheme], True) without one."""
    root = ctx.toolchain_root
    cands = glob.glob(os.path.join(root, "*.xcworkspace", "xcshareddata", "xcschemes", scheme + ".xcscheme")) + \
        glob.glob(os.path.join(root, "**", "*.xcodeproj", "xcshareddata", "xcschemes", scheme + ".xcscheme"),
                  recursive=True) + \
        glob.glob(os.path.join(root, "**", "xcuserdata", "*", "xcschemes", scheme + ".xcscheme"), recursive=True)
    for f in cands:
        try:
            text = open(f, errors="replace").read()
        except OSError:
            continue
        m = re.search(r"<BuildAction\b([^>]*)>(.*?)</BuildAction>", text, re.S)
        if not m:
            continue
        implicit = 'buildImplicitDependencies = "NO"' not in m.group(1)
        names = []
        for entry in re.finditer(r"<BuildActionEntry\b([^>]*)>(.*?)</BuildActionEntry>", m.group(2), re.S):
            if 'buildForRunning = "NO"' in entry.group(1):
                continue
            n = re.search(r'BlueprintName\s*=\s*"([^"]+)"', entry.group(2))
            if n:
                names.append(n.group(1))
        if names:
            return names, implicit
    return [scheme], True


def target_guids(pif, names):
    targets = [e["contents"] for e in pif if e.get("type") == "target"]
    out = []
    for n in names:
        m = [t for t in targets if t.get("name") == n]
        if not m:
            raise Fail("target %r is not in the PIF (give \"target\" in the config)" % n, USAGE)
        # Packages export a static and a dynamic variant of a target under one name: build the static one.
        m.sort(key=lambda t: (t.get("productTypeIdentifier") != "com.apple.product-type.application",
                              "-dynamic" in t.get("guid", ""), t.get("guid")))
        out.append(m[0]["guid"])
    return out


def run_destination(dest):
    d = dest or "generic/platform=iOS Simulator"
    arch = "arm64" if os.uname().machine == "arm64" else "x86_64"
    if "macOS" in d:
        plat = sdk = "macosx"
    elif "Simulator" in d or d.startswith("id="):
        plat = sdk = "iphonesimulator"
    elif "iOS" in d:
        plat = sdk = "iphoneos"
        arch = "arm64"
    else:
        raise Fail("the session cannot build for destination %r yet; use the xcodebuild driver" % dest, USAGE)
    # Like xcodebuild: a generic destination builds every supported architecture, a concrete one only its own.
    return {"platform": plat, "sdk": sdk, "sdkVariant": None, "targetArchitecture": arch,
            "supportedArchitectures": ["arm64", "x86_64"], "disableOnlyActiveArch": d.startswith("generic/"),
            "hostTargetedPlatform": None}


def metal_toolchain(ctx):
    """(EXTERNAL_TOOLCHAINS_DIR, TOOLCHAINS value) when the checkout has Metal sources and the Metal toolchain is
    installed; (None, None) otherwise."""
    files = run("git", "-C", ctx.toolchain_root, "ls-files", "*.metal", check=False).stdout.strip()
    if not files:
        return None, None
    for d in sorted(glob.glob("/var/run/com.apple.security.cryptexd/mnt/com.apple.MobileAsset.MetalToolchain-*")):
        for info in glob.glob(os.path.join(d, "**", "*.xctoolchain", "ToolchainInfo.plist"), recursive=True) + \
                glob.glob(os.path.join(d, "**", "*.xctoolchain", "Info.plist"), recursive=True):
            try:
                with open(info, "rb") as f:
                    ident = plistlib.load(f).get("Identifier")
            except Exception:
                continue
            if ident:
                return d, "%s com.apple.dt.toolchain.XcodeDefault" % ident
    warn("this checkout has Metal sources but no Metal toolchain was found")
    return None, None


def make_request(adapter, ctx, pif, conf, dd, files=None, scheme_cache=None):
    scheme = adapter.scheme(ctx)
    if ctx.entry.get("target"):
        names, implicit = [ctx.entry["target"]], True
    else:   # the PIF's key covers the scheme files, so its name keys the answer
        cached = read_json(scheme_cache, None) if scheme_cache else None
        names, implicit = cached if cached else scheme_info(adapter, ctx, scheme)
        if scheme_cache and not cached:
            write_json(scheme_cache, [names, implicit])
    if files:
        ft = file_targets(pif)
        # One target per file: a package exports a static and a dynamic variant of each target, and asking for both
        # makes two commands produce the same object file.
        guids = sorted({sorted(ft[f], key=lambda g: ("-dynamic" in g, g))[0] for f in files if f in ft})
        missing = [f for f in files if f not in ft]
        if missing:
            raise Fail("not compiled by any target in the PIF: %s" % ", ".join(missing), USAGE)
    else:
        guids = target_guids(pif, names)
    table = adapter.build_settings(ctx, conf)
    ext, tc = metal_toolchain(ctx)
    if tc:
        table["TOOLCHAINS"] = tc
    req = {"parameters": {"action": "build", "configurationName": conf.get("configuration", "Debug"),
                          "activeRunDestination": run_destination(conf.get("destination")),
                          "activeArchitecture": "arm64" if os.uname().machine == "arm64" else "x86_64",
                          "arenaInfo": {"derivedDataPath": dd, "buildProductsPath": dd + "/Build/Products",
                                        "buildIntermediatesPath": dd + "/Build/Intermediates.noindex",
                                        "pchPath": dd + "/Build/Intermediates.noindex/PrecompiledHeaders",
                                        "indexRegularBuildProductsPath": None, "indexRegularBuildIntermediatesPath": None,
                                        "indexPCHPath": dd + "/Index.noindex/PrecompiledHeaders",
                                        "indexDataStoreFolderPath": dd + "/Index.noindex/DataStore",
                                        "indexEnableDataStore": False},
                          "overrides": {"commandLine": {"table": table}}},
           "configuredTargets": [{"guid": g, "parameters": None} for g in guids],
           "useParallelTargets": True, "useImplicitDependencies": implicit, "dependencyScope": "workspace",
           "useDryRun": False, "continueBuildingAfterErrors": False, "hideShellScriptEnvironment": True,
           "showNonLoggedProgress": True, "recordBuildBacktraces": None, "generatePrecompiledModulesReport": None,
           "buildPlanDiagnosticsDirPath": None,
           "buildCommand": {"command": "buildFiles", "files": files, "action": "compile"} if files else
           {"command": "build", "style": "buildOnly", "skipDependencies": False},
           "schemeCommand": "launch", "containerPath": None, "buildOnlyTheseFiles": files or None,
           "buildOnlyTheseTargets": None, "buildDescriptionID": None, "enableIndexBuildArena": False, "qos": None,
           "schedulerLaneWidthOverride": None, "useLegacyBuildLocations": False}
    return req, names[0], ext


# ------------------------------------------------------------------ the daemon
class Swbuild:
    """One swbuild process and its output, read on a thread."""

    def __init__(self, path, cwd, env, log):
        self.path, self.cwd, self.env, self.log = path, cwd, env, log
        self.p = None
        self.start()

    def start(self):
        self.p = subprocess.Popen([self.path], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  cwd=self.cwd, env=self.env, start_new_session=True)
        self.q = queue.Queue()
        p, q = self.p, self.q

        def reader():  # raw chunks, not lines: swbuild's error answers have no newline
            fd = p.stdout.fileno()
            while True:
                b = os.read(fd, 65536)
                if not b:
                    break
                q.put(b.decode("utf-8", "replace"))
            q.put(None)
        threading.Thread(target=reader, daemon=True).start()

    def kill(self):
        try:
            os.killpg(self.p.pid, signal.SIGKILL)
        except OSError:
            pass

    def restart(self):
        self.kill()
        self.start()

    def quit(self):
        try:
            self.p.stdin.write(b"quit\n")
            self.p.stdin.flush()
            self.p.wait(timeout=20)
        except Exception:
            self.kill()

    def build(self, configuration, request_file, pif, timeout):
        """Run one build. The request's configuredTargets choose the targets: naming one with --target fails when a
        package exports a static and a dynamic variant under the same name."""
        while not self.q.empty():
            self.q.get_nowait()
        t0 = time.time()
        self.p.stdin.write(("build --configuration %s --buildRequestFile %s %s\n"
                            % (shq(configuration), shq(request_file), shq(pif))).encode())
        self.p.stdin.flush()
        diags, seen, desc, result, compiled = [], set(), None, None, 0
        started, by_kind, slowest, marks = {}, {}, [], {}
        buf = ""
        while True:
            left = timeout - (time.time() - t0)
            if left <= 0:
                return {"result": "timeout", "seconds": time.time() - t0, "diagnostics": diags}
            try:
                chunk = self.q.get(timeout=min(2, left))
            except queue.Empty:
                rest = buf.replace("swbuild>", "").strip()
                if rest and not rest.startswith("{") and not rest.isdigit():
                    # swbuild answers a command it cannot run with one line and no newline, then hangs
                    self.log.write(buf + "\n")
                    return {"result": "command-error", "seconds": time.time() - t0, "message": rest[:300],
                            "diagnostics": diags}
                continue
            if chunk is None:
                return {"result": "exited", "seconds": time.time() - t0, "diagnostics": diags}
            buf += chunk
            *lines, buf = buf.split("\n")
            for line in lines:
                self.log.write(line + "\n")
                s = line.replace("swbuild>", "").strip()
                if not s.startswith("{"):
                    continue
                try:
                    j = json.loads(s)
                except ValueError:
                    continue
                k = j.get("kind")
                if k in ("error", "warning"):
                    loc = j.get("location") or {}
                    d = {"file": phys(loc["path"]) if loc.get("path") else None, "line": loc.get("line", 0),
                         "column": loc.get("column", 0), "severity": k, "message": j.get("message", "")}
                    key = tuple(d.values())
                    if key not in seen:
                        seen.add(key)
                        diags.append(d)
                elif k == "reportBuildDescription":
                    desc = j.get("buildDescriptionID")
                elif k == "taskStarted":
                    rule = str(j.get("ruleInfo", ""))
                    started[j.get("id")] = (time.time(), rule.split(" ")[0],
                                            (j.get("executionDescription") or rule)[:100])
                    if is_compile_rule(rule):
                        compiled += 1
                    marks.setdefault("first_task", time.time() - t0)
                elif k == "taskComplete" and j.get("id") in started:
                    t1, kind, what = started.pop(j["id"])
                    dt = time.time() - t1
                    c = by_kind.setdefault(kind, [0, 0.0])
                    c[0] += 1
                    c[1] += dt
                    slowest.append((dt, what))
                elif k in ("planningOperationStarted", "planningOperationCompleted", "buildStarted"):
                    marks.setdefault(k, time.time() - t0)
                elif k == "buildCompleted":
                    result = j.get("result")
            if result is not None:
                break
        diags.sort(key=lambda d: d["severity"] != "error")
        slowest.sort(reverse=True)
        timing = {"planning_seconds": round(marks.get("planningOperationCompleted", 0) -
                                            marks.get("planningOperationStarted", 0), 2),
                  "first_task_at": round(marks.get("first_task", 0), 2),
                  "task_seconds_by_kind": {k: [n, round(sec, 2)] for k, (n, sec) in
                                           sorted(by_kind.items(), key=lambda kv: -kv[1][1])[:12]},
                  "slowest_tasks": [[what, round(dt, 2)] for dt, what in slowest[:10]]}
        return {"result": result, "seconds": time.time() - t0, "diagnostics": diags, "description": desc,
                "compiled": compiled, "timing": timing}


def shq(s):
    if re.match(r"^[\w./@:+=-]+$", s):
        return s
    return '"%s"' % s.replace("\\", "\\\\").replace('"', '\\"')


def serve(sock_path, swb, cwd, env_json, log_dir):
    """Daemon main loop: one request per connection, one build at a time, quit after IDLE_SECONDS without work."""
    os.makedirs(log_dir, exist_ok=True)
    log = open(os.path.join(log_dir, "session-%s.log" % time.strftime("%Y%m%d-%H%M%S")), "w", buffering=1)
    env = dict(os.environ, **json.loads(env_json))
    sb = Swbuild(swb, cwd, env, log)
    last = {}  # (pif, kind) -> build description id, reused while the PIF is the same
    if os.path.exists(sock_path):
        os.remove(sock_path)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock_path)
    os.chmod(sock_path, 0o600)
    srv.listen(4)
    srv.settimeout(IDLE_SECONDS)
    try:
        while True:
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                break
            with conn:
                f = conn.makefile("rw")
                try:
                    req = json.loads(f.readline())
                except ValueError:
                    continue
                if req.get("op") == "quit":
                    f.write(json.dumps({"ok": True}) + "\n")
                    f.flush()
                    break
                if req.get("op") == "ping":
                    f.write(json.dumps({"ok": True, "pid": os.getpid(), "swbuild": sb.p.pid}) + "\n")
                    f.flush()
                    continue
                res = handle(sb, req, last)
                f.write(json.dumps(res) + "\n")
                f.flush()
    finally:
        sb.quit()
        try:
            os.remove(sock_path)
        except OSError:
            pass


def handle(sb, req, last):
    with open(req["request"]) as fh:
        rq = json.load(fh)
    # A build description belongs to one PIF *and* one set of request parameters (build settings, configuration,
    # destination, targets): reusing it after any of them changed would silently build with the old ones.
    fingerprint = hashlib.sha256(json.dumps(dict(rq, buildDescriptionID=None), sort_keys=True).encode()).hexdigest()
    key = (req["pif"], req["kind"], fingerprint)
    reused = False
    if last.get(key):
        rq["buildDescriptionID"] = last[key]
        reused = True
    with open(req["request"], "w") as fh:
        json.dump(rq, fh)
    attempts, out = 0, None
    while attempts < 2:
        attempts += 1
        out = sb.build(req["configuration"], req["request"], req["pif"], req["timeout"])
        if out["result"] in ("timeout", "exited", "command-error"):
            sb.restart()          # a hung or dead swbuild never gets a second command
            last.clear()
            if out["result"] == "command-error":
                break             # the same command fails the same way: report it
            continue
        break
    if out.get("description") and out["result"] in ("ok", "succeeded", "failed"):
        last[key] = out["description"]
    out.update(reused_description=reused, attempts=attempts)
    return out


# ------------------------------------------------------------------ the client
def lib_root():
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def ensure_daemon(adapter, ctx, cwd, env):
    sp = socket_path(ctx)
    if ping(sp):
        return sp
    swb = swbuild_path()
    if not swb:
        raise Fail("no swbuild in this Xcode")
    code = ("import sys; sys.path.insert(0, %r); from xcfs.adapters.xcode_session import serve; "
            "serve(*sys.argv[1:])" % lib_root())
    log_dir = os.path.join(session_dir(ctx), "logs")
    os.makedirs(log_dir, exist_ok=True)
    with open(os.path.join(log_dir, "daemon.log"), "a") as log:
        p = subprocess.Popen([sys.executable, "-c", code, sp, swb, cwd, json.dumps(env), log_dir], stdin=subprocess.DEVNULL,
                             stdout=log, stderr=log, start_new_session=True, cwd="/")
    ctx.resources["session_pid"] = p.pid
    t0 = time.time()
    while time.time() - t0 < 30:
        if ping(sp):
            say("  started a build session (pid %d)" % p.pid)
            return sp
        if p.poll() is not None:
            break
        time.sleep(0.1)
    raise Fail("the build session did not start; see %s" % os.path.join(log_dir, "daemon.log"))


def ping(sp):
    try:
        return call(sp, {"op": "ping"}, 5).get("ok")
    except (OSError, ValueError):
        return False


def call(sp, req, timeout):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(sp)
        f = s.makefile("rw")
        f.write(json.dumps(req) + "\n")
        f.flush()
        line = f.readline()
        if not line:
            raise ValueError("the session closed the connection")
        return json.loads(line)
    finally:
        s.close()


def session_build(adapter, ctx, name, conf, args, files=None):
    t0 = time.time()
    scheme = adapter.scheme(ctx)
    pif_path, exported = export_pif(adapter, ctx, scheme)
    setting = ctx.entry.get("skipScripts", "auto")
    if isinstance(setting, list):
        skip, skip_why = setting, "configured"
    else:   # decided once per PIF (its name is a hash of what it depends on)
        cached = read_json(pif_path[:-len(".json")] + ".autoskip", None)
        skip, skip_why = cached if cached else scripts_to_skip(load_pif(pif_path), setting)
        if not cached:
            write_json(pif_path[:-len(".json")] + ".autoskip", [skip, skip_why])
    pif_path, skipped = patch_pif(pif_path, skip)
    pif = load_pif(pif_path)
    dd = adapter.derived_data(ctx, name, "swbuild")
    req, target, ext = make_request(adapter, ctx, pif, conf, dd, files, scheme_cache=pif_path[:-len(".json")] + ".scheme")
    kind = "files" if files else "build"
    rdir = os.path.join(session_dir(ctx), "requests")
    os.makedirs(rdir, exist_ok=True)
    rfile = os.path.join(rdir, "%s-%s.json" % (name, kind))   # this session's own request file
    write_json(rfile, req)
    _, cwd = adapter.container_args(ctx)
    env = {"PATH": "/usr/bin:" + os.environ.get("PATH", "")}
    if ext:
        env["EXTERNAL_TOOLCHAINS_DIR"] = ext
    sp = ensure_daemon(adapter, ctx, cwd, env)
    timeout = args.get("timeout") or 3600
    try:
        out = call(sp, {"op": "build", "pif": pif_path, "request": rfile, "target": target,
                        "configuration": conf.get("configuration", "Debug"), "timeout": timeout, "kind": kind},
                   timeout * 2 + 120)
    except (OSError, ValueError) as e:
        raise Fail("the build session failed: %s" % e)
    res = {"ok": out["result"] in ("ok", "succeeded"), "timeout": out["result"] == "timeout",
           "seconds": round(time.time() - t0, 2), "build_seconds": round(out.get("seconds", 0), 2),
           "diagnostics": out.get("diagnostics", []), "derived_data": dd, "configuration": name, "driver": "session",
           "pif_exported": exported, "reused_description": out.get("reused_description", False),
           "compile_tasks": out.get("compiled", 0), "result": out["result"], "timing": out.get("timing"),
           "skipped_scripts": skipped, "skipped_scripts_reason": skip_why if skipped else None}
    if out.get("message"):
        res["diagnostics"].insert(0, {"file": None, "line": 0, "column": 0, "severity": "error",
                                      "message": "swbuild: " + out["message"]})
    return res


def build(adapter, ctx, name, conf, args):
    return session_build(adapter, ctx, name, conf, args)


def check(adapter, ctx, name, conf, files, args):
    """`buildFiles`: compile only these files, against the modules of the last build. It does not rebuild upstream
    modules, so a pass means "these files compile against what was last built", not "the app builds"."""
    r = session_build(adapter, ctx, name, conf, args, files)
    return dict(r, files=files, against="the last full build of this checkout (upstream modules are not rebuilt)")


def invalidate(ctx):
    pass  # the PIF key notices a changed file list or project; the build description follows the PIF


def stop(ctx):
    """Stop the checkout's session and wait until it and its swbuild are gone."""
    sp = socket_path(ctx)
    pid = ctx.resources.pop("session_pid", None)
    if os.path.exists(sp):
        try:
            call(sp, {"op": "quit"}, 30)
        except (OSError, ValueError):
            pass
    t0 = time.time()
    while time.time() - t0 < 30 and (os.path.exists(sp) or (pid and alive(pid))):
        time.sleep(0.1)
    if pid and alive(pid):
        try:
            os.killpg(int(pid), signal.SIGKILL)  # the daemon leads its own process group, swbuild included
        except OSError:
            pass


def alive(pid):
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError):
        return False
    r = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return bool(r.stdout.strip()) and not r.stdout.strip().startswith("Z")


# ------------------------------------------------------------------ regression check
PROBE_A = "public enum A { public static func a() -> Int { 1 } }\n"
PROBE_B = "public enum B { public static func b() -> Int { A.a() + 1 } }\n"


def regression(adapter):
    """Drive swbuild through the operations xcfs relies on, on a throw-away Swift package, and record the outcome for
    this Xcode version: cold build, no-op build, an edit, a compile error with its location, buildFiles, recovery."""
    from ..adapter import Context
    from ..state import state_dir
    rec = {"ok": False, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "steps": []}
    base = tempfile.mkdtemp(prefix="xcfs-regression-")
    try:
        pkg = os.path.join(base, "Probe")
        os.makedirs(os.path.join(pkg, "Sources", "Probe"))
        with open(os.path.join(pkg, "Package.swift"), "w") as f:
            f.write('// swift-tools-version: 5.9\nimport PackageDescription\n'
                    'let package = Package(name: "Probe", targets: [.target(name: "Probe")])\n')
        a, b = os.path.join(pkg, "Sources", "Probe", "A.swift"), os.path.join(pkg, "Sources", "Probe", "B.swift")
        open(a, "w").write(PROBE_A)
        open(b, "w").write(PROBE_B)
        run("git", "init", "-q", pkg)
        pkg = phys(pkg)
        a, b = phys(a), phys(b)
        entry = {"adapter": "xcode", "package": ".", "scheme": "Probe", "target": "Probe",
                 "configurations": {"mac": {"configuration": "Debug", "destination": "platform=macOS"}}}
        ctx = Context("checkout", pkg, pkg, {}, entry, os.path.join(base, "out"))
        conf = entry["configurations"]["mac"]

        def step(label, files=None, expect_ok=True, check=None):
            r = session_build(adapter, ctx, "mac", conf, {"timeout": 300}, files)
            good = r["ok"] == expect_ok and (check is None or check(r))
            rec["steps"].append({"step": label, "ok": good, "seconds": r["build_seconds"], "result": r["result"]})
            if not good:
                raise Fail("%s: got %s %s" % (label, r["result"], r["diagnostics"][:1]))
            return r
        step("cold build")
        step("no-op build", check=lambda r: r["compile_tasks"] == 0 and r["reused_description"])
        open(a, "a").write("public enum A2 { public static func x() -> Int { 2 } }\n")
        step("edit", check=lambda r: r["compile_tasks"] > 0)
        open(b, "a").write('public enum Broken { public static func f() -> Int { return "x" } }\n')
        step("compile error", expect_ok=False,
             check=lambda r: any(d["file"] == b and d["line"] == 2 for d in r["diagnostics"]))
        open(b, "w").write(PROBE_B)
        step("buildFiles", files=[a])
        step("recovery")
        rec["ok"] = True
    except Fail as e:
        rec["error"] = str(e)[:300]
    except Exception as e:
        rec["error"] = "%s: %s" % (type(e).__name__, str(e)[:300])
    finally:
        try:
            stop(ctx)
        except Exception:
            pass
        shutil.rmtree(base, ignore_errors=True)
    rec["check"] = check_id()
    data = read_json(regression_file(), {}) or {}
    data[adapter.version()] = rec
    write_json(regression_file(), data)
    return rec


def doctor(adapter, root, entry, run_check=False):
    checks = []
    swb = swbuild_path()
    checks.append({"ok": True, "message": "swbuild: %s" % (swb or "not in this Xcode (session builds unavailable)")})
    if not swb:
        return checks
    rec = (read_json(regression_file(), {}) or {}).get(adapter.version())
    if run_check or os.environ.get("XCFS_SESSION_CHECK"):
        rec = regression(adapter)
    if not regression_current(rec):
        checks.append({"ok": True, "message": "session regression check %s on %s (the next build runs it, or "
                                              "`xcfs doctor --session-check`)" % (
            "not run" if rec is None else "out of date", adapter.version())})
    else:
        steps = ", ".join("%s %.1fs" % (s["step"], s["seconds"]) for s in rec.get("steps", []))
        checks.append({"ok": rec["ok"], "message": "session regression check on %s: %s%s" % (
            adapter.version(), "passed" if rec["ok"] else "FAILED (%s)" % rec.get("error"), " (%s)" % steps if steps
            else "")})
    return checks
