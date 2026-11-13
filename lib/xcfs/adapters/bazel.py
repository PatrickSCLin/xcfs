"""The Bazel adapter: run the project's build command, and own the Bazel output base of every checkout.

Bazel keeps a checkout's build state in an output base outside the checkout: <output user root>/<md5 of the workspace
path>, with a resident server process per output base. Removing a worktree with git leaves both behind — gigabytes of
build state and a JVM — so this adapter reports the output base as a cache the checkout owns (removed with it) and
stops its server when the checkout is returned or removed. Because the output base is keyed by the path, a pool slot
at a fixed path keeps its build state from one lease to the next.
"""
import getpass, glob, hashlib, os, re, signal, time

from ..adapter import Adapter
from ..util import Fail, USAGE, phys, run, run_streaming, under

ENTRY_KEYS = {"adapter", "root", "build", "test", "run", "warm", "outputUserRoot", "env", "prepare", "stopServerOnRelease"}
ERR = re.compile(r"^(?:ERROR|error): (?P<file>/[^:\n]+?):(?P<line>\d+):(?P<col>\d+): (?P<msg>.*)$")


def user_roots():
    """Where Bazel keeps output bases by default: ~/Library/Caches/bazel/_bazel_<user> on macOS since Bazel 7,
    /private/var/tmp/_bazel_<user> before."""
    if os.environ.get("XCFS_BAZEL_USER_ROOT"):
        return [os.environ["XCFS_BAZEL_USER_ROOT"]]
    u = getpass.getuser()
    return [os.path.expanduser("~/Library/Caches/bazel/_bazel_%s" % u), "/private/var/tmp/_bazel_%s" % u]


def default_user_root():
    roots = user_roots()
    return next((r for r in roots if os.path.isdir(r)), roots[0])


