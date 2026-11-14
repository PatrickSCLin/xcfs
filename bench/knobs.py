#!/usr/bin/env python3
"""knobs.py <profile.json> --source <clone> [--work DIR] [--driver session|xcodebuild] [--repeat N] [--only a,b] [--no-check]

Find out what build settings cost on a real project. One xcfs worktree is built under each variant of the profile's
"knobs" ({"name": {SETTING: value, ...}}, applied on top of the profile's toolchain buildSettings): a first build
after the change (the settings change invalidates incremental state), two no-op builds, then the profile's edits,
each repeated N times with fresh text, each timed as `xcfs build` and — with the session driver — `xcfs check` of the
edited file (--no-check leaves the check out: a check compiles the file, and a build that can reuse that work
looks faster than it is). The edits are undone and rebuilt (not timed) before the next variant.

Prints a table and writes <work>/knobs-<name>-<time>.json and .md.
"""
import json, os, re, subprocess, sys, time

HERE = os.path.dirname(os.path.realpath(__file__))
ROOT = os.path.dirname(HERE)


def arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


PROFILE = json.load(open(sys.argv[1]))
SOURCE = os.path.realpath(arg("--source"))
WORK = os.path.realpath(arg("--work", os.path.join(os.path.dirname(SOURCE), "xcfs-knobs-work")))
DRIVER = arg("--driver", "session")
REPEAT = int(arg("--repeat", 2))
ONLY = arg("--only", "").split(",") if arg("--only") else None
CHECK = DRIVER == "session" and "--no-check" not in sys.argv
XCFS = os.path.join(ROOT, "bin", "xcfs")
STAMP = time.strftime("%Y%m%d-%H%M%S")
os.makedirs(WORK, exist_ok=True)
ENV = dict(os.environ, XCFS_STATE=WORK + "/state", XCFS_CACHE=WORK + "/cache", XCFS_POOL_ROOT=WORK + "/pool",
           XDG_CONFIG_HOME=WORK + "/config", XCFS_DRIVER=DRIVER)
ENV.pop("CLAUDE_CODE_SESSION_ID", None)


def say(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def xcfs(*args):
    t0 = time.time()
    r = subprocess.run([sys.executable, XCFS, *args, "--json"], capture_output=True, text=True, env=ENV, cwd=WORK)
    try:
        out = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        out = {"ok": False, "error": (r.stderr or r.stdout)[-300:]}
    return round(time.time() - t0, 1), out


def write_config(settings):
    """settings: build settings, plus an optional "_entry" of toolchain-entry keys (skipScripts, …)."""
    settings = dict(settings)
    entry = settings.pop("_entry", {})
    cfg = json.loads(json.dumps(PROFILE["xcfs"]["config"]))
    tc = cfg["toolchains"][0]
    tc.update(entry)
    tc["buildSettings"] = dict(tc.get("buildSettings", {}), **settings)
    tc["driver"] = DRIVER
    d = os.path.join(WORK, "config", "xcfs")
    os.makedirs(d, exist_ok=True)
    json.dump(cfg, open(os.path.join(d, os.path.basename(SOURCE) + ".json"), "w"), indent=1)


def apply_edit(root, e, n):
    p = os.path.join(root, e["file"])
    text = open(p).read()
    if "append" in e:
        text += e["append"].replace("{n}", str(n))
    else:
        text = text.replace(e["find"], e["replace"].replace("{n}", str(n)), 1)
    open(p, "w").write(text)


def main():
    knobs = PROFILE.get("knobs") or {"baseline": {}}
    if ONLY:
        knobs = {k: v for k, v in knobs.items() if k in ONLY}
    write_config({})
    t, out = xcfs("new", SOURCE, "xcfs-knobs-%s" % STAMP)
    if not out.get("ok"):
        raise SystemExit("xcfs new failed: %s" % out.get("error"))
    wt = out["worktree"]
    say("worktree %s (%s driver)" % (wt, DRIVER))
    rows = []
    try:
        for name, settings in knobs.items():
            write_config(settings)
            t, out = xcfs("build", wt)
            row = {"knob": name, "settings": settings, "first_build": t, "ok": out.get("ok")}
            if not out.get("ok"):
                row["error"] = (out.get("error") or str(out.get("diagnostics", [])[:2]))[:300]
                say("%-18s FAILED: %s" % (name, row["error"]))
                rows.append(row)
                continue
            noops = [xcfs("build", wt)[0] for _ in range(2)]
            row["noop"] = noops
            base = int(time.time() * 1000) % 10 ** 9 * 10
            n = 0
            for e in PROFILE.get("edits", []):
                b, c = [], []
                for r in range(REPEAT):
                    n += 1
                    apply_edit(wt, e, base + n)
                    if CHECK:
                        c.append(xcfs("check", os.path.join(wt, e["file"]))[0])
                    tb, ob = xcfs("build", wt)
                    b.append(tb if ob.get("ok") else None)
                row.setdefault("edits", {})[e["name"]] = {"build": b, "check": c}
            subprocess.run(["git", "-C", wt, "checkout", "--", "."], capture_output=True)
            xcfs("build", wt)  # back to the baseline sources before the next variant (not timed)
            say("%-18s first %.1fs  no-op %s  %s" % (name, row["first_build"], noops, "  ".join(
                "%s: build %s check %s" % (k[:24], v["build"], v["check"]) for k, v in row["edits"].items())))
            rows.append(row)
    finally:
        subprocess.run(["git", "-C", wt, "checkout", "--", "."], capture_output=True)
        xcfs("rm", wt, "--apply", "--prune-branch")
        base = os.path.join(WORK, "knobs-%s-%s" % (re.sub(r"\W+", "-", PROFILE["name"]).lower(), STAMP))
        data = json.dumps({"profile": PROFILE["name"], "driver": DRIVER, "repeat": REPEAT, "rows": rows}, indent=1)
        for a, b in ((WORK, "<work>"), (SOURCE, "<source>"), (os.path.expanduser("~"), "~")):
            data = data.replace(a, b)
        open(base + ".json", "w").write(data)
        L = ["# Build settings on %s (%s driver)" % (PROFILE["name"], DRIVER), "",
             "| knob | settings | first build after the change | no-op | " +
             " | ".join(e["name"] for e in PROFILE.get("edits", [])) + " |",
             "|---|---|---|---|" + "---|" * len(PROFILE.get("edits", []))]
        for r in rows:
            if not r.get("ok"):
                L.append("| %s | `%s` | failed: %s | | %s" % (r["knob"], r["settings"], r.get("error", "")[:80],
                                                          "| " * len(PROFILE.get("edits", []))))
                continue
            cells = []
            for e in PROFILE.get("edits", []):
                v = r["edits"][e["name"]]
                cells.append(" / ".join("%.1f" % x if x is not None else "fail" for x in v["build"]) +
                             (" (check %s)" % " / ".join("%.1f" % x for x in v["check"]) if v["check"] else ""))
            L.append("| %s | `%s` | %.1f s | %s s | %s |" % (r["knob"], json.dumps(r["settings"]), r["first_build"],
                                                             " / ".join("%.1f" % x for x in r["noop"]), " | ".join(cells)))
        open(base + ".md", "w").write("\n".join(L) + "\n")
        say("results: %s.md" % base)


if __name__ == "__main__":
    main()
