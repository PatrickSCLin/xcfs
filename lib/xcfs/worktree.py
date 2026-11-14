"""Checkouts of a repository set: create one (git worktrees + seeded dependency layer), inspect it for unsaved work,
remove it with everything it owns. Used by plain worktrees (`xcfs new`) and by pool slots alike."""
import os, shutil, time

from . import builds, config, seed, state
from .adapter import all_cache_adapters
from .util import DIRTY, Fail, IGNORED_CHANGED, IN_USE, git, git_ok, now_iso, phys, run, say, trash_and_delete, \
    under, users_of

TRASH = ".xcfs-trash"


def branch_exists(src, branch):
    return git_ok(src, "show-ref", "--verify", "--quiet", "refs/heads/" + branch)


def checked_out_at(src, branch):
    """Where branch is checked out among src's worktrees, or None. git refuses a branch in two worktrees at once."""
    cur = None
    for l in git(src, "worktree", "list", "--porcelain").splitlines():
        if l.startswith("worktree "):
            cur = l[9:]
        elif l == "branch refs/heads/" + branch:
            return cur
    return None


def submodule_overrides(src):
    """Config overrides that make `git submodule update` clone each submodule from the source checkout's own copy:
    a local clone (objects hard-linked, no network) instead of a fresh download per checkout."""
    if not os.path.isfile(os.path.join(src, ".gitmodules")):
        return []
    out = ["-c", "protocol.file.allow=always"]
    listing = git(src, "config", "-f", ".gitmodules", "--get-regexp", r"^submodule\..*\.path$", check=False)
    for line in listing.splitlines():
        key, _, path = line.partition(" ")
        name = key[len("submodule."):-len(".path")]
        local = os.path.join(src, path)
        if os.path.exists(os.path.join(local, ".git")):
            out += ["-c", "submodule.%s.url=%s" % (name, local)]
    return out


def initialized_submodules(src):
    """Paths of the source checkout's submodules that are checked out there. One the source never initialized (an
    optional or private one, say) is left alone: a checkout made from it should not need what the source does without."""
    r = run("git", "-C", src, "submodule", "status", check=False)
    out = []
    for line in r.stdout.splitlines():
        if line and line[0] != "-":
            parts = line[1:].split()
            if len(parts) >= 2:
                out.append(parts[1])
    return out


def sync_submodules(src, target):
    """Check out the submodules a commit records, those the source checkout has checked out. From the source's copies
    first; if those lack a needed commit, from the submodules' own remotes."""
    if not os.path.isfile(os.path.join(target, ".gitmodules")):
        return False
    paths = initialized_submodules(src)
    if not paths:
        return False
    args = ["submodule", "update", "--init", "--recursive", "--checkout", "-j", "8", "--", *paths]
    r = run("git", "-C", target, *submodule_overrides(src), *args, check=False, timeout=3600)
    if r.returncode != 0:
        r = run("git", "-C", target, *args, check=False, timeout=3600)
        if r.returncode != 0:
            raise Fail("git submodule update failed in %s: %s" % (target, (r.stderr or r.stdout).strip()[-400:]))
    return True


def switch(path, src, *checkout_args):
    """git checkout in a checkout of src, then bring its submodules along."""
    git(path, "checkout", "-q", *checkout_args)
    sync_submodules(src, path)


def add_worktree(src, dest, mode):
    """mode: ("branch", name, start-or-None) or ("detach", rev). Returns True when this call created the branch."""
    with state.repo_lock(src):
        if mode[0] == "detach":
            git(src, "worktree", "add", "--detach", dest, mode[1])
            return False
        name, start = mode[1], mode[2]
        if branch_exists(src, name):
            git(src, "worktree", "add", dest, name)
            return False
        git(src, "worktree", "add", "-b", name, dest, *([start] if start else []))
        return True


