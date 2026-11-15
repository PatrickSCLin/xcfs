"""The baseline moves: a pool of 3 slots built at an older commit follows it to a newer one.

    compile-cache-advance.py <work dir> <mode: defaults|cache-all|cache-dependencies> <old ref> <new ref>
                             --source <Ice Cubes clone with that history> [--next-day]

Prints and writes <work dir>/advance.json: the warm-up at the old baseline (not the measure), then the warm-up that
brings the 3 slots to the new baseline (the measure): wall time, peak memory and disk, per-slot seconds. With
--next-day, the pool is then retired and a fresh pool is warmed at the new baseline with the same cache (a pool
re-created the next day).
"""
import json, os, subprocess, sys, threading, time

HERE = os.path.dirname(os.path.realpath(__file__))
XCFS = os.path.join(os.path.dirname(HERE), "bin", "xcfs")
SRC = os.path.realpath(sys.argv[sys.argv.index("--source") + 1])
PROFILE = os.path.join(HERE, "profiles", "icecubes-zero-config.json")
W, mode, old, new = sys.argv[1:5]
next_day = "--next-day" in sys.argv
os.makedirs(W + "/config/xcfs", exist_ok=True)
os.makedirs(W + "/cache", exist_ok=True)
if not os.path.lexists(W + "/cache/swift-driver"):
    os.symlink(os.path.join(os.environ.get("XCFS_CACHE") or os.path.expanduser("~/Library/Caches/xcfs"),
                            "swift-driver"), W + "/cache/swift-driver")
env = dict(os.environ, XCFS_STATE=W + "/state", XCFS_CACHE=W + "/cache", XCFS_POOL_ROOT=W + "/pool",
           XDG_CONFIG_HOME=W + "/config", XCFS_DRIVER="session")
env.pop("CLAUDE_CODE_SESSION_ID", None)


def write_config(ref):
    cfg = json.load(open(PROFILE))["xcfs"]["config"]
    cfg["repos"] = [{"path": ".", "baseline": {"ref": ref}}]
    if mode.startswith("cache-"):
        cfg["toolchains"][0]["compileCache"] = mode[len("cache-"):]
    json.dump(cfg, open(W + "/config/xcfs/IceCubesApp.json", "w"), indent=1)
    json.dump({"warmOnRelease": False}, open(W + "/config/xcfs/settings.json", "w"))


def free_kb():
    st = os.statvfs(W)
    return st.f_bavail * st.f_frsize // 1024


def sample(stop, out):
    """Resident memory of every process below the warm-up and of the slots' build sessions (they name W)."""
    peak, min_free = 0, free_kb()
    while not stop.is_set():
        table = {}
        for l in subprocess.run(["ps", "-axo", "pid=,ppid=,rss=,command="], capture_output=True, text=True).stdout.splitlines():
            p = l.split(None, 3)
            if len(p) == 4:
                table[int(p[0])] = (int(p[1]), int(p[2]), p[3])
        roots = {p for p, (_, _, c) in table.items() if W in c}
        kids = {}
        for p, (pp, _, _) in table.items():
            kids.setdefault(pp, []).append(p)
        todo, seen = list(roots), set(roots)
        while todo:
            for c in kids.get(todo.pop(), []):
                if c not in seen:
                    seen.add(c)
                    todo.append(c)
        peak = max(peak, sum(table[p][1] for p in seen if p in table))
        min_free = min(min_free, free_kb())
        stop.wait(1.0)
    out.update(peak_rss_gb=round(peak / 2 ** 20, 1), peak_disk_gb=round((out["free0"] - min_free) / 2 ** 20, 1))


def warm(label):
    out = {"free0": free_kb()}
    stop = threading.Event()
    th = threading.Thread(target=sample, args=(stop, out))
    th.start()
    t0 = time.time()
    r = subprocess.run([sys.executable, XCFS, "pool", "warm", SRC, "--slots", "3", "--json"], capture_output=True,
                       text=True, env=env)
    out["seconds"] = round(time.time() - t0, 1)
    stop.set()
    th.join()
    d = json.loads(r.stdout.strip().splitlines()[-1])
    out["slots"] = [(s["index"], s.get("seconds"), s.get("simulator_first_boot"), s.get("already_warm"),
                     (s.get("error") or "")[:200]) for s in d.get("slots", [])]
    out.pop("free0")
    print("%-18s %-26s %6.1fs  peak %5.1f GB  disk %5.1f GB  slots %s" % (mode, label, out["seconds"], out["peak_rss_gb"],
          out["peak_disk_gb"], out["slots"]), flush=True)
    return out


res = {"mode": mode, "old": old, "new": new}
write_config(old)
res["at the old baseline"] = warm("at the old baseline")
write_config(new)
if next_day:   # the pool is gone; the next day's pool starts at the new baseline, the cache holds the old one only
    subprocess.run([sys.executable, XCFS, "pool", "resize", SRC, "0", "--json"], capture_output=True, env=env)
    time.sleep(15)
    res["a fresh pool the next day"] = warm("a fresh pool the next day")
else:
    res["follows the new baseline"] = warm("follows the new baseline")
subprocess.run([sys.executable, XCFS, "pool", "resize", SRC, "0", "--json"], capture_output=True, env=env)
json.dump(res, open(W + "/advance.json", "w"), indent=1)
