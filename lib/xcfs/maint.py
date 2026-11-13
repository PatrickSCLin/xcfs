"""Maintenance commands: gc, doctor, init, trust."""
import json, os, shutil, subprocess, time

from . import builds, config, pool, seed, state, worktree
from .adapter import adapters_for, all_cache_adapters, builtin_adapters
from .util import ERROR, Fail, USAGE, background_delete, du_kb, fs_type, human, phys, pid_alive, run, same_apfs_volume, say, \
    trash_and_delete, under, users_of


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
    with state.registry(write=False) as reg:
        gone = [p for p in reg["worktrees"] if not os.path.isdir(p)] + \
               [s["src"] for s in reg["slots"].values() if not os.path.isdir(s["src"]) and s["state"] != "creating"]
    check(not gone, "registry matches disk" + (" (%d row(s) point nowhere: run `xcfs gc --apply`)" % len(gone) if gone
                                               else ""))
    a.result(problems=probs)
    if probs:
        raise Fail("%d problem(s)" % len(probs))


def suggest_config(repo):
    pairs = adapters_for(repo, {})
    seed_dirs = []
    for ad, _ in pairs:
        for s in ad.suggest_seed(repo):
            if s not in seed_dirs:
                seed_dirs.append(s)
    cfg = {"version": 1}
    if seed_dirs:
        cfg["seed"] = seed_dirs
    cfg["toolchains"] = [e for _, e in pairs]
    return cfg


def cmd_init(a):
    repo = config.toplevel(phys(a.pos[0] if a.pos else "."))
    cfg = suggest_config(repo)
    text = json.dumps(cfg, indent=2) + "\n"
    path = os.path.join(repo, ".xcfs.json")
    if a.flag("write"):
        if os.path.exists(path) and not a.flag("force"):
            raise Fail("%s exists; pass --force to overwrite it" % path)
        with open(path, "w") as f:
            f.write(text)
        say("wrote %s" % path)
    else:
        say(text.rstrip())
        say("(not written: pass --write to save it as %s)" % path)
    if not cfg["toolchains"]:
        say("no toolchain recognized this repository; add \"toolchains\" by hand")
    a.result(config=cfg, written=path if a.flag("write") else None)


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
