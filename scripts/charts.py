#!/usr/bin/env python3
"""Draw the README's benchmark charts from the raw results in bench/results, as SVG (light and dark).

    scripts/charts.py            writes docs/img/<chart>-light.svg and <chart>-dark.svg

Every number comes from a results file named below; nothing is typed in. Standard library only.
"""
import json, os

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
RES = os.path.join(ROOT, "bench", "results")
OUT = os.path.join(ROOT, "docs", "img")

THEMES = {
    "light": {"text": "#1f2328", "muted": "#59636e", "grid": "#d1d9e0", "plain": "#8c959f", "xcfs": "#0969da",
              "xcfs2": "#80b9f2", "opt": "#8250df", "opt2": "#c8b3f5", "plain2": "#c4cbd3", "whisker": "#1f2328"},
    "dark": {"text": "#e6edf3", "muted": "#9198a1", "grid": "#3d444d", "plain": "#6e7681", "xcfs": "#4493f8",
             "xcfs2": "#1f4f8f", "opt": "#a371f7", "opt2": "#4c2f8a", "plain2": "#484f58", "whisker": "#e6edf3"},
}
FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"


def load(name):
    return json.load(open(os.path.join(RES, name)))


def rng(values):
    return min(values), max(values)


# ------------------------------------------------------------------ the data, from the results files
# Before 0.3 the experimental Swift driver was on by default: in the results files "xcfs (defaults)" is xcfs WITH the
# driver (now opt-in, "swiftDriver": "patched") and "xcfs (defaults-without-driver-patch)" is xcfs as it is by default
# now. The charts label them by what they are.
ICE = "tasks-icecubes-%s--results-ice-cubes-arm64-three-agents-real-tasks--20261115-%s.json"
WIKI = "tasks-wikipedia-%s--results-wikipedia-ios-arm64-three-agents-real-tasks--20261115-%s.json"
WIKI2 = "tasks-wikipedia-nodriver-%s--results-wikipedia-ios-arm64-three-agents-real-tasks--20261117-%s.json"
TASKS = {
    "Ice Cubes": {
        "plain": [ICE % ("v4-rep1", "062620"), ICE % ("v4-rep2", "064126")],
        "xcfs": [ICE % ("v4-rep2", "064126"), ICE % ("v4-rep3", "073552"), ICE % ("v4-rep4", "075201")],
        "driver": [ICE % ("v5-rep5", "094011"), ICE % ("v5-rep6", "094853")],
    },
    "Wikipedia iOS": {   # with the driver on 2026-11-15 (0.2); without it on 2026-11-17 (0.3.1, where it is the default)
        "plain": [WIKI % ("rep1", "161441"), WIKI % ("rep2", "163328"), WIKI2 % ("rep1", "200836"),
                  WIKI2 % ("rep2", "202910")],
        "xcfs": [WIKI2 % ("rep1", "200836"), WIKI2 % ("rep2", "202910")],
        "driver": [WIKI % ("rep1", "161441"), WIKI % ("rep2", "163328")],
    },
}
# Results written by 0.3 or later name xcfs without the driver "xcfs (defaults)": the files of these runs.
DEFAULTS_ARE_NATIVE = {WIKI2 % ("rep1", "200836"), WIKI2 % ("rep2", "202910")}
WITH, WITHOUT = "xcfs (defaults)", "xcfs (defaults-without-driver-patch)"
NEXT = ", the next 3 agents on the same pool"

ZERO = [  # project, plain round files (three runs), xcfs round files
    ("Kickstarter", ["150920", "20261115-013105", "20261115-013631"], ["183610"], "kickstarter-ios-arm64-zero-config"),
    ("Element X", ["152658", "20261115-014123", "20261115-014631"], ["182835"], "element-x-ios-arm64-zero-config"),
    ("Signal", ["153216", "20261115-015102", "20261115-015637"], ["184224"], "signal-ios-arm64-zero-config"),
    ("Home Assistant", ["153836", "20261115-020202", "20261115-021101"], ["185000"],
     "home-assistant-ios-arm64-zero-config"),
    ("Ice Cubes", ["154957", "20261115-021926", "20261115-022213"], ["190207", "205259"],
     "ice-cubes-arm64-only-zero-config"),
    ("Wikipedia iOS", ["155808", "20261115-022441", "20261115-022900"], ["190515"],
     "wikipedia-ios-arm64-only-zero-config"),
]


