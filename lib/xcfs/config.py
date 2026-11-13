"""The project config (`.xcfs.json`): reading, validation, approval of the commands it carries, the repository set."""
import hashlib, json, os, subprocess

from . import state
from .util import Fail, USAGE, UNTRUSTED, git, git_ok, now_iso, phys, run

CORE_KEYS = {
    "version": int,
    "seed": list,        # directories cloned (APFS clonefile) into a new worktree or slot
    "carry": list,       # ignored files copied into a new worktree or slot
    "afterSeed": list,   # commands run in a new worktree or slot (needs `xcfs trust`)
    "seedKey": list,     # lock files whose content keys dependency-layer snapshots
    "repos": list,       # the repository set: [{"path": ".", "baseline": {...}}, {"path": "sub/repo", ...}]
    "toolchains": list,  # [{"adapter": "<name>", "root": ".", ...adapter keys}]
    "prepare": str,      # command run after a switch, before a build (needs `xcfs trust`)
    "pool": dict,        # {"slots": N}
}


def config_paths(repo):
    return [os.path.join(repo, ".xcfs.json"), os.path.join(state.config_home(), os.path.basename(repo) + ".json")]


def load_config(repo, validate=True):
    for p in config_paths(repo):
        if os.path.isfile(p):
            try:
                with open(p) as f:
                    cfg = json.load(f)
            except ValueError as e:
                raise Fail("config %s is not valid JSON: %s" % (p, e))
            if not isinstance(cfg, dict):
                raise Fail("config %s is not a JSON object" % p)
            if cfg.get("version", 1) != 1:
                raise Fail("config %s has version %s; this xcfs reads version 1" % (p, cfg.get("version")))
            cfg["_path"] = p
            if validate:
                problems = validate_config(cfg)
                if problems:
                    raise Fail("config %s: %s" % (p, "; ".join(problems)), USAGE)
            return cfg
    return {"_path": None}


def validate_config(cfg):
    from .adapter import adapter_keys, get_adapter
    problems = []
    claimed = adapter_keys()
    for k, v in cfg.items():
        if k.startswith("_"):
            continue
        if k in claimed:
            continue
        if k not in CORE_KEYS:
            problems.append("unknown key %r" % k)
        elif not isinstance(v, CORE_KEYS[k]):
            problems.append("%r must be a %s" % (k, CORE_KEYS[k].__name__))
    for k in ("seed", "carry", "afterSeed", "seedKey"):
        if isinstance(cfg.get(k), list) and not all(isinstance(x, str) and x for x in cfg[k]):
            problems.append("%r must be a list of non-empty strings" % k)
        if k != "afterSeed" and isinstance(cfg.get(k), list):
            for x in cfg[k]:
                if isinstance(x, str) and (os.path.isabs(x) or ".." in x.split("/")):
                    problems.append("%r entries must be relative paths inside the repository: %r" % (k, x))
    for i, r in enumerate(cfg.get("repos") or []):
        if not isinstance(r, dict) or not isinstance(r.get("path"), str):
            problems.append("repos[%d] needs a \"path\"" % i)
            continue
        if os.path.isabs(r["path"]) or ".." in r["path"].split("/"):
            problems.append("repos[%d].path must be relative and inside the repository" % i)
        b = r.get("baseline")
        if b is not None and (not isinstance(b, dict) or len(b) != 1 or list(b)[0] not in ("branch", "ref", "command")):
            problems.append("repos[%d].baseline must be one of {\"branch\": …}, {\"ref\": …}, {\"command\": …}" % i)
    for i, t in enumerate(cfg.get("toolchains") or []):
        if not isinstance(t, dict) or not isinstance(t.get("adapter"), str):
            problems.append("toolchains[%d] needs an \"adapter\"" % i)
            continue
        try:
            a = get_adapter(t["adapter"])
        except Fail as e:
            problems.append("toolchains[%d]: %s" % (i, e))
            continue
        problems += ["toolchains[%d]: %s" % (i, p) for p in a.validate(t)]
    pool = cfg.get("pool")
    if isinstance(pool, dict) and "slots" in pool and not (isinstance(pool["slots"], int) and pool["slots"] >= 0):
        problems.append("pool.slots must be a non-negative integer")
    return problems


# ------------------------------------------------------------------ commands from the repository need approval
def commands_of(cfg):
    """Every command the config would make xcfs run. They come from the repository, so cloning an untrusted repository
    must not mean running them."""
    cmds = [("afterSeed", c) for c in cfg.get("afterSeed", [])]
    if cfg.get("prepare"):
        cmds.append(("prepare", cfg["prepare"]))
    for r in cfg.get("repos") or []:
        if isinstance(r.get("baseline"), dict) and "command" in r["baseline"]:
            cmds.append(("baseline of %s" % r["path"], r["baseline"]["command"]))
    for t in cfg.get("toolchains") or []:
        for k in ("prepare",):
            if t.get(k):
                cmds.append(("%s %s" % (t["adapter"], k), t[k]))
    return cmds