def materialize(repo, cfg, dest, plan, on_progress=None):
    """Create the worktrees of a repository set under dest. plan: [(rel, src, mode)], primary first.
    Returns {rel: {"source", "branch", "made_branch"}}. On failure everything made so far is undone."""
    made = {}
    try:
        for rel, src, mode in plan:
            target = dest if rel == "." else os.path.join(dest, rel)
            if rel != ".":
                os.makedirs(os.path.dirname(target), exist_ok=True)
            mb = add_worktree(src, target, mode)
            made[rel] = {"source": src, "branch": mode[1] if mode[0] == "branch" else None, "made_branch": mb}
            sync_submodules(src, target)
            if on_progress:
                on_progress(made)
        return made
    except BaseException:
        undo_materialize(dest, made)
        raise


def undo_materialize(dest, made):
    for rel in sorted(made, key=lambda r: -r.count("/") if r != "." else 1):
        info = made[rel]
        target = dest if rel == "." else os.path.join(dest, rel)
        with state.repo_lock(info["source"]):
            run("git", "-C", info["source"], "worktree", "remove", "--force", target, check=False)
            run("git", "-C", info["source"], "worktree", "prune", check=False)
            if info.get("made_branch") and info.get("branch"):
                run("git", "-C", info["source"], "branch", "-D", info["branch"], check=False)
    if os.path.lexists(dest):
        shutil.rmtree(dest, ignore_errors=True)
    for info in made.values():
        with state.repo_lock(info["source"]):
            run("git", "-C", info["source"], "worktree", "prune", check=False)


def repos_of(row, root):
    """The repository set of a registered checkout, primary first: {rel: info}. Rows written by v0.1 have no "repos"."""
    if row.get("repos"):
        return row["repos"]
    src = row.get("repo") or config.main_checkout(root)
    return {".": {"source": src, "branch": row.get("branch"), "made_branch": row.get("made_branch", False)}}


def path_of(root, rel):
    return root if rel == "." else os.path.join(root, rel)


def unsaved_work(root, repos, carried):
    """Why removing (or switching) root would lose work: (exit code, message) or (0, "")."""
    for rel in sorted(repos, key=lambda r: -r.count("/") if r != "." else 1):
        p = path_of(root, rel)
        if not os.path.isdir(p):
            continue
        dirty = [l for l in git(p, "status", "--porcelain").splitlines() if l.strip()]
        if dirty:
            return DIRTY, "%d uncommitted file(s)%s" % (len(dirty), "" if rel == "." else " in " + rel)
        lost = submodule_commits_only_here(p, repos[rel].get("source"))
        if lost:
            return DIRTY, "submodule commit(s) that exist only in this checkout: %s" % ", ".join(lost)
    return 0, ""


def submodule_commits_only_here(path, src):
    """Submodules whose checked-out commit the source checkout's copy does not have: a commit made here would vanish
    with this checkout's submodule repositories."""
    if not src or not os.path.isfile(os.path.join(path, ".gitmodules")):
        return []
    out = []
    listing = run("git", "-C", path, "submodule", "foreach", "--quiet", "--recursive",
                  'echo "$displaypath $(git rev-parse HEAD)"', check=False).stdout
    for line in listing.splitlines():
        sub, _, sha = line.rpartition(" ")
        mine = os.path.join(src, sub)
        if sha and os.path.exists(os.path.join(mine, ".git")) and not git_ok(mine, "cat-file", "-e", sha + "^{commit}"):
            out.append(sub)
    return out


def stray_commits(root, repos):
    """[(rel, source, sha)] for repositories on a detached HEAD holding commits that no branch, tag or remote has."""
    out = []
    for rel, info in repos.items():
        p = path_of(root, rel)
        if os.path.isdir(p) and not git(p, "branch", "--show-current", check=False):
            sha = git(p, "rev-list", "--max-count=1", "HEAD", "--not", "--branches", "--tags", "--remotes", check=False)
            if sha:
                out.append((rel, info["source"], git(p, "rev-parse", "HEAD")))
    return out


