"""The pool: checkouts at fixed paths that are kept built at their baseline, handed out to one session at a time, and
returned. Build state is bound to absolute paths, so a slot never moves; handing one out costs only the difference
between its baseline and the requested branch.

Slot states: creating -> cold -> (warming -> idle) -> leased -> (release) -> idle/cold ... -> retiring -> gone.
"""
import os, time

from . import builds, config, seed, state, worktree
from .util import DIRTY, Fail, NO_SPACE, POOL_FULL, USAGE, git, git_ok, iso_epoch, now_iso, phys, pid_alive, run, \
    say, self_argv, spawn_detached, trash_and_delete, under, warn

GB = 1024 ** 3


def pool_dir(repo, settings=None):
    s = settings or state.settings()
    from .util import short_hash
    return os.path.join(s["poolRoot"], "%s-%s.noindex" % (os.path.basename(repo), short_hash(repo, 6)))


def target_slots(repo, cfg, reg, settings):
    p = reg["pools"].get(repo, {})
    if "slots" in p:
        return p["slots"]
    if isinstance(cfg.get("pool"), dict) and "slots" in cfg["pool"]:
        return cfg["pool"]["slots"]
    return settings["slots"]


def slots_of(reg, repo):
    return sorted(((p, s) for p, s in reg["slots"].items() if s["project"] == repo), key=lambda x: x[1]["index"])


def find_slot(path):
    """(slot dir, row) for a slot dir, its source root, or any path inside it."""
    p = phys(path)
    with state.registry(write=False) as reg:
        for slot, s in reg["slots"].items():
            if under(p, slot):
                return slot, s
    raise Fail("%s is not in a pool slot" % p, USAGE)


def current_baselines(repo, cfg):
    return {rel: config.resolve_baseline(src, spec) for rel, src, spec in config.repo_set(repo, cfg)}


def current_versions(repo, cfg):
    from .adapter import adapters_for
    return {a.name: a.version() for a, _ in adapters_for(repo, cfg)}


def warm_matches(w, revs, versions):
    """A slot built at these revisions with these toolchain versions: its build output can be reused as is."""
    return bool(w.get("ok") and w.get("revs") == revs and all(
        (w.get("versions") or {}).get(k) == v for k, v in versions.items() if v))


def check_space(settings, where):
    from .util import free_bytes
    need = settings["slotDiskGB"] * GB
    if free_bytes(where) < need:
        raise Fail("less than %d GB free on the pool's volume (%s): not creating or warming a slot"
                   % (settings["slotDiskGB"], where), NO_SPACE)


def exclude_from_backups(path):
    """A slot is rebuildable build state: never worth backing up. This is the attribute `tmutil addexclusion` writes
    (a sticky exclusion that moves with the directory); writing it directly takes milliseconds instead of ~10 s."""
    import plistlib
    value = plistlib.dumps("com.apple.backupd", fmt=plistlib.FMT_BINARY).hex()
    run("xattr", "-wx", "com.apple.metadata:com_apple_backup_excludeItem", value, path, check=False)


# ------------------------------------------------------------------ create / retire
def create_slot(repo, cfg, index, settings):
    """Make slot <index> of repo: the repository set checked out detached at its baselines, the dependency layer
    seeded. Returns the slot dir. The caller has reserved the registry row (state "creating")."""
    pdir = pool_dir(repo, settings)
    slot = os.path.join(pdir, "slot-%d" % index)
    src = os.path.join(slot, os.path.basename(repo))
    new_pool = not os.path.isdir(pdir)
    os.makedirs(slot, exist_ok=True)
    if new_pool:
        exclude_from_backups(pdir)
    try:
        seed.check_volume(repo, slot, cfg)
        revs = current_baselines(repo, cfg)
        plan = [(rel, s, ("detach", revs[rel])) for rel, s, _ in config.repo_set(repo, cfg)]

        def progress(made):
            with state.registry() as reg:
                if slot in reg["slots"]:
                    reg["slots"][slot]["repos"] = made
        made = worktree.materialize(repo, cfg, src, plan, progress)
        for rel in made:
            made[rel]["baseline"] = revs[rel]
        seeded, origin = seed.seed_checkout(repo, src, cfg, revs["."])
        carried = seed.carry_files(repo, src, cfg)
        seed.after_seed_commands(repo, src, cfg)
        builds.after_seed(src, repo, cfg, "slot", slot)
        with state.registry() as reg:
            reg["slots"][slot].update(state="cold", repos=made, seeded=seeded, carried=carried, seed_origin=origin)
            reg["slots"][slot].pop("pid", None)
        say("  created slot %d: %s" % (index, src))
        return slot
    except BaseException:
        with state.registry(write=False) as reg:
            made = reg["slots"].get(slot, {}).get("repos", {})
        worktree.undo_materialize(src, made)
        if os.path.isdir(slot):
            trash_and_delete(slot, os.path.join(pdir, worktree.TRASH))
        with state.registry() as reg:
            reg["slots"].pop(slot, None)
        raise


