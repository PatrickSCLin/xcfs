"""Entry points for agent tools that create and remove worktrees themselves, so that their worktrees come from xcfs.

    xcfs hook worktree-create    reads {"name", "cwd", "session_id", ...} on stdin; prints the new worktree's path
    xcfs hook worktree-remove    reads {"worktree_path", ...} on stdin; exit 0 when it is gone
    xcfs hook session-end        reads {"session_id", ...} on stdin; returns what the session opened; always exit 0
    xcfs hook install | uninstall | status [repo] [--apply]
                                 put these three in Claude Code's settings (the user's, or a repository's), or take
                                 them out; nothing is written without --apply

This is the contract of Claude Code's WorktreeCreate and WorktreeRemove hooks (docs/agents.md shows the settings);
another tool can call the same commands. The worktree is a pool slot when the repository has a free one (already
built), otherwise a new xcfs worktree, on branch `worktree-<name>` from the remote's default branch (as the tool's own
default would be; XCFS_HOOK_BASE=head starts from the current HEAD instead). It is handed to the agent's session, so
`xcfs reap --session` cleans up after it. Removal goes through `xcfs release` / `xcfs rm`, which refuse when anything
would be lost; the tool then keeps the worktree. Messages go to stderr; stdout carries only the path.
"""
import json, os, re, shutil, subprocess, sys

from . import config, state
from .util import OUT, Fail, USAGE, phys, run, say, self_argv, warn, write_json


def log(msg):
    print("xcfs hook: " + msg, file=sys.stderr, flush=True)


SHELLS = {"sh", "bash", "zsh", "dash", "fish"}


def tool_pid():
    """The agent tool that ran this hook: the parent, above any shell the hook command was started through."""
    pid = os.getppid()
    for _ in range(4):
        r = run("ps", "-o", "ppid=,comm=", "-p", str(pid), check=False, cwd="/").stdout.split(None, 1)
        if len(r) < 2 or os.path.basename(r[1].strip()).lstrip("-") not in SHELLS:
            break
        pid = int(r[0])
    return pid


def call(*args, tool=False):
    """Run this xcfs with --json; (exit code, result). tool: on behalf of the agent tool that ran the hook, whose own
    processes do not keep a checkout in use (XCFS_TOOL_PID)."""
    env = dict(os.environ, XCFS_TOOL_PID=str(tool_pid())) if tool else None
    r = subprocess.run(self_argv() + list(args) + ["--json"], capture_output=True, text=True, stdin=subprocess.DEVNULL,
                       env=env, cwd="/")
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
        code, out = call("release", path, "--prune-branch", tool=True)
    elif managed:
        code, out = call("rm", path, "--apply", "--prune-branch", tool=True)
    else:   # not made by xcfs: what git would do, never forcing
        r = run("git", "-C", path, "worktree", "remove", path, check=False)
        code, out = r.returncode, {"error": r.stderr.strip()}
    if code != 0:
        raise Fail("not removed: %s" % out.get("error"), code)
    log("removed %s" % path)


def session_end(data):
    """The agent's session is over: remove its clean worktrees, return its clean slots, and keep what was left (with
    unsaved work) in last-reap.json in the state directory. Never fails: a session must be able to end."""
    sid = str(data.get("session_id") or "")
    if not sid:
        return
    code, out = call("reap", "--session", sid, "--apply", "--prune-branch", tool=True)
    try:
        write_json(os.path.join(state.state_dir(), "last-reap.json"), dict(out, session=sid))
    except OSError as e:
        log("could not write last-reap.json: %s" % e)
    log("session %s: %s removed, %s returned, %d left" % (sid[:8], out.get("removed", 0), out.get("released", 0),
                                                         len(out.get("left") or [])))


# ------------------------------------------------------------------ installing the hooks in the agent tool's settings
EVENTS = (("WorktreeCreate", "xcfs hook worktree-create"), ("WorktreeRemove", "xcfs hook worktree-remove"),
          ("SessionEnd", "xcfs hook session-end"))
# Claude Code gives SessionEnd hooks 1.5 s in all unless a hook sets a timeout of its own (then up to 60 s); returning
# a session's checkouts can take longer than 1.5 s.
TIMEOUTS = {"SessionEnd": 60}


def hook_entry(event, command):
    h = {"type": "command", "command": command}
    if event in TIMEOUTS:
        h["timeout"] = TIMEOUTS[event]
    return h
EXCLUSIVE = ("WorktreeCreate", "WorktreeRemove")   # the tool takes a worktree from one command only