def zero_file(profile, stamp):
    stamp = stamp if stamp.startswith("2026") else "20261114-" + stamp
    return "results-%s--%s.json" % (profile, stamp)


def side(files, key):
    """(fresh pool with warm-up, next batch, peak memory while the agents work) ranges, or None if not measured."""
    runs = [(load(f)["tasks"], WITH if f in DEFAULTS_ARE_NATIVE else key) for f in files]
    if not runs:
        return None
    return {"fresh": rng([r[k]["wall_with_warm_seconds"] for r, k in runs]),
            "next": rng([r[k + NEXT]["wall_with_warm_seconds"] for r, k in runs]),
            "mem": rng([r[x]["peak_rss_kb"] / 2 ** 20 for r, k in runs for x in (k, k + NEXT)])}


def task_numbers():
    out = {}
    for app, files in TASKS.items():
        plain = [load(f)["tasks"]["plain"] for f in files["plain"]]
        out[app] = {
            "plain": rng([p["wall_seconds"] for p in plain]),
            "plain_mem": rng([p["peak_rss_kb"] / 2 ** 20 for p in plain]),
            "plain_left": rng([p["left_kb"] / 2 ** 20 for p in plain]),
            "xcfs": side(files["xcfs"], WITHOUT),
            "driver": side(files["driver"], WITH),
        }
    return out


def zero_numbers():
    out = []
    for name, plain, xcfs, profile in ZERO:
        p = [load(zero_file(profile, s))["round"]["plain"]["total_seconds"] for s in plain]
        x = [load(zero_file(profile, s))["round"][WITHOUT]["total_seconds"] for s in xcfs]
        d = [load(zero_file(profile, s))["round"][WITH]["total_seconds"] for s in xcfs]
        out.append((name, rng(p), rng(x), rng(d)))
    return out


# ------------------------------------------------------------------ drawing
def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def fmt(lo, hi, unit):
    """A range as the docs write it: seconds rounded half up to whole seconds, gigabytes to one decimal."""
    r = (lambda v: "%.1f" % (int(v * 10 + 0.5) / 10)) if unit == " GB" else (lambda v: "%d" % int(v + 0.5))
    return (r(hi) if r(lo) == r(hi) else "%s–%s" % (r(lo), r(hi))) + unit


def hbar_chart(groups, legend, unit, scale_max, theme, width=760, label_w=150, title=None, right=250):
    """groups: [(group label, [(series key, (lo, hi), note or None)])]; legend: [(series key, label)]."""
    t = THEMES[theme]
    bar_h, gap, group_gap, top = 18, 6, 18, 34 if title else 10
    plot_x, plot_w = label_w, width - label_w - right   # room on the right for the longest label
    rows = sum(len(b) for _, b in groups)
    height = top + rows * (bar_h + gap) + len(groups) * group_gap + 44
    svg = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" font-family="%s" '
           'font-size="12">' % (width, height, width, height, FONT)]
    if title:
        svg.append('<text x="0" y="16" fill="%s" font-size="13" font-weight="600">%s</text>' % (t["text"], esc(title)))
    x_of = lambda v: plot_x + plot_w * v / scale_max
    y = top
    for g, bars in groups:
        svg.append('<text x="0" y="%d" fill="%s" font-weight="600">%s</text>' % (y + bar_h - 4, t["text"], esc(g)))
        for key, (lo, hi), note in bars:
            mid = (lo + hi) / 2
            svg.append('<rect x="%.1f" y="%d" width="%.1f" height="%d" rx="2" fill="%s"/>' % (
                plot_x, y, max(1.0, x_of(mid) - plot_x), bar_h, t[key]))
            if hi - lo > 1e-9:   # the range of the runs
                cy = y + bar_h / 2
                svg.append('<path d="M%.1f %.1f H%.1f M%.1f %.1f V%.1f M%.1f %.1f V%.1f" stroke="%s" stroke-width="1.2" '
                           'fill="none"/>' % (x_of(lo), cy, x_of(hi), x_of(lo), cy - 5, cy + 5, x_of(hi), cy - 5, cy + 5,
                                              t["whisker"]))
            label = note if hi == 0 and note else fmt(lo, hi, unit) + (" " + note if note else "")
            svg.append('<text x="%.1f" y="%d" fill="%s">%s</text>' % (max(x_of(hi), plot_x) + 6, y + bar_h - 4,
                                                                    t["text"], esc(label)))
            y += bar_h + gap
        y += group_gap
    lx = 0
    for key, text in legend:   # the legend, under the bars
        svg.append('<rect x="%d" y="%d" width="12" height="12" rx="2" fill="%s"/>' % (lx, y + 2, t[key]))
        svg.append('<text x="%d" y="%d" fill="%s">%s</text>' % (lx + 17, y + 12, t["muted"], esc(text)))
        lx += 17 + 7 * len(text) + 22
    svg.append("</svg>")
    return "\n".join(svg) + "\n"