def reserve(reg, repo, index, extra=None):
    slot = os.path.join(pool_dir(repo), "slot-%d" % index)
    reg["slots"][slot] = dict({"project": repo, "index": index, "state": "creating", "pid": os.getpid(),
                               "src": os.path.join(slot, os.path.basename(repo)), "created": now_iso(),
                               "repos": {}, "lease": None, "warm": None}, **(extra or {}))
    return slot


def free_index(reg, repo):
    used = {s["index"] for _, s in slots_of(reg, repo)}
    i = 1
    while i in used:
        i += 1
    return i


def retire_slot(slot, row, cfg, out=say):
    """Remove an idle slot for good: adapters free their resources, the tree and its build output go to the trash
    (deleted in the background), git forgets the worktrees."""
    src = row["src"]
    builds.retire_outputs(src, cfg, "slot", slot)
    if os.path.isdir(slot):
        trash_and_delete(slot, os.path.join(os.path.dirname(slot), worktree.TRASH))
    for info in worktree.repos_of(row, src).values():
        with state.repo_lock(info["source"]):
            run("git", "-C", info["source"], "worktree", "prune", check=False)
    with state.registry() as reg:
        reg["slots"].pop(slot, None)
    out("  retired slot %d (deleting in the background)" % row["index"])


# ------------------------------------------------------------------ switching
def switch_to(src, rev):
    git(src, "checkout", "-q", "--detach", rev)


def bring_to_baseline(slot, row, repo, cfg, fetch=False):
    """Move every repository of an idle slot to its current baseline. Returns (revs, changed, stale)."""
    stale = False
    if fetch:
        for rel, src, _ in config.repo_set(repo, cfg):
            if git(src, "remote", check=False).split():
                r = run("git", "-C", src, "fetch", "--quiet", "--prune", "origin", check=False, timeout=300)
                if r.returncode != 0:  # no network, no credentials: keep the slot as it is, marked behind
                    stale = True
                    warn("fetch failed in %s: the slot stays on the last known baseline" % src)
    revs = current_baselines(repo, cfg)
    changed = False
    src = row["src"]
    for rel, info in worktree.repos_of(row, src).items():
        p = worktree.path_of(src, rel)
        if git(p, "rev-parse", "HEAD") != revs[rel] or git(p, "branch", "--show-current", check=False):
            switch_to(p, revs[rel])
            changed = True
        info["baseline"] = revs[rel]
    return revs, changed, stale


def after_switch(slot, row, repo, cfg):
    builds.for_each_adapter(row["src"], repo, cfg, "slot", "after_switch", slot)
    if cfg.get("prepare"):
        r = config.shell(cfg["prepare"], row["src"], {"XCFS_WORKTREE": row["src"], "XCFS_SOURCE": repo}, timeout=3600)
        if r.returncode != 0:
            raise Fail("prepare failed (%d): %s\n%s" % (r.returncode, cfg["prepare"], (r.stderr or r.stdout).strip()[:300]))


# ------------------------------------------------------------------ warm
def foreground_busy(repo, except_slot):
    with state.registry(write=False) as reg:
        leased = [s["src"] for p, s in slots_of(reg, repo) if p != except_slot and s.get("lease")]
    return any(state.lock_busy("build", src) for src in leased)


