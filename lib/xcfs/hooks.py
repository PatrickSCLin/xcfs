"""Entry points for agent tools that create and remove worktrees themselves, so that their worktrees come from xcfs.

    xcfs hook worktree-create    reads {"name", "cwd", "session_id", ...} on stdin; prints the new worktree's path
    xcfs hook worktree-remove    reads {"worktree_path", ...} on stdin; exit 0 when it is gone

This is the contract of Claude Code's WorktreeCreate and WorktreeRemove hooks (docs/agents.md shows the settings);
another tool can call the same commands. The worktree is a pool slot when the repository has a free one (already
built), otherwise a new xcfs worktree, on branch `worktree-<name>` from the remote's default branch (as the tool's own
default would be; XCFS_HOOK_BASE=head starts from the current HEAD instead). It is handed to the agent's session, so
`xcfs reap --session` cleans up after it. Removal goes through `xcfs release` / `xcfs rm`, which refuse when anything
would be lost; the tool then keeps the worktree. Messages go to stderr; stdout carries only the path.
"""
import json, os, subprocess, sys

from . import config, state
from .util import OUT, Fail, USAGE, phys, run, self_argv


def log(msg):
    print("xcfs hook: " + msg, file=sys.stderr, flush=True)


def call(*args):
    """Run this xcfs with --json; (exit code, result)."""
    r = subprocess.run(self_argv() + list(args) + ["--json"], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    try:
        out = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        out = {"error": (r.stderr or r.stdout).strip()[-500:]}
    return r.returncode, out


def read_input():
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError as e:
        raise Fail("the hook input is not JSON: %s" % e, USAGE)
    if not isinstance(data, dict):
        raise Fail("the hook input is not a JSON object", USAGE)
    return data


def base_ref(repo):
    """The remote's default branch (origin/HEAD), unless XCFS_HOOK_BASE=head or there is none."""
    if os.environ.get("XCFS_HOOK_BASE", "fresh") == "head":
        return None
    r = run("git", "-C", repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD", check=False)
    ref = r.stdout.strip()
    return ref if r.returncode == 0 and ref else None


def has_free_slot(repo):
    from . import pool
    rows = pool.status(repo).get("slots", [])
    return any(r.get("state") == "idle" and r.get("warm") and r.get("at_baseline") for r in rows)


def create(data):
    name = str(data.get("name") or "").strip()
    if not name or "/" in name or name.startswith("."):
        raise Fail("the hook input has no usable worktree name: %r" % name, USAGE)
    cwd = data.get("cwd") or os.getcwd()
    try:
        repo = config.main_checkout(config.toplevel(phys(cwd)))
    except Fail:
        raise Fail("%s is not in a git repository" % cwd, USAGE)
    branch = "worktree-" + name
    session = data.get("session_id") or os.environ.get("XCFS_SESSION") or os.environ.get("CLAUDE_CODE_SESSION_ID") or ""
    common = (["--session", session] if session else []) + (["--from", base_ref(repo)] if base_ref(repo) else [])
    if has_free_slot(repo):
        code, out = call("checkout", repo, branch, *common)
        if code == 0:
            log("leased a built slot: %s" % out["worktree"])
            return out["worktree"]
        log("no slot (%s); making a new worktree" % (out.get("error") or code))
    code, out = call("new", repo, branch, *common)
    if code != 0:
        raise Fail("xcfs new failed: %s" % out.get("error"), code)
    log("new worktree: %s" % out["worktree"])
    return out["worktree"]


def remove(data):
    path = data.get("worktree_path")
    if not path:
        raise Fail("the hook input has no worktree_path", USAGE)
    path = phys(path)
    if not os.path.exists(path):
        return
    with state.registry(write=False) as reg:
        slot = any(path == s.get("src") or path.startswith(p + os.sep) or path == p for p, s in reg["slots"].items())
        managed = path in reg["worktrees"]
    if slot:
        code, out = call("release", path, "--prune-branch")
    elif managed:
        code, out = call("rm", path, "--apply", "--prune-branch")
    else:   # not made by xcfs: what git would do, never forcing
        r = run("git", "-C", path, "worktree", "remove", path, check=False)
        code, out = r.returncode, {"error": r.stderr.strip()}
    if code != 0:
        raise Fail("not removed: %s" % out.get("error"), code)
    log("removed %s" % path)


def cmd_hook(a):
    OUT.quiet = True   # stdout carries the path and nothing else
    sub = a.pos[0] if a.pos else ""
    if sub == "worktree-create":
        path = create(read_input())
        print(path, flush=True)
    elif sub == "worktree-remove":
        remove(read_input())
    else:
        raise Fail("usage: xcfs hook worktree-create|worktree-remove  (JSON on stdin)", USAGE)