def rescue(root, strays):
    refs = []
    for rel, src, sha in strays:
        ref = "refs/xcfs/rescue/%s%s-%s" % (os.path.basename(root), "" if rel == "." else "-" + rel.replace("/", "-"),
                                            time.strftime("%Y%m%d-%H%M%S"))
        git(src, "update-ref", ref, sha)
        refs.append(ref)
    return refs


def own_commits(src, branch):
    """Commits on branch that no other branch, tag or remote has. (With --branches, --exclude takes the name without
    refs/heads/: a full ref name never matches, and then every branch looks empty.)"""
    return git(src, "rev-list", "--max-count=1", branch, "--not", "--exclude=" + branch, "--branches",
               "--tags", "--remotes", check=False)


def prune_branches(repos, out):
    deleted = []
    for rel, info in repos.items():
        b = info.get("branch")
        if not (b and info.get("made_branch")):
            continue
        # Only a branch xcfs itself created, and only when it holds nothing reachable from nowhere else.
        if not checked_out_at(info["source"], b) and not own_commits(info["source"], b):
            if run("git", "-C", info["source"], "branch", "-D", b, check=False).returncode == 0:
                deleted.append(b if rel == "." else "%s:%s" % (rel, b))
                out("  deleted branch %s (xcfs created it and it never got a commit of its own)" % b)
    return deleted


def check_removable(root, row, discard_ignored, skip_pids=(), release=None):
    repos = repos_of(row, root)
    code, why = unsaved_work(root, repos, row.get("carried"))
    if code:
        raise Fail("%s in %s: commit or stash first, xcfs will not force" % (why, root), code)
    lost = seed.changed_carried(root, row.get("carried"))
    if lost and not discard_ignored:
        raise Fail("ignored file(s) changed in %s and exist nowhere else: %s (pass --discard-ignored to drop them)"
                   % (root, ", ".join(lost)), IGNORED_CHANGED)
    if release:
        release()  # the checkout's own helpers (a build session, a simulator) must not count as "in use"
    busy = users_of(root, skip_pids)
    if busy:
        raise Fail("%s is in use by: %s" % (root, "; ".join(busy[:3])), IN_USE)


def owned_caches(root, cfg):
    out = []
    for a in all_cache_adapters(cfg):
        out += [p for p in a.owned_caches(root) if os.path.exists(p)]
    return out


def destroy(root, row, cfg, out, prune_branch=False):
    """Remove a checkout that passed check_removable: rescue stray commits, release adapter resources, move the tree
    and every cache it owns to the trash (deleted in the background), drop the records, prune git."""
    repos = repos_of(row, root)
    refs = rescue(root, stray_commits(root, repos))
    for r in refs:
        out("  kept detached commits as %s" % r)
    builds.retire_outputs(root, cfg)
    caches = owned_caches(root, cfg)
    for d in caches:
        trash_and_delete(d, os.path.join(os.path.dirname(d), TRASH))
    trash_and_delete(root, os.path.join(os.path.dirname(root), TRASH))
    for info in repos.values():
        with state.repo_lock(info["source"]):
            run("git", "-C", info["source"], "worktree", "prune", check=False)
    deleted = prune_branches(repos, out) if prune_branch else []
    return {"rescue_ref": refs[0] if refs else None, "rescue_refs": refs, "derived_data": len(caches),
            "caches": len(caches), "branches_deleted": deleted, "branch_deleted": bool(deleted and
            repos.get(".", {}).get("branch") in deleted)}


