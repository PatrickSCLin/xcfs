"""The Xcode adapter: xcodebuild (and, behind a regression check, the swbuild session), DerivedData, simulators."""
import glob, json, os, plistlib, re, subprocess, time

from ..adapter import Adapter
from ..util import Fail, USAGE, git_ok, phys, read_json, run, run_streaming, say, trash_and_delete, under

ENTRY_KEYS = {"adapter", "root", "workspace", "project", "package", "scheme", "target", "configurations", "default",
              "warm", "simulator", "packagesDir", "xcodebuildArgs", "buildSettings", "driver", "prepare",
              "reexportWhenChanged", "pinDerivedData", "sharedXcodeCaches", "skipScripts", "swiftDriver"}
CONFIG_KEYS = {"configuration", "destination", "testDestination", "buildSettings"}
TRASH = ".xcfs-trash"
PCM_DIR = "Build/Intermediates.noindex/SwiftExplicitPrecompiledModules"
DIAG = re.compile(r"^(?P<file>/[^:\n]+?):(?P<line>\d+):(?:(?P<col>\d+):)? (?P<sev>error|warning|note): (?P<msg>.*)$")
BARE = re.compile(r"^(?:[\w./+-]+: )?(?P<sev>error|fatal error): (?P<msg>.*)$")


def dd_root():
    return os.environ.get("XCFS_DD_ROOT") or os.path.expanduser("~/Library/Developer/Xcode/DerivedData")


def developer_dir():
    d = os.environ.get("DEVELOPER_DIR")
    if d:
        return d
    r = run("xcode-select", "-p", check=False)
    return r.stdout.strip() if r.returncode == 0 else ""


def first_json_object(text):
    """The first JSON object in text that also holds log lines (some of them with braces)."""
    dec = json.JSONDecoder()
    i = text.find("{")
    while i != -1:
        try:
            obj, _ = dec.raw_decode(text, i)
            if isinstance(obj, dict):
                return obj
        except ValueError:
            pass
        i = text.find("{", i + 1)
    return None


def parse_diagnostics(lines, limit_warnings=100):
    seen, out, warnings = set(), [], 0
    for line in lines:
        line = line.rstrip("\n")
        m = DIAG.match(line)
        if m:
            d = {"file": phys(m.group("file")), "line": int(m.group("line")), "column": int(m.group("col") or 0),
                 "severity": m.group("sev"), "message": m.group("msg").strip()}
        else:
            m = BARE.match(line.strip())
            if not m or line.startswith(" "):
                continue
            d = {"file": None, "line": 0, "column": 0, "severity": "error", "message": m.group("msg").strip()}
        if d["severity"] == "note":
            continue
        key = (d["file"], d["line"], d["column"], d["severity"], d["message"])
        if key in seen:
            continue
        seen.add(key)
        if d["severity"] == "warning":
            warnings += 1
            if warnings > limit_warnings:
                continue
        out.append(d)
    out.sort(key=lambda d: d["severity"] != "error")
    return out


