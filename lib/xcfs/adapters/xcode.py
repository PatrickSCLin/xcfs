"""The Xcode adapter: xcodebuild (and, behind a regression check, the swbuild session), DerivedData, simulators."""
import glob, json, os, plistlib, re, subprocess, time

from ..adapter import Adapter
from ..util import Fail, USAGE, git_ok, phys, read_json, run, run_streaming, say, trash_and_delete, under

ENTRY_KEYS = {"adapter", "root", "workspace", "project", "package", "scheme", "target", "configurations", "default",
              "warm", "simulator", "packagesDir", "xcodebuildArgs", "buildSettings", "driver", "prepare",
              "reexportWhenChanged", "pinDerivedData"}
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


def parse_diagnostics(lines, limit_warnings=100):
    seen, out, warnings = set(), [], 0
    for line in lines:
        line = line.rstrip("\n")
        m = DIAG.match(line)
        if m:
            d = {"file": m.group("file"), "line": int(m.group("line")), "column": int(m.group("col") or 0),
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
        if entry.get("driver", "xcodebuild") not in ("xcodebuild", "session", "auto"):
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
        r = run("xcodebuild", "-list", "-json", *args, cwd=cwd, check=False, timeout=600)
        try:
            data = json.loads(r.stdout[r.stdout.index("{"):])
        except ValueError:
            raise Fail("cannot list schemes (give \"scheme\" in the config): %s" % (r.stderr or r.stdout).strip()[:300])
        info = data.get("workspace") or data.get("project") or {}
        schemes = info.get("schemes") or []
        if not schemes:
            raise Fail("no schemes found in %s; give \"scheme\" in the config" % ctx.toolchain_root, USAGE)
        want = os.path.splitext(os.path.basename((args or [ctx.toolchain_root])[-1]))[0]
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
        settings = dict(ctx.entry.get("buildSettings", {}), **conf.get("buildSettings", {}))
        cmd += ["%s=%s" % kv for kv in sorted(settings.items())]
        log = os.path.join(ctx.build_dir, "logs", "%s-%s-%s.log" % (time.strftime("%Y%m%d-%H%M%S"), action, conf_name))
        self.prune_logs(os.path.dirname(log))
        env = dict(os.environ, NSUnbufferedIO="YES")
        lines = []
        code, secs = run_streaming(cmd, cwd, env, log, timeout or 3600, lines.append)
        return {"ok": code == 0, "timeout": code is None, "seconds": round(secs, 2), "log": log,
                "diagnostics": parse_diagnostics(lines), "derived_data": dd, "configuration": conf_name,
                "driver": "xcodebuild", "_lines": lines}

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
            return xcode_session.build(self, ctx, name, conf, args)
        r = self.xcodebuild(ctx, "build", name, conf, timeout=args.get("timeout"))
        r.pop("_lines", None)
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

    def release(self, ctx):
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
        from . import xcode_session
        checks += xcode_session.doctor(self, root, entry)
        return checks