# ------------------------------------------------------------------ commands
def cmd_new(a):
    if len(a.pos) < 2:
        raise Fail("usage: xcfs new <repo> <branch> [dest] [--from REF] [--session ID]", 2)
    repo = config.toplevel(phys(a.pos[0]))
    branch = a.pos[1]
    dest = a.pos[2] if len(a.pos) > 2 else os.path.join(os.path.dirname(repo), "%s-wt-%s" % (os.path.basename(repo),
                                                                                              branch.replace("/", "-")))
    parent = phys(os.path.dirname(os.path.abspath(os.path.expanduser(dest))))
    dest = os.path.join(parent, os.path.basename(dest.rstrip("/")))
    cfg = config.load_config(repo)
    sets = config.repo_set(repo, cfg)
    config.require_trust(repo, cfg)
    if not os.path.isdir(parent):
        raise Fail("no such directory: %s" % parent)
    seed.check_volume(repo, parent, cfg)
    if not git_ok(repo, "rev-parse", "--verify", "--quiet", "HEAD"):
        raise Fail("%s has no commit yet" % repo)
    at = checked_out_at(repo, branch) if branch_exists(repo, branch) else None
    if at:
        raise Fail("branch %s is already checked out at %s" % (branch, at))
    with state.registry() as reg:
        if os.path.lexists(dest) or dest in reg["worktrees"] or dest in reg["slots"]:
            raise Fail("destination already exists: %s" % dest)
        reg["worktrees"][dest] = {"state": "creating", "repo": repo, "branch": branch, "pid": os.getpid(),
                                  "created": now_iso()}
    t0 = time.time()
    plan = [(".", repo, ("branch", branch, a.opt.get("from")))]
    for rel, src, spec in sets[1:]:
        plan.append((rel, src, ("branch", branch, None) if branch_exists(src, branch) and not checked_out_at(src, branch)
                     else ("detach", config.resolve_baseline(src, spec))))

    def progress(made):  # so a `new` that dies from here on can still be undone completely by `gc`
        with state.registry() as reg:
            if dest in reg["worktrees"]:
                reg["worktrees"][dest]["repos"] = made
                reg["worktrees"][dest]["made_branch"] = made.get(".", {}).get("made_branch", False)
    try:
        made = materialize(repo, cfg, dest, plan, progress)
        t_git = time.time() - t0
        rev = git(dest, "rev-parse", "HEAD")
        seeded, origin = seed.seed_checkout(repo, dest, cfg, rev)
        carried = seed.carry_files(repo, dest, cfg)
        seed.after_seed_commands(repo, dest, cfg)
        builds.after_seed(dest, repo, cfg, "worktree")
        session = a.session()
        with state.registry() as reg:
            reg["worktrees"][dest].update(state="ready", session=session, seeded=seeded, carried=carried, repos=made,
                                          made_branch=made["."]["made_branch"], seed_origin=origin)
            reg["worktrees"][dest].pop("pid", None)
    except BaseException:
        with state.registry(write=False) as reg:
            made = reg["worktrees"].get(dest, {}).get("repos", {})
        undo_materialize(dest, made)
        with state.registry() as reg:
            reg["worktrees"].pop(dest, None)
        raise
    a.result(worktree=dest, branch=branch, seeded=seeded, seed_origin=origin, repos=sorted(made),
             seconds=round(time.time() - t0, 2), git_seconds=round(t_git, 2))
    say("worktree ready: %s  (%.1fs)" % (dest, time.time() - t0))
    if not session:
        say("  no session id: `xcfs reap --session` will not pick this one up")


def resolve_worktree(path):
    """(worktree root, registry row or {}) for a path inside a worktree (never the main checkout)."""
    if not os.path.isdir(path):
        raise Fail("no such path: %s" % path)
    r = run("git", "-C", path, "rev-parse", "--git-common-dir", "--absolute-git-dir", check=False)
    if r.returncode != 0:
        raise Fail("not a git worktree: %s" % path)
    common, gitdir = [phys(os.path.join(path, x)) for x in r.stdout.split("\n")[:2]]
    top = phys(git(path, "rev-parse", "--show-toplevel"))
    with state.registry(write=False) as reg:
        if top in reg["worktrees"]:
            return top, reg["worktrees"][top]
        for root, row in reg["worktrees"].items():  # a nested repository's worktree belongs to the set around it
            if under(top, root):
                return root, row
        for s in reg["slots"].values():
            if under(top, s["src"]):
                raise Fail("%s is a pool slot: use `xcfs release`, not rm" % s["src"])
    if gitdir == common:
        raise Fail("%s is the main checkout, not a worktree: refusing" % top)
    return top, {}



