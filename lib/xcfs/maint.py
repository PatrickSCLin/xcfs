"""Maintenance commands: gc, doctor, init, trust, uninstall."""
import json, os, shutil, subprocess, time

from . import builds, config, hooks, pool, seed, state, worktree
from .adapter import adapters_for, all_cache_adapters, builtin_adapters
from .util import DIRTY, ERROR, IN_USE, Fail, USAGE, background_delete, du_kb, fs_type, human, phys, pid_alive, run, same_apfs_volume, say, \
    trash_and_delete, under, users_of, warn


def cmd_gc(a):
    apply, days = a.flag("apply"), int(a.opt.get("stale-days", 21))
    settings = state.settings()
    actions = []
    with state.registry() as reg:
        for p, r in list(reg["worktrees"].items()):
            alive = r.get("state") != "creating" or pid_alive(r.get("pid"))
            if not os.path.isdir(p) or not alive:  # removed behind our back, or a `new` that died half way
                actions.append(("stale registry row", p))
                if apply:
                    if not alive:
                        worktree.undo_materialize(p, r.get("repos") or ({".": {"source": r["repo"], "branch": r.get(
                            "branch"), "made_branch": r.get("made_branch")}} if r.get("made_branch") else {}))
                    for info in worktree.repos_of(r, p).values() if r.get("repo") else []:
                        with state.repo_lock(info["source"]):
                            run("git", "-C", info["source"], "worktree", "prune", check=False)
                    reg["worktrees"].pop(p)
        managed = set(reg["worktrees"]) | {s["src"] for s in reg["slots"].values()}
        orphan_builds = [(root, rec) for root, rec in reg["builds"].items()
                         if not os.path.isdir(root) and root not in managed]
        roots = {os.path.dirname(p) for p in reg["worktrees"]} | {os.path.dirname(x[1]) for x in actions} | \
                {os.path.dirname(s) for s in reg["slots"]}
        owned = sorted(managed)
    for root, rec in orphan_builds:  # build output of a checkout that no longer exists
        actions.append(("build output of a vanished checkout", rec.get("dir", root)))
        if apply:
            with state.registry() as reg:
                reg["builds"].pop(root, None)
            if os.path.isdir(rec.get("dir", "")):
                trash_and_delete(rec["dir"], os.path.join(os.path.dirname(rec["dir"]), worktree.TRASH))
    actions += pool.gc(apply, settings)
    with state.registry(write=False) as reg:   # what live checkouts hold, by adapter
        in_use = {}
        for root, rec in reg["builds"].items():
            if os.path.isdir(root):
                for name, res in (rec.get("resources") or {}).items():
                    in_use.setdefault(name, []).append(res)
    for ad in all_cache_adapters():
        actions += ad.orphans(in_use.get(ad.name, []), apply)
    for d, meta in seed.snapshots(max_age_days=days):
        actions.append(("dependency snapshot unused for %d+ days" % days, d))
        if apply:
            trash_and_delete(d, os.path.join(os.path.dirname(d), worktree.TRASH))
    trash_roots = set(roots) | {os.path.join(state.cache_dir(), "build.noindex"), state.settings()["poolRoot"]}
    for ad in all_cache_adapters():
        trash_roots |= set(ad.cache_roots())
    for root in sorted(trash_roots):
        t = os.path.join(root, worktree.TRASH)
        if os.path.isdir(t):
            for n in os.listdir(t):  # a background delete that was interrupted (reboot, kill)
                actions.append(("leftover trash", os.path.join(t, n)))
                if apply:
                    background_delete(os.path.join(t, n))
    foreign = []
    for ad in all_cache_adapters():
        foreign += [dict(c, adapter=ad.name) for c in ad.foreign_caches(owned, days)]
    for kind, p in actions:
        say("  %-36s %s" % (kind, p))
    if a.flag("foreign"):
        say("caches not created through xcfs:")
        for c in foreign:
            say("  %8s  %-32s %s" % (human(du_kb(c["path"])), c["kind"], os.path.basename(c["path"])))
            if apply:
                if c.get("owner") and os.path.exists(c["owner"]) and users_of(phys(c["owner"])):
                    say("            in use, skipped")
                    continue
                trash_and_delete(c["path"], os.path.join(os.path.dirname(c["path"]), worktree.TRASH))
    elif foreign:
        say("%d cache(s) look orphaned or stale; they were not created through xcfs, so they are left alone (see them "
            "with --foreign)" % len(foreign))
    if not apply and (actions or (foreign and a.flag("foreign"))):
        say("dry-run: nothing removed. re-run with --apply.")
    if not actions and not foreign:
        say("nothing to reclaim.")
    a.result(actions=[list(x) for x in actions], foreign=[[c["kind"], c["path"]] for c in foreign], applied=apply)