class BazelAdapter(Adapter):
    name = "bazel"

    def detect(self, root):
        for f in ("MODULE.bazel", "WORKSPACE", "WORKSPACE.bazel"):
            if os.path.isfile(os.path.join(root, f)):
                return {"adapter": "bazel"}
        return None

    def validate(self, entry):
        p = ["unknown key %r" % k for k in entry if k not in ENTRY_KEYS]
        for k in ("build", "test", "run", "warm"):
            if k in entry and not isinstance(entry[k], str):
                p.append("%r must be a shell command (a string)" % k)
        if "env" in entry and not isinstance(entry["env"], dict):
            p.append("\"env\" must map names to values")
        return p

    def user_root(self, entry):
        return phys(os.path.expanduser(entry.get("outputUserRoot") or default_user_root()))

    def output_base(self, root, entry):
        # Bazel names the output base after the MD5 of the workspace directory's path.
        return os.path.join(self.user_root(entry), hashlib.md5(phys(root).encode()).hexdigest())

    # ---------------------------------------------------------------- commands
    def command(self, ctx, op, args):
        cmd = ctx.entry.get(op)
        if not cmd and op in ("build", "warm"):
            cmd = ctx.entry.get("build") or "bazel build //..."
        if not cmd:
            raise Fail("the bazel toolchain entry has no %r command" % op, USAGE)
        cfg = args.get("config")
        if cfg:
            cmd += " " + cfg  # extra arguments, e.g. a target or --config
        log = os.path.join(ctx.build_dir, "logs", "%s-%s.log" % (time.strftime("%Y%m%d-%H%M%S"), op))
        env = dict(os.environ, **{k: os.path.expanduser(str(v)) for k, v in ctx.entry.get("env", {}).items()})
        env.update(XCFS_WORKTREE=ctx.root, XCFS_BUILD_DIR=ctx.build_dir)
        lines = []
        code, secs = run_streaming(["/bin/sh", "-c", cmd], ctx.toolchain_root, env, log, args.get("timeout") or 3600,
                                   lines.append)
        from .xcode import parse_diagnostics  # compiler diagnostics look the same whoever runs the compiler
        diags = parse_diagnostics(lines)
        for l in lines:
            m = ERR.match(l.strip())
            if m:
                diags.append({"file": phys(m.group("file")), "line": int(m.group("line")),
                              "column": int(m.group("col")), "severity": "error", "message": m.group("msg").strip()})
        ob = self.output_base(ctx.toolchain_root, ctx.entry)
        return {"ok": code == 0, "timeout": code is None, "seconds": round(secs, 2), "log": log, "diagnostics": diags,
                "output_base": ob if os.path.isdir(ob) else None}

    def build(self, ctx, args):
        return self.command(ctx, "build", args)

    def warm(self, ctx, args):
        return self.command(ctx, "warm" if ctx.entry.get("warm") else "build", args)

    def test(self, ctx, args):
        return self.command(ctx, "test", args)

    def run(self, ctx, args):
        return self.command(ctx, "run", args)

    # ---------------------------------------------------------------- resources
    def stop_server(self, root, entry):
        """Stop the Bazel server of this checkout's output base (a JVM that otherwise stays resident)."""
        pid = self.server_pid(root, entry)
        if not pid:
            return False
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return False
        t0 = time.time()
        while time.time() - t0 < 30:
            try:
                os.kill(pid, 0)
            except OSError:
                return True
            time.sleep(0.2)
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        return True

    def server_pid(self, root, entry):
        try:
            return int(open(os.path.join(self.output_base(root, entry), "server", "server.pid.txt")).read().strip())
        except (OSError, ValueError):
            return None

    def own_processes(self, ctx):
        pid = self.server_pid(ctx.toolchain_root, ctx.entry)
        return [pid] if pid else []

    def release(self, ctx):
        # The server holds the analysis cache: keeping it makes the next lease's first build incremental instead of
        # a re-analysis (minutes on a large graph). Bazel stops an idle server on its own (--max_idle_secs, 3 h by
        # default); "stopServerOnRelease" trades that speed for memory now. Removing the checkout always stops it.
        if ctx.entry.get("stopServerOnRelease") or ctx.kind != "slot":
            self.stop_server(ctx.toolchain_root, ctx.entry)

    def retire(self, ctx):
        self.stop_server(ctx.toolchain_root, ctx.entry)

    def owned_caches(self, root):
        # Without a config entry at hand, look in the default root; a checkout's output base may sit at the
        # workspace root or below it (a toolchain "root"), so match every output base whose workspace is inside.
        out = [ob for ob, ws in self.output_bases() if ws and under(phys(ws), root)]
        for r in user_roots():
            mine = os.path.join(r, hashlib.md5(phys(root).encode()).hexdigest())
            if os.path.isdir(mine) and mine not in out:
                out.append(mine)
        return out

    def output_bases(self, user_root=None):
        """(output base, workspace path or None) for every output base under the user root."""
        out = []
        for ob in [p for r in ([user_root] if user_root else user_roots()) for p in glob.glob(os.path.join(r, "*"))]:
            if not re.match(r"^[0-9a-f]{32}$", os.path.basename(ob)) or not os.path.isdir(ob):
                continue
            out.append((ob, self.workspace_of(ob)))
        return out

    def workspace_of(self, ob):
        # Bazel writes a README into each output base naming its workspace.
        try:
            text = open(os.path.join(ob, "README"), errors="replace").read()
        except OSError:
            return None
        m = re.search(r"^WORKSPACE:\s*(.+)$", text, re.M)
        return m.group(1).strip() if m else None

    def cache_roots(self):
        return [r for r in user_roots() if os.path.isdir(r)]

    def foreign_caches(self, owned, stale_days):
        out, now = [], time.time()
        for ob, ws in self.output_bases():
            if ws and any(under(phys(ws), o) for o in owned):
                continue
            try:
                age = (now - os.stat(os.path.join(ob, "server")).st_mtime) / 86400
            except OSError:
                age = (now - os.stat(ob).st_mtime) / 86400
            kind = "orphan (no README)" if not ws else "orphan (workspace is gone)" if not os.path.exists(ws) \
                else "stale (%dd since last use)" % age if age >= stale_days else None
            if kind:
                out.append({"kind": kind, "path": ob, "owner": ws})
        return out

    def doctor(self, root, entry):
        ob = self.output_base(root, entry or {})
        return [{"ok": True, "message": "bazel output base for %s: %s%s" % (root, ob, "" if os.path.isdir(ob) else
                                                                           " (not created yet)")}]