def remove_worktree(path, apply, discard_ignored=False, quiet=False, prune_branch=False):
    out = (lambda *_: None) if quiet else say
    wt, row = resolve_worktree(path)
    if row.get("state") == "creating":
        raise Fail("%s is still being created" % wt)
    repo = row.get("repo") or config.main_checkout(wt)
    cfg = config.load_config(repo, validate=False)
    repos = repos_of(row, wt)
    branch = git(wt, "branch", "--show-current", check=False)
    out("worktree: %s  (branch %s)" % (wt, branch or "detached"))
    for d in owned_caches(wt, cfg):
        out("  + %s" % d)
    if stray_commits(wt, repos):
        out("  detached HEAD has commits on no branch; they will be kept as refs/xcfs/rescue/…")
    if not apply:
        code, why = unsaved_work(wt, repos, row.get("carried"))
        lost = seed.changed_carried(wt, row.get("carried"))
        if code:
            out("  would refuse: %s" % why)
        if lost:
            out("  would refuse: changed ignored file(s): %s" % ", ".join(lost))
        out("dry-run: nothing removed. re-run with --apply.")
        return {"worktree": wt, "removed": False, "blocked": why or (", ".join(lost) if lost else "")}
    check_removable(wt, row, discard_ignored,
                    release=lambda: builds.for_each_adapter(wt, repo, cfg, "worktree", "release"))
    with state.registry() as reg:          # the directory is going: the record goes too, whatever git says next
        reg["worktrees"].pop(wt, None)
    res = destroy(wt, row or {"repo": repo}, cfg, out, prune_branch)
    out("removed (deleting in the background).")
    return dict(res, worktree=wt, removed=True, blocked="", branch=branch)


def cmd_rm(a):
    if not a.pos:
        raise Fail("usage: xcfs rm <worktree> [--apply] [--discard-ignored] [--prune-branch]", 2)
    a.result(**remove_worktree(phys(a.pos[0]), a.flag("apply"), a.flag("discard-ignored"),
                               prune_branch=a.flag("prune-branch")))


def cmd_reap(a):
    from . import pool
    sid, older = a.session(), a.opt.get("older-than")
    if a.flag("all") or older is not None:
        sid = a.opt.get("session", "")           # with --all / --older-than the session is only an optional extra filter
    elif not sid:
        raise Fail("which worktrees? pass --session ID (or set XCFS_SESSION), --older-than DAYS, or --all", 2)
    cutoff = time.time() - float(older) * 86400 if older is not None else None
    from .util import iso_epoch
    with state.registry(write=False) as reg:
        mine = [p for p, r in reg["worktrees"].items() if r.get("state") == "ready"
                and (not sid or r.get("session") == sid) and (cutoff is None or iso_epoch(r.get("created")) <= cutoff)]
    done = []
    for wt in sorted(mine):
        if not os.path.isdir(wt):
            continue
        try:
            r = remove_worktree(wt, a.flag("apply"), quiet=True, prune_branch=a.flag("prune-branch"))
            say("  %s  %s" % ("REAP" if r["removed"] or not r["blocked"] else "SKIP", wt) +
                ("  (%s)" % r["blocked"] if r["blocked"] else ""))
            done.append(r)
        except Fail as e:
            say("  SKIP  %s  (%s)" % (wt, str(e)[:90]))
            done.append({"worktree": wt, "removed": False, "blocked": str(e)})
    slots = pool.release_matching(sid, cutoff, a.flag("apply"), a.flag("prune-branch"), a.flag("all"))
    if not mine and not slots:
        say("no matching worktrees" + (" from session %s" % sid[:8] if sid else ""))
    elif not a.flag("apply"):
        say("dry-run: nothing removed. re-run with --apply.")
    a.result(session=sid, worktrees=done, removed=sum(1 for d in done if d["removed"]),
             left=([d["worktree"] for d in done if not d["removed"]] + [s["slot"] for s in slots if not s["released"]])
             if a.flag("apply") else [],
             slots=slots, released=sum(1 for s in slots if s["released"]))


