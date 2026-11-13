"""The dependency layer: clone it (APFS clonefile) into a new checkout, from a snapshot keyed by the lock files when the
config asks for one, and copy the carried ignored files."""
import json, os, re, shutil, time

from . import state
from .config import shell
from .util import Fail, VOLUME, git_ok, now_iso, phys, read_json, run, same_apfs_volume, say, sha256_bytes, \
    sha256_file, short_hash, warn, write_json


def rewrite_paths(root, old, new):
    """SwiftPM's workspace-state.json records absolute paths. Rewrite them as JSON string values, matching `old`
    only as a whole path prefix (never as a regex, never inside a longer sibling name)."""
    pat = re.compile(re.escape(old) + r"(?=/|$)")
    n = 0

    def walk(v):
        nonlocal n
        if isinstance(v, str):
            v2, k = pat.subn(lambda m: new, v)
            n += k
            return v2
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        return v
    for d in (root, os.path.join(root, ".build")):
        f = os.path.join(d, "workspace-state.json")
        if os.path.isfile(f):
            try:
                with open(f) as fh:
                    data = json.load(fh)
            except ValueError:
                continue
            data = walk(data)
            tmp = f + ".xcfs.tmp"
            with open(tmp, "w") as fh:
                json.dump(data, fh, indent=2)
            os.replace(tmp, f)
    return n


def present_seeds(repo, cfg):
    return [s for s in cfg.get("seed", []) if os.path.exists(os.path.join(repo, s))]


def check_volume(repo, dest_parent, cfg):
    """clonefile only exists inside one APFS volume; `cp -c` silently falls back to a full copy elsewhere."""
    if present_seeds(repo, cfg) and not same_apfs_volume(repo, dest_parent):
        raise Fail("%s and %s are not on one APFS volume: the dependency layer would be copied in full, not cloned"
                   % (repo, dest_parent), VOLUME)


def lock_key(repo, cfg, rev=None):
    """Hash of the config's seedKey files, at rev (from git) or in the working tree (rev None). None if no seedKey."""
    files = cfg.get("seedKey") or []
    if not files:
        return None
    parts = []
    for f in files:
        if rev:
            r = run("git", "-C", repo, "show", "%s:%s" % (rev, f), check=False)
            content = r.stdout if r.returncode == 0 else "\0missing"
        else:
            p = os.path.join(repo, f)
            content = open(p, errors="replace").read() if os.path.isfile(p) else "\0missing"
        parts.append(f + "\n" + content)
    return sha256_bytes("\n\0\n".join(parts).encode())[:24]


def snapshot_dir(repo, key):
    return os.path.join(state.cache_dir(), "deps", "%s-%s" % (os.path.basename(repo), short_hash(repo)), key)


def clone(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    run("cp", "-c", "-R", src, dst)


def seed_checkout(repo, dest, cfg, rev):
    """Clone the seed directories into dest. Returns (seeded, origin) where origin says where they came from."""
    seeds = present_seeds(repo, cfg)
    key_rev = lock_key(repo, cfg, rev) if seeds else None
    origin, src_root, src_path_in_files = "source", repo, repo
    if key_rev:
        snap = snapshot_dir(repo, key_rev)
        meta = read_json(os.path.join(snap, ".xcfs-snapshot.json"))
        if meta:
            origin, src_root, src_path_in_files = "snapshot " + key_rev, snap, meta["origin"]
            meta["last_used"] = now_iso()
            write_json(os.path.join(snap, ".xcfs-snapshot.json"), meta)
        elif lock_key(repo, cfg) == key_rev:
            save_snapshot(repo, cfg, seeds, key_rev)
        else:
            warn("the dependency layer in %s does not match the lock files of %s (%s) and no snapshot exists for "
                 "them: it was cloned as it is; resolve dependencies in the new checkout"
                 % (repo, rev[:10] if rev else "HEAD", ", ".join(cfg["seedKey"])))
            origin = "source (lock files differ)"
    seeded = []
    for rel in seeds:
        dst = os.path.join(dest, rel)
        if os.path.lexists(dst):
            continue
        src = os.path.join(src_root, rel)
        if not os.path.exists(src):
            src = os.path.join(repo, rel)
        clone(src, dst)
        n = rewrite_paths(dst, src_path_in_files, dest)
        seeded.append(rel)
        say("  seeded %s (clonefile from %s)%s" % (rel, origin, ", rewrote %d path(s)" % n if n else ""))
        if not git_ok(dest, "check-ignore", "-q", rel):
            # Not ignored = the checkout is dirty from birth and `rm` / `release` would refuse it forever.
            warn("%s is not git-ignored: the checkout will always look dirty" % rel)
    return seeded, origin


def save_snapshot(repo, cfg, seeds, key):
    """Keep a clone of the dependency layer as it is for these lock files. Clones share blocks, so this costs only
    metadata; it lets later checkouts of the same lock files seed correctly even after the source checkout moved on."""
    snap = snapshot_dir(repo, key)
    if os.path.exists(snap) or not same_apfs_volume(repo, state.cache_dir()):
        return
    tmp = "%s.tmp.%d" % (snap, os.getpid())
    try:
        os.makedirs(tmp)
        for rel in seeds:
            clone(os.path.join(repo, rel), os.path.join(tmp, rel))
        write_json(os.path.join(tmp, ".xcfs-snapshot.json"),
                   {"origin": repo, "key": key, "files": cfg["seedKey"], "created": now_iso(), "last_used": now_iso()})
        os.rename(tmp, snap)
        say("  saved a dependency snapshot for these lock files (%s)" % key)
    except OSError:
        pass
    finally:
        if os.path.exists(tmp):
            shutil.rmtree(tmp, ignore_errors=True)


def carry_files(repo, dest, cfg):
    carried = {}
    for rel in cfg.get("carry", []):
        src = os.path.join(repo, rel)
        if os.path.isfile(src) and not os.path.lexists(os.path.join(dest, rel)):
            os.makedirs(os.path.dirname(os.path.join(dest, rel)), exist_ok=True)
            run("cp", "-c", src, os.path.join(dest, rel))
            carried[rel] = sha256_file(src)
    return carried


def changed_carried(root, carried):
    """Carried ignored files whose content changed: `git status` cannot see them, and they exist nowhere else."""
    out = []
    for rel, sha in (carried or {}).items():
        f = os.path.join(root, rel)
        if os.path.isfile(f) and sha256_file(f) != sha:
            out.append(rel)
    return out


def after_seed_commands(repo, dest, cfg):
    for c in cfg.get("afterSeed", []):
        r = shell(c, dest, {"XCFS_WORKTREE": dest, "XCFS_SOURCE": repo})
        if r.returncode != 0:
            raise Fail("afterSeed command failed (%d): %s\n%s" % (r.returncode, c, (r.stderr or r.stdout).strip()[:300]))
        say("  ran: %s" % c)


def snapshots(max_age_days=None):
    """[(dir, meta)] of every dependency snapshot; with max_age_days only those unused for longer."""
    root = os.path.join(state.cache_dir(), "deps")
    out = []
    if not os.path.isdir(root):
        return out
    from .util import iso_epoch
    for proj in os.listdir(root):
        for key in os.listdir(os.path.join(root, proj)):
            d = os.path.join(root, proj, key)
            meta = read_json(os.path.join(d, ".xcfs-snapshot.json"))
            if meta is None:
                continue
            if max_age_days is None or time.time() - iso_epoch(meta.get("last_used")) > max_age_days * 86400:
                out.append((d, meta))
    return out