def sizing_advice(settings, mem=None):
    """How many slots and parallel warm-ups suit this machine's memory. Measured on mid-size apps: one agent building
    in a slot peaks at about 3–4 GB, a slot's warm-up (a full build) at about 8 GB; xcfs warms one slot per 16 GB at a
    time by default."""
    mem = pool.memory_gb() if mem is None else mem
    if not mem:
        return []
    jobs = int(settings.get("warmJobs") or 0) or max(1, mem // 16)
    out = ["%d GB of memory: a warm-up builds %d slot(s) at a time (warmJobs %s); pools have %d slot(s) unless a "
           "project says otherwise (slots)" % (mem, jobs, settings.get("warmJobs") or "0 = one per 16 GB",
                                                 settings["slots"])]
    if mem < 32:
        out.append("with less than 32 GB, keep pools at 1–2 slots and warmJobs at 1 (settings.json), and run at most "
                   "two agents that build at the same time; three building agents peaked at 8–12 GB with xcfs on a "
                   "mid-size app, plus the agents themselves")
    out.append("each slot needs %d GB free on the volume to be created or warmed (slotDiskGB); measured: 1.5–3 GB of "
               "build output per slot on mid-size apps" % settings["slotDiskGB"])
    return out


def cmd_doctor(a):
    probs = []

    def check(ok, msg):
        say("  %s %s" % ("ok  " if ok else "FAIL", msg))
        if not ok:
            probs.append(msg)
    check(shutil.which("git") is not None, "git on PATH")
    os.makedirs(state.state_dir(), exist_ok=True)
    check(fs_type(state.state_dir()) == "apfs", "state directory is on APFS (%s)" % state.state_dir())
    try:
        settings = state.settings()
        check(True, "settings are valid")
    except Fail as e:
        settings = None
        check(False, str(e))
    root = phys(a.pos[0]) if a.pos else (phys(".") if a.flag("session-check") else None)
    if a.flag("session-check"):
        os.environ["XCFS_SESSION_CHECK"] = "1"  # adapters with a build session run their regression check now
    if root:
        check(fs_type(root) == "apfs", "%s is on APFS (clonefile needs it)" % root)
        if settings and os.path.isdir(os.path.join(root, ".git")):
            check(same_apfs_volume(root, settings["poolRoot"]),
                  "pool root %s is on the same APFS volume as %s" % (settings["poolRoot"], root))
        cfg = {}
        try:
            cfg = config.load_config(config.toplevel(root)) if os.path.isdir(root) else {}
        except Fail as e:
            check(False, str(e))
        try:
            pairs = adapters_for(root, cfg)
        except Fail as e:
            pairs = []
            check(False, str(e))
        # Built-in adapters check their hygiene rules even where they do not recognize the root itself (a monorepo
        # with Swift packages below the top, say).
        named = {ad.name for ad, _ in pairs}
        pairs += [(ad, {"adapter": n}) for n, ad in builtin_adapters().items() if n not in named]
        for ad, entry in pairs:
            for c in ad.doctor(root, entry):
                check(c["ok"], "[%s] %s" % (ad.name, c["message"]))
        try:   # a checkout xcfs built: what its adapters keep for it
            loc = builds.locate(root)
            with state.registry(write=False) as reg:
                known = loc["root"] in reg["builds"]
            if known:
                say("checkout %s (%s):" % (loc["root"], loc["kind"]))
                for ad, ctx in builds.contexts(loc, config.load_config(loc["project"], validate=False)):
                    for c in ad.doctor_checkout(ctx):
                        check(c["ok"], "[%s] %s" % (ad.name, c["message"]))
        except Fail:
            pass
    with state.registry(write=False) as reg:
        gone = [p for p in reg["worktrees"] if not os.path.isdir(p)] + \
               [s["src"] for s in reg["slots"].values() if not os.path.isdir(s["src"]) and s["state"] != "creating"]
    check(not gone, "registry matches disk" + (" (%d row(s) point nowhere: run `xcfs gc --apply`)" % len(gone) if gone
                                               else ""))
    advice = sizing_advice(settings) if settings else []
    for line in advice:
        say("  note " + line)
    a.result(problems=probs, advice=advice)
    if probs:
        raise Fail("%d problem(s)" % len(probs))


def suggest_config(repo, notes=None):
    pairs = adapters_for(repo, {})
    seed_dirs, seed_key, carry, toolchains = [], [], [], []
    if not pairs and notes is not None:
        for name, ad in builtin_adapters().items():
            notes += ["%s: %s" % (name, n) for n in ad.undetected(repo)]
    for ad, entry in pairs:
        for s in ad.suggest_seed(repo):
            if s not in seed_dirs:
                seed_dirs.append(s)
        seed_key += [k for k in ad.suggest_seed_key(repo) if k not in seed_key]
        entry, more, said = ad.suggest(repo, entry)
        toolchains.append(entry)
        carry += [c for c in more if c not in carry]
        if notes is not None:
            notes += ["%s: %s" % (ad.name, n) for n in said]
    cfg = {"version": 1}
    if seed_dirs:
        cfg["seed"] = seed_dirs
        if seed_key:
            cfg["seedKey"] = seed_key
    if carry:
        cfg["carry"] = carry
    cfg["toolchains"] = toolchains
    return cfg


def cmd_init(a):
    """Detect what a repository needs and suggest a config: in the repository (--write: .xcfs.json, to commit for
    everyone) or for this user only (--user: ~/.config/xcfs/<repo>.json, the repository untouched). An existing
    config, in either place, is never overwritten without --force."""
    repo = config.toplevel(phys(a.pos[0] if a.pos else "."))
    notes = []
    cfg = suggest_config(repo, notes)
    text = json.dumps(cfg, indent=2) + "\n"
    repo_path, user_path = config.config_paths(repo)
    path = user_path if a.flag("user") else repo_path
    existing = [p for p in (repo_path, user_path) if os.path.isfile(p)]
    say("detected in %s:" % repo)
    for n in notes or ["nothing"]:
        say("  " + n)
    write = a.flag("write") or a.flag("user")
    if write:
        if existing and not a.flag("force"):
            raise Fail("a config exists already: %s; pass --force to write %s anyway" % (", ".join(existing), path))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        say("wrote %s" % path)
    else:
        say(text.rstrip())
        say("(not written: --write saves it as %s, --user as %s)" % (repo_path, user_path))
        if existing:
            say("note: %s exists and is the config in use" % existing[0])
    if not cfg["toolchains"]:
        say("no toolchain recognized this repository; add \"toolchains\" by hand")
    else:
        say("next: xcfs doctor %s; xcfs pool warm %s --slots 2 --background; then xcfs checkout %s <branch>"
            % (repo, repo, repo))
    a.result(config=cfg, notes=notes, written=path if write else None, existing=existing)


def cmd_trust(a):
    if not a.pos:
        raise Fail("usage: xcfs trust <repo>", USAGE)
    repo = config.toplevel(phys(a.pos[0]))
    cfg = config.load_config(repo)
    cmds = config.commands_of(cfg)
    if not cmds:
        say("nothing to approve: no commands in %s" % (cfg["_path"] or "any config"))
        a.result(trusted=None)
        return
    say("%s will run these commands for %s:" % (cfg["_path"], repo))
    for k, c in cmds:
        say("    [%s] %s" % (k, c))
    config.trust(repo, cfg)
    say("approved. Changing those commands asks again.")
    a.result(trusted=repo, commands=[c for _, c in cmds])


# ------------------------------------------------------------------ uninstall
def own_dir(path):
    """A directory xcfs may delete as a whole (its state, its cache): never a home directory, a volume root or
    anything above the home directory, wherever an environment variable points."""
    p, home = phys(path), phys(os.path.expanduser("~"))
    return os.path.isdir(p) and p != home and not under(home, p) and len(p.strip("/").split("/")) >= 3


def cmd_uninstall(a):
    """Everything xcfs made on this machine and, with --apply, its removal: worktrees, pools, build output, dependency
    snapshots, what adapters created (simulators, build caches of their own), the hooks in the agent tool's settings,
    the state. A worktree or slot with unsaved work is kept, and then so is the state that knows about it (exit 5):
    save the work and run it again. Configs (written by the user) and the program itself are left."""
    apply = a.flag("apply")
    settings = state.settings()
    with state.registry(write=False) as reg:
        wts = sorted((p, dict(r)) for p, r in reg["worktrees"].items())
        slots = sorted((p, dict(s)) for p, s in reg["slots"].items())
        repos = sorted(set(reg["pools"]) | {s["project"] for _, s in slots})
        built = sorted((root, dict(rec)) for root, rec in reg["builds"].items())
    busy = [s["src"] for _, s in slots if s["state"] in ("creating", "warming", "retiring") and pid_alive(s.get("pid"))]
    if busy:
        raise Fail("a pool is being warmed (%s): wait for it or stop it, then run this again" % busy[0], IN_USE)
    try:
        hook_path = hooks.settings_file(None)
        hook_new, hook_changes = hooks.planned(hooks.load_settings(hook_path), False)
    except Fail as e:
        hook_path, hook_new, hook_changes = None, None, []
        warn("hooks not looked at: %s" % e)
    pool_dirs = [d for d in (pool.pool_dir(r, settings) for r in repos) if os.path.isdir(d)]
    cache, st = state.cache_dir(), state.state_dir()

    def unsaved(root, row):
        if not os.path.isdir(root):
            return ""
        code, why = worktree.unsaved_work(root, worktree.repos_of(row, root), row.get("carried"))
        return why if code else ""
    say("worktrees: %d" % len(wts))
    for p, r in wts:
        why = unsaved(p, r)
        say("  %s%s" % (p, "  (kept: %s)" % why if why else ""))
    say("pool slots: %d in %d pool(s)" % (len(slots), len(repos)))
    for p, s in slots:
        why = unsaved(s["src"], s) if s["state"] == "leased" else ""
        say("  %s  %s%s" % (s["src"], s["state"], "  (kept: %s)" % why if why else ""))
    for d in pool_dirs:
        say("pool directory: %s" % d)
    say("cache (build output, dependency snapshots, toolchain caches of xcfs): %s%s"
        % (cache, "  %s" % human(du_kb(cache)) if os.path.isdir(cache) else "  (none)"))
    say("state: %s" % st)
    for c in hook_changes:
        say("hooks in %s: %s" % (hook_path, c))
    say("left alone: your configs in %s and in repositories (.xcfs.json), hooks in a repository's own settings "
        "(`xcfs hook uninstall <repo> --apply`), the program itself" % state.config_home())
    if not apply:
        say("dry-run: nothing removed. re-run with --apply.")
        a.result(worktrees=[p for p, _ in wts], slots=[s["src"] for _, s in slots], pools=pool_dirs, cache=cache,
                 state=st, hooks=hook_changes, applied=False, kept=[])
        return 0
    kept = []
    quiet = lambda *_: None
    for p, r in wts:
        if not os.path.isdir(p):
            continue
        try:
            worktree.remove_worktree(p, True, quiet=True)
        except Fail as e:
            kept.append({"path": p, "why": str(e)})
    for slot, s in slots:
        if s["state"] == "leased":
            try:
                pool.release(slot, s, settings, rewarm=False, out=quiet)
            except Fail as e:
                kept.append({"path": s["src"], "why": str(e)})
    for repo in repos:
        try:
            pool.resize(repo, 0, config.load_config(repo, validate=False), settings)
        except Fail as e:
            kept.append({"path": repo, "why": "its pool: %s" % e})
    with state.registry(write=False) as reg:
        kept += [{"path": s["src"], "why": "slot is %s" % s["state"]} for s in reg["slots"].values()
                 if not any(k["path"] == s["src"] for k in kept)]
    if kept:
        for k in kept:
            say("  KEPT  %s  (%s)" % (k["path"], k["why"]))
        a.result(applied=True, kept=kept, removed_state=False)
        raise Fail("%d worktree(s) or slot(s) hold unsaved work or could not be removed; everything else that belonged "
                   "to removed checkouts is gone. Save the work, then run `xcfs uninstall --apply` again"
                   % len(kept), DIRTY, kept=kept, applied=True)
    for root, rec in built:   # build output (and sessions, simulators) of checkouts that are not worktrees or slots
        try:
            cfg = config.load_config(config.main_checkout(root), validate=False) if os.path.isdir(root) else {}
            builds.retire_outputs(root, cfg, rec.get("kind", "checkout"))
        except Fail as e:
            warn("%s: %s" % (root, e))
    for ad in all_cache_adapters():   # whatever adapters created and recorded for checkouts: none is in use now
        try:
            ad.orphans([], True)
        except Fail as e:
            warn("%s: %s" % (ad.name, e))
    if hook_changes:
        hooks.save_settings(hook_path, hook_new)
    for d in pool_dirs:
        trash_and_delete(d, os.path.join(os.path.dirname(d), worktree.TRASH))
    removed = []
    for d in (cache, st):
        if own_dir(d):
            background_delete(d) if d == cache else shutil.rmtree(d, ignore_errors=True)
            removed.append(d)
        elif os.path.isdir(d):
            warn("%s does not look like a directory of xcfs's own; left in place" % d)
    say("removed: %d worktree(s), %d slot(s), %s" % (len(wts), len(slots), ", ".join(removed) or "nothing else"))
    say("what is left is the program: `brew uninstall xcfs`, or delete the checkout and the link you installed")
    a.result(applied=True, kept=[], removed=removed, hooks=hook_changes, worktrees=[p for p, _ in wts],
             slots=[s["src"] for _, s in slots])
    return 0


# ------------------------------------------------------------------ stats
def median(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else 0


def cmd_stats(a):
    """What xcfs did on this machine over the last days, from its own history (nothing is sent anywhere)."""
    days = a.num("days", 7, float)
    events = state.history(time.time() - days * 86400)
    co = [e for e in events if e["event"] == "checkout"]
    built = [e for e in co if e.get("warm")]
    avoided = sum(e.get("full_build_seconds") or 0 for e in built)
    ops = {}
    for e in events:
        if e["event"] in ("build", "check", "test", "run", "warm"):
            ops.setdefault(e["event"], []).append(e)
    say("the last %g day(s), from %s:" % (days, state.history_file()))
    say("  checkouts handed out: %d, %d already built (%d at the current baseline), %d cold"
        % (len(co), len(built), sum(1 for e in built if e.get("at_baseline")), len(co) - len(built)))
    if built:
        say("  full builds agents did not wait for: %d, about %s (each slot's own last full build)"
            % (len(built), fmt_minutes(avoided)))
    rows = {}
    for op, es in sorted(ops.items()):
        secs = [e.get("seconds") or 0 for e in es]
        rows[op] = {"count": len(es), "failed": sum(1 for e in es if not e.get("ok")), "median_seconds": median(secs),
                    "total_seconds": round(sum(secs), 1)}
        say("  %-6s %4d, median %.1fs, %s in all%s" % (op, len(es), median(secs), fmt_minutes(sum(secs)),
                                                      ", %d failed" % rows[op]["failed"] if rows[op]["failed"] else ""))
    if not events:
        say("  nothing recorded yet")
    a.result(days=days, checkouts=len(co), checkouts_built=len(built), full_build_seconds_avoided=round(avoided, 1),
             operations=rows)


def fmt_minutes(secs):
    return "%.1f h" % (secs / 3600) if secs >= 5400 else "%.0f min" % (secs / 60) if secs >= 90 else "%.0f s" % secs