def warm(repo, cfg, settings, slot_filter=None, count=None, clean=False, fetch=False, timeout=None):
    """Create missing slots, then bring every idle (or cold) slot to its baseline and build it. One warm-up at a time
    per project; a build an agent is waiting for goes first."""
    config.require_trust(repo, cfg)
    results = []
    with state.named_lock("warm", repo):
        with state.registry() as reg:
            n = count if count is not None else target_slots(repo, cfg, reg, settings)
            if count is not None:
                reg["pools"].setdefault(repo, {})["slots"] = count
            have = len(slots_of(reg, repo))
        os.makedirs(settings["poolRoot"], exist_ok=True)
        for _ in range(max(0, n - have) if slot_filter is None else 0):
            check_space(settings, settings["poolRoot"])
            with state.registry() as reg:
                slot = reserve(reg, repo, free_index(reg, repo))
            create_slot(repo, cfg, reg["slots"][slot]["index"], settings)
        with state.registry(write=False) as reg:
            todo = [(p, s) for p, s in slots_of(reg, repo) if s["state"] in ("idle", "cold")
                    and (slot_filter is None or p == slot_filter)]
        for slot, _ in todo:
            with state.registry() as reg:
                row = reg["slots"].get(slot)
                if not row or row["state"] not in ("idle", "cold"):
                    continue  # leased or retired meanwhile
                prev = row["state"]
                row.update(state="warming", pid=os.getpid())
                row = dict(row)
            try:
                code, why = worktree.unsaved_work(row["src"], worktree.repos_of(row, row["src"]), row.get("carried"))
                if code:
                    raise Fail("slot %d has uncommitted changes (%s): left alone" % (row["index"], why), DIRTY)
                check_space(settings, slot)
                revs, changed, stale = bring_to_baseline(slot, row, repo, cfg, fetch)
                if changed or prev == "cold":
                    after_switch(slot, row, repo, cfg)
                if clean:
                    with state.registry(write=False) as reg:
                        bdir = reg["builds"].get(row["src"], {}).get("dir")
                    if bdir and os.path.isdir(bdir):
                        trash_and_delete(bdir, os.path.join(slot, worktree.TRASH))
                waited = 0
                while foreground_busy(repo, slot) and waited < 1800:
                    time.sleep(2)
                    waited += 2
                say("  warming slot %d at %s ..." % (row["index"], revs["."][:10]))
                r = builds.operate("warm", row["src"], {"timeout": timeout or settings["buildTimeout"]})
                with state.registry(write=False) as reg:
                    version = reg["builds"].get(row["src"], {}).get("versions", {})
                w = {"revs": revs, "ok": bool(r.get("ok")), "at": now_iso(), "seconds": round(r.get("seconds", 0), 1),
                     "versions": version}
                if not r.get("ok"):
                    w["error"] = "; ".join(d.get("message", "") for d in r.get("diagnostics", [])[:3]) or "build failed"
                results.append({"slot": slot, "index": row["index"], "ok": w["ok"], "seconds": w["seconds"],
                                "error": w.get("error")})
                say("  slot %d %s in %.1fs" % (row["index"], "warm" if w["ok"] else "FAILED: " + w.get("error", ""),
                                              w["seconds"]))
                with state.registry() as reg:
                    if slot in reg["slots"]:
                        reg["slots"][slot].update(state="idle", warm=w, baseline_stale=stale, repos=row["repos"])
                        reg["slots"][slot].pop("pid", None)
            except BaseException as e:
                with state.registry() as reg:
                    if slot in reg["slots"]:
                        reg["slots"][slot].update(state="cold" if prev == "cold" else "idle",
                                                  warm=dict(reg["slots"][slot].get("warm") or {}, ok=False,
                                                            error=str(e)[:300], at=now_iso()))
                        reg["slots"][slot].pop("pid", None)
                if isinstance(e, Fail):
                    results.append({"slot": slot, "index": row["index"], "ok": False, "error": str(e)})
                    say("  slot %d: %s" % (row["index"], e))
                    continue
                raise
    return results


def warm_in_background(repo, slot=None):
    argv = self_argv() + ["pool", "warm", repo] + (["--slot", slot] if slot else [])
    env = dict(os.environ, XCFS_BACKGROUND="1")
    return spawn_detached(argv, state.log_file("warm-%s" % os.path.basename(repo)), env)


