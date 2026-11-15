"""The adapter interface: the only boundary between the core and a toolchain.

An adapter answers for one toolchain (Xcode, Gradle, …). Built-in adapters live in xcfs.adapters and are Python classes;
external adapters are executables named `xcfs-adapter-<name>` on PATH that speak the same interface as JSON over
stdin/stdout, one process per call:

    request  (stdin):  {"protocol": 1, "op": "<operation>", "context": {...} | null, "args": {...}}
    response (stdout): one JSON object. {"unsupported": true} for an operation the adapter does not implement;
                       {"error": "<message>"} for a failure. "resources" in a response replaces the context's
                       resources, which the core stores and hands back on the next call.

Operations and what they return:

    detect        args {root}                 -> {"entry": {...toolchain entry...} | null}
    validate      args {entry}                -> {"problems": [str]}
    suggestSeed   args {root}                 -> {"seed": [relative dirs]}
    suggestSeedKey args {root}                -> {"seedKey": [tracked files whose content keys the seeded layer]}
    suggest       args {root, entry}          -> {"entry": {...}, "carry": [...], "notes": [...]}  (xcfs init: fill
                                                                        in what a detected entry needs)
    version       args {}                     -> {"version": str}      (a change marks build output cold)
    afterSeed     context                     -> {}
    afterSwitch   context                     -> {}                    (after the source set changed revision)
    build         context, args {config, timeout}          -> result
    check         context, args {files, config, timeout}   -> result
    test          context, args {config, only, timeout}    -> result
    run           context, args {config, timeout}          -> result
    warm          context, args {timeout}                  -> result   (a full build of what a slot keeps ready)
    release       context                     -> {}                    (free resources: simulators, ports)
    retire        context                     -> {}                    (resources are going away for good)
    ownedCaches   args {root}                 -> {"paths": [...]}      (caches outside root that belong to root)
    foreignCaches args {owned, staleDays}     -> {"caches": [{"kind", "path", "owner"}]}
    cacheRoots    args {}                     -> {"paths": [...]}      (where those caches live)
    sharesResults args {entry}                -> {"shares": bool}      (checkouts reuse one another's compile
                                                                        results: a warm-up builds one slot first)
    orphans       args {inUse, apply}         -> {"actions": [[kind, what]]} (resources it created for checkouts
                                                                        that are gone: inUse is every live
                                                                        checkout's saved resources)
    ownProcesses  context                     -> {"pids": [...]}       (the checkout's own helpers, e.g. a build
                                                                        server kept between leases: not "in use")
    doctor        args {root, entry}          -> {"checks": [{"ok": bool, "message": str}]}

A build-like result is {"ok": bool, "seconds": float, "diagnostics": [{"file", "line", "column", "severity",
"message"}], "log": path, ...}; "timeout": true when it was killed for running too long.
"""
import json, os, shutil, subprocess

from .util import Fail, USAGE, which

PROTOCOL = 1


class Context:
    """What an adapter is handed for an operation on one checkout."""

    def __init__(self, kind, root, project, cfg, entry, build_root, session="", resources=None, name=""):
        self.kind = kind              # "worktree", "slot" or "checkout"
        self.root = root              # the checkout's primary source root
        self.project = project        # the main checkout of the primary repository
        self.cfg = cfg
        self.entry = entry
        self.toolchain_root = os.path.normpath(os.path.join(root, entry.get("root", ".")))
        self.build_root = build_root  # every adapter gets <build_root>/<adapter name> for its output
        self.session = session
        self.resources = dict(resources or {})
        self.name = name or os.path.basename(root)

    @property
    def build_dir(self):
        return os.path.join(self.build_root, self.entry["adapter"])

    def to_json(self):
        return {"kind": self.kind, "root": self.root, "project": self.project, "toolchainRoot": self.toolchain_root,
                "entry": self.entry, "buildDir": self.build_dir, "session": self.session,
                "resources": self.resources, "name": self.name}


