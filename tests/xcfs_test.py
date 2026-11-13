#!/usr/bin/env python3
"""tests/xcfs_test.py [--xcfs PATH] [--python /path/to/python3] [--only t03,t07] [--keep]

End-to-end tests for bin/xcfs. Every test builds its own throw-away git repositories in an isolated sandbox: its own
state directory, its own fake DerivedData root, its own config directory. The sandbox path deliberately contains a
space and non-ASCII characters. Needs macOS (APFS clonefile, hdiutil, lsof).
"""
import json, os, plistlib, shutil, signal, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.realpath(__file__))
XCFS = os.path.realpath(sys.argv[sys.argv.index("--xcfs") + 1]) if "--xcfs" in sys.argv else os.path.join(os.path.dirname(HERE), "bin", "xcfs")
SB = os.path.realpath(tempfile.mkdtemp(prefix="xcfs-test 空格 "))
LINK = SB + "-link"  # a symlink to the sandbox, for the physical-path test
PY = sys.argv[sys.argv.index("--python") + 1] if "--python" in sys.argv else sys.executable
ONLY = sys.argv[sys.argv.index("--only") + 1].split(",") if "--only" in sys.argv else None
ENV = dict(os.environ, XCFS_STATE=SB + "/state", XCFS_DD_ROOT=SB + "/dd", XDG_CONFIG_HOME=SB + "/config")
for k in ("CLAUDE_CODE_SESSION_ID", "XCFS_SESSION"):  # the caller's own session must not leak into the tests
    ENV.pop(k, None)
L28 = "abcdefghijklmnopqrstuvwxyzab"
results = []
os.makedirs(SB + "/dd"); os.makedirs(SB + "/config")