def settings_file(repo):
    """Claude Code's settings: a repository's (.claude/settings.json), else the user's."""
    if repo:
        return os.path.join(config.toplevel(phys(repo)), ".claude", "settings.json")
    return os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"), "settings.json")


def ours(command):
    """A hook command that runs xcfs: `xcfs hook ...`, or the session-end line of earlier versions (`xcfs reap`)."""
    return bool(re.search(r"(^|[\s/;|&{(])xcfs (hook|reap) ", str(command)))


def load_settings(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
    except ValueError as e:
        raise Fail("%s is not valid JSON (%s); nothing changed" % (path, e))
    if not isinstance(data, dict) or not isinstance(data.get("hooks", {}), dict):
        raise Fail("%s does not look like a settings file; nothing changed" % path)
    return data


def without_ours(groups):
    """The hook groups of one event without xcfs's commands; groups left empty are dropped."""
    out = []
    for g in groups:
        if not isinstance(g, dict):
            out.append(g)
            continue
        kept = [h for h in g.get("hooks") or [] if not (isinstance(h, dict) and ours(h.get("command")))]
        if kept:
            out.append(dict(g, hooks=kept))
    return out


def commands(groups):
    return [h.get("command") for g in groups if isinstance(g, dict) for h in g.get("hooks") or [] if isinstance(h, dict)]


def planned(data, install):
    """(the settings as they would be, the changes as text). Hooks that are not xcfs's are never touched."""
    hooks = dict(data.get("hooks") or {})
    changes = []
    for event, command in EVENTS:
        before = hooks.get(event) or []
        if not isinstance(before, list):
            raise Fail("hooks.%s is not a list; nothing changed" % event)
        others = without_ours(before)
        mine = [c for c in commands(before) if ours(c)]
        mine_hooks = [h for g in before if isinstance(g, dict) for h in g.get("hooks") or []
                      if isinstance(h, dict) and ours(h.get("command"))]
        if install:
            if event in EXCLUSIVE and commands(others):
                raise Fail("another %s hook is installed (%s): the agent tool takes a worktree from one command only; "
                           "remove it first" % (event, commands(others)[0]))
            if mine_hooks == [hook_entry(event, command)]:
                continue
            hooks[event] = others + [{"hooks": [hook_entry(event, command)]}]
            changes.append("%s %s: %s" % ("replace" if mine else "add", event, command))
        elif mine:
            if others:
                hooks[event] = others
            else:
                hooks.pop(event)
            changes.append("remove %s: %s" % (event, "; ".join(mine)))
    out = dict(data)
    if hooks:
        out["hooks"] = hooks
    else:
        out.pop("hooks", None)
    return out, changes


def save_settings(path, data):
    real = os.path.realpath(path)   # a settings file that is a link (managed elsewhere) stays a link
    os.makedirs(os.path.dirname(real), exist_ok=True)
    tmp = "%s.%d.tmp" % (real, os.getpid())
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, real)


def cmd_settings(a, sub):
    path = settings_file(a.pos[1] if len(a.pos) > 1 else None)
    data = load_settings(path)
    present = {event: command in commands((data.get("hooks") or {}).get(event) or []) for event, command in EVENTS}
    if sub == "status":
        say(path)
        for event, command in EVENTS:
            say("  %-15s %s" % (event, command if present[event] else "not installed"))
        a.result(settings=path, installed=all(present.values()), events=present)
        return
    new, changes = planned(data, sub == "install")
    say(path)
    for c in changes or ["nothing to change"]:
        say("  " + c)
    if sub == "install" and not shutil.which("xcfs"):
        warn("xcfs is not on PATH: the hooks call `xcfs`, so put it there")
    applied = bool(changes) and a.flag("apply")
    if applied:
        save_settings(path, new)
        say("written" + ("; the hooks take effect in sessions started from now on" if sub == "install" else ""))
    elif changes:
        say("dry-run: nothing written. re-run with --apply.")
    a.result(settings=path, changes=changes, applied=applied)


def cmd_hook(a):
    sub = a.pos[0] if a.pos else ""
    if sub in ("install", "uninstall", "status"):
        return cmd_settings(a, sub)
    OUT.quiet = True   # stdout carries the path and nothing else
    if sub == "worktree-create":
        path = create(read_input())
        print(path, flush=True)
    elif sub == "worktree-remove":
        remove(read_input())
    elif sub == "session-end":
        try:
            session_end(read_input())
        except Exception as e:   # the session ends whatever happened here
            log("session-end: %s" % e)
    else:
        raise Fail("usage: xcfs hook worktree-create|worktree-remove|session-end  (JSON on stdin), "
                   "or xcfs hook install|uninstall|status [repo] [--apply]", USAGE)