class XcodeAdapter(Adapter):
    name = "xcode"
    top_level_keys = ("pinDerivedData",)

    # ---------------------------------------------------------------- config
    def detect(self, root):
        try:
            names = sorted(os.listdir(root))
        except OSError:
            return None
        ws = [n for n in names if n.endswith(".xcworkspace")]
        proj = [n for n in names if n.endswith(".xcodeproj")]
        if ws:
            return {"adapter": "xcode", "workspace": ws[0]}
        if proj:
            return {"adapter": "xcode", "project": proj[0]}
        if "Package.swift" in names:
            return {"adapter": "xcode", "package": "."}
        return None

    def validate(self, entry):
        p = ["unknown key %r" % k for k in entry if k not in ENTRY_KEYS]
        if sum(1 for k in ("workspace", "project", "package") if k in entry) > 1:
            p.append("give one of workspace, project, package")
        confs = entry.get("configurations", {})
        if not isinstance(confs, dict) or not all(isinstance(v, dict) for v in confs.values()):
            p.append("configurations must map a name to {\"configuration\", \"destination\"}")
        else:
            for n, c in confs.items():
                p += ["configurations.%s: unknown key %r" % (n, k) for k in c if k not in CONFIG_KEYS]
        if entry.get("default") and entry["default"] not in confs:
            p.append("default %r is not in configurations" % entry["default"])
        if "skipScripts" in entry and entry["skipScripts"] != "auto" and not (
                isinstance(entry["skipScripts"], list) and all(isinstance(x, str) for x in entry["skipScripts"])):
            p.append("skipScripts must be \"auto\" or a list of script phase names")
        if entry.get("swiftDriver", "auto") not in ("auto", "native", "api-digests"):
            p.append("swiftDriver must be \"auto\", \"native\" or \"api-digests\"")
        if entry.get("driver", "auto") not in ("xcodebuild", "session", "auto"):
            p.append("driver must be \"xcodebuild\", \"session\" or \"auto\"")
        return p

    def suggest_seed(self, root):
        out = []
        for d in ("Pods", ".spm_cache", "SourcePackages", "Carthage/Build", "vendor/bundle"):
            if os.path.isdir(os.path.join(root, d)) and git_ok(root, "check-ignore", "-q", d):
                out.append(d)
        return out

    _version = None

    def version(self):
        if XcodeAdapter._version is None:
            dev = developer_dir()
            v = {}
            for f in (os.path.join(os.path.dirname(dev), "version.plist"), os.path.join(os.path.dirname(dev), "Info.plist")):
                try:
                    with open(f, "rb") as fh:
                        v.update(plistlib.load(fh))
                except Exception:
                    pass
            XcodeAdapter._version = ("%s (%s)" % (v.get("CFBundleShortVersionString", "?"), v.get("ProductBuildVersion", "?"))
                                     if v else dev)
        return XcodeAdapter._version

    def configurations(self, entry):
        confs = entry.get("configurations")
        if confs:
            return confs
        dest = "platform=macOS" if entry.get("package") and not (entry.get("workspace") or entry.get("project")) \
            else "generic/platform=iOS Simulator"
        return {"debug": {"configuration": "Debug", "destination": dest}}

    def conf(self, entry, name):
        confs = self.configurations(entry)
        name = name or entry.get("default") or next(iter(confs))
        if name not in confs:
            raise Fail("no configuration %r (have: %s)" % (name, ", ".join(confs)), USAGE)
        return name, confs[name]

    def container_args(self, ctx):
        e = ctx.entry
        if e.get("workspace"):
            return ["-workspace", os.path.join(ctx.toolchain_root, e["workspace"])], ctx.toolchain_root
        if e.get("project"):
            return ["-project", os.path.join(ctx.toolchain_root, e["project"])], ctx.toolchain_root
        if e.get("package") is not None:
            return [], os.path.normpath(os.path.join(ctx.toolchain_root, e["package"]))
        found = self.detect(ctx.toolchain_root)
        if not found:
            raise Fail("no .xcworkspace, .xcodeproj or Package.swift in %s" % ctx.toolchain_root, USAGE)
        return self.container_args(type(ctx)(ctx.kind, ctx.root, ctx.project, ctx.cfg, dict(found, **{
            k: v for k, v in e.items() if k not in ("workspace", "project", "package")}), ctx.build_root))

    def scheme(self, ctx):
        if ctx.entry.get("scheme"):
            return ctx.entry["scheme"]
        if ctx.resources.get("scheme"):
            return ctx.resources["scheme"]
        args, cwd = self.container_args(ctx)
        want = os.path.splitext(os.path.basename((args or [ctx.toolchain_root])[-1]))[0]
        container = args[-1] if args else ctx.toolchain_root
        shared = [os.path.splitext(os.path.basename(f))[0]
                  for f in glob.glob(os.path.join(container, "xcshareddata", "xcschemes", "*.xcscheme"))]
        if want in shared:  # the common case, answered without resolving any package
            s = want
        else:
            # `xcodebuild -list` resolves packages: into this checkout's own directory, never the global DerivedData
            pk = os.path.join(self.derived_data(ctx, self.conf(ctx.entry, None)[0]), "SourcePackages")
            r = run("xcodebuild", "-list", "-json", *args, "-clonedSourcePackagesDirPath", pk, cwd=cwd, check=False,
                    timeout=1800)
            data = first_json_object(r.stdout)
            info = (data or {}).get("workspace") or (data or {}).get("project") or {}
            schemes = info.get("schemes") or []
            if not schemes:
                raise Fail("cannot list schemes (give \"scheme\" in the config): %s" % (r.stderr or r.stdout).strip()[-300:],
                           USAGE)
            s = want if want in schemes else schemes[0]
        ctx.resources["scheme"] = s
        return s

    # ---------------------------------------------------------------- xcodebuild
    def derived_data(self, ctx, conf_name, driver="xcodebuild"):
        # One DerivedData per configuration and per driver: xcodebuild and the session each rebuild what the other built.
        return os.path.join(ctx.build_dir, driver, conf_name)

    def xcodebuild(self, ctx, action, conf_name, conf, extra=(), destination=None, log_name=None, timeout=None):
        args, cwd = self.container_args(ctx)
        dd = self.derived_data(ctx, conf_name)
        cmd = ["xcodebuild", action, *args, "-scheme", self.scheme(ctx), "-configuration", conf.get("configuration", "Debug"),
               "-destination", destination or conf.get("destination", "generic/platform=iOS Simulator"),
               "-derivedDataPath", dd, "-hideShellScriptEnvironment"]
        if ctx.entry.get("packagesDir"):
            cmd += ["-clonedSourcePackagesDirPath", os.path.join(ctx.toolchain_root, ctx.entry["packagesDir"])]
        cmd += list(ctx.entry.get("xcodebuildArgs", [])) + list(extra)
        settings = self.build_settings(ctx, conf)
        cmd += ["%s=%s" % kv for kv in sorted(settings.items())]
        log = os.path.join(ctx.build_dir, "logs", "%s-%s-%s.log" % (time.strftime("%Y%m%d-%H%M%S"), action, conf_name))
        self.prune_logs(os.path.dirname(log))
        env = dict(os.environ, NSUnbufferedIO="YES")
        lines = []
        code, secs = run_streaming(cmd, cwd, env, log, timeout or 3600, lines.append)
        return {"ok": code == 0, "timeout": code is None, "seconds": round(secs, 2), "log": log,
                "diagnostics": parse_diagnostics(lines), "derived_data": dd, "configuration": conf_name,
                "driver": "xcodebuild", "_lines": lines}

    def build_settings(self, ctx, conf):
        """The config's build settings, plus the caches Xcode shares between all projects when DerivedData is in its
        default place: the module cache (with its build-session file), the compilation cache and the SDK stat cache.
        Given a DerivedData location of its own, a build would otherwise start fresh copies of them inside it.
        Measured on a large app, an edit that rebuilds the modules downstream of a package took 51-55 s that way
        and 44-46 s with the shared caches, the same as a build into the default DerivedData."""
        settings = {}
        if ctx.entry.get("sharedXcodeCaches", True):
            root = dd_root()
            mc = os.path.join(root, "ModuleCache.noindex")
            session_file = os.path.join(mc, "Session.modulevalidation")
            if not os.path.exists(session_file):  # clang needs it; Xcode maintains it once it exists
                os.makedirs(mc, exist_ok=True)
                open(session_file, "a").close()
            settings.update(MODULE_CACHE_DIR=mc, CLANG_MODULES_BUILD_SESSION_FILE=session_file,
                            COMPILATION_CACHE_CAS_PATH=os.path.join(root, "CompilationCache.noindex"),
                            SDK_STAT_CACHE_DIR=root)
        settings.update(ctx.entry.get("buildSettings", {}))
        settings.update(conf.get("buildSettings", {}))
        swiftc = self.swift_driver(ctx.entry)
        if swiftc:
            settings.update(SWIFT_USE_INTEGRATED_DRIVER="NO", SWIFT_EXEC=swiftc)
        return settings

    swift_driver_decision = None   # what the last build used, for its output
    _driver_noted = set()

    def swift_driver(self, entry):
        """The experimental Swift driver's SWIFT_EXEC wrapper when the entry allows it ("swiftDriver": "auto", the
        default, or "api-digests"; $XCFS_SWIFT_DRIVER overrides) and it is built for the selected Xcode's Swift.
        In auto mode a missing driver is built in the background; until then, and for any other Swift, builds use
        Xcode's own driver."""
        from . import swift_driver as sd
        mode = os.environ.get("XCFS_SWIFT_DRIVER") or entry.get("swiftDriver", "auto")
        if mode == "native":
            XcodeAdapter.swift_driver_decision = "native (configured)"
            return None
        wrapper, why, buildable = sd.status()
        if wrapper:
            XcodeAdapter.swift_driver_decision = "api-digests"
            return wrapper
        if buildable:
            log = sd.start_build(force=True)
            if log:
                why = "building it in the background for %s (log: %s)" % (sd.selected_swift(), log)
        XcodeAdapter.swift_driver_decision = "native: " + why
        if why not in XcodeAdapter._driver_noted and (mode == "api-digests" or buildable):
            from ..util import warn
            warn("experimental Swift driver: %s; this build uses Xcode's own driver" % why)
            XcodeAdapter._driver_noted.add(why)
        return None

    def prune_logs(self, d, keep=20):
        try:
            logs = sorted(glob.glob(os.path.join(d, "*.log")))
        except OSError:
            return
        for f in logs[:-keep]:
            try:
                os.remove(f)
            except OSError:
                pass

    def driver(self, ctx):
        from . import xcode_session
        return xcode_session.choose_driver(self, ctx)

    def build(self, ctx, args):
        name, conf = self.conf(ctx.entry, args.get("config"))
        if self.driver(ctx) == "session":
            from . import xcode_session
            r = xcode_session.build(self, ctx, name, conf, args)
            if XcodeAdapter.swift_driver_decision:
                r["swift_driver"] = XcodeAdapter.swift_driver_decision
            return r
        if isinstance(ctx.entry.get("skipScripts"), list) and ctx.entry["skipScripts"]:
            from ..util import warn
            warn("skipScripts applies to the session driver only; this xcodebuild build runs every script phase")
        r = self.xcodebuild(ctx, "build", name, conf, timeout=args.get("timeout"))
        r.pop("_lines", None)
        if XcodeAdapter.swift_driver_decision:
            r["swift_driver"] = XcodeAdapter.swift_driver_decision
        return r

    def check(self, ctx, files, args):
        name, conf = self.conf(ctx.entry, args.get("config"))
        missing = [f for f in files if not os.path.isfile(f)]
        if missing:
            raise Fail("no such file: %s" % ", ".join(missing), USAGE)
        if self.driver(ctx) == "session":
            from . import xcode_session
            return xcode_session.check(self, ctx, name, conf, files, args)
        r = self.xcodebuild(ctx, "build", name, conf, timeout=args.get("timeout"))
        r.pop("_lines", None)
        mine = [d for d in r["diagnostics"] if d["file"] and phys(d["file"]) in files]
        others = [d for d in r["diagnostics"] if d not in mine and d["severity"] == "error"]
        # xcodebuild cannot compile single files: this is a full build, reported for the given files.
        return dict(r, diagnostics=mine, other_errors=len(others), files=files, against="full build",
                    ok=r["ok"] and not any(d["severity"] == "error" for d in mine))

    def test(self, ctx, args):
        name, conf = self.conf(ctx.entry, args.get("config"))
        dest = conf.get("testDestination") or "id=" + self.simulator(ctx)
        bundle = os.path.join(ctx.build_dir, "results", "%s-%s.xcresult" % (time.strftime("%Y%m%d-%H%M%S"), name))
        extra = ["-resultBundlePath", bundle]
        for t in args.get("only") or []:
            extra.append("-only-testing:" + t)
        r = self.xcodebuild(ctx, "test", name, conf, extra, destination=dest, timeout=args.get("timeout"))
        lines = r.pop("_lines")
        tests = failures = None
        failed = []
        for l in lines:
            m = re.search(r"Executed (\d+) tests?, with (\d+) failures?", l)
            if m:
                tests, failures = int(m.group(1)), int(m.group(2))
            m = re.match(r"Test Case '-\[(\S+) (\S+)\]' failed", l.strip())
            if m:
                failed.append("%s/%s" % (m.group(1).split(".")[-1], m.group(2)))
            m = re.search(r"Test (\S+) .*failed after", l)
            if m and "✘" in l:
                failed.append(m.group(1))
        return dict(r, tests=tests, failures=failures, failed=sorted(set(failed)), result_bundle=bundle,
                    destination=dest)

    def run(self, ctx, args):
        name, conf = self.conf(ctx.entry, args.get("config"))
        if "Simulator" not in conf.get("destination", "Simulator") and not conf.get("destination", "").startswith("id="):
            raise Fail("xcfs run installs on a simulator; configuration %r builds for %s" % (name, conf.get("destination")),
                       USAGE)
        sim = self.simulator(ctx)
        r = self.xcodebuild(ctx, "build", name, conf, destination="id=" + sim, timeout=args.get("timeout"))
        r.pop("_lines", None)
        if not r["ok"]:
            return r
        app = self.find_app(r["derived_data"], conf.get("configuration", "Debug"), self.scheme(ctx))
        with open(os.path.join(app, "Info.plist"), "rb") as f:
            bundle_id = plistlib.load(f)["CFBundleIdentifier"]
        self.boot(sim)
        run("xcrun", "simctl", "install", sim, app, timeout=600)
        out = run("xcrun", "simctl", "launch", "--terminate-running-process", sim, bundle_id, timeout=120).stdout
        m = re.search(r":\s*(\d+)\s*$", out.strip())
        return dict(r, app=app, bundle_id=bundle_id, simulator=sim, pid=int(m.group(1)) if m else None)

    def find_app(self, dd, configuration, scheme):
        apps = glob.glob(os.path.join(dd, "Build", "Products", "%s-iphonesimulator" % configuration, "*.app"))
        if not apps:
            raise Fail("the build produced no .app under %s" % dd)
        named = [a for a in apps if os.path.splitext(os.path.basename(a))[0] == scheme]
        return (named or sorted(apps))[0]

    def warm(self, ctx, args):
        names = ctx.entry.get("warm") or [self.conf(ctx.entry, None)[0]]
        total, results = 0.0, []
        for n in names:
            r = self.build(ctx, dict(args, config=n))
            total += r.get("seconds", 0)
            results.append(r)
            if not r["ok"]:
                break
        return {"ok": all(r["ok"] for r in results), "seconds": round(total, 2),
                "diagnostics": [d for r in results for d in r["diagnostics"] if d["severity"] == "error"][:50],
                "timeout": any(r.get("timeout") for r in results), "configurations": names}

    # ---------------------------------------------------------------- switching
    def after_seed(self, ctx):
        pin = ctx.entry.get("pinDerivedData", ctx.cfg.get("pinDerivedData"))
        if pin:
            for n in sorted(os.listdir(ctx.toolchain_root)):
                if n.endswith(".xcworkspace"):
                    self.pin_derived_data(os.path.join(ctx.toolchain_root, n))
                    say("  pinned DerivedData for %s" % n)

    def pin_derived_data(self, ws):
        f = os.path.join(ws, "xcuserdata", os.environ.get("USER", "user") + ".xcuserdatad", "WorkspaceSettings.xcsettings")
        os.makedirs(os.path.dirname(f), exist_ok=True)
        try:
            with open(f, "rb") as fh:
                d = plistlib.load(fh)
        except Exception:
            d = {}
        d.update(DerivedDataLocationStyle="WorkspaceRelativePath", DerivedDataCustomLocation="DerivedData",
                 BuildLocationStyle="UseAppPreferences", CustomBuildLocationType="RelativeToDerivedData")
        with open(f, "wb") as fh:
            plistlib.dump(d, fh)

    def after_switch(self, ctx):
        """Explicit modules are validated against header mtimes: after the source set changed, a changed binary
        dependency fails the build ("has been modified since the module file was built") and retrying does not heal
        it. Clearing them costs seconds; not clearing can cost a broken slot."""
        n = 0
        for pcm in glob.glob(os.path.join(ctx.build_dir, "*", "*", PCM_DIR)):
            trash_and_delete(pcm, os.path.join(ctx.build_root, TRASH))
            n += 1
        from . import xcode_session
        xcode_session.invalidate(ctx)
        return n

    # ---------------------------------------------------------------- simulators
    def devices(self):
        data = json.loads(run("xcrun", "simctl", "list", "devices", "-j", timeout=120).stdout)
        return [dict(d, runtime=rt) for rt, ds in data.get("devices", {}).items() for d in ds]

    def base_device(self, want):
        devs = [d for d in self.devices() if d.get("isAvailable", True)]
        if want:
            m = [d for d in devs if want in (d["udid"], d["name"])]
            if not m:
                raise Fail("no simulator %r" % want, USAGE)
            return sorted(m, key=lambda d: d["runtime"])[-1]
        ios = [d for d in devs if "iOS" in d["runtime"] and d["name"].startswith("iPhone")]
        if not ios:
            raise Fail("no iPhone simulator available; install an iOS runtime or give \"simulator\" in the config")

        def rt_key(d):
            return [int(x) for x in re.findall(r"\d+", d["runtime"])]
        top = max(rt_key(d) for d in ios)
        return sorted((d for d in ios if rt_key(d) == top), key=lambda d: d["name"])[0]

    def simulator(self, ctx):
        """Each managed checkout gets its own simulator device, created on first use and deleted with the checkout:
        agents in parallel never install over each other. A plain checkout uses the base device."""
        known = {d["udid"] for d in self.devices()}
        if ctx.resources.get("simulator") in known:
            return ctx.resources["simulator"]
        base = self.base_device(ctx.entry.get("simulator"))
        if ctx.kind == "checkout":
            return base["udid"]
        udid = run("xcrun", "simctl", "create", "xcfs %s" % ctx.name, base["deviceTypeIdentifier"], base["runtime"],
                   timeout=120).stdout.strip()
        ctx.resources["simulator"] = udid
        say("  created simulator %s (%s) for %s" % (udid, base["name"], ctx.name))
        return udid

    def boot(self, udid):
        run("xcrun", "simctl", "bootstatus", udid, "-b", timeout=600)

    def xcode_helpers(self, root):
        """Xcode's own helper daemons (ibtoold, for one) that a build leaves running with their cwd inside the
        checkout. They belong to the checkout's builds, not to anyone working in it."""
        app = developer_dir()
        app = app[:app.index(".app/") + 4] if ".app/" in app else app
        if not app:
            return []
        out, pid = [], None
        try:
            lines = run("lsof", "-a", "-d", "cwd", "-Fpn", check=False, timeout=10, cwd="/").stdout.splitlines()
        except subprocess.TimeoutExpired:
            return []
        for l in lines:
            if l.startswith("p"):
                pid = l[1:]
            elif l.startswith("n") and pid and under(l[1:], root):
                exe = run("ps", "-o", "comm=", "-p", pid, check=False, cwd="/").stdout.strip()
                if exe.startswith(app + "/"):
                    out.append(int(pid))
        return out

    def own_processes(self, ctx):
        return self.xcode_helpers(ctx.root)

    def release(self, ctx):
        for pid in self.xcode_helpers(ctx.root):
            try:
                os.kill(pid, 15)
            except OSError:
                pass
        if ctx.resources.get("simulator"):
            run("xcrun", "simctl", "shutdown", ctx.resources["simulator"], check=False, timeout=120)
        from . import xcode_session
        xcode_session.stop(ctx)

    def retire(self, ctx):
        self.release(ctx)
        if ctx.resources.get("simulator"):
            run("xcrun", "simctl", "delete", ctx.resources["simulator"], check=False, timeout=120)
            ctx.resources.pop("simulator", None)

    # ---------------------------------------------------------------- global DerivedData
    def buckets(self):
        """(dir, workspace path | None, last build epoch) for every per-workspace DerivedData bucket. Buckets are named
        <name>-<28 lowercase letters>; anything else under the root is shared by all workspaces."""
        out, root = [], dd_root()
        if not os.path.isdir(root):
            return out
        for n in os.listdir(root):
            d = os.path.join(root, n)
            if not os.path.isdir(d) or not re.search(r"-[a-z]{28}$", n):
                continue
            wp = None
            try:
                with open(os.path.join(d, "info.plist"), "rb") as f:
                    wp = plistlib.load(f).get("WorkspacePath")
            except Exception:
                pass
            last = 0  # the bucket's own mtime never moves after creation; the build log directory's does
            for sub in ("Logs/Build", "Build/Intermediates.noindex", "info.plist", ""):
                try:
                    last = max(last, os.stat(os.path.join(d, sub)).st_mtime)
                except OSError:
                    pass
            out.append((d, wp, last))
        return out

    def owned_caches(self, root):
        return [d for d, wp, _ in self.buckets() if wp and under(phys(wp), root)]

    def cache_roots(self):
        return [dd_root()]

    def foreign_caches(self, owned, stale_days):
        out, now = [], time.time()
        for d, wp, last in self.buckets():
            if wp and any(under(phys(wp), o) for o in owned):
                continue
            age = int((now - last) / 86400)
            kind = "orphan (no info.plist)" if not wp else "orphan (workspace path is gone)" if not os.path.exists(wp) \
                else "stale (%dd since last build)" % age if age >= stale_days else None
            if kind:
                out.append({"kind": kind, "path": d, "owner": wp})
        return out

    # ---------------------------------------------------------------- doctor
    def doctor(self, root, entry):
        checks = []
        dev = developer_dir()
        checks.append({"ok": bool(dev) and os.path.isdir(dev), "message": "Xcode developer directory: %s (%s)"
                       % (dev or "none; run xcode-select", self.version() if dev else "-")})
        bad = []
        for dp, dns, fns in os.walk(root):
            dns[:] = [d for d in dns if not d.startswith(".") and d not in ("node_modules", "Pods")]
            if "Package.swift" in fns:
                for d in list(dns):
                    if d == "build" or d.startswith("DerivedData"):
                        n = sum(len(f) for _, _, f in os.walk(os.path.join(dp, d)))
                        if n:
                            bad.append("%s (%d files)" % (os.path.join(dp, d), n))
                        dns.remove(d)
            if dp.count(os.sep) - root.count(os.sep) >= 4:
                dns[:] = []
        # Xcode walks every non-hidden file under a local package directory each time it resolves the package graph;
        # hidden directories are skipped (measured: 105k stray files made resolution take 24s instead of 12s).
        checks.append({"ok": not bad, "message": "no build output inside local package directories" +
                       "".join("\n         " + b for b in bad)})
        from . import swift_driver as sd
        mode = os.environ.get("XCFS_SWIFT_DRIVER") or entry.get("swiftDriver", "auto")
        wrapper, why, buildable = sd.status()
        checks.append({"ok": True, "message": "experimental Swift driver (%s): %s" % (
            mode, "off" if mode == "native" else wrapper if wrapper else
            why + ("; the next build starts building it" if buildable else "") + "; builds use Xcode's own driver")})
        from . import xcode_session
        checks += xcode_session.doctor(self, root, entry)
        return checks