def trust_key(repo, cfg):
    cmds = commands_of(cfg)
    if all(k == "afterSeed" for k, _ in cmds):  # the v0.1 formula, so approvals given then stay valid
        payload = json.dumps(cfg.get("afterSeed", []))
    else:
        payload = json.dumps(cmds, sort_keys=True)
    return hashlib.sha256((repo + "\n" + payload).encode()).hexdigest()


def require_trust(repo, cfg):
    cmds = commands_of(cfg)
    if cmds and not state.trusted(trust_key(repo, cfg)):
        raise Fail("%s wants to run %d command(s); review them and run `xcfs trust %s`" % (cfg["_path"], len(cmds), repo),
                   UNTRUSTED)


def trust(repo, cfg):
    state.save_trust(trust_key(repo, cfg), {"repo": repo, "at": now_iso()})


def shell(cmd, cwd, env_extra=None, timeout=None):
    env = dict(os.environ, **(env_extra or {}))
    try:
        return subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise Fail("command timed out after %ss: %s" % (timeout, cmd))


# ------------------------------------------------------------------ the repository set and its baselines
def toplevel(path):
    if not git_ok(path, "rev-parse", "--git-dir"):
        raise Fail("not a git repo: %s" % path)
    return phys(git(path, "rev-parse", "--show-toplevel"))


def main_checkout(path):
    """The main checkout of the repository that path (a worktree or the checkout itself) belongs to."""
    r = run("git", "-C", path, "rev-parse", "--git-common-dir", check=False)
    if r.returncode != 0:
        raise Fail("not a git repo: %s" % path)
    common = phys(os.path.join(path, r.stdout.strip()))
    return os.path.dirname(common) if os.path.basename(common) == ".git" else common


def repo_set(repo, cfg):
    """[(rel, source checkout, baseline spec)], the primary repository first, then nested ones by depth."""
    entries = cfg.get("repos") or []
    primary = next((e for e in entries if e.get("path", "").strip("/") in ("", ".")), {})
    out = [(".", repo, primary.get("baseline"))]
    for e in sorted((e for e in entries if e.get("path", "").strip("/") not in ("", ".")),
                    key=lambda e: e["path"].count("/")):
        rel = e["path"].strip("/")
        src = phys(os.path.join(repo, rel))
        if not os.path.isdir(src) or not git_ok(src, "rev-parse", "--git-dir") or toplevel(src) != src:
            raise Fail("repos: %s is not the top of a git repository" % src)
        if git(repo, "ls-files", "--stage", "--", rel, check=False).startswith("160000"):
            raise Fail("repos: %s is a git submodule of the primary repository; submodules are not supported" % rel)
        if not git_ok(repo, "check-ignore", "-q", rel):
            raise Fail("repos: %s must be git-ignored by the primary repository, or every worktree looks dirty" % rel)
        out.append((rel, src, e.get("baseline")))
    return out


def default_branch(src):
    r = run("git", "-C", src, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD", check=False)
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip().rsplit("/", 1)[-1]
    for b in ("main", "master", "trunk", "develop"):
        if git_ok(src, "show-ref", "--verify", "--quiet", "refs/heads/" + b):
            return b
    return git(src, "branch", "--show-current", check=False) or "HEAD"


def resolve_baseline(src, spec):
    """The commit a slot's repository should sit on when idle."""
    if not spec:
        spec = {"branch": default_branch(src)}
    if "branch" in spec:
        b = spec["branch"]
        for ref in ("refs/remotes/origin/" + b, "refs/heads/" + b):
            if git_ok(src, "show-ref", "--verify", "--quiet", ref):
                return git(src, "rev-parse", ref)
        if b == "HEAD":
            return git(src, "rev-parse", "HEAD")
        raise Fail("baseline branch %r does not exist in %s" % (b, src))
    if "ref" in spec:
        r = run("git", "-C", src, "rev-parse", "--verify", "--quiet", spec["ref"] + "^{commit}", check=False)
        if r.returncode != 0:
            raise Fail("baseline ref %r does not exist in %s" % (spec["ref"], src))
        return r.stdout.strip()
    r = shell(spec["command"], src, timeout=300)
    ref = r.stdout.strip().splitlines()[-1].strip() if r.returncode == 0 and r.stdout.strip() else ""
    if not ref:
        raise Fail("baseline command failed in %s: %s" % (src, (r.stderr or r.stdout).strip()[:300]))
    return resolve_baseline(src, {"ref": ref})