# ------------------------------------------------------------------ hand out / return
def checkout(repo, branch, start, session, settings, wait=0):
    cfg = config.load_config(repo)
    config.require_trust(repo, cfg)
    sets = config.repo_set(repo, cfg)
    if not git_ok(repo, "rev-parse", "--verify", "--quiet", "HEAD"):
        raise Fail("%s has no commit yet" % repo)
    at = worktree.checked_out_at(repo, branch) if worktree.branch_exists(repo, branch) else None
    if at:
        raise Fail("branch %s is already checked out at %s; a branch can be in one checkout at a time" % (branch, at))
    revs = current_baselines(repo, cfg)
    versions = current_versions(repo, cfg)
    deadline = time.time() + wait
    lease = {"session": session, "branch": branch, "at": now_iso(), "active": now_iso(), "pid": os.getpid()}
    while True:
        create = None
        with state.registry() as reg:
            free = [(p, s) for p, s in slots_of(reg, repo) if s["state"] in ("idle", "cold")]

            def score(item):
                s = item[1]
                w = s.get("warm") or {}
                stale_tool = any((w.get("versions") or {}).get(k) not in (None, v) for k, v in versions.items())
                return (0 if warm_matches(w, revs, versions) else 2 if stale_tool or not w.get("ok") else 1, s["index"])
            if free:
                slot, row = min(free, key=score)
                row.update(state="leased", lease=lease)
                row = dict(row)
                break
            if len(slots_of(reg, repo)) < max(1, target_slots(repo, cfg, reg, settings)):
                create = reserve(reg, repo, free_index(reg, repo), {"lease": lease})
        if create:
            check_space(settings, settings["poolRoot"])
            os.makedirs(settings["poolRoot"], exist_ok=True)
            with state.registry(write=False) as reg:
                idx = reg["slots"][create]["index"]
            create_slot(repo, cfg, idx, settings)
            with state.registry() as reg:
                reg["slots"][create].update(state="leased", lease=lease)
                slot, row = create, dict(reg["slots"][create])
            break
        if time.time() >= deadline:
            raise Fail("every slot of %s is in use or warming (pool of %d); wait with --wait SECONDS, release one, or "
                       "grow the pool with `xcfs pool resize`" % (repo, len(slots_of(reg, repo))), POOL_FULL)
        time.sleep(2)
    src = row["src"]
    made = False
    try:
        repos = worktree.repos_of(row, src)
        p = src
        if worktree.branch_exists(repo, branch):
            git(p, "checkout", "-q", branch)
        else:
            git(p, "checkout", "-q", "-b", branch, start or revs["."])
            made = True
        repos["."].update(branch=branch, made_branch=made)
        for rel, nsrc, _ in sets[1:]:
            if rel in repos and worktree.branch_exists(nsrc, branch) and not worktree.checked_out_at(nsrc, branch):
                git(worktree.path_of(src, rel), "checkout", "-q", branch)
                repos[rel].update(branch=branch, made_branch=False)
        after_switch(slot, row, repo, cfg)
    except BaseException:
        # Give the slot back untouched: back to its baseline, no lease.
        try:
            for rel, info in worktree.repos_of(row, src).items():
                pth = worktree.path_of(src, rel)
                if info.get("baseline") and not git(pth, "status", "--porcelain", check=False):
                    switch_to(pth, info["baseline"])
            if worktree.branch_exists(repo, branch) and not worktree.checked_out_at(repo, branch) and \
                    not worktree.own_commits(repo, branch) and made:
                run("git", "-C", repo, "branch", "-D", branch, check=False)
        finally:
            with state.registry() as reg:
                if slot in reg["slots"]:
                    reg["slots"][slot].update(state="idle" if (row.get("warm") or {}).get("ok") else "cold", lease=None)
        raise
    with state.registry() as reg:
        reg["slots"][slot]["repos"] = repos
        reg["slots"][slot]["lease"].pop("pid", None)
        w = reg["slots"][slot].get("warm") or {}
    at_base = warm_matches(w, revs, versions)
    warm_state = "warm at baseline" if at_base else \
        "warm at an older baseline" if w.get("ok") else "cold (first build is a full build)"
    say("slot %d: %s  [%s, %s]" % (row["index"], src, branch, warm_state))
    return {"slot": slot, "worktree": src, "branch": branch, "index": row["index"], "made_branch": made,
            "warm": w.get("ok", False), "warm_at_baseline": at_base,
            "session": session}


