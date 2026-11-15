"""Helpers shared by every module: errors, output, processes, paths. Nothing here knows a toolchain or a project."""
import calendar, ctypes, hashlib, json, os, re, shutil, signal, subprocess, sys, time, threading

# Exit codes. They are part of the interface: agents branch on them.
OK, ERROR, USAGE, UNTRUSTED, VOLUME, DIRTY, IN_USE, IGNORED_CHANGED, BUILD_FAILED, TIMEOUT, POOL_FULL, NO_SPACE = range(12)

# What to do after each refusal: in every --json result as "next" (unless something more specific said so), and on
# stderr for people. Written for agents as much as for people.
NEXT = {
    UNTRUSTED: "review the commands the config runs, then approve them with `xcfs trust <repo>`",
    VOLUME: "put the destination (or poolRoot) on the repository's APFS volume",
    DIRTY: "commit the work (or ask whoever owns it), then run the command again; xcfs never stashes or discards it",
    IN_USE: "stop what runs inside the checkout (a build, a shell, an editor), then run the command again",
    IGNORED_CHANGED: "the carried files changed; copy what is needed out first, then pass --discard-ignored",
    BUILD_FAILED: "fix what `diagnostics` lists (for tests, `failed`), then build again",
    TIMEOUT: "run it once more; if it times out again, raise buildTimeout or --timeout, or report it",
    POOL_FULL: "retry with --wait 600, release a slot (`xcfs release`), grow the pool (`xcfs pool resize`), or use "
               "`xcfs new` for a plain worktree",
    NO_SPACE: "`xcfs gc --apply` removes what xcfs no longer needs; `xcfs pool resize <repo> N` with a smaller N "
              "retires idle slots; do not delete other files to make room without asking",
}


class Fail(Exception):
    def __init__(self, msg, code=ERROR, **extra):
        super().__init__(msg)
        self.code = code
        self.extra = extra


class Output:
    """Human text unless --json; with --json exactly one JSON object is printed at the end (see cli.main)."""
    def __init__(self):
        self.json = False
        self.result = {}
        self.quiet = False


OUT = Output()


def say(msg=""):
    if not OUT.json and not OUT.quiet:
        print(msg, flush=True)


def warn(msg):
    OUT.result.setdefault("warnings", []).append(msg)
    say("  warning: " + msg)


def phys(p):
    """Physical path. Toolchains and git record physical paths; a logical one (through a symlink) never matches them."""
    return os.path.realpath(os.path.expanduser(p))


def under(path, root):
    """path is root or inside it, on a path boundary: /a/wt-foo does not own /a/wt-foobar."""
    return path == root or path.startswith(root.rstrip("/") + "/")


def run(*cmd, cwd=None, check=True, env=None, timeout=None, input=None):
    try:
        r = subprocess.run(list(cmd), cwd=cwd, capture_output=True, text=True, env=env, timeout=timeout, input=input)
    except FileNotFoundError:
        raise Fail("%s: command not found" % cmd[0])
    if check and r.returncode != 0:
        raise Fail("%s failed: %s" % (" ".join(cmd[:4]), (r.stderr or r.stdout).strip()[:400]))
    return r


def git(repo, *args, check=True):
    return run("git", "-C", repo, *args, check=check).stdout.strip()


def git_ok(repo, *args):
    return run("git", "-C", repo, *args, check=False).returncode == 0


def human(kb):
    return "%.1fG" % (kb / 1048576) if kb >= 1048576 else "%.0fM" % (kb / 1024) if kb >= 1024 else "%dK" % kb


def du_kb(path):
    r = run("du", "-sk", path, check=False).stdout.split()
    return int(r[0]) if r and r[0].isdigit() else 0


def free_bytes(path):
    while path and not os.path.exists(path):
        path = os.path.dirname(path)
    st = os.statvfs(path or "/")
    return st.f_bavail * st.f_frsize


def now_iso(t=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if t is None else t))