class Adapter:
    """Base class: every operation has a harmless default, so an adapter implements only what its toolchain needs."""
    name = ""
    top_level_keys = ()   # config keys outside "toolchains" that this adapter reads (kept for older configs)

    def detect(self, root):
        return None

    def validate(self, entry):
        return []

    def suggest_seed(self, root):
        return []

    def version(self):
        return ""

    def after_seed(self, ctx):
        pass

    def after_switch(self, ctx):
        pass

    def build(self, ctx, args):
        raise Fail("the %s adapter cannot build" % self.name, USAGE)

    def check(self, ctx, files, args):
        raise Fail("the %s adapter cannot check single files" % self.name, USAGE)

    def test(self, ctx, args):
        raise Fail("the %s adapter cannot run tests" % self.name, USAGE)

    def run(self, ctx, args):
        raise Fail("the %s adapter cannot run the app" % self.name, USAGE)

    def warm(self, ctx, args):
        return {"ok": True, "seconds": 0, "diagnostics": [], "skipped": "nothing to build"}

    def release(self, ctx):
        pass

    def retire(self, ctx):
        pass

    def owned_caches(self, root):
        return []

    def orphans(self, in_use, apply):
        """[(kind, what)] for resources this adapter created for checkouts that no longer exist (removed with
        --apply). in_use: the saved resources of every checkout that still exists."""
        return []

    def shares_results(self, entry):
        """Whether checkouts reuse one another's compile results (a shared cache): a fresh pool's warm-up then builds
        one slot first and the others from what it left."""
        return False

    def suggest(self, root, entry):
        """For `xcfs init`: (the entry with what it needs filled in, ignored files to carry, notes for the user)."""
        return entry, [], []

    def advise(self, result):
        """For a failed build, check or test: what to do next when the failure is a known one, else None."""
        return None

    def suggest_seed_key(self, root):
        """For `xcfs init`: tracked files (lock files, project generator specs) whose content decides the seeded layer."""
        return []

    def undetected(self, root):
        """For `xcfs init` when this adapter did not recognize the repository: notes on what would make it (a project
        that has to be generated first, say)."""
        return []

    def foreign_caches(self, owned, stale_days):
        return []

    def cache_roots(self):
        return []

    def own_processes(self, ctx):
        return []

    def doctor(self, root, entry):
        return []