def sh(*cmd, cwd=None, check=True):
    r = subprocess.run(list(cmd), cwd=cwd, capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError("%s -> %d %s" % (cmd, r.returncode, r.stderr[:300]))
    return r.stdout.strip()


def xcfs(*args, cwd=None, env=None):
    t0 = time.time()
    r = subprocess.run([PY, XCFS, *args, "--json"], cwd=cwd, capture_output=True, text=True, env=env or ENV)
    try:
        out = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        out = {"_raw": r.stdout + r.stderr}
    out["_code"] = r.returncode; out["_sec"] = time.time() - t0
    assert "_raw" not in out and "internal error" not in str(out.get("error", "")), "xcfs %s did not answer cleanly: %s" % (args[:2], str(out)[:300])
    return out


def reg():
    try:
        return json.load(open(SB + "/state/registry.json"))["worktrees"]
    except FileNotFoundError:
        return {}


def mkrepo(name, seed_mb=1, cfg=None, ignore=True):
    repo = os.path.join(SB, name); os.makedirs(repo + "/deps/checkouts/a")
    sh("git", "init", "-q", "-b", "main", repo)
    open(repo + "/main.swift", "w").write("print(1)\n")
    if ignore:
        open(repo + "/.gitignore", "w").write("deps/\nlocal.xcconfig\nDerivedData/\n.xcfs-trash/\n")
    with open(repo + "/deps/blob.bin", "wb") as f:
        f.write(os.urandom(1024 * 1024) * seed_mb)
    json.dump({"object": {"dependencies": [
        {"path": repo + "/deps/checkouts/a", "url": "file://" + repo + "/deps/checkouts/a"},
        {"path": repo + "2/other"}, {"path": repo.replace(".", "X") + "/dot"}, {"note": "see " + repo}]}},
        open(repo + "/deps/workspace-state.json", "w"))
    open(repo + "/local.xcconfig", "w").write("SECRET = 1\n")
    if cfg is not None:
        json.dump(dict({"version": 1}, **cfg), open(repo + "/.xcfs.json", "w"))
    sh("git", "-C", repo, "add", "-A"); sh("git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@t.invalid", "commit", "-q", "-m", "init")
    return os.path.realpath(repo)


def commit(wt, msg="c"):
    open(wt + "/extra.swift", "a").write("// %s\n" % msg)
    sh("git", "-C", wt, "add", "-A"); sh("git", "-C", wt, "-c", "user.name=t", "-c", "user.email=t@t.invalid", "commit", "-q", "-m", msg)


def worktrees(repo):
    return [l.split()[0] for l in sh("git", "-C", repo, "worktree", "list").splitlines()]


def branches(repo):
    return sh("git", "-C", repo, "branch", "--format=%(refname:short)").split()


def bucket(name, ws_path, age_days=0, log_age_days=None):
    d = "%s/dd/%s-%s" % (SB, name, L28); os.makedirs(d + "/Logs/Build", exist_ok=True)
    if ws_path:
        plistlib.dump({"WorkspacePath": ws_path}, open(d + "/info.plist", "wb"))
    old = time.time() - age_days * 86400
    for p in (d + "/info.plist", d + "/Logs", d):
        if os.path.exists(p): os.utime(p, (old, old))
    la = time.time() - (age_days if log_age_days is None else log_age_days) * 86400
    os.utime(d + "/Logs/Build", (la, la))
    os.utime(d, (old, old))
    return d


def wait_gone(path, sec=20):
    t = time.time()
    while os.path.exists(path) and time.time() - t < sec:
        time.sleep(0.1)
    return not os.path.exists(path)


def free_kb():
    return int(sh("df", "-k", SB).splitlines()[-1].split()[3])


def test(fn):
    name = fn.__name__
    if ONLY and name.split("_")[0] not in ONLY:
        return fn
    t0 = time.time()
    try:
        note = fn() or ""
        results.append((name, True, note, time.time() - t0))
    except AssertionError as e:
        results.append((name, False, "ASSERT " + str(e)[:1500], time.time() - t0))
    except Exception as e:
        results.append((name, False, "%s: %s" % (type(e).__name__, str(e)[:200]), time.time() - t0))
    print("%-4s %-52s %5.1fs  %s" % ("ok" if results[-1][1] else "FAIL", name, results[-1][3], results[-1][2]), flush=True)
    return fn


CFG = {"seed": ["deps"], "carry": ["local.xcconfig"]}


@test
def t01_new_seeds_by_clone_and_registers():
    repo = mkrepo("r01", seed_mb=300, cfg=CFG)
    before = free_kb(); r = xcfs("new", repo, "feat/a"); used = before - free_kb()
    wt = r.get("worktree", "")
    assert r["_code"] == 0, r
    assert os.path.isfile(wt + "/deps/blob.bin") and os.path.isfile(wt + "/local.xcconfig")
    assert reg()[wt]["state"] == "ready" and "feat/a" in branches(repo)
    assert used < 30 * 1024, "300MB seed cost %d KB on disk: not cloned" % used
    assert xcfs("rm", wt, "--apply")["_code"] == 0
    return "300MB seed cost %dKB; new %.2fs" % (max(used, 0), r["_sec"])


@test
def t02_path_rewrite_respects_boundaries():
    repo = mkrepo("r02.v1", cfg=CFG); r = xcfs("new", repo, "b"); wt = r["worktree"]
    deps = json.load(open(wt + "/deps/workspace-state.json"))["object"]["dependencies"]
    assert deps[0]["path"] == wt + "/deps/checkouts/a", deps[0]
    assert deps[0]["url"] == "file://" + wt + "/deps/checkouts/a", deps[0]
    assert deps[1]["path"] == repo + "2/other", "sibling with the repo path as a prefix was rewritten: %s" % deps[1]
    assert deps[2]["path"] == repo.replace(".", "X") + "/dot", "'.' matched as a wildcard: %s" % deps[2]
    assert deps[3]["note"] == "see " + wt
    xcfs("rm", wt, "--apply")


@test
def t03_existing_destination_is_refused():
    repo = mkrepo("r03", cfg=CFG); os.makedirs(SB + "/taken")
    r = xcfs("new", repo, "b", SB + "/taken")
    assert r["_code"] == 1 and "already exists" in r["error"], r
    assert "b" not in branches(repo) and not reg().get(SB + "/taken")


@test
def t04_config_commands_need_trust():
    repo = mkrepo("r04", cfg=dict(CFG, afterSeed=["touch ran.marker"]))
    r = xcfs("new", repo, "b")
    assert r["_code"] == 3, r
    assert len(worktrees(repo)) == 1 and "b" not in branches(repo), "something was created before trust"
    assert xcfs("trust", repo)["_code"] == 0
    r = xcfs("new", repo, "b"); assert r["_code"] == 0 and os.path.exists(r["worktree"] + "/ran.marker"), r
    open(r["worktree"] + "/.gitignore", "a").write("")  # keep clean
    sh("rm", r["worktree"] + "/ran.marker"); xcfs("rm", r["worktree"], "--apply")
    json.dump({"version": 1, "seed": ["deps"], "afterSeed": ["touch other.marker"]}, open(repo + "/.xcfs.json", "w"))
    assert xcfs("new", repo, "c")["_code"] == 3, "changed commands were not re-approved"


@test
def t05_failed_new_rolls_back_everything():
    repo = mkrepo("r05", cfg=dict(CFG, afterSeed=["exit 9"])); xcfs("trust", repo)
    r = xcfs("new", repo, "b")
    assert r["_code"] == 1 and "afterSeed" in r["error"], r
    assert len(worktrees(repo)) == 1 and "b" not in branches(repo), (worktrees(repo), branches(repo))
    assert not any(p.startswith(repo) for p in reg()) and not os.path.exists(repo + "-wt-b")


@test
def t06_other_volume_is_refused():
    img = SB + "/vol.dmg"; sh("hdiutil", "create", "-size", "80m", "-fs", "APFS", "-volname", "xcfsvol", "-quiet", img)
    mnt = sh("hdiutil", "attach", img, "-nobrowse").splitlines()[-1].split("\t")[-1].strip()
    try:
        repo = mkrepo("r06", cfg=CFG)
        r = xcfs("new", repo, "b", mnt + "/wt")
        assert r["_code"] == 4, r
        assert not os.path.exists(mnt + "/wt") and "b" not in branches(repo)
    finally:
        sh("hdiutil", "detach", mnt, "-quiet", check=False)
    return "mounted %s" % mnt


@test
def t07_dirty_worktree_is_never_removed():
    repo = mkrepo("r07", cfg=CFG); wt = xcfs("new", repo, "b")["worktree"]
    assert xcfs("rm", wt)["removed"] is False and os.path.isdir(wt), "dry-run removed something"
    open(wt + "/main.swift", "a").write("//x\n")
    assert xcfs("rm", wt, "--apply")["_code"] == 5 and os.path.isdir(wt)
    sh("git", "-C", wt, "checkout", "--", "main.swift"); open(wt + "/untracked.swift", "w").write("x")
    assert xcfs("rm", wt, "--apply")["_code"] == 5 and os.path.isdir(wt), "untracked file ignored"
    os.remove(wt + "/untracked.swift"); assert xcfs("rm", wt, "--apply")["_code"] == 0


@test
def t08_only_own_derived_data_is_removed():
    repo = mkrepo("r08", cfg=CFG)
    foo = xcfs("new", repo, "foo", SB + "/w8-wt-foo")["worktree"]; bar = xcfs("new", repo, "foobar", SB + "/w8-wt-foobar")["worktree"]
    b1 = bucket("Foo", foo + "/App.xcworkspace"); b2 = bucket("Foobar", bar + "/App.xcworkspace")
    r = xcfs("rm", foo, "--apply"); assert r["_code"] == 0 and r["derived_data"] == 1, r
    assert wait_gone(b1) and os.path.isdir(b2), "prefix match removed the sibling's DerivedData"
    xcfs("rm", bar, "--apply")


@test
def t09_in_use_is_per_worktree():
    repo = mkrepo("r09", cfg=CFG); wt = xcfs("new", repo, "b")["worktree"]
    fake = SB + "/elsewhere"; os.makedirs(fake); shutil.copy("/bin/sleep", fake + "/xcodebuild")
    other = subprocess.Popen([fake + "/xcodebuild", "300"], cwd=fake)       # a build somewhere else
    inside = subprocess.Popen(["/bin/sleep", "300"], cwd=wt)                # something working in this worktree
    try:
        time.sleep(0.3)
        r = xcfs("rm", wt, "--apply"); assert r["_code"] == 6 and os.path.isdir(wt), r
        inside.kill(); inside.wait()
        byarg = subprocess.Popen(["/usr/bin/tail", "-f", wt + "/main.swift"], stdout=subprocess.DEVNULL)
        time.sleep(0.3); r = xcfs("rm", wt, "--apply"); byarg.kill(); byarg.wait()
        assert r["_code"] == 6, "path in a command line was not seen"
        r = xcfs("rm", wt, "--apply"); assert r["_code"] == 0, "an unrelated build blocked the removal: %s" % r
    finally:
        other.kill(); inside.poll() is None and inside.kill()


@test
def t10_detached_commits_are_kept():
    repo = mkrepo("r10", cfg=CFG); wt = xcfs("new", repo, "b")["worktree"]
    sh("git", "-C", wt, "checkout", "-q", "--detach"); commit(wt, "lonely"); head = sh("git", "-C", wt, "rev-parse", "HEAD")
    r = xcfs("rm", wt, "--apply"); assert r["_code"] == 0 and r["rescue_ref"], r
    assert sh("git", "-C", repo, "rev-parse", r["rescue_ref"]) == head


@test
def t11_changed_ignored_file_blocks_removal():
    repo = mkrepo("r11", cfg=CFG); wt = xcfs("new", repo, "b")["worktree"]
    open(wt + "/local.xcconfig", "a").write("EDITED = 1\n")
    assert sh("git", "-C", wt, "status", "--porcelain") == "", "test premise: git must not see this file"
    r = xcfs("rm", wt, "--apply"); assert r["_code"] == 7 and os.path.isdir(wt), r
    assert xcfs("rm", wt, "--apply", "--discard-ignored")["_code"] == 0


@test
def t12_main_checkout_and_non_repos_are_refused():
    repo = mkrepo("r12", cfg=CFG)
    r = xcfs("rm", repo, "--apply"); assert r["_code"] == 1 and "main checkout" in r["error"] and os.path.isdir(repo), r
    os.makedirs(SB + "/plain"); assert xcfs("rm", SB + "/plain", "--apply")["_code"] == 1
    assert xcfs("new", SB + "/plain", "b")["_code"] == 1 and xcfs("bogus")["_code"] == 2 and xcfs("rm", repo, "--nope")["_code"] == 2


@test
def t13_symlinked_paths_resolve_to_physical():
    repo = mkrepo("r13", cfg=CFG); os.symlink(SB, LINK)
    try:
        r = xcfs("new", LINK + "/r13", "b", LINK + "/r13-wt"); wt = r["worktree"]
        assert r["_code"] == 0 and wt == SB + "/r13-wt", r
        b = bucket("Sym", wt + "/App.xcodeproj")
        r = xcfs("rm", LINK + "/r13-wt", "--apply"); assert r["_code"] == 0 and r["derived_data"] == 1 and wait_gone(b), r
    finally:
        os.remove(LINK)


@test
def t14_reap_takes_only_its_session_and_only_clean():
    repo = mkrepo("r14", cfg=CFG); os.makedirs(SB + "/deep/a/b")
    a = xcfs("new", repo, "s1a", SB + "/deep/a/b/wt1", "--session", "S1")["worktree"]
    b = xcfs("new", repo, "s1b", "--session", "S1")["worktree"]; c = xcfs("new", repo, "s2", "--session", "S2")["worktree"]
    open(b + "/main.swift", "a").write("//wip\n")
    assert all(os.path.isdir(x) for x in (a, b, c)) and xcfs("reap", "--session", "S1")["_code"] == 0 and os.path.isdir(a)
    r = xcfs("reap", "--session", "S1", "--apply")
    assert r["_code"] == 0 and r["removed"] == 1 and r["left"] == [b], r
    assert wait_gone(a) and os.path.isdir(b) and os.path.isdir(c), "reap took the wrong set"
    assert a not in reg() and b in reg() and c in reg()
    sh("git", "-C", b, "checkout", "--", "."); xcfs("rm", b, "--apply"); xcfs("rm", c, "--apply")
    return "nested path 3 levels deep reaped"


@test
def t15_gc_leaves_foreign_caches_alone_and_dates_by_last_build():
    repo = mkrepo("r15", cfg=CFG); wt = xcfs("new", repo, "b")["worktree"]
    shutil.rmtree(wt); sh("git", "-C", repo, "worktree", "prune")                # removed behind xcfs's back
    orphan = bucket("Orphan", SB + "/no-such-project/App.xcworkspace")
    live = bucket("Live", repo, age_days=40, log_age_days=0)                    # bucket dir old, built today
    stale = bucket("Stale", repo + "/main.swift", age_days=40)
    r = xcfs("gc"); assert wt in reg(), "dry-run changed the registry"
    r = xcfs("gc", "--apply"); kinds = [k for k, _ in r["foreign"]]
    assert wt not in reg() and all(os.path.isdir(x) for x in (orphan, live, stale)), "gc touched caches it does not own"
    assert len(kinds) == 2 and not any("Live" in d for _, d in r["foreign"]), "a bucket built today was called stale: %s" % r["foreign"]
    r = xcfs("gc", "--apply", "--foreign")
    assert wait_gone(orphan) and wait_gone(stale) and os.path.isdir(live)
    shutil.rmtree(live)


@test
def t16_killed_new_is_cleaned_by_gc():
    repo = mkrepo("r16", cfg=dict(CFG, afterSeed=["sleep 60"])); xcfs("trust", repo)
    p = subprocess.Popen([PY, XCFS, "new", repo, "half"], env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    wt = repo + "-wt-half"; t = time.time()
    while not os.path.exists(wt + "/deps") and time.time() - t < 15: time.sleep(0.1)
    time.sleep(0.5); p.kill(); p.wait()
    assert reg()[wt]["state"] == "creating" and os.path.isdir(wt)
    assert xcfs("gc", "--apply")["_code"] == 0
    assert wt not in reg() and not os.path.exists(wt) and "half" not in branches(repo) and len(worktrees(repo)) == 1, (reg().keys(), branches(repo))


@test
def t17_concurrent_calls_do_not_corrupt_state():
    repo = mkrepo("r17", cfg=CFG)
    ps = [subprocess.Popen([PY, XCFS, "new", repo, "p%d" % i, "--json"], env=ENV, stdout=subprocess.PIPE, text=True) for i in range(8)]
    outs = [json.loads(p.communicate()[0].strip().splitlines()[-1]) for p in ps]
    assert all(o["ok"] for o in outs), [o.get("error") for o in outs if not o["ok"]]
    assert sum(1 for p in reg() if p.startswith(repo + "-wt-p")) == 8 and len(worktrees(repo)) == 9, (sorted(reg()), worktrees(repo))
    ps = [subprocess.Popen([PY, XCFS, "new", repo, "same", "--json"], env=ENV, stdout=subprocess.PIPE, text=True) for _ in range(4)]
    oks = [json.loads(p.communicate()[0].strip().splitlines()[-1])["ok"] for p in ps]
    assert oks.count(True) == 1, "same destination created %d times" % oks.count(True)
    assert os.path.isdir(repo + "-wt-same") and "same" in branches(repo), "the losers' rollback destroyed the winner"
    targets = [repo + "-wt-p%d" % i for i in range(8)] + [repo + "-wt-same"]
    ps = [subprocess.Popen([PY, XCFS, "rm", t, "--apply", "--json"], env=ENV, stdout=subprocess.PIPE, text=True) for t in targets]
    outs = [json.loads(p.communicate()[0].strip().splitlines()[-1]) for p in ps]
    assert all(o["ok"] for o in outs), [o.get("error") for o in outs if not o["ok"]]
    assert not any(p.startswith(repo) for p in reg()) and len(worktrees(repo)) == 1, (sorted(reg()), worktrees(repo))
    json.load(open(SB + "/state/registry.json"))
    return "8 parallel new, 4 racing for one path, 9 parallel rm"


@test
def t18_unignored_seed_is_reported():
    repo = mkrepo("r18", cfg={"seed": ["deps", "vendor"]})
    os.makedirs(repo + "/vendor"); open(repo + "/vendor/lib.a", "w").write("x")    # untracked and NOT ignored
    r = xcfs("new", repo, "b"); assert r["_code"] == 0 and len(r["warnings"]) == 1 and "vendor" in r["warnings"][0], r
    assert xcfs("rm", r["worktree"], "--apply")["_code"] == 5, "a worktree that is dirty from birth was removed"
    shutil.rmtree(r["worktree"] + "/vendor"); assert xcfs("rm", r["worktree"], "--apply")["_code"] == 0


@test
def t19_repo_without_config_still_works():
    repo = mkrepo("r19"); r = xcfs("new", repo, "b", "--from", "main")
    assert r["_code"] == 0 and r["seeded"] == [] and not os.path.exists(r["worktree"] + "/deps"), r
    assert xcfs("ls")["worktrees"] and xcfs("rm", r["worktree"], "--apply")["_code"] == 0
    assert xcfs("doctor", repo)["_code"] == 0


@test
def t20_doctor_finds_build_output_in_package_dirs():
    repo = mkrepo("r20"); os.makedirs(repo + "/pkgs/core/build/x"); os.makedirs(repo + "/pkgs/core/.build/x")
    open(repo + "/pkgs/core/Package.swift", "w").write("// swift-tools-version: 5.9\n")
    open(repo + "/pkgs/core/.build/x/a.o", "w").write("x")
    assert xcfs("doctor", repo)["_code"] == 0, "a hidden build dir, or an empty one, was reported"
    open(repo + "/pkgs/core/build/x/a.o", "w").write("x")
    r = xcfs("doctor", repo); assert r["_code"] == 1 and "build" in r["problems"][0], r


@test
def t21_stability_60_cycles_leave_nothing_behind():
    repo = mkrepo("r21", seed_mb=20, cfg=CFG); times = []
    for i in range(60):
        r = xcfs("new", repo, "loop"); assert r["_code"] == 0, (i, r)
        os.makedirs(r["worktree"] + "/DerivedData/x"); open(r["worktree"] + "/DerivedData/x/f", "w").write("x" * 4096)
        d = xcfs("rm", r["worktree"], "--apply"); assert d["_code"] == 0, (i, d)
        times.append((r["_sec"], d["_sec"]))
    assert len(worktrees(repo)) == 1 and not any(p.startswith(repo) for p in reg())
    trash = os.path.dirname(repo) + "/.xcfs-trash"; t = time.time()
    while os.path.isdir(trash) and os.listdir(trash) and time.time() - t < 60: time.sleep(0.2)
    assert not os.listdir(trash), "%d entries left in trash" % len(os.listdir(trash))
    n = sorted(x[0] for x in times); m = sorted(x[1] for x in times)
    return "new median %.2fs max %.2fs; rm median %.2fs max %.2fs" % (n[30], n[-1], m[30], m[-1])


@test
def t22_large_tree_removal_does_not_block():
    repo = mkrepo("r22", cfg=CFG); wt = xcfs("new", repo, "b")["worktree"]
    for i in range(200):
        os.makedirs("%s/DerivedData/d%03d" % (wt, i))
        for j in range(100):
            open("%s/DerivedData/d%03d/f%03d" % (wt, i, j), "wb").write(b"x" * 2048)
    t0 = time.time(); sh("cp", "-R", wt + "/DerivedData", SB + "/dd-copy"); t1 = time.time(); sh("rm", "-rf", SB + "/dd-copy"); sync = time.time() - t1
    r = xcfs("rm", wt, "--apply"); assert r["_code"] == 0 and not os.path.exists(wt), r
    assert r["_sec"] < 3, "rm took %.1fs" % r["_sec"]
    return "20,000 files: xcfs rm returned in %.2fs (a plain rm -rf of the same tree: %.2fs)" % (r["_sec"], sync)


@test
def t23_session_end_closes_the_loop():
    repo = mkrepo("r23", cfg=CFG); E = dict(ENV, XCFS_SESSION="sess-A")
    w1 = xcfs("new", repo, "agent/untouched", env=E)["worktree"]      # never committed to
    w2 = xcfs("new", repo, "agent/done", env=E)["worktree"]; commit(w2, "work")
    w3 = xcfs("new", repo, "agent/wip", env=E)["worktree"]; open(w3 + "/main.swift", "a").write("//wip\n")
    other = xcfs("new", repo, "someone-else", "--session", "sess-B")["worktree"]
    dd = bucket("W1", w1 + "/App.xcworkspace"); head2 = sh("git", "-C", w2, "rev-parse", "HEAD")
    st = {os.path.basename(r["worktree"]): r for r in xcfs("ls", "--session", "sess-A", "--status")["worktrees"]}
    assert len(st) == 3 and st[os.path.basename(w3)]["dirty"] == 1 and st[os.path.basename(w2)]["unpushed"] >= 1, st
    r = xcfs("reap", "--apply", "--prune-branch", cwd=w1, env=E)       # the hook runs with the session's env, cwd inside a worktree
    assert r["_code"] == 0 and r["removed"] == 2 and r["left"] == [w3], r
    assert wait_gone(w1) and wait_gone(w2) and wait_gone(dd) and os.path.isdir(w3) and os.path.isdir(other)
    b = branches(repo)
    assert "agent/untouched" not in b, "an empty branch xcfs created was left behind"
    assert "agent/done" in b and sh("git", "-C", repo, "rev-parse", "agent/done") == head2, "committed work was lost"
    left = xcfs("ls", "--session", "sess-A")["worktrees"]; assert [x["worktree"] for x in left] == [w3]
    commit(w3, "finish"); r = xcfs("reap", "--apply", "--prune-branch", env=E)
    assert r["removed"] == 1 and xcfs("ls", "--session", "sess-A")["worktrees"] == [] and "agent/wip" in branches(repo)
    xcfs("rm", other, "--apply")
    return "untouched: gone with its branch; committed: gone, branch kept; dirty: kept and reported"


@test
def t24_reap_by_age_and_all():
    repo = mkrepo("r24", cfg=CFG)
    old = xcfs("new", repo, "old", "--session", "X")["worktree"]; new = xcfs("new", repo, "new")["worktree"]
    d = json.load(open(SB + "/state/registry.json"))
    d["worktrees"][old]["created"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 5 * 86400))
    json.dump(d, open(SB + "/state/registry.json", "w"))
    e = dict(ENV); e.pop("XCFS_SESSION", None)
    assert xcfs("reap", "--apply", env=e)["_code"] == 2, "reap with no selector must not guess"
    r = xcfs("reap", "--older-than", "3", "--apply"); assert r["removed"] == 1 and wait_gone(old) and os.path.isdir(new), r
    r = xcfs("reap", "--all", "--apply"); assert r["removed"] == 1 and wait_gone(new), "a worktree with no session was not reachable: %s" % r


@test
def t25_adopt_and_release():
    repo = mkrepo("r25", cfg=CFG); plain = SB + "/r25-plain"
    sh("git", "-C", repo, "worktree", "add", "-q", "-b", "manual", plain); plain = os.path.realpath(plain)
    assert xcfs("reap", "--all", "--apply")["removed"] == 0 and os.path.isdir(plain), "xcfs touched a worktree it does not manage"
    assert xcfs("adopt", repo, "--session", "S")["_code"] == 1, "the main checkout was adopted"
    assert xcfs("adopt", plain, "--session", "S")["_code"] == 0
    mine = xcfs("new", repo, "kept", "--session", "S")["worktree"]; assert xcfs("adopt", mine, "--release")["_code"] == 0
    r = xcfs("reap", "--session", "S", "--apply", "--prune-branch")
    assert r["removed"] == 1 and wait_gone(plain) and os.path.isdir(mine), r
    assert "manual" in branches(repo), "a branch xcfs did not create was deleted"
    xcfs("rm", mine, "--apply")


@test
def t26_prune_branch_never_deletes_work_or_foreign_branches():
    repo = mkrepo("r26", cfg=CFG); sh("git", "-C", repo, "branch", "preexisting")
    a = xcfs("new", repo, "preexisting")["worktree"]; b = xcfs("new", repo, "fresh")["worktree"]; c = xcfs("new", repo, "fresh2")["worktree"]
    d = xcfs("new", repo, "worked")["worktree"]; commit(d)
    for w in (a, b, d): assert xcfs("rm", w, "--apply", "--prune-branch")["_code"] == 0
    assert xcfs("rm", c, "--apply")["branch_deleted"] is False
    b_ = branches(repo)
    assert "preexisting" in b_ and "worked" in b_ and "fresh2" in b_ and "fresh" not in b_, b_


@test
def t27_help_and_version_answer_without_side_effects():
    before = reg()
    for args in (["--help"], ["new", "--help"], ["rm", "-h"], ["--version"]):
        r = subprocess.run([PY, XCFS, *args], capture_output=True, text=True, env=ENV)
        assert r.returncode == 0 and r.stdout.startswith("xcfs"), (args, r.returncode, r.stdout[:80], r.stderr[:200])
    assert reg() == before, "help changed the registry"


def main():
    bad = [r for r in results if not r[1]]
    print("\n%d passed, %d failed, python %s" % (len(results) - len(bad), len(bad), sh(PY, "--version")))
    for r in bad:
        print("  FAIL %s: %s" % (r[0], r[2]))
    if "--keep" in sys.argv:
        print("sandbox kept: %s" % SB)
    else:
        time.sleep(1); shutil.rmtree(SB, ignore_errors=True)
    sys.exit(1 if bad else 0)


main()