def iso_epoch(s):
    try:
        return calendar.timegm(time.strptime(s or "", "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return 0


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def sha256_file(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def short_hash(s, n=8):
    return hashlib.sha256(s.encode()).hexdigest()[:n]


def read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return default


def write_json(path, data):
    """Atomic: write a temporary file next to it, then rename."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())   # threads of one process write too
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def fs_type(path):
    """Filesystem type name of the volume holding path ("apfs", "hfs", …), from statfs(2)."""
    libc = ctypes.CDLL(None, use_errno=True)
    try:  # on x86_64 the plain symbol is the legacy 32-bit-inode struct with another layout; arm64 has only one
        statfs = libc["statfs$INODE64"]
    except AttributeError:
        statfs = libc.statfs
    buf = ctypes.create_string_buffer(4096)
    while path and not os.path.exists(path):
        path = os.path.dirname(path)
    if statfs(os.fsencode(path or "/"), buf) != 0:
        return ""
    return buf.raw[72:88].split(b"\0")[0].decode()   # struct statfs.f_fstypename (64-bit-inode layout)


def same_apfs_volume(a, b):
    def existing(p):
        while p and not os.path.exists(p):
            p = os.path.dirname(p)
        return p or "/"
    a, b = existing(a), existing(b)
    return os.stat(a).st_dev == os.stat(b).st_dev and fs_type(a) == "apfs"


def ancestors():
    out, pid = set(), os.getpid()
    table = dict(l.split() for l in run("ps", "-axo", "pid=,ppid=", cwd="/").stdout.splitlines() if len(l.split()) == 2)
    while pid and str(pid) in table and str(pid) not in out:
        out.add(str(pid))
        pid = int(table[str(pid)])
    return out


def process_tree(pid):
    """pid and every process below it."""
    kids = {}
    for l in run("ps", "-axo", "pid=,ppid=", cwd="/").stdout.splitlines():
        p = l.split()
        if len(p) == 2:
            kids.setdefault(p[1], []).append(p[0])
    out, todo = set(), [str(pid)]
    while todo:
        p = todo.pop()
        if p not in out:
            out.add(p)
            todo += kids.get(p, [])
    return out


def users_of(root, skip_pids=()):
    """Processes working inside root (its path in their command line, or as their cwd). A process elsewhere on the
    machine is never a reason to refuse. XCFS_TOOL_PID (set by `xcfs hook` for the agent tool that asked for the
    removal, at its session's end or for its own worktree) leaves out that tool and every process it started: their
    working directory is the checkout because the tool put them there, and the tool is done with it."""
    skip = ancestors() | set(str(p) for p in skip_pids)
    if os.environ.get("XCFS_TOOL_PID", "").isdigit():
        skip |= process_tree(os.environ["XCFS_TOOL_PID"])
    hits = []
    # Our own helpers run from "/": a hook started with its cwd inside the worktree must not count itself.
    for l in run("ps", "-axo", "pid=,command=", cwd="/").stdout.splitlines():
        pid, _, cmdline = l.strip().partition(" ")
        if pid in skip:
            continue
        if re.search(re.escape(root) + r"(?=/|\s|$|\")", cmdline):
            hits.append("%s %s" % (pid, cmdline.strip()[:70]))
    pid = None
    try:  # lsof can hang on a dead network mount; the command-line scan above still stands
        cwds = run("lsof", "-a", "-d", "cwd", "-Fpn", check=False, timeout=10, cwd="/").stdout.splitlines()
    except subprocess.TimeoutExpired:
        cwds = []
    for l in cwds:
        if l.startswith("p"):
            pid = l[1:]
        elif l.startswith("n") and pid not in skip and under(l[1:], root):
            hits.append("%s (cwd %s)" % (pid, l[1:][-60:]))
    return sorted(set(hits))


def trash_and_delete(path, trash_dir):
    """Rename (instant, same volume) then delete in the background, so a 30G build directory never blocks the caller."""
    os.makedirs(trash_dir, exist_ok=True)
    dst = os.path.join(trash_dir, "%s.%d.%d" % (os.path.basename(path.rstrip("/")), int(time.time() * 1000), os.getpid()))
    try:
        os.rename(path, dst)
    except OSError:  # another volume: no rename possible; delete in place, still in the background
        dst = path
    background_delete(dst)
    return dst


def background_delete(path):
    """rm -rf in the background, after making every directory writable: build tools leave read-only directories
    (Bazel's external repositories, for one), and rm cannot empty those."""
    subprocess.Popen(["/bin/sh", "-c", 'chmod -R u+w "$1" 2>/dev/null; exec /bin/rm -rf "$1"', "sh", path],
                     start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd="/")


def run_streaming(cmd, cwd, env, log_path, timeout, on_line=None):
    """Run cmd with its output (stdout + stderr) going to log_path; return (returncode | None on timeout, seconds).
    On timeout the whole process group is killed."""
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    t0 = time.time()
    with open(log_path, "w") as log:
        p = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             errors="replace", start_new_session=True, bufsize=1)
        timed_out = [False]

        def kill():
            timed_out[0] = True
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
        import threading
        timer = threading.Timer(timeout, kill) if timeout else None
        if timer:
            timer.daemon = True
            timer.start()
        try:
            for line in p.stdout:
                log.write(line)
                if on_line:
                    on_line(line)
            p.wait()
        except KeyboardInterrupt:
            kill()
            raise
        finally:
            if timer:
                timer.cancel()
    return (None if timed_out[0] else p.returncode), time.time() - t0


def spawn_detached(argv, log_path, env=None):
    """Start a background xcfs (or other) process that outlives the caller."""
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "a") as log:
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, cwd="/",
                             env=env)
    return p.pid


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def which(name):
    return shutil.which(name)


def self_argv():
    """How to start this same xcfs again (for background work)."""
    return [sys.executable, os.path.realpath(sys.argv[0])]