def write(name, make):
    os.makedirs(OUT, exist_ok=True)
    for theme in THEMES:
        with open(os.path.join(OUT, "%s-%s.svg" % (name, theme)), "w") as f:
            f.write(make(theme))
        print("docs/img/%s-%s.svg" % (name, theme))


NOT_MEASURED = (0.0, 0.0)

# ------------------------------------------------------------------ the fair comparison (2026-11-18, xcfs 0.3.1+)
FAIR = {"Ice Cubes": ["fair-icecubes-rep1--results-ice-cubes-arm64-three-agents-real-tasks--20261118-022927.json",
                      "fair-icecubes-rep2--results-ice-cubes-arm64-three-agents-real-tasks--20261118-032927.json"],
        "Wikipedia": ["fair-wikipedia-rep1--results-wikipedia-ios-arm64-three-agents-real-tasks--20261118-042542.json",
                      "fair-wikipedia-rep2--results-wikipedia-ios-arm64-three-agents-real-tasks--20261118-051030.json"]}
# A new worktree per batch (what agent tools do by default) builds cold whatever came before; measured in every run
# that had it, on several days and xcfs versions (it does not use xcfs).
COLD = {"Ice Cubes": ([ICE % ("v4-rep1", "062620"), ICE % ("v4-rep2", "064126"),
                       "tasks-icecubes-v2-rep1--results-ice-cubes-arm64-three-agents-real-tasks--20261115-034835.json",
                       "tasks-icecubes-v2-rep2--results-ice-cubes-arm64-three-agents-real-tasks--20261115-041047.json"],
                      FAIR["Ice Cubes"][:1]),
        "Wikipedia": ([WIKI % ("rep1", "161441"), WIKI % ("rep2", "163328"), WIKI2 % ("rep1", "200836"),
                       WIKI2 % ("rep2", "202910")], [])}


# The same pools after the warm-up started waiting for new simulators to settle (2026-11-18, later that morning): the
# first batch as it is now, and two more runs of the next batch (the same code path as above).
SETTLED = {"Ice Cubes": ["settled-icecubes-rep1--results-ice-cubes-arm64-three-agents-real-tasks--20261118-080123.json",
                         "settled-icecubes-rep2--results-ice-cubes-arm64-three-agents-real-tasks--20261118-084434.json"],
           "Wikipedia": ["settled-wikipedia-rep1--results-wikipedia-ios-arm64-three-agents-real-tasks--20261118-082740.json",
                         "settled-wikipedia-rep2--results-wikipedia-ios-arm64-three-agents-real-tasks--20261118-091041.json"]}


