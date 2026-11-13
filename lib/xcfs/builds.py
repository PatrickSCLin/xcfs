"""Build output and adapter operations on a checkout: where output goes, which adapters answer, the per-checkout build
lock, and what happens when the toolchain changed underneath."""
import os, time

from . import config, state
from .adapter import Context, adapters_for
from .util import BUILD_FAILED, Fail, TIMEOUT, USAGE, now_iso, phys, say, short_hash, trash_and_delete, under, warn

TRASH = ".xcfs-trash"


def build_root(root, kind, slot_dir=None):
    """Build output never lives inside the source tree: `git status` stays clean, and a checkout is retired by moving
    whole directories aside. A slot keeps it next to its source; any other checkout in the cache directory."""
    if kind == "slot":
        return os.path.join(slot_dir, "build")
    return os.path.join(state.cache_dir(), "build.noindex", "%s-%s" % (os.path.basename(root), short_hash(root)))


def locate(path):
    """What managed checkout path is in: {"kind", "root", "project", "slot", "session", "row"}. Paths in no managed
    checkout are plain git checkouts (kind "checkout"): they can be built too, with output in the cache directory."""
    p = phys(path)
    with state.registry(write=False) as reg:
        for slot, s in reg["slots"].items():
            if under(p, s["src"]):
                return {"kind": "slot", "root": s["src"], "project": s["project"], "slot": slot,
                        "session": (s.get("lease") or {}).get("session", ""), "row": s}
        for wt, row in reg["worktrees"].items():
            if under(p, wt):
                return {"kind": "worktree", "root": wt, "project": row.get("repo") or config.main_checkout(wt),
                        "slot": None, "session": row.get("session", ""), "row": row}
    top = config.toplevel(p)
    return {"kind": "checkout", "root": top, "project": config.main_checkout(top), "slot": None, "session": "",
            "row": {}}


def contexts(loc, cfg, toolchain=None):
    with state.registry(write=False) as reg:
        rec = reg["builds"].get(loc["root"], {})
    broot = rec.get("dir") or build_root(loc["root"], loc["kind"], loc.get("slot"))
    out = []
    for a, entry in adapters_for(loc["root"], cfg):
        if toolchain and toolchain not in (a.name, entry.get("adapter")):
            continue
        out.append((a, Context(loc["kind"], loc["root"], loc["project"], cfg, entry, broot, loc.get("session", ""),
                               (rec.get("resources") or {}).get(a.name), name=os.path.basename(loc.get("slot") or
                                                                                               loc["root"]))))
    if toolchain and not out:
        raise Fail("no toolchain %r in this checkout's config" % toolchain, USAGE)
    return out


def save(ctx, adapter, built=False, version=None):
    with state.registry() as reg:
        rec = reg["builds"].setdefault(ctx.root, {"dir": ctx.build_root, "kind": ctx.kind, "created": now_iso()})
        rec.setdefault("resources", {})[adapter.name] = ctx.resources
        if version is not None:
            rec.setdefault("versions", {})[adapter.name] = version
        if built:
            rec["last_build"] = now_iso()
        for s in reg["slots"].values():
            if s["src"] == ctx.root and s.get("lease"):
                s["lease"]["active"] = now_iso()


def ensure_version(adapter, ctx):
    """A toolchain change (an update, a switched developer directory) makes old build output a trap: never build incrementally
    on top of it. Returns the current version."""
    v = adapter.version()
    with state.registry(write=False) as reg:
        old = (reg["builds"].get(ctx.root, {}).get("versions") or {}).get(adapter.name)
    if old and v and old != v and os.path.isdir(ctx.build_dir):
        trash_and_delete(ctx.build_dir, os.path.join(ctx.build_root, TRASH))
        warn("%s changed (%s -> %s): the old build output was discarded, this build starts cold" % (adapter.name, old, v))
    return v