def release(slot, row, settings, prune_branch=False, discard_ignored=False, rewarm=True, out=say):
    """Return a leased slot: refuse if anything would be lost; back to the baseline; free resources; warm again in the
    background (or retire it, if the pool shrank meanwhile)."""
    repo, src = row["project"], row["src"]
    cfg = config.load_config(repo, validate=False)
    if row["state"] != "leased":
        raise Fail("slot %d is not leased (state %s)" % (row["index"], row["state"]), USAGE)
    worktree.check_removable(src, row, discard_ignored,
                             release=lambda: builds.for_each_adapter(src, repo, cfg, "slot", "release", slot))
    repos = worktree.repos_of(row, src)
    refs = worktree.rescue(src, worktree.stray_commits(src, repos))
    for r in refs:
        out("  kept detached commits as %s" % r)
    with state.registry(write=False) as reg:
        rec = reg["slots"].get(slot)
    revs = {rel: (info.get("baseline") or rec["repos"].get(rel, {}).get("baseline")) for rel, info in repos.items()}
    for rel, info in repos.items():
        if revs[rel]:
            switch_to(worktree.path_of(src, rel), revs[rel])
    deleted = worktree.prune_branches(repos, out) if prune_branch else []
    for info in repos.values():
        info.update(branch=None, made_branch=False)
    builds.for_each_adapter(src, repo, cfg, "slot", "release", slot)
    with state.registry() as reg:
        s = reg["slots"][slot]
        s.update(state="idle" if (s.get("warm") or {}).get("ok") else "cold", lease=None, repos=repos)
        excess = len(slots_of(reg, repo)) > target_slots(repo, cfg, reg, settings)
        s = dict(s)
    action = "released"
    if excess:
        retire_slot(slot, s, cfg, out)
        action = "retired"
    elif rewarm and settings["warmOnRelease"]:
        warm_in_background(repo, slot)
        action = "released, warming in the background"
    out("slot %d %s" % (row["index"], action))
    return {"slot": slot, "released": True, "action": action, "rescue_refs": refs, "branches_deleted": deleted}


def release_matching(sid, cutoff, apply, prune_branch):
    """Slots leased by a session (or leased before cutoff): what `reap` does to slots."""
    settings = state.settings()
    with state.registry(write=False) as reg:
        mine = [(p, dict(s)) for p, s in reg["slots"].items() if s["state"] == "leased" and s.get("lease")
                and (not sid or s["lease"].get("session") == sid)
                and (cutoff is None or iso_epoch(s["lease"].get("active")) <= cutoff)
                and (sid or cutoff is not None)]
    done = []
    for slot, row in sorted(mine):
        if not apply:
            code, why = worktree.unsaved_work(row["src"], worktree.repos_of(row, row["src"]), row.get("carried"))
            say("  %s  %s%s" % ("SKIP" if code else "FREE", row["src"], "  (%s)" % why if code else ""))
            done.append({"slot": slot, "released": False, "blocked": why})
            continue
        try:
            r = release(slot, row, settings, prune_branch=prune_branch, out=lambda *_: None)
            say("  FREE  %s" % row["src"])
            done.append(dict(r, blocked=""))
        except Fail as e:
            say("  SKIP  %s  (%s)" % (row["src"], str(e)[:90]))
            done.append({"slot": slot, "released": False, "blocked": str(e)})
    return done


# ------------------------------------------------------------------ status / resize / gc
def status(repo=None):
    settings = state.settings()
    rows = []
    with state.registry(write=False) as reg:
        projects = sorted({s["project"] for s in reg["slots"].values()} | set(reg["pools"])) if repo is None else [repo]
        slots = {p: dict(s) for p, s in reg["slots"].items()}
        targets = {}
        for pr in projects:
            cfg = config.load_config(pr, validate=False) if os.path.isdir(pr) else {}
            targets[pr] = target_slots(pr, cfg, reg, settings)
    now = time.time()
    for pr in projects:
        try:
            revs = current_baselines(pr, config.load_config(pr, validate=False)) if os.path.isdir(pr) else {}
        except Fail:
            revs = {}
        for p, s in sorted(((p, s) for p, s in slots.items() if s["project"] == pr), key=lambda x: x[1]["index"]):
            w = s.get("warm") or {}
            lease = s.get("lease") or {}
            idle_h = (now - iso_epoch(lease.get("active"))) / 3600 if lease else 0
            state_ = s["state"] if os.path.isdir(s["src"]) or s["state"] == "creating" else "missing"
            if state_ in ("creating", "warming") and not pid_alive(s.get("pid")):
                state_ += " (interrupted)"
            r = {"project": pr, "slot": p, "index": s["index"], "worktree": s["src"], "state": state_,
                 "warm": bool(w.get("ok")), "warm_at": w.get("at"), "at_baseline": bool(w.get("revs") == revs and revs),
                 "baseline_stale": s.get("baseline_stale", False), "warm_error": w.get("error")}
            if lease:
                r.update(session=lease.get("session", ""), branch=lease.get("branch"), leased_at=lease.get("at"),
                         idle_hours=round(idle_h, 1), stale_lease=idle_h >= settings["leaseHours"])
            rows.append(r)
            say("  %-24s slot %d  %-18s %s%s%s" % (os.path.basename(pr), s["index"], state_,
                                                   ("warm" + ("" if r["at_baseline"] else ", behind baseline")) if r["warm"]
                                                   else "cold",
                                                   "  [%s, session %s, idle %.1fh%s]" % (lease.get("branch"),
                                                   (lease.get("session") or "-")[:8], idle_h,
                                                   ", STALE" if r.get("stale_lease") else "") if lease else "",
                                                   "  (%s)" % w["error"][:60] if w.get("error") else ""))
        say("  %-24s %d of %d slot(s)" % (os.path.basename(pr), sum(1 for r in rows if r["project"] == pr), targets[pr]))
    if not projects:
        say("no pools. create one with `xcfs pool warm <repo>` (or just `xcfs checkout <repo> <branch>`).")
    return {"slots": rows, "targets": targets, "pool_root": settings["poolRoot"]}