def fair_numbers():
    out = {}
    for app, files in FAIR.items():
        runs = [load(f)["tasks"] for f in files]
        later = [load(f)["tasks"] for f in SETTLED[app]]

        def get(prefix, field="wall_seconds", among=None, exact=False):
            vals = [v[field] for r in (among if among is not None else runs) for k, v in r.items()
                    if (k == prefix if exact else k.startswith(prefix)) and v.get(field) is not None]
            return rng(vals) if vals else None
        cold = [load(f)["tasks"]["plain"] for f in COLD[app][0] + COLD[app][1]]
        out[app] = {
            "cold": rng([c["wall_seconds"] for c in cold]),
            "kept_first": get("plain, kept worktrees", exact=True),
            "kept_next": get("plain, kept worktrees, the next"),
            "kept_adv": get("plain, kept worktrees, after the baseline moved"),
            "xcfs_first": get("xcfs (defaults)", among=later, exact=True),
            "xcfs_first_warm": get("xcfs (defaults)", "warm_seconds", among=later, exact=True),
            "drv_first": get("xcfs (experimental-driver)", among=later, exact=True),
            "drv_first_warm": get("xcfs (experimental-driver)", "warm_seconds", among=later, exact=True),
            "xcfs_next": get("xcfs (defaults), the next", among=runs + later),
            "xcfs_next_warm": get("xcfs (defaults), the next", "warm_seconds", among=runs + later),
            "xcfs_adv": get("xcfs (defaults), after the baseline moved"),
            "xcfs_adv_warm": get("xcfs (defaults), after the baseline moved", "warm_seconds"),
            "drv_next": get("xcfs (experimental-driver), the next", among=runs + later),
            "drv_adv": get("xcfs (experimental-driver), after the baseline moved"),
            "cold_left": rng([c["left_kb"] / 2 ** 20 for c in cold if c.get("left_kb")]),
            "moved": next((int(k.split("moved ")[1].split()[0]) for r in runs for k in r if "after the baseline moved" in k
                           and k.startswith("xcfs")), None),
        }
    return out


def secs(r):
    lo, hi = r
    return fmt(lo, hi, " s")


def fair_groups(fn):
    groups = []
    for app, n in fn.items():
        for case, kept, x, xw, what, d in (
                ("a fresh pool's first batch", n["kept_first"], n["xcfs_first"], n["xcfs_first_warm"], "warm-up first",
                 n["drv_first"] and (n["drv_first"], n["drv_first_warm"])),
                ("the next batch", n["kept_next"], n["xcfs_next"], n["xcfs_next_warm"], "re-warm, done beforehand",
                 n["drv_next"] and (n["drv_next"], None)),
                ("after main moved %d commits" % n["moved"], n["kept_adv"], n["xcfs_adv"], n["xcfs_adv_warm"],
                 "following main, beforehand", n["drv_adv"] and (n["drv_adv"], None))):
            bars = [("plain", n["cold"], "new worktree each batch"),
                    ("plain2", kept, "kept worktrees"),
                    ("xcfs", x, "xcfs (+%s %s)" % (secs(xw), what))]
            if d:
                bars.append(("opt", d[0], "xcfs + driver" + (" (+%s warm-up first)" % secs(d[1]) if d[1] else "")))
            groups.append(("%s, %s" % (app, case), bars))
    return groups


def main():
    fn = fair_numbers()
    write("agents-wall-time", lambda theme: hbar_chart(
        fair_groups(fn),
        [("plain", "new worktree per batch"), ("plain2", "kept worktrees"), ("xcfs", "xcfs"),
         ("opt", "xcfs + driver (opt-in)")],
        " s", 500, theme, label_w=265, width=1135, right=560,
        title="Three agents with real tasks: how long they wait for builds and tests per batch (shorter is better)"))
    write("agents-memory-disk", lambda theme: hbar_chart(
        [(app, [("plain", n["cold_left"], "of DerivedData per batch of three"), ("xcfs", NOT_MEASURED, "nothing")])
         for app, n in fn.items()],
        [("plain", "plain git worktrees, removed"), ("xcfs", "xcfs checkouts, removed")],
        " GB", 12, theme, label_w=150, width=920,
        title="Left behind on disk after the agents' worktrees are removed (less is better)"))
    zn = zero_numbers()
    write("zero-config-round", lambda theme: hbar_chart(
        [(name, [("plain", p, None), ("xcfs", x, None), ("opt", d, None)]) for name, p, x, d in zn],
        [("plain", "plain worktree"), ("xcfs", "xcfs"), ("opt", "xcfs + experimental driver (opt-in)")],
        " s", 560, theme, width=840,
        title="One agent's round of work, zero configuration (shorter is better; xcfs 0.2, 2026-11-14)"))


if __name__ == "__main__":
    main()