class ExternalAdapter(Adapter):
    """An `xcfs-adapter-<name>` executable on PATH."""

    def __init__(self, name, path):
        self.name, self.path = name, path

    def call(self, op, ctx=None, **args):
        req = {"protocol": PROTOCOL, "op": op, "context": ctx.to_json() if ctx else None, "args": args}
        timeout = args.get("timeout") or 3600
        try:
            r = subprocess.run([self.path], input=json.dumps(req), capture_output=True, text=True, timeout=timeout + 60,
                               cwd=(ctx.toolchain_root if ctx else "/"))
        except subprocess.TimeoutExpired:
            raise Fail("adapter %s did not answer %s within %ss" % (self.name, op, timeout + 60))
        try:
            out = json.loads(r.stdout.strip().splitlines()[-1]) if r.stdout.strip() else None
        except ValueError:
            out = None
        if not isinstance(out, dict):
            raise Fail("adapter %s answered %s with no JSON object (exit %d): %s"
                       % (self.name, op, r.returncode, (r.stderr or r.stdout).strip()[:300]))
        if out.get("error"):
            raise Fail("adapter %s: %s" % (self.name, out["error"]))
        if ctx is not None and isinstance(out.get("resources"), dict):
            ctx.resources = out["resources"]
        return out

    def _opt(self, op, ctx=None, default=None, **args):
        out = self.call(op, ctx, **args)
        return default if out.get("unsupported") else out

    def detect(self, root):
        return (self._opt("detect", root=root, default={}) or {}).get("entry")

    def validate(self, entry):
        return (self._opt("validate", entry=entry, default={}) or {}).get("problems", [])

    def suggest_seed(self, root):
        return (self._opt("suggestSeed", root=root, default={}) or {}).get("seed", [])

    def version(self):
        return (self._opt("version", default={}) or {}).get("version", "")

    def after_seed(self, ctx):
        self._opt("afterSeed", ctx)

    def after_switch(self, ctx):
        self._opt("afterSwitch", ctx)

    def _result(self, op, ctx, **args):
        out = self.call(op, ctx, **args)
        if out.get("unsupported"):
            raise Fail("the %s adapter does not support %s" % (self.name, op), USAGE)
        out.setdefault("ok", False)
        out.setdefault("diagnostics", [])
        return out

    def build(self, ctx, args):
        return self._result("build", ctx, **args)

    def check(self, ctx, files, args):
        return self._result("check", ctx, files=files, **args)

    def test(self, ctx, args):
        return self._result("test", ctx, **args)

    def run(self, ctx, args):
        return self._result("run", ctx, **args)

    def warm(self, ctx, args):
        out = self._opt("warm", ctx, **args)
        return out if out is not None else Adapter.warm(self, ctx, args)

    def release(self, ctx):
        self._opt("release", ctx)

    def retire(self, ctx):
        self._opt("retire", ctx)

    def owned_caches(self, root):
        return (self._opt("ownedCaches", root=root, default={}) or {}).get("paths", [])

    def foreign_caches(self, owned, stale_days):
        return (self._opt("foreignCaches", owned=owned, staleDays=stale_days, default={}) or {}).get("caches", [])

    def orphans(self, in_use, apply):
        return [tuple(x) for x in (self._opt("orphans", inUse=in_use, apply=apply, default={}) or {}).get("actions", [])]

    def shares_results(self, entry):
        return bool((self._opt("sharesResults", entry=entry, default={}) or {}).get("shares"))

    def advise(self, result):
        return None   # an external adapter puts "next" in its result itself

    def suggest_seed_key(self, root):
        return (self._opt("suggestSeedKey", root=root, default={}) or {}).get("seedKey", [])

    def suggest(self, root, entry):
        out = self._opt("suggest", root=root, entry=entry, default={}) or {}
        return out.get("entry") or entry, out.get("carry") or [], out.get("notes") or []

    def cache_roots(self):
        return (self._opt("cacheRoots", default={}) or {}).get("paths", [])

    def own_processes(self, ctx):
        return (self._opt("ownProcesses", ctx, default={}) or {}).get("pids", [])

    def doctor(self, root, entry):
        return (self._opt("doctor", root=root, entry=entry, default={}) or {}).get("checks", [])


def builtin_adapters():
    from .adapters.bazel import BazelAdapter
    from .adapters.xcode import XcodeAdapter
    return {"xcode": XcodeAdapter(), "bazel": BazelAdapter()}


_CACHE = {}


def get_adapter(name):
    if name in _CACHE:
        return _CACHE[name]
    builtins = builtin_adapters()
    if name in builtins:
        a = builtins[name]
    else:
        path = which("xcfs-adapter-" + name)
        if not path:
            raise Fail("no adapter named %r: not built in, and no xcfs-adapter-%s on PATH" % (name, name), USAGE)
        a = ExternalAdapter(name, path)
    _CACHE[name] = a
    return a


def adapter_keys():
    keys = set()
    for a in builtin_adapters().values():
        keys |= set(a.top_level_keys)
    return keys


def adapters_for(root, cfg):
    """[(adapter, toolchain entry)] for a checkout: the config's toolchains, else every built-in adapter that
    recognizes the checkout."""
    if cfg.get("toolchains"):
        return [(get_adapter(t["adapter"]), t) for t in cfg["toolchains"]]
    out = []
    for name, a in builtin_adapters().items():
        entry = a.detect(root)
        if entry:
            entry = dict(entry, adapter=name)
            out.append((get_adapter(name), entry))
    return out


def all_cache_adapters(cfg=None):
    """Adapters asked about caches that belong to a checkout: every built-in one (their caches exist whether or not a
    project has a config) plus the external ones a config names."""
    names = list(builtin_adapters())
    for t in (cfg or {}).get("toolchains") or []:
        if t.get("adapter") and t["adapter"] not in names:
            names.append(t["adapter"])
    return [get_adapter(n) for n in names]