def operate(op, path, args, toolchain=None):
    """Run a build-like operation (build, check, test, run, warm) through every adapter of the checkout that supports
    it, one build at a time per checkout."""
    loc = locate(path)
    cfg = config.load_config(loc["project"])
    ctxs = contexts(loc, cfg, toolchain)
    if not ctxs:
        raise Fail("no toolchain recognizes %s: add \"toolchains\" to its config (see `xcfs init`)" % loc["root"], USAGE)
    results = []
    with state.named_lock("build", loc["root"]):
        for a, ctx in ctxs:
            v = ensure_version(a, ctx)
            try:
                if op == "check":
                    r = a.check(ctx, [phys(f) for f in args.get("files", [])], args)
                else:
                    r = getattr(a, op)(ctx, args)
            except Fail as e:
                if e.code == USAGE and len(ctxs) > 1 and not toolchain:
                    continue  # this toolchain has no such operation; the others answer
                raise
            r = dict(r, toolchain=a.name)
            results.append(r)
            save(ctx, a, built=op in ("build", "warm", "test", "run") and r.get("ok"), version=v)
            if not r.get("ok"):
                break
    if not results:
        raise Fail("no toolchain of %s supports %s" % (loc["root"], op), USAGE)
    out = results[0] if len(results) == 1 else {"ok": all(r.get("ok") for r in results), "results": results}
    out.update(root=loc["root"], kind=loc["kind"])
    return out


def report(res, op):
    """Print a build-like result for people; return the exit code."""
    rs = res.get("results") or [res]
    for r in rs:
        for d in r.get("diagnostics", []):
            loc = ":".join(str(x) for x in (d.get("file"), d.get("line"), d.get("column")) if x not in (None, "", 0))
            say("%s%s: %s" % (loc + ": " if loc else "", d.get("severity", "error"), d.get("message", "")))
        bits = ["%s %s" % (r.get("toolchain", ""), "ok" if r.get("ok") else ("TIMEOUT" if r.get("timeout") else "FAILED")),
                "%.1fs" % r.get("seconds", 0)]
        for k in ("tests", "failures", "app", "pid", "against"):
            if r.get(k) not in (None, ""):
                bits.append("%s=%s" % (k, r[k]))
        if r.get("log"):
            bits.append("log: %s" % r["log"])
        say("%s: %s" % (op, "  ".join(bits)))
    if any(r.get("timeout") for r in rs):
        return TIMEOUT
    return 0 if res.get("ok") else BUILD_FAILED


def for_each_adapter(root, project, cfg, kind, method, slot=None):
    """Call a non-build adapter hook (after_seed, after_switch, release, retire) on every adapter of a checkout."""
    loc = {"kind": kind, "root": root, "project": project, "slot": slot, "session": ""}
    for a, ctx in contexts(loc, cfg):
        getattr(a, method)(ctx)
        save(ctx, a)


def own_pids(root, project, cfg, kind, slot=None):
    """Processes the checkout's adapters keep for it (a build server between leases): never a reason to refuse."""
    loc = {"kind": kind, "root": root, "project": project, "slot": slot, "session": ""}
    pids = []
    for a, ctx in contexts(loc, cfg):
        pids += [str(p) for p in a.own_processes(ctx)]
    return pids


def after_seed(root, project, cfg, kind, slot=None):
    for_each_adapter(root, project, cfg, kind, "after_seed", slot)


def retire_outputs(root, cfg, kind="worktree", slot=None):
    """A checkout goes away: let adapters free what they hold (simulators, …) and trash its build output."""
    with state.registry(write=False) as reg:
        rec = reg["builds"].get(root)
    if rec:
        try:
            for_each_adapter(root, config.main_checkout(root) if os.path.isdir(root) else root, cfg,
                             rec.get("kind", kind), "retire", slot)
        except Fail:
            pass
        with state.registry() as reg:
            reg["builds"].pop(root, None)
        if os.path.isdir(rec.get("dir", "")):
            trash_and_delete(rec["dir"], os.path.join(os.path.dirname(rec["dir"]), TRASH))
