"""Where xcfs keeps its state, and the locks around it."""
import contextlib, fcntl, os, time

from .util import Fail, USAGE, phys, read_json, short_hash, write_json

HOME = os.path.expanduser("~")


def state_dir():
    return os.environ.get("XCFS_STATE") or os.path.join(os.environ.get("XDG_STATE_HOME") or HOME + "/.local/state", "xcfs")


def config_home():
    return os.path.join(os.environ.get("XDG_CONFIG_HOME") or HOME + "/.config", "xcfs")


def cache_dir():
    return os.environ.get("XCFS_CACHE") or HOME + "/Library/Caches/xcfs"


DEFAULT_SETTINGS = {
    "poolRoot": "~/Library/Developer/xcfs/pools",  # one directory per project under it; must be on the repo's volume
    "slots": 2,               # default slots per project; each costs one full build directory (several GB)
    "slotDiskGB": 8,          # free space required before a new slot is created or warmed
    "leaseHours": 24,         # a leased slot idle this long is listed as stale; `gc --apply` returns it if clean
    "warmOnRelease": True,    # a released slot is brought back to its baseline and rebuilt in the background
    "buildTimeout": 3600,     # seconds; a build that runs longer is killed (exit 9)
    "warmJobs": 0,            # slots one warm-up builds at the same time; 0: one per 16 GB of memory
}


def settings():
    """Machine-wide settings: $XDG_CONFIG_HOME/xcfs/settings.json over the defaults above."""
    path = os.path.join(config_home(), "settings.json")
    s = dict(DEFAULT_SETTINGS)
    data = read_json(path, {})
    if not isinstance(data, dict):
        raise Fail("%s is not a JSON object" % path)
    unknown = set(data) - set(DEFAULT_SETTINGS)
    if unknown:
        raise Fail("%s: unknown setting(s): %s" % (path, ", ".join(sorted(unknown))), USAGE)
    s.update(data)
    if os.environ.get("XCFS_POOL_ROOT"):
        s["poolRoot"] = os.environ["XCFS_POOL_ROOT"]
    s["poolRoot"] = phys(s["poolRoot"])
    return s


def _empty_registry():
    return {"version": 2, "worktrees": {}, "slots": {}, "pools": {}, "builds": {}}


@contextlib.contextmanager
def registry(write=True):
    """All state changes happen under one file lock; the file is replaced atomically. Hold it briefly: never across a
    build or a git operation on a large tree."""
    st = state_dir()
    os.makedirs(st, exist_ok=True)
    with open(os.path.join(st, "registry.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = os.path.join(st, "registry.json")
        reg = read_json(path, None) or _empty_registry()
        for k, v in _empty_registry().items():  # a v0.1 registry has only "worktrees"
            reg.setdefault(k, v if not isinstance(v, dict) else {})
        reg["version"] = 2
        yield reg
        if write:
            write_json(path, reg)


def lock_path(kind, key):
    d = os.path.join(state_dir(), "locks")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "%s-%s.lock" % (kind, short_hash(key, 16)))


@contextlib.contextmanager
def named_lock(kind, key, wait=True):
    """An exclusive lock per (kind, key). With wait=False, yields False instead of blocking when it is taken."""
    with open(lock_path(kind, key), "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        f.write(str(os.getpid()))
        f.flush()
        yield True


def lock_busy(kind, key):
    """Is somebody holding this lock right now?"""
    p = lock_path(kind, key)
    with open(p, "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(f, fcntl.LOCK_UN)
        return False


@contextlib.contextmanager
def repo_lock(repo):
    """git's worktree bookkeeping is not safe against itself: two `worktree add` (or `prune`) in one repository at the
    same moment fail now and then ("failed to read .git/worktrees/…/commondir", "Invalid path '.git/worktrees'";
    measured 3 failures in 14 runs of 8 parallel calls). Every such call goes through this per-repository lock."""
    with named_lock("repo", repo):
        yield


def trust_file():
    return os.path.join(state_dir(), "trust.json")


def logs_dir():
    d = os.path.join(state_dir(), "logs")
    os.makedirs(d, exist_ok=True)
    return d


def log_file(name):
    return os.path.join(logs_dir(), "%s-%s.log" % (time.strftime("%Y%m%d-%H%M%S"), name))


def save_trust(key, info):
    with registry(write=False):
        t = read_json(trust_file(), {})
        t[key] = info
        write_json(trust_file(), t)


def trusted(key):
    return key in (read_json(trust_file(), {}) or {})
