#!/usr/bin/env python3
"""pool-idle.py <profile.json> --source <clone> --work DIR [--slots N] [--minutes 5,25,35]

What a warmed pool costs while nobody uses it: warm N slots with the profile's xcfs config, then, right after and at
each of the given minutes, the processes that belong to the pool (their own command line names the work directory,
or they descend from one that does: build sessions, swbuild, the build service) with their resident memory and the
CPU time they used since the previous sample; and the disk: the volume's free space before the warm-up and after
(clones counted once), next to the logical size of each part (checkouts, build directories, simulators). Writes
<work>/pool-idle.json.
"""
import json, os, subprocess, sys, time

HERE = os.path.dirname(os.path.realpath(__file__))
XCFS = os.path.join(os.path.dirname(HERE), "bin", "xcfs")


def arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


PROFILE = json.load(open(sys.argv[1]))
SRC = os.path.realpath(arg("--source"))
W = os.path.realpath(arg("--work"))
N = int(arg("--slots", 3))
MINUTES = [float(m) for m in arg("--minutes", "5,25,35").split(",")]
os.makedirs(W + "/config/xcfs", exist_ok=True)
os.makedirs(W + "/cache", exist_ok=True)
driver = os.path.join(os.environ.get("XCFS_CACHE") or os.path.expanduser("~/Library/Caches/xcfs"), "swift-driver")
if os.path.isdir(driver) and not os.path.lexists(W + "/cache/swift-driver"):
    os.symlink(driver, W + "/cache/swift-driver")
json.dump(PROFILE["xcfs"]["config"], open(W + "/config/xcfs/%s.json" % os.path.basename(SRC), "w"), indent=1)
json.dump({"warmOnRelease": False}, open(W + "/config/xcfs/settings.json", "w"))
ENV = dict(os.environ, XCFS_STATE=W + "/state", XCFS_CACHE=W + "/cache", XCFS_POOL_ROOT=W + "/pool",
           XDG_CONFIG_HOME=W + "/config")
ENV.pop("CLAUDE_CODE_SESSION_ID", None)


def xcfs(*a):
    r = subprocess.run([sys.executable, XCFS, *a, "--json"], capture_output=True, text=True, env=ENV)
    return json.loads(r.stdout.strip().splitlines()[-1])


def free_kb():
    st = os.statvfs(W)
    return st.f_bavail * st.f_frsize // 1024


def cpu_seconds(t):
    """ps cputime ([[dd-]hh:]mm:ss) in seconds."""
    days, _, t = t.rpartition("-")
    parts = [float(x) for x in t.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0.0)
    return (int(days) if days else 0) * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]


def pool_processes():
    table = {}
    for l in subprocess.run(["ps", "-axo", "pid=,ppid=,rss=,time=,command="], capture_output=True,
                            text=True).stdout.splitlines():
        p = l.split(None, 4)
        if len(p) == 5:
            table[int(p[0])] = (int(p[1]), int(p[2]), cpu_seconds(p[3]), p[4])
    roots = {p for p, v in table.items() if W in v[3] and p != os.getpid()}
    kids = {}
    for p, v in table.items():
        kids.setdefault(v[0], []).append(p)
    todo, seen = list(roots), set(roots)
    while todo:
        for c in kids.get(todo.pop(), []):
            if c not in seen:
                seen.add(c)
                todo.append(c)
    return {p: table[p] for p in seen if p in table}


def du_kb(path):
    out = subprocess.run(["du", "-sk", path], capture_output=True, text=True).stdout.split()
    return int(out[0]) if out else 0


free0 = free_kb()
t0 = time.time()
warm = xcfs("pool", "warm", SRC, "--slots", str(N))
print("warmed %d slots in %.0fs: %s" % (N, time.time() - t0, [(s["index"], s.get("seconds"), s.get("error"))
                                                              for s in warm.get("slots", [])]), flush=True)
t_warm, free_warm = time.time(), free_kb()
status = xcfs("pool", "status", SRC)
reg = json.load(open(W + "/state/registry.json"))
parts = []
for s in status.get("slots", []):
    wt = s["worktree"]
    bdir = (reg["builds"].get(wt) or {}).get("dir")
    sim = ((reg["builds"].get(wt) or {}).get("resources") or {}).get("xcode", {}).get("simulator")
    simdir = os.path.expanduser("~/Library/Developer/CoreSimulator/Devices/%s" % sim) if sim else None
    parts.append({"slot": s["index"], "checkout_kb": du_kb(wt), "build_kb": du_kb(bdir) if bdir else 0,
                  "simulator_kb": du_kb(simdir) if simdir and os.path.isdir(simdir) else 0})
samples, prev = [], {}
for at in [0.0] + MINUTES:
    while time.time() - t_warm < at * 60:
        time.sleep(min(30, at * 60 - (time.time() - t_warm)))
    procs = pool_processes()
    by = {}
    cpu_used = 0.0
    for p, (_, rss, cpu, cmd) in procs.items():
        name = os.path.basename(cmd.split(" ", 1)[0])
        if "xcode_session import serve" in cmd:
            name = "xcfs build session"
        n = by.setdefault(name, [0, 0])
        n[0] += 1
        n[1] += rss
        cpu_used += cpu - prev.get(p, cpu)
    prev = {p: v[2] for p, v in procs.items()}
    row = {"minutes_idle": at, "processes": len(procs), "rss_mb": round(sum(v[1] for v in procs.values()) / 1024),
           "cpu_seconds_since_previous_sample": round(cpu_used, 2),
           "by_process": {k: {"count": v[0], "rss_mb": round(v[1] / 1024)} for k, v in sorted(by.items())}}
    samples.append(row)
    print("idle %4.0f min: %d processes, %d MB resident, %.2f s of CPU since the previous sample; %s" % (
        at, row["processes"], row["rss_mb"], row["cpu_seconds_since_previous_sample"],
        ", ".join("%s x%d %d MB" % (k, v["count"], v["rss_mb"]) for k, v in row["by_process"].items())), flush=True)
out = {"profile": PROFILE["name"], "slots": N, "warm_seconds": round(t_warm - t0, 1),
       "disk_used_kb": free0 - free_warm, "parts": parts, "samples": samples}
json.dump(out, open(W + "/pool-idle.json", "w"), indent=1)
print("disk: %.1f GB used on the volume; logical sizes per slot: %s" % (
    (free0 - free_warm) / 2 ** 20, [(p["slot"], round(p["checkout_kb"] / 2 ** 20, 2), round(p["build_kb"] / 2 ** 20, 2),
                                    round(p["simulator_kb"] / 2 ** 20, 2)) for p in parts]), flush=True)
xcfs("pool", "resize", SRC, "0")