def cmd_adopt(a):
    if not a.pos:
        raise Fail("usage: xcfs adopt <worktree> [--session ID | --release]", 2)
    wt = phys(a.pos[0])
    r = run("git", "-C", wt, "rev-parse", "--git-common-dir", "--absolute-git-dir", "--show-toplevel", check=False)
    if r.returncode != 0:
        raise Fail("not a git worktree: %s" % wt)
    common, gitdir, top = [phys(os.path.join(wt, x)) for x in r.stdout.split("\n")[:3]]
    if gitdir == common:
        raise Fail("%s is the main checkout: xcfs only manages worktrees" % top)
    sid = "" if a.flag("release") else a.session()
    if not sid and not a.flag("release"):
        raise Fail("which session? pass --session ID (or set XCFS_SESSION), or --release to exempt it from reap", 2)
    with state.registry() as reg:
        if any(top == s["src"] for s in reg["slots"].values()):
            raise Fail("%s is a pool slot: slots are handed out with `xcfs checkout`" % top)
        repo = os.path.dirname(common) if os.path.basename(common) == ".git" else common
        branch = git(top, "branch", "--show-current")
        row = reg["worktrees"].setdefault(top, {
            "state": "ready", "repo": repo, "branch": branch, "seeded": [], "carried": {}, "made_branch": False,
            "created": now_iso(), "repos": {".": {"source": repo, "branch": branch or None, "made_branch": False}}})
        if row.get("state") != "ready":
            raise Fail("%s is still being created" % top)
        row["session"] = sid
    say("%s now belongs to %s" % (top, "session " + sid[:8] if sid else "no session (reap will leave it alone)"))
    a.result(worktree=top, session=sid)


def cmd_ls(a):
    from .util import du_kb, human, iso_epoch
    with state.registry(write=False) as reg:
        rows = sorted(reg["worktrees"].items())
    if "session" in a.opt:
        rows = [(p, r) for p, r in rows if r.get("session") == a.opt["session"]]
    out = []
    for p, r in rows:
        row = {"worktree": p, "branch": r.get("branch"), "state": r.get("state") if os.path.isdir(p) else "missing",
               "session": r.get("session", ""), "seeded": r.get("seeded", []),
               "age_days": round((time.time() - iso_epoch(r.get("created"))) / 86400, 1)}
        extra = ""
        if a.flag("status") and os.path.isdir(p):
            row["dirty"] = sum(len([l for l in git(path_of(p, rel), "status", "--porcelain", check=False).splitlines()
                                    if l.strip()]) for rel in repos_of(r, p) if os.path.isdir(path_of(p, rel)))
            n = git(p, "rev-list", "--count", "HEAD", "--not", "--remotes", check=False)
            row["unpushed"] = int(n) if n.isdigit() else 0
            row["branch"] = git(p, "branch", "--show-current", check=False) or "detached"
            extra = "  dirty=%d unpushed=%d age=%.1fd" % (row["dirty"], row["unpushed"], row["age_days"])
        if a.flag("sizes") and os.path.isdir(p):
            row["kb"] = du_kb(p)
        out.append(row)
        say("  %-8s %-9s %s  [%s]%s%s" % (row["state"], (row["session"] or "-")[:8], p, row["branch"], extra,
                                          "  " + human(row["kb"]) if "kb" in row else ""))
    if not rows:
        say("no worktrees created through xcfs.")
    a.result(worktrees=out)