def resize(repo, n, cfg, settings):
    with state.registry() as reg:
        reg["pools"].setdefault(repo, {})["slots"] = n
        extra = [(p, dict(s)) for p, s in slots_of(reg, repo)][n:]
    retired, kept = [], []
    for slot, row in reversed(extra):
        if row["state"] in ("idle", "cold"):
            with state.registry() as reg:
                if reg["slots"].get(slot, {}).get("state") not in ("idle", "cold"):
                    kept.append(slot)
                    continue
                reg["slots"][slot]["state"] = "retiring"
            retire_slot(slot, row, cfg)
            retired.append(slot)
        else:
            kept.append(slot)
            say("  slot %d is %s: it is retired when it is returned" % (row["index"], row["state"]))
    return {"slots": n, "retired": retired, "retire_on_release": kept}


def gc(apply, settings, out=say):
    """Pool housekeeping: rows whose directory vanished, slots whose creation or warm-up was interrupted, stale leases
    (returned if clean, reported if not)."""
    actions = []
    with state.registry(write=False) as reg:
        rows = [(p, dict(s)) for p, s in reg["slots"].items()]
    for slot, s in rows:
        alive = pid_alive(s.get("pid"))
        if s["state"] == "creating" and not alive:
            actions.append(("interrupted slot creation", slot))
            if apply:
                worktree.undo_materialize(s["src"], s.get("repos", {}))
                if os.path.isdir(slot):
                    trash_and_delete(slot, os.path.join(os.path.dirname(slot), worktree.TRASH))
                with state.registry() as reg:
                    reg["slots"].pop(slot, None)
        elif not os.path.isdir(s["src"]) and s["state"] != "creating":
            actions.append(("slot directory is gone", slot))
            if apply:
                with state.registry() as reg:
                    reg["slots"].pop(slot, None)
                    reg["builds"].pop(s["src"], None)
                for info in worktree.repos_of(s, s["src"]).values():
                    with state.repo_lock(info["source"]):
                        run("git", "-C", info["source"], "worktree", "prune", check=False)
        elif s["state"] in ("warming", "retiring") and not alive:
            actions.append(("interrupted %s" % s["state"], slot))
            if apply:
                with state.registry() as reg:
                    reg["slots"][slot]["state"] = "cold"
                    reg["slots"][slot].pop("pid", None)
        elif s["state"] == "leased" and s.get("lease"):
            idle_h = (time.time() - iso_epoch(s["lease"].get("active"))) / 3600
            if idle_h >= settings["leaseHours"]:
                code, why = worktree.unsaved_work(s["src"], worktree.repos_of(s, s["src"]), s.get("carried"))
                if code:
                    actions.append(("stale lease, NOT returned: %s" % why, slot))
                else:
                    actions.append(("stale lease (%.0fh idle), returned" % idle_h, slot))
                    if apply:
                        try:
                            release(slot, s, settings, out=lambda *_: None)
                        except Fail as e:
                            actions[-1] = ("stale lease, NOT returned: %s" % e, slot)
    return actions
