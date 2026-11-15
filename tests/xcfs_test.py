#!/usr/bin/env python3
"""tests/xcfs_test.py [--xcfs PATH] [--python /path/to/python3] [--only t03,t07] [--keep]

End-to-end tests for bin/xcfs. Every test builds its own throw-away git repositories in an isolated sandbox: its own
state directory, its own fake DerivedData root, its own config directory. The sandbox path deliberately contains a
space and non-ASCII characters. Needs macOS (APFS clonefile, hdiutil, lsof).
"""
import json, os, plistlib, re, shutil, signal, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.realpath(__file__))
XCFS = os.path.realpath(sys.argv[sys.argv.index("--xcfs") + 1]) if "--xcfs" in sys.argv else os.path.join(os.path.dirname(HERE), "bin", "xcfs")
SB = os.path.realpath(tempfile.mkdtemp(prefix="xcfs-test 空格 "))
LINK = SB + "-link"  # a symlink to the sandbox, for the physical-path test
PY = sys.argv[sys.argv.index("--python") + 1] if "--python" in sys.argv else sys.executable
ONLY = sys.argv[sys.argv.index("--only") + 1].split(",") if "--only" in sys.argv else None
ENV = dict(os.environ, XCFS_STATE=SB + "/state", XCFS_DD_ROOT=SB + "/dd", XDG_CONFIG_HOME=SB + "/config",
           XCFS_CACHE=SB + "/cache", XCFS_POOL_ROOT=SB + "/pool", XCFS_BAZEL_USER_ROOT=SB + "/bazel-root",
           XCFS_SWIFT_DRIVER="native",  # the tests never build the experimental Swift driver
           XCFS_DRIVER="xcodebuild")    # nor run the session regression check (sample_e2e.sh covers the session)
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

TOY = r"""#!/usr/bin/env python3
import json, os, sys
req = json.load(sys.stdin); op = req["op"]; ctx = req.get("context") or {}; args = req.get("args") or {}
def out(**kw): print(json.dumps(kw)); sys.exit(0)
if op == "detect":
    out(entry={"adapter": "toy"} if os.path.exists(os.path.join(args["root"], "toy.txt")) else None)
if op == "version":
    out(version=os.environ.get("TOY_VERSION", "1"))
if op in ("build", "warm"):
    if os.environ.get("TOY_SLEEP"):   # a build that takes a while, with its start and end in TOY_LOG
        import time
        t0 = time.time(); time.sleep(float(os.environ["TOY_SLEEP"]))
        open(os.environ["TOY_LOG"], "a").write("%f %f\n" % (t0, time.time()))
    src = open(os.path.join(ctx["toolchainRoot"], "toy.txt")).read()
    os.makedirs(ctx["buildDir"], exist_ok=True)
    marker = os.path.join(ctx["buildDir"], "out.txt"); had = os.path.exists(marker)
    open(marker, "w").write(src)
    res = dict(ctx["resources"]); res["count"] = res.get("count", 0) + 1
    if "broken" in src:
        out(ok=False, seconds=0.01, resources=res, diagnostics=[{"file": os.path.join(ctx["toolchainRoot"], "toy.txt"),
            "line": 1, "column": 1, "severity": "error", "message": "broken"}])
    out(ok=True, seconds=0.01, diagnostics=[], resources=res, count=res["count"], had_previous=had, build_dir=ctx["buildDir"])
if op == "sharesResults":
    out(shares=os.environ.get("TOY_SHARES") == "1")
if op == "afterSwitch":
    res = dict(ctx["resources"]); res["switches"] = res.get("switches", 0) + 1
    out(resources=res)
out(unsupported=True)
"""


def toy_env():
    os.makedirs(SB + "/bin", exist_ok=True)
    open(SB + "/bin/xcfs-adapter-toy", "w").write(TOY); os.chmod(SB + "/bin/xcfs-adapter-toy", 0o755)
    return dict(ENV, PATH=SB + "/bin:" + os.environ["PATH"])


@test
def t28_core_names_no_toolchain():
    lib = os.path.join(os.path.dirname(HERE), "lib", "xcfs")
    import re
    bad = []
    for n in sorted(os.listdir(lib)):
        if n.endswith(".py"):
            for i, l in enumerate(open(os.path.join(lib, n)), 1):
                if re.search(r"xcodebuild|swbuild|simctl|DerivedData|xcodeproj|xcworkspace", l):
                    bad.append("%s:%d" % (n, i))
    assert not bad, "toolchain names in the core: %s" % bad


@test
def t29_config_is_validated():
    repo = mkrepo("r29", cfg={"seed": ["deps"], "sede": ["x"]})
    r = xcfs("new", repo, "b"); assert r["_code"] == 2 and "sede" in r["error"], r
    json.dump({"version": 1, "repos": [{"path": "../outside"}]}, open(repo + "/.xcfs.json", "w"))
    assert xcfs("new", repo, "b")["_code"] == 2
    json.dump({"version": 1, "toolchains": [{"adapter": "no-such-thing"}]}, open(repo + "/.xcfs.json", "w"))
    r = xcfs("new", repo, "b"); assert r["_code"] == 2 and "no-such-thing" in r["error"], r
    json.dump({"version": 1, "toolchains": [{"adapter": "xcode", "configurations": {"d": {"configuraton": "Debug"}}}]},
              open(repo + "/.xcfs.json", "w"))
    r = xcfs("new", repo, "b"); assert r["_code"] == 2 and "configuraton" in r["error"], r
    assert "b" not in branches(repo)


@test
def t30_external_adapter_protocol():
    E = toy_env(); repo = mkrepo("r30", cfg={"toolchains": [{"adapter": "toy"}]})
    open(repo + "/toy.txt", "w").write("fine\n"); sh("git", "-C", repo, "add", "-A"); commit(repo, "toy")
    wt = xcfs("new", repo, "b", env=E)["worktree"]
    r = xcfs("build", wt, env=E); assert r["_code"] == 0 and r["toolchain"] == "toy" and r["count"] == 1, r
    assert not r["build_dir"].startswith(wt) and os.path.isfile(r["build_dir"] + "/out.txt"), "output inside the tree"
    assert sh("git", "-C", wt, "status", "--porcelain") == ""
    r = xcfs("build", wt, env=E); assert r["count"] == 2 and r["had_previous"], "resources were not kept: %s" % r
    r = xcfs("build", wt, env=dict(E, TOY_VERSION="2"))
    assert r["_code"] == 0 and not r["had_previous"] and any("changed" in w for w in r.get("warnings", [])), \
        "a toolchain change did not start cold: %s" % r
    open(wt + "/toy.txt", "w").write("broken\n")
    r = xcfs("build", wt, env=E); assert r["_code"] == 8 and r["diagnostics"][0]["file"] == wt + "/toy.txt", r
    assert xcfs("test", wt, env=E)["_code"] == 2, "an unsupported operation must be a usage error"
    sh("git", "-C", wt, "checkout", "--", "toy.txt")
    build_dir = os.path.dirname(r["build_dir"]) if "build_dir" in r else None
    d = xcfs("rm", wt, "--apply", env=E); assert d["_code"] == 0, d
    assert not any(k == wt for k in json.load(open(SB + "/state/registry.json"))["builds"]), "build record kept"


@test
def t31_init_suggests_a_config():
    repo = mkrepo("r31"); os.makedirs(repo + "/App.xcodeproj"); os.makedirs(repo + "/Pods/X")
    open(repo + "/.gitignore", "a").write("Pods/\n")
    r = xcfs("init", repo); c = r["config"]
    t = c["toolchains"][0]
    assert (t["adapter"], t["project"]) == ("xcode", "App.xcodeproj") and c["seed"] == ["Pods"], c
    assert t["configurations"]["sim-debug"]["configuration"] == "Debug", t
    assert not os.path.exists(repo + "/.xcfs.json")
    assert xcfs("init", repo, "--write")["_code"] == 0 and json.load(open(repo + "/.xcfs.json"))["seed"] == ["Pods"]
    assert xcfs("init", repo, "--write")["_code"] == 1 and xcfs("init", repo, "--write", "--force")["_code"] == 0


def nested_repo(repo, rel):
    sub = os.path.join(repo, rel); os.makedirs(sub)
    sh("git", "init", "-q", "-b", "main", sub); open(sub + "/lib.swift", "w").write("let x = 1\n")
    sh("git", "-C", sub, "add", "-A"); sh("git", "-C", sub, "-c", "user.name=t", "-c", "user.email=t@t.invalid", "commit", "-q", "-m", "i")
    open(repo + "/.gitignore", "a").write(rel.split("/")[0] + "/\n")
    sh("git", "-C", repo, "add", "-A"); commit(repo, "ignore nested")
    return os.path.realpath(sub)


@test
def t32_repository_set_moves_together():
    repo = mkrepo("r32", cfg=dict(CFG, repos=[{"path": "mods/lib"}]))
    sub = nested_repo(repo, "mods/lib"); sh("git", "-C", sub, "branch", "feat")
    r = xcfs("new", repo, "feat"); wt = r["worktree"]
    assert r["_code"] == 0 and r["repos"] == [".", "mods/lib"], r
    assert sh("git", "-C", wt + "/mods/lib", "branch", "--show-current") == "feat"
    assert len(worktrees(sub)) == 2 and sh("git", "-C", wt, "status", "--porcelain") == ""
    open(wt + "/mods/lib/lib.swift", "a").write("// wip\n")
    r = xcfs("rm", wt, "--apply"); assert r["_code"] == 5 and "mods/lib" in r["error"], r
    sh("git", "-C", wt + "/mods/lib", "checkout", "--", ".")
    r = xcfs("rm", wt, "--apply"); assert r["_code"] == 0 and wait_gone(wt), r
    assert len(worktrees(sub)) == 1 and len(worktrees(repo)) == 1
    other = xcfs("new", repo, "other")["worktree"]          # no such branch in the nested repo: detached at its baseline
    assert sh("git", "-C", other + "/mods/lib", "branch", "--show-current") == ""
    assert sh("git", "-C", other + "/mods/lib", "rev-parse", "HEAD") == sh("git", "-C", sub, "rev-parse", "main")
    assert xcfs("rm", other, "--apply", "--prune-branch")["_code"] == 0


@test
def t33_every_config_command_needs_trust():
    repo = mkrepo("r33", cfg=dict(CFG, prepare="true"))
    assert xcfs("new", repo, "b")["_code"] == 3 and xcfs("checkout", repo, "b")["_code"] == 3
    r = xcfs("trust", repo); assert r["commands"] == ["true"], r
    w = xcfs("new", repo, "b"); assert w["_code"] == 0, w
    xcfs("rm", w["worktree"], "--apply")


@test
def t34_dependency_snapshot_by_lock_files():
    repo = mkrepo("r34", cfg={"seed": ["deps"], "seedKey": ["deps.lock"]})
    open(repo + "/deps.lock", "w").write("v1\n"); sh("git", "-C", repo, "add", "-A"); commit(repo, "lock v1")
    v1 = sh("git", "-C", repo, "rev-parse", "HEAD")
    a = xcfs("new", repo, "a"); assert a["seed_origin"] == "source" and os.path.isdir(SB + "/cache/deps"), a
    open(repo + "/deps/marker", "w").write("two"); open(repo + "/deps.lock", "w").write("v2\n")
    sh("git", "-C", repo, "add", "deps.lock"); commit(repo, "lock v2")
    b = xcfs("new", repo, "b", "--from", v1)
    assert b["seed_origin"].startswith("snapshot") and not os.path.exists(b["worktree"] + "/deps/marker"), b
    ws = json.load(open(b["worktree"] + "/deps/workspace-state.json"))["object"]["dependencies"][0]["path"]
    assert ws == b["worktree"] + "/deps/checkouts/a", "paths from the snapshot were not rewritten: %s" % ws
    shutil.rmtree(SB + "/cache/deps")
    c = xcfs("new", repo, "c", "--from", v1)
    assert c["_code"] == 0 and any("does not match" in w for w in c.get("warnings", [])), c
    for w in (a, b, c):
        assert xcfs("rm", w["worktree"], "--apply")["_code"] == 0


@test
def t35_xcode_diagnostics_are_parsed():
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))
    from xcfs.adapters.xcode import parse_diagnostics
    f = SB + "/a b/File.swift"
    lines = [f + ":12:5: error: cannot find 'x' in scope\n", f + ":12:5: error: cannot find 'x' in scope\n",
             f + ":3:1: warning: unused\n", f + ":12:5: note: did you mean 'y'?\n", "error: Signing requires a team\n",
             "    error: indented continuation is not a diagnostic\n", "ld: warning: ignored\n", "** BUILD FAILED **\n"]
    d = parse_diagnostics(lines)
    assert [(x["severity"], x["line"]) for x in d] == [("error", 12), ("error", 0), ("warning", 3)], d
    assert d[0]["file"] == f and d[0]["column"] == 5 and d[1]["file"] is None
    from xcfs.adapters.xcode import XcodeAdapter, first_json_object
    from xcfs.adapter import Context
    os.environ["XCFS_DD_ROOT"] = SB + "/dd-mc"
    ad = XcodeAdapter()
    ctx = Context("worktree", SB, SB, {}, {"adapter": "xcode", "buildSettings": {"A": "1"}}, SB + "/b")
    bs = ad.build_settings(ctx, {"buildSettings": {"B": "2"}})
    assert bs["A"] == "1" and bs["B"] == "2" and bs["MODULE_CACHE_DIR"] == SB + "/dd-mc/ModuleCache.noindex", bs
    assert os.path.isfile(bs["CLANG_MODULES_BUILD_SESSION_FILE"]), "clang needs the build-session file to exist"
    assert bs["COMPILATION_CACHE_CAS_PATH"] == SB + "/dd-mc/CompilationCache.noindex" and bs["SDK_STAT_CACHE_DIR"] == SB + "/dd-mc", bs
    ctx.entry["sharedXcodeCaches"] = False
    assert "MODULE_CACHE_DIR" not in ad.build_settings(ctx, {}), "sharedXcodeCaches: false was ignored"
    del os.environ["XCFS_DD_ROOT"]
    out = 'Resolve Package Graph\n{ broken log line\nFetching {x}\n{"project": {"schemes": ["App"]}}\ntrailing'
    assert first_json_object(out) == {"project": {"schemes": ["App"]}}, first_json_object(out)


def toy_repo(name, slots=2, extra=None):
    cfg = dict({"toolchains": [{"adapter": "toy"}], "pool": {"slots": slots}}, **(extra or {}))
    repo = mkrepo(name, cfg=dict(CFG, **cfg))
    open(repo + "/toy.txt", "w").write("fine\n"); sh("git", "-C", repo, "add", "-A"); commit(repo, "toy")
    return repo


def slot_rows(repo=None):
    try:
        rows = json.load(open(SB + "/state/registry.json"))["slots"]
    except FileNotFoundError:
        return {}
    return {p: r for p, r in rows.items() if repo is None or r["project"] == repo}


def wait_for(pred, sec=30):
    t = time.time()
    while time.time() - t < sec:
        if pred():
            return True
        time.sleep(0.2)
    return False


@test
def t36_pool_lifecycle():
    E = toy_env(); repo = toy_repo("r36")
    r = xcfs("pool", "warm", repo, env=E); assert r["_code"] == 0 and len(r["slots"]) == 2 and all(x["ok"] for x in r["slots"]), r
    rows = slot_rows(repo); assert len(rows) == 2 and all(x["state"] == "idle" for x in rows.values()), rows
    src = sorted(rows.values(), key=lambda x: x["index"])[0]["src"]
    assert src.startswith(SB + "/pool/") and ".noindex/slot-1/r36" in src, src
    pdir = os.path.dirname(os.path.dirname(os.path.dirname(src)))
    assert "[Excluded]" in sh("tmutil", "isexcluded", os.path.dirname(os.path.dirname(src))), "pool not excluded from backups"
    assert sh("git", "-C", src, "branch", "--show-current") == "" and sh("git", "-C", src, "rev-parse", "HEAD") == sh("git", "-C", repo, "rev-parse", "main")
    c = xcfs("checkout", repo, "feat", "--session", "S", env=E)
    assert c["_code"] == 0 and c["warm_at_baseline"] and c["index"] == 1 and c["made_branch"], c
    wt = c["worktree"]; assert sh("git", "-C", wt, "branch", "--show-current") == "feat"
    commit(repo, "baseline moves")                                     # main moves on
    st = {x["index"]: x for x in xcfs("pool", "status", repo, env=E)["slots"]}
    assert st[1]["state"] == "leased" and st[1]["session"] == "S" and not st[2]["at_baseline"], st
    assert xcfs("pool", "warm", repo, env=E)["_code"] == 0
    s2 = [x for x in slot_rows(repo).values() if x["index"] == 2][0]
    assert sh("git", "-C", s2["src"], "rev-parse", "HEAD") == sh("git", "-C", repo, "rev-parse", "main"), "idle slot did not follow the baseline"
    assert xcfs("pool", "status", repo, env=E)["slots"][1]["at_baseline"]
    c2 = xcfs("checkout", repo, "feat2", "--session", "S", env=E); assert c2["_code"] == 0 and c2["index"] == 2, c2
    r = xcfs("checkout", repo, "feat3", env=E); assert r["_code"] == 10, "a full pool must say so: %s" % r
    assert "feat3" not in branches(repo)
    r = xcfs("build", wt, env=E); assert r["_code"] == 0 and r["kind"] == "slot", r
    open(wt + "/main.swift", "a").write("// wip\n")
    r = xcfs("release", wt, "--no-warm", env=E); assert r["_code"] == 5, r
    sh("git", "-C", wt, "checkout", "--", "."); commit(wt, "work on feat")
    r = xcfs("release", wt, "--no-warm", "--prune-branch", env=E); assert r["_code"] == 0 and r["released"], r
    assert "feat" in branches(repo), "a branch with commits was deleted"
    assert sh("git", "-C", wt, "branch", "--show-current") == "" and sh("git", "-C", wt, "status", "--porcelain") == ""
    r = xcfs("release", c2["worktree"], "--prune-branch", env=E)            # warms again in the background
    assert r["_code"] == 0 and "feat2" not in branches(repo), r
    assert wait_for(lambda: (lambda x: x["state"] == "idle" and x["warm"]["ok"])([v for v in slot_rows(repo).values() if v["index"] == 2][0])), "no background warm"
    c3 = xcfs("checkout", repo, "feat3", env=E); assert c3["_code"] == 0 and c3["warm"], c3
    assert xcfs("release", c3["worktree"], "--no-warm", "--prune-branch", env=E)["_code"] == 0
    return "2 slots: warm, lease, follow baseline, pool full, release"


@test
def t37_reap_returns_a_sessions_slots():
    E = toy_env(); repo = toy_repo("r37")
    a = xcfs("checkout", repo, "a", "--session", "R", env=E); b = xcfs("checkout", repo, "b", "--session", "R", env=E)
    assert a["_code"] == 0 and b["_code"] == 0 and not a["warm"], (a, b)       # no warm-up: slots made on demand
    open(b["worktree"] + "/main.swift", "a").write("// wip\n")
    r = xcfs("reap", "--session", "R", "--apply", "--prune-branch", env=dict(E, XCFS_SESSION="R"))
    assert r["_code"] == 0 and r["released"] == 1 and r["left"] == [b["slot"]], r
    st = {x["index"]: x["state"] for x in xcfs("pool", "status", repo, env=E)["slots"]}
    assert sorted(st.values()) == ["idle", "leased"] or sorted(st.values()) == ["cold", "leased"] or "warming" in st.values(), st
    sh("git", "-C", b["worktree"], "checkout", "--", ".")
    r = xcfs("reap", "--all", "--apply", "--prune-branch", env=E)
    assert r["released"] == 1 and "b" not in branches(repo), "reap --all did not return a clean slot: %s" % r


@test
def t38_resize_retires_idle_slots_only():
    E = toy_env(); repo = toy_repo("r38", slots=3)
    assert xcfs("pool", "warm", repo, env=E)["_code"] == 0 and len(slot_rows(repo)) == 3
    c = xcfs("checkout", repo, "x", env=E); assert c["index"] == 1
    r = xcfs("pool", "resize", repo, "0", env=E)
    assert r["_code"] == 0 and len(r["retired"]) == 2 and r["retire_on_release"] == [c["slot"]], r
    assert all(wait_gone(p) for p in r["retired"]) and len(worktrees(repo)) == 2
    r = xcfs("release", c["worktree"], "--prune-branch", env=E); assert r["action"] == "retired", r
    assert wait_gone(c["slot"]) and slot_rows(repo) == {} and len(worktrees(repo)) == 1


@test
def t39_gc_handles_stale_leases_and_interrupted_slots():
    E = toy_env(); repo = toy_repo("r39")
    a = xcfs("checkout", repo, "a", env=E); b = xcfs("checkout", repo, "b", env=E)
    open(b["worktree"] + "/main.swift", "a").write("// wip\n")
    d = json.load(open(SB + "/state/registry.json"))
    for k in (a["slot"], b["slot"]):
        d["slots"][k]["lease"]["active"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3 * 86400))
    json.dump(d, open(SB + "/state/registry.json", "w"))
    r = xcfs("gc", env=E); assert slot_rows(repo)[a["slot"]]["state"] == "leased", "dry-run returned a slot"
    r = xcfs("gc", "--apply", env=E); kinds = " | ".join(k for k, _ in r["actions"])
    assert "returned" in kinds and "NOT returned" in kinds, r["actions"]
    rows = slot_rows(repo); assert rows[a["slot"]]["state"] != "leased" and rows[b["slot"]]["state"] == "leased", rows
    sh("git", "-C", b["worktree"], "checkout", "--", "."); xcfs("release", b["worktree"], "--no-warm", env=E)
    p = subprocess.Popen([PY, XCFS, "pool", "warm", repo, "--slots", "3"], env=dict(E, TOY_SLEEP="1"),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert wait_for(lambda: any(x["state"] == "creating" for x in slot_rows(repo).values()) or p.poll() is not None, 15)
    p.kill(); p.wait()
    creating = [k for k, x in slot_rows(repo).items() if x["state"] in ("creating", "warming")]
    if creating:  # killed half way: gc puts it right
        assert xcfs("gc", "--apply", env=E)["_code"] == 0
        assert all(slot_rows(repo).get(k, {}).get("state") not in ("creating", "warming") for k in creating)
    return "interrupted: %d" % len(creating)


@test
def t40_checkout_refusals():
    E = toy_env(); repo = toy_repo("r40", slots=1)
    sh("git", "-C", repo, "branch", "busy"); plain = SB + "/r40-busy"; sh("git", "-C", repo, "worktree", "add", "-q", plain, "busy")
    r = xcfs("checkout", repo, "busy", env=E); assert r["_code"] == 1 and "already checked out" in r["error"], r
    c = xcfs("checkout", repo, "one", env=E); assert c["_code"] == 0
    t0 = time.time(); r = xcfs("checkout", repo, "two", "--wait", "3", env=E)
    assert r["_code"] == 10 and time.time() - t0 >= 3, r
    json.dump({"slotDiskGB": 10 ** 6}, open(SB + "/config/xcfs/settings.json", "w")) if os.path.isdir(SB + "/config/xcfs") else (os.makedirs(SB + "/config/xcfs"), json.dump({"slotDiskGB": 10 ** 6}, open(SB + "/config/xcfs/settings.json", "w")))
    try:
        r = xcfs("pool", "warm", repo, "--slots", "2", env=E); assert r["_code"] == 11, r
    finally:
        os.remove(SB + "/config/xcfs/settings.json")
    assert xcfs("release", c["worktree"], "--no-warm", "--prune-branch", env=E)["_code"] == 0
    assert xcfs("pool", "resize", repo, "0", env=E)["_code"] == 0


@test
def t41_slot_switch_runs_adapter_hooks_and_repository_set():
    E = toy_env(); repo = toy_repo("r41", slots=1, extra={"repos": [{"path": "mods/lib"}]})
    sub = nested_repo(repo, "mods/lib"); sh("git", "-C", sub, "branch", "topic")
    c = xcfs("checkout", repo, "topic", env=E); assert c["_code"] == 0, c
    assert sh("git", "-C", c["worktree"] + "/mods/lib", "branch", "--show-current") == "topic"
    reg_ = json.load(open(SB + "/state/registry.json"))
    sw = reg_["builds"][c["worktree"]]["resources"]["toy"].get("switches", 0); assert sw >= 1, reg_["builds"][c["worktree"]]
    r = xcfs("release", c["worktree"], "--no-warm", "--prune-branch", env=E); assert r["_code"] == 0, r
    assert sh("git", "-C", c["worktree"] + "/mods/lib", "branch", "--show-current") == "", "nested repository stayed on the branch"
    assert "topic" in branches(sub), "a nested branch xcfs did not create was deleted"
    assert xcfs("pool", "resize", repo, "0", env=E)["_code"] == 0 and len(worktrees(sub)) == 1


@test
def t42_parallel_checkouts_and_toolchain_change():
    E = toy_env(); repo = toy_repo("r42", slots=2)
    assert xcfs("pool", "warm", repo, env=E)["_code"] == 0
    ps = [subprocess.Popen([PY, XCFS, "checkout", repo, "p%d" % i, "--json"], env=E, stdout=subprocess.PIPE, text=True)
          for i in range(4)]
    outs = [json.loads(p.communicate()[0].strip().splitlines()[-1]) for p in ps]
    ok = [o for o in outs if o["ok"]]
    assert len(ok) == 2 and len({o["slot"] for o in ok}) == 2, outs
    assert all("in use" in o["error"] for o in outs if not o["ok"]), outs
    for o in ok:
        assert xcfs("release", o["worktree"], "--no-warm", "--prune-branch", env=E)["_code"] == 0
    c = xcfs("checkout", repo, "after-upgrade", env=dict(E, TOY_VERSION="9"))
    assert c["_code"] == 0 and c["warm"] and not c["warm_at_baseline"], "a slot built with another toolchain counted as ready: %s" % c
    r = xcfs("build", c["worktree"], env=dict(E, TOY_VERSION="9"))
    assert r["_code"] == 0 and not r["had_previous"], "old output was reused after a toolchain change: %s" % r
    assert xcfs("release", c["worktree"], "--no-warm", "--prune-branch", env=E)["_code"] == 0


FAKE_SWB = r"""#!/usr/bin/env python3
import json, os, sys, time
mode = open(os.environ["FAKE_MODE"]).read().strip()
sys.stdout.write("swbuild> "); sys.stdout.flush()
for line in sys.stdin:
    if line.startswith("quit"):
        break
    if mode == "hang-with-error":
        sys.stdout.write("Found multiple targets named 'App'"); sys.stdout.flush(); time.sleep(600)
    if mode == "silent":
        time.sleep(600)
    if mode.startswith("stat-cache-stall"):
        stall, mode = mode == "stat-cache-stall", "stat-cache-stall-done"
        evs = [{"kind": "taskStarted", "ruleInfo": "ClangStatCache /x/clang-stat-cache /sdk", "id": 2}]
        if stall:   # the first build: a stat cache that never finishes, until it is killed
            for ev in evs:
                s = json.dumps(ev); sys.stdout.write("%d\n%s\n" % (len(s), s)); sys.stdout.flush()
            import subprocess
            r = subprocess.run([os.environ["FAKE_STAT_CACHE"], "600"]).returncode
            evs = [{"kind": "taskComplete", "id": 2, "result": "failed"}, {"kind": "buildCompleted", "result": "failed"}]
        else:
            evs += [{"kind": "taskComplete", "id": 2, "result": "success"}, {"kind": "buildCompleted", "result": "ok"}]
        for ev in evs:
            s = json.dumps(ev); sys.stdout.write("%d\n%s\n" % (len(s), s)); sys.stdout.flush()
        sys.stdout.write("swbuild> "); sys.stdout.flush()
        continue
    for ev in ({"kind": "reportBuildDescription", "buildDescriptionID": "d1"},
               {"kind": "taskStarted", "ruleInfo": "SwiftCompile normal arm64 /x/A.swift", "id": 1},
               {"kind": "error", "message": "boom", "location": {"path": "/tmp/A.swift", "line": 3, "column": 7}},
               {"kind": "buildCompleted", "result": "failed"}):
        s = json.dumps(ev); sys.stdout.write("%d\n%s\n" % (len(s), s)); sys.stdout.flush()
    sys.stdout.write("swbuild> "); sys.stdout.flush()
"""


@test
def t43_session_failure_modes():
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))
    from xcfs.adapters import xcode_session as xs
    fake = SB + "/fake-swbuild"; open(fake, "w").write(FAKE_SWB); os.chmod(fake, 0o755)
    mode = SB + "/fake-mode"; env = dict(os.environ, FAKE_MODE=mode); log = open(SB + "/fake.log", "w")
    open(mode, "w").write("normal")
    sb = xs.Swbuild(fake, SB, env, log)
    r = sb.build("Debug", SB + "/req.json", SB + "/p.json", 20)
    assert r["result"] == "failed" and r["description"] == "d1" and r["compiled"] == 1, r
    assert r["diagnostics"][0]["line"] == 3 and r["diagnostics"][0]["message"] == "boom", r
    open(mode, "w").write("hang-with-error"); sb.restart()
    t0 = time.time(); r = sb.build("Debug", SB + "/req.json", SB + "/p.json", 60)
    assert r["result"] == "command-error" and "multiple targets" in r["message"] and time.time() - t0 < 10, r
    open(mode, "w").write("silent"); sb.restart()
    json.dump({}, open(SB + "/req.json", "w"))
    out = xs.handle(sb, {"pif": "p", "kind": "build", "request": SB + "/req.json", "configuration": "Debug", "timeout": 2}, {})
    assert out["result"] == "timeout" and out["attempts"] == 2, "a hung build was not restarted and retried: %s" % out
    open(mode, "w").write("normal"); sb.restart()
    last = {}
    xs.handle(sb, {"pif": "p", "kind": "build", "request": SB + "/req.json", "configuration": "Debug", "timeout": 20}, last)
    out = xs.handle(sb, {"pif": "p", "kind": "build", "request": SB + "/req.json", "configuration": "Debug", "timeout": 20}, last)
    assert out["reused_description"] and json.load(open(SB + "/req.json"))["buildDescriptionID"] == "d1", out
    json.dump({"parameters": {"overrides": {"commandLine": {"table": {"ARCHS": "arm64"}}}}}, open(SB + "/req.json", "w"))
    out = xs.handle(sb, {"pif": "p", "kind": "build", "request": SB + "/req.json", "configuration": "Debug", "timeout": 20}, last)
    assert not out["reused_description"], "a description was reused after the build settings changed: %s" % out
    sb.quit()
    took = time.time() - t0
    # A stat-cache task that never finishes is ended, and the build runs again.
    os.symlink("/bin/sleep", SB + "/clang-stat-cache")
    open(mode, "w").write("stat-cache-stall")
    sb = xs.Swbuild(fake, SB, dict(env, FAKE_STAT_CACHE=SB + "/clang-stat-cache"), log)
    saved, xs.STAT_CACHE_STALL = xs.STAT_CACHE_STALL, 1
    try:
        t0 = time.time()
        out = xs.handle(sb, {"pif": "p", "kind": "build", "request": SB + "/req.json", "configuration": "Debug",
                             "timeout": 60}, {})
    finally:
        xs.STAT_CACHE_STALL = saved
        sb.quit()
    assert out["result"] == "ok" and out["attempts"] == 2 and len(out["ended_stalled_processes"]) == 1, out
    assert time.time() - t0 < 15, "the stalled stat cache was not ended in time"
    return "error-then-hang detected in %.1fs" % took


def git_commit(path, msg):
    sh("git", "-C", path, "add", "-A"); sh("git", "-C", path, "-c", "user.name=t", "-c", "user.email=t@t.invalid", "commit", "-q", "-m", msg)


@test
def t44_submodules_come_along_locally():
    lib = SB + "/r44-lib"; sh("git", "init", "-q", "-b", "main", lib)
    open(lib + "/lib.c", "w").write("int v = 1;\n"); git_commit(lib, "v1")
    repo = toy_repo("r44", slots=1); E = toy_env()
    sh("git", "-C", repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", lib, "vendor/lib"); git_commit(repo, "add submodule")
    # a second submodule the source never checked out (optional, or private and unreachable): left alone
    sh("git", "-C", repo, "config", "-f", ".gitmodules", "submodule.private.path", "vendor/private")
    sh("git", "-C", repo, "config", "-f", ".gitmodules", "submodule.private.url", "git@example.com:nobody/private")
    sh("git", "-C", repo, "update-index", "--add", "--cacheinfo", "160000," + sh("git", "-C", lib, "rev-parse", "HEAD").strip() + ",vendor/private")
    git_commit(repo, "add a submodule the source does not check out")
    w = xcfs("new", repo, "sub", env=E); assert w["_code"] == 0, w; wt = w["worktree"]
    assert open(wt + "/vendor/lib/lib.c").read() == "int v = 1;\n"
    assert not os.path.isdir(wt + "/vendor/private") or not os.listdir(wt + "/vendor/private"), \
        "a submodule the source does not check out was initialized"
    assert os.path.realpath(sh("git", "-C", wt + "/vendor/lib", "remote", "get-url", "origin")) == repo + "/vendor/lib", \
        "the submodule was not cloned from the source checkout"
    assert sh("git", "-C", wt, "status", "--porcelain") == ""
    open(wt + "/vendor/lib/lib.c", "w").write("int v = 2;\n"); git_commit(wt + "/vendor/lib", "only here")
    sh("git", "-C", wt + "/vendor/lib", "checkout", "-q", "--detach")
    r = xcfs("rm", wt, "--apply", env=E); assert r["_code"] == 5, r      # the superproject sees the moved submodule
    git_commit(wt, "record the new submodule commit")
    r = xcfs("rm", wt, "--apply", env=E); assert r["_code"] == 5 and "only in this checkout" in r["error"], r
    sh("git", "-C", wt, "reset", "-q", "--hard", "HEAD~1"); sh("git", "-C", wt, "submodule", "update", "-q")
    assert xcfs("rm", wt, "--apply", "--prune-branch", env=E)["_code"] == 0
    # a branch whose submodule points at a newer commit (the source's copy has it): a slot follows it both ways
    open(lib + "/lib.c", "w").write("int v = 3;\n"); git_commit(lib, "v3")
    sh("git", "-C", repo + "/vendor/lib", "pull", "-q", "origin", "main")
    sh("git", "-C", repo, "checkout", "-q", "-b", "bump"); git_commit(repo, "bump submodule"); sh("git", "-C", repo, "checkout", "-q", "main")
    sh("git", "-C", repo, "submodule", "update", "-q")
    c = xcfs("checkout", repo, "bump", env=E); assert c["_code"] == 0, c
    assert open(c["worktree"] + "/vendor/lib/lib.c").read() == "int v = 3;\n", "the slot's submodule did not follow"
    r = xcfs("release", c["worktree"], "--no-warm", env=E); assert r["_code"] == 0, r
    assert open(c["worktree"] + "/vendor/lib/lib.c").read() == "int v = 1;\n", "the slot's submodule did not go back"
    assert xcfs("pool", "resize", repo, "0", env=E)["_code"] == 0


FAKE_BAZEL = r"""OB="$XCFS_BAZEL_USER_ROOT/$(python3 -c 'import hashlib,os;print(hashlib.md5(os.getcwd().encode()).hexdigest())')"
mkdir -p "$OB/server" "$OB/execroot" "$OB/external/ro/sub"; echo "WORKSPACE: $(pwd -P)" > "$OB/README"; echo x > "$OB/execroot/out.o"
echo x > "$OB/external/ro/sub/f"; chmod 555 "$OB/external/ro/sub" "$OB/external/ro"
if [ ! -f "$OB/server/server.pid.txt" ] || ! kill -0 "$(cat "$OB/server/server.pid.txt")" 2>/dev/null; then
  (cd "$PWD" && exec sleep 300) >/dev/null 2>&1 & echo $! > "$OB/server/server.pid.txt"
fi
[ -f FAIL ] && { echo "ERROR: $(pwd -P)/BUILD:3:1: broken target"; exit 1; }
echo built"""


@test
def t45_bazel_output_base_belongs_to_its_checkout():
    repo = mkrepo("r45", cfg=dict(CFG, toolchains=[{"adapter": "bazel", "build": FAKE_BAZEL, "stopServerOnRelease": True}], pool={"slots": 1}))
    open(repo + "/MODULE.bazel", "w").write("module(name = 'x')\n"); git_commit(repo, "bazel")
    import hashlib
    ob_of = lambda p: SB + "/bazel-root/" + hashlib.md5(p.encode()).hexdigest()
    wt = xcfs("new", repo, "b")["worktree"]
    r = xcfs("build", wt); assert r["_code"] == 0 and r["output_base"] == ob_of(wt), r
    pid = int(open(ob_of(wt) + "/server/server.pid.txt").read())
    open(wt + "/FAIL", "w").write(""); r = xcfs("build", wt); os.remove(wt + "/FAIL")
    assert r["_code"] == 8 and r["diagnostics"][0]["file"] == wt + "/BUILD" and r["diagnostics"][0]["line"] == 3, r
    r = xcfs("rm", wt, "--apply"); assert r["_code"] == 0 and r["caches"] == 1, r
    assert wait_gone(ob_of(wt)), "the output base outlived its worktree"
    assert wait_for(lambda: subprocess.run(["kill", "-0", str(pid)], capture_output=True).returncode != 0, 10), "server left running"
    c = xcfs("checkout", repo, "s"); src = c["worktree"]
    assert xcfs("build", src)["_code"] == 0 and os.path.isdir(ob_of(src))
    pid = int(open(ob_of(src) + "/server/server.pid.txt").read())
    assert xcfs("release", src, "--no-warm", "--prune-branch")["_code"] == 0
    assert wait_for(lambda: subprocess.run(["kill", "-0", str(pid)], capture_output=True).returncode != 0, 10), "server of a returned slot left running"
    assert os.path.isfile(ob_of(src) + "/execroot/out.o"), "a returned slot lost its build state"
    assert xcfs("pool", "resize", repo, "0")["_code"] == 0 and wait_gone(ob_of(src)), "a retired slot kept its output base"
    # by default a returned slot keeps its server (its cwd is the slot): that must not block the return
    d = json.load(open(repo + "/.xcfs.json")); d["toolchains"][0].pop("stopServerOnRelease"); json.dump(d, open(repo + "/.xcfs.json", "w"))
    assert xcfs("pool", "resize", repo, "1")["_code"] == 0
    c = xcfs("checkout", repo, "s2"); src = c["worktree"]
    assert xcfs("build", src)["_code"] == 0
    pid = int(open(ob_of(src) + "/server/server.pid.txt").read())
    assert subprocess.run(["kill", "-0", str(pid)], capture_output=True).returncode == 0, "the server died before release"
    r = xcfs("release", src, "--no-warm", "--prune-branch"); assert r["_code"] == 0, "the slot's own server blocked its return: %s" % r
    assert subprocess.run(["kill", "-0", str(pid)], capture_output=True).returncode == 0, "the server was stopped on release"
    r = xcfs("pool", "resize", repo, "0"); assert r["_code"] == 0 and r["retired"], r
    assert wait_gone(ob_of(src)) and wait_for(lambda: subprocess.run(["kill", "-0", str(pid)], capture_output=True).returncode != 0, 10)
    os.makedirs(SB + "/bazel-root/" + "0" * 32); open(SB + "/bazel-root/" + "0" * 32 + "/README", "w").write("WORKSPACE: /no/such/place\n")
    r = xcfs("gc", "--foreign"); assert any("workspace is gone" in k for k, _ in r["foreign"]), r["foreign"]


@test
def t46_session_can_skip_named_script_phases():
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))
    from xcfs.adapters.xcode_session import patch_pif
    sh_phase = lambda n: {"type": "com.apple.buildphase.shell-script", "name": n, "scriptContents": "true"}
    pif = [{"type": "workspace", "signature": "W1", "contents": {"projects": ["P1", "P2"]}},
           {"type": "project", "signature": "P1", "contents": {"targets": ["T1", "T2"]}},
           {"type": "project", "signature": "P2", "contents": {"targets": ["T3"]}},
           {"type": "target", "signature": "T1", "contents": {"buildPhases": [sh_phase("Lint"), sh_phase("Keep"),
                                                                             {"type": "com.apple.buildphase.sources"}]}},
           {"type": "target", "signature": "T2", "contents": {"buildPhases": [sh_phase("Keep")]}},
           {"type": "target", "signature": "T3", "contents": {"buildPhases": [sh_phase("Lint")]}}]
    path = SB + "/pif.json"; json.dump(pif, open(path, "w"))
    assert patch_pif(path, []) == (path, [])
    out, skipped = patch_pif(path, ["Lint", "Absent"])
    assert out != path and skipped == ["Lint"], (out, skipped)
    p = {e["signature"]: e for e in json.load(open(out))}
    sigs = set(p)
    t1 = [e for e in p.values() if e["type"] == "target" and len(e["contents"]["buildPhases"]) == 2][0]
    assert [ph.get("name") for ph in t1["contents"]["buildPhases"]] == ["Keep", None]
    assert "T2" in sigs and "T1" not in sigs and "T3" not in sigs, sigs        # changed targets get new signatures
    for e in p.values():                                                        # and every reference follows them
        for ref in e["contents"].get("targets", []) + e["contents"].get("projects", []):
            assert ref in sigs, "dangling reference %s" % ref
    assert "W1" not in sigs and "P1" not in sigs and "P2" not in sigs
    assert json.load(open(path))[0]["signature"] == "W1", "the original PIF was modified"
    assert patch_pif(path, ["Lint", "Absent"]) == (out, ["Lint"]), "a second call should reuse the patched PIF"



@test
def t47_experimental_swift_driver_follows_the_selected_xcode():
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))
    from xcfs.adapters.xcode import XcodeAdapter
    from xcfs.adapters import swift_driver as sd
    a = XcodeAdapter()
    for v in ("auto", "native", "api-digests"):
        assert a.validate({"adapter": "xcode", "swiftDriver": v}) == [], v
    assert a.validate({"adapter": "xcode", "swiftDriver": "fast"}), "an unknown swiftDriver value must be refused"
    v642 = sd.parse_version("swift-driver version: 1.168.6 Apple Swift version 6.4.2 (swiftlang-6.4.2.1.9 clang-2100.4.1.1)\n")
    assert v642["swift"] == "6.4.2" and v642["driver"] == "1.168.6" and v642["build"] == "swiftlang-6.4.2.1.9", v642
    assert sd.kit_for(v642)[0] == "swift-6.4.2-RELEASE"
    assert sd.parse_version("Apple Swift version 6.4.2 (swiftlang-6.4.2.1.9 clang-2100.4.1.1)\nTarget: arm64\n"
                            "swift-driver version: 1.168.6 ")["driver"] == "1.168.6", "stdout, then stderr"
    v641 = sd.parse_version("swift-driver version: 1.168.2 Apple Swift version 6.4.1 (swiftlang-6.4.1.2.3 clang-2100.3.1)")
    assert sd.kit_for(v641) == ("swift-6.4.1-RELEASE", sd.KITS["6.4"]), "a release of the same line uses its line's patch"
    assert sd.kit_for(sd.parse_version("Apple Swift version 6.5 (swiftlang-6.5.0.1.1 clang-2200)"))[0] is None
    assert "6.5" in sd.kit_for(sd.parse_version("swift-driver version: 1.170 Apple Swift version 6.5 (swiftlang-6.5.0.1.1 clang-2200)"))[1]
    ctx = lambda **e: type("Ctx", (), {"entry": dict(e, sharedXcodeCaches=False)})
    started = []
    saved = (os.environ.get("XCFS_CACHE"), os.environ.pop("XCFS_SWIFT_DRIVER", None), sd.start_build)
    os.environ["XCFS_CACHE"] = SB + "/cache-driver"
    sd.start_build = lambda: started.append(1) or SB + "/build.log"
    try:
        sd._selected = dict(v642, developer_dir="/Applications/XcodeA.app/Contents/Developer")
        root_a = sd.root()
        assert "6.4.2-swiftlang-6.4.2.1.9-" in root_a
        assert "SWIFT_EXEC" not in a.build_settings(ctx(), {}) and started == [1], "auto builds a missing driver"
        os.makedirs(root_a + "/bin")
        wrapper = root_a + "/bin/swiftc"
        open(wrapper, "w").write("#!/bin/sh\n"); os.chmod(wrapper, 0o755)
        json.dump({"swiftVersion": v642["release"]}, open(root_a + "/toolchain.json", "w"))
        assert "SWIFT_EXEC" not in a.build_settings(ctx(), {}), "not before its self-test passed"
        json.dump({"ok": False}, open(root_a + "/selftest.json", "w"))
        open(root_a + "/build-failed", "w").write("the driver failed its correctness self-test\n")
        n = len(started)
        assert "SWIFT_EXEC" not in a.build_settings(ctx(), {}) and len(started) == n, "a failed self-test: not retried"
        assert "self-test" in a.swift_driver_decision, a.swift_driver_decision
        os.remove(root_a + "/build-failed")
        json.dump({"ok": True}, open(root_a + "/selftest.json", "w"))         # passed an older self-test
        n = len(started)
        assert "SWIFT_EXEC" not in a.build_settings(ctx(), {}) and len(started) == n + 1, "the newer self-test runs first"
        json.dump({"ok": True, "version": sd.SELFTEST_VERSION}, open(root_a + "/selftest.json", "w"))
        s = a.build_settings(ctx(buildSettings={"OTHER": "1"}), {})
        assert s.get("SWIFT_EXEC") == wrapper and s.get("SWIFT_USE_INTEGRATED_DRIVER") == "NO" and s["OTHER"] == "1", s
        assert "SWIFT_EXEC" not in a.build_settings(ctx(swiftDriver="native"), {}), "native turns it off"
        os.environ["XCFS_SWIFT_DRIVER"] = "native"
        assert "SWIFT_EXEC" not in a.build_settings(ctx(swiftDriver="api-digests"), {}), "the environment overrides"
        del os.environ["XCFS_SWIFT_DRIVER"]
        sd._selected = dict(v641, developer_dir="/Applications/XcodeB.app/Contents/Developer")   # another Xcode
        n = len(started)
        assert sd.root() != root_a and "SWIFT_EXEC" not in a.build_settings(ctx(), {}) and len(started) == n + 1, \
            "another Xcode gets its own driver"
        sd._selected = dict(sd.parse_version("swift-driver version: 1.170 Apple Swift version 6.5 (swiftlang-6.5.0.1.1 x)"),
                            developer_dir="/Applications/XcodeC.app/Contents/Developer")
        n = len(started)
        assert "SWIFT_EXEC" not in a.build_settings(ctx(swiftDriver="api-digests"), {}) and len(started) == n
        assert "no patch for Swift 6.5" in a.swift_driver_decision, a.swift_driver_decision
        os.makedirs(sd.root(sd._selected), exist_ok=True)
        sd._selected = dict(v641, developer_dir="/Applications/XcodeB.app/Contents/Developer")
        os.makedirs(sd.root(), exist_ok=True)
        open(sd.root() + "/build-failed", "w").write("the patch does not apply to this release\n")
        assert "SWIFT_EXEC" not in a.build_settings(ctx(), {}) and len(started) == n, "no retry within a day"
        assert "does not apply" in a.swift_driver_decision
    finally:
        sd._selected, sd.start_build = None, saved[2]
        for k, v in (("XCFS_CACHE", saved[0]), ("XCFS_SWIFT_DRIVER", saved[1])):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@test
def t48_lint_only_script_phases_are_skipped_by_default():
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))
    from xcfs.adapters.xcode_session import lint_only, scripts_to_skip
    sh = lambda s, **k: dict({"type": "com.apple.buildphase.shell-script", "scriptContents": s}, **k)
    lint = sh('export PATH="$PATH:/opt/homebrew/bin"\nif which swiftlint >/dev/null; then\n  swiftlint\nelse\n'
              '  echo "warning: SwiftLint not installed"\nfi\n')
    assert lint_only(lint) and lint_only(sh('"${PODS_ROOT}/SwiftLint/swiftlint"\n'))
    assert lint_only(sh("swiftformat --lint .\n")) and lint_only(sh("swiftlint || true\n"))
    for keep in ("swiftlint --fix\n", "swiftformat .\n", "swiftlint > lint.txt\n", "swiftlint\nsourcery\n",
                 "./scripts/localize.sh\n", "X=$(make-something)\nswiftlint\n", ""):
        assert not lint_only(sh(keep)), keep
    assert not lint_only(dict(lint, outputFilePaths=["$(DERIVED_FILE_DIR)/x"])), "declared outputs: kept"
    pif = [{"type": "target", "contents": {"buildPhases": [dict(lint, name="Lint"), sh("./gen.sh", name="Gen"),
                                                           {"type": "com.apple.buildphase.sources"}]}}]
    assert scripts_to_skip(pif, "auto") == (["Lint"], "auto: linter-only phases")
    assert scripts_to_skip(pif, ["Gen"]) == (["Gen"], "configured"), "a configured list replaces the detection"
    assert scripts_to_skip(pif, []) == ([], "configured"), "an empty list keeps every phase"
    from xcfs.adapters.xcode import XcodeAdapter
    assert XcodeAdapter().validate({"adapter": "xcode", "skipScripts": "auto"}) == []
    assert XcodeAdapter().validate({"adapter": "xcode", "skipScripts": "all"})


@test
def t49_compile_tasks_count_with_either_swift_driver():
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))
    from xcfs.adapters.xcode_session import is_compile_rule
    for r in ("SwiftCompile normal arm64 /a/A.swift", "SwiftEmitModule normal arm64 Emitting\\ module\\ for\\ A",
              "CompileC /x/a.o /a/a.m normal arm64 objective-c", "CompileSwift normal arm64 /a/A.swift",
              "EmitSwiftModule normal arm64"):
        assert is_compile_rule(r), r
    for r in ("CompileSwiftSources normal arm64 com.apple.xcode.tools.swift.compiler", "Ld /x/A normal",
              "PhaseScriptExecution Lint /x.sh", "SwiftDriver A normal arm64"):
        assert not is_compile_rule(r), r


@test
def t50_session_check_is_redone_for_new_code_or_after_a_failure():
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))
    from xcfs.adapters.xcode_session import regression_current, check_id
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 2 * 86400))
    assert not regression_current(None)
    assert regression_current({"ok": True, "at": old, "check": check_id()}), "a passed check stays valid"
    assert not regression_current({"ok": True, "at": now, "check": "older-code"}), "another xcfs: check again"
    assert not regression_current({"ok": True, "at": now}), "a record from before checks were identified"
    assert regression_current({"ok": False, "at": now, "check": check_id()}), "a fresh failure is trusted"
    assert not regression_current({"ok": False, "at": old, "check": check_id()}), "a failure is retried after a day"


@test
def t51_package_resolution_options_reach_the_pif_export():
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))
    from xcfs.adapters.xcode_session import package_args
    assert package_args(["-scmProvider", "system", "-quiet", "-onlyUsePackageVersionsFromResolvedFile",
                         "-jobs", "4", "-packageCachePath", "/tmp/c"]) == \
        ["-scmProvider", "system", "-onlyUsePackageVersionsFromResolvedFile", "-packageCachePath", "/tmp/c"]
    assert package_args([]) == [] and package_args(["-quiet"]) == []


@test
def t52_metal_sources_found_through_the_project_description():
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))
    from xcfs.adapters.xcode_session import pif_has_metal
    pif = [{"type": "project", "contents": {"groupTree": {"children": [
        {"type": "group", "path": "Sources", "children": [{"type": "file", "path": "Effects/Shader.metal"}]}]}}}]
    assert pif_has_metal(pif), "a Metal file in a package or a nested repository counts"
    assert not pif_has_metal([{"type": "project", "contents": {"groupTree": {"children": [{"path": "A.swift"}]}}}])
    assert not pif_has_metal(None)


@test
def t53_session_restarts_when_its_environment_changes():
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))
    from xcfs.adapters import xcode_session as xs
    from xcfs.util import Fail
    d = SB + "/t53"; os.makedirs(d + "/session", exist_ok=True)
    ctx = type("Ctx", (), {"build_dir": d, "resources": {}})
    stopped = []
    saved = (xs.ping, xs.stop, xs.swbuild_path, xs.socket_path)
    xs.ping, xs.stop = (lambda sp: True), (lambda c: stopped.append(1))
    xs.swbuild_path, xs.socket_path = (lambda: None), (lambda c: d + "/s.sock")
    try:
        json.dump({"PATH": "/usr/bin"}, open(d + "/session/daemon-env.json", "w"))
        assert xs.ensure_daemon(None, ctx, d, {"PATH": "/usr/bin"}) == d + "/s.sock" and not stopped
        try:
            xs.ensure_daemon(None, ctx, d, {"PATH": "/usr/bin", "EXTERNAL_TOOLCHAINS_DIR": "/x"})
        except Fail:
            pass   # no swbuild here to start a new one; what matters is that the old one was stopped
        assert stopped == [1], "a session with another environment must be replaced"
    finally:
        xs.ping, xs.stop, xs.swbuild_path, xs.socket_path = saved


@test
def t54_driver_selftest_edits_are_consistent():
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib", "xcfs", "adapters", "swiftdriver"))
    saved = sys.argv
    sys.argv = ["selftest.py"]
    try:
        import selftest as st
    finally:
        sys.argv = saved
    from xcfs.adapters import swift_driver as sd
    assert st.VERSION == sd.SELFTEST_VERSION, "selftest.py and swift_driver.py must agree on the self-test version"
    files = dict(st.A_FILES)
    for label, rel, find, replace, b_builds in st.EDITS:
        assert find is None or find in files[rel], "%s: %r is not in %s at that point" % (label, find, rel)
        files[rel] = files[rel] + replace if find is None else files[rel].replace(find, replace, 1)
        if not b_builds:
            files[rel] = files[rel].replace(replace, find, 1)
    assert len(st.EDITS) >= 12, "the self-test covers at least the cases listed in its docstring"


@test
def t55_ignored_xcconfig_changes_reexport_the_pif():
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))
    from xcfs.adapters.xcode_session import pif_key
    r = SB + "/pifkey"
    os.makedirs(r + "/App")
    sh("git", "init", "-q", r)
    open(r + "/.gitignore", "w").write("Local.xcconfig\nPods/\n")
    open(r + "/App/A.swift", "w").write("let a = 1\n")
    open(r + "/Local.xcconfig", "w").write("OTHER_SWIFT_FLAGS = $(inherited)\n")
    os.makedirs(r + "/Pods/Target Support Files/Pods-App")
    pods = r + "/Pods/Target Support Files/Pods-App/Pods-App.debug.xcconfig"
    open(pods, "w").write("OTHER_LDFLAGS = -ObjC\n")

    class Adapter:
        def version(self): return "Xcode test"
        def container_args(self, ctx): return (["-project", "App.xcodeproj"], None)

    class Ctx:
        toolchain_root, entry = r, {}
    key = lambda: pif_key(Adapter(), Ctx(), "App")
    k0 = key()
    assert key() == k0, "the key is stable"
    open(r + "/App/A.swift", "w").write("let a = 2\n")
    assert key() == k0, "a source edit does not re-export the PIF"
    open(r + "/Local.xcconfig", "w").write("OTHER_SWIFT_FLAGS = $(inherited) -DLOCAL\n")
    k1 = key()
    assert k1 != k0, "a git-ignored xcconfig is read by the build service when it plans: its change must count"
    open(pods, "w").write("OTHER_LDFLAGS = -ObjC -lz\n")
    assert key() != k1, "so must a change to a generated (ignored) CocoaPods xcconfig"


@test
def t56_driver_release_tags_are_pinned_to_their_commits():
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))
    from xcfs.adapters import swift_driver as sd
    for line, kit in sd.KITS.items():
        assert kit.get("commits"), "the releases of %s are pinned" % line
        for tag, commit in kit["commits"].items():
            assert tag.startswith("swift-%s." % line) and re.fullmatch(r"[0-9a-f]{40}", commit), (tag, commit)
    # A mirror whose tag points at another commit: build.sh stops before applying or building anything.
    mirror = SB + "/driver-mirror"
    sh("git", "init", "-q", mirror)
    open(mirror + "/README", "w").write("not the release\n")
    sh("git", "-C", mirror, "add", "-A")
    sh("git", "-C", mirror, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "x")
    sh("git", "-C", mirror, "tag", "swift-6.4.2-RELEASE")
    kit = os.path.join(os.path.dirname(HERE), "lib", "xcfs", "adapters", "swiftdriver")
    root = SB + "/driver-install"
    r = subprocess.run(["sh", os.path.join(kit, "build.sh"), root, "swift-6.4.2-RELEASE", "api-digests-swift-6.4.2.patch",
                        "Package-swift-6.4.resolved", "1.168.6", sd.KITS["6.4"]["commits"]["swift-6.4.2-RELEASE"]],
                       capture_output=True, text=True, env=dict(os.environ, XCFS_SWIFT_DRIVER_REPO=mirror))
    assert r.returncode == 5, (r.returncode, r.stderr[-500:])
    assert "not building it" in r.stderr and not os.path.exists(root + "/bin"), r.stderr[-500:]
    assert 5 in sd.FAILURES


@test
def t57_agent_tool_worktree_hooks():
    E = toy_env(); repo = toy_repo("r57", slots=1)

    def hook(sub, data, env=E):
        r = subprocess.run([PY, XCFS, "hook", sub], input=json.dumps(data), capture_output=True, text=True, env=env)
        return r.returncode, r.stdout, r.stderr
    # no pool warmed: a new worktree on worktree-<name>, handed to the session; stdout is the path alone
    code, out, err = hook("worktree-create", {"name": "feat-a", "cwd": repo + "/deps", "session_id": "S57"})
    path = out.strip()
    assert code == 0 and out.count("\n") == 1 and os.path.isdir(path), (code, out, err)
    assert reg()[path]["branch"] == "worktree-feat-a" and reg()[path].get("session") == "S57", reg().get(path)
    # unsaved work: removal refuses, the tool keeps the worktree
    open(path + "/toy.txt", "a").write("wip\n")
    code, out, err = hook("worktree-remove", {"worktree_path": path})
    assert code != 0 and os.path.isdir(path) and out == "", (code, out, err)
    sh("git", "-C", path, "checkout", "--", ".")
    code, out, err = hook("worktree-remove", {"worktree_path": path})
    assert code == 0 and not os.path.exists(path) and "worktree-feat-a" not in branches(repo), (code, err)
    # a warm slot at the baseline is leased instead, and returned on removal
    assert xcfs("pool", "warm", repo, env=E)["_code"] == 0
    code, out, err = hook("worktree-create", {"name": "feat-b", "cwd": repo, "session_id": "S57"})
    path = out.strip()
    assert code == 0 and any(r["src"] == path for r in slot_rows(repo).values()), (code, out, err, slot_rows(repo))
    code, out, err = hook("worktree-remove", {"worktree_path": path})
    assert code == 0 and [r["state"] for r in slot_rows(repo).values()] != ["leased"], (code, err, slot_rows(repo))
    assert "worktree-feat-b" not in branches(repo)
    # outside a repository: a clear failure, nothing on stdout
    code, out, err = hook("worktree-create", {"name": "x", "cwd": SB})
    assert code != 0 and out == "" and "not in a git repository" in err, (code, out, err)


@test
def t58_test_takes_a_scheme_of_its_own():
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))
    from xcfs.adapter import Context
    from xcfs.adapters.xcode import XcodeAdapter
    seen = []

    class Recording(XcodeAdapter):
        def driver(self, ctx): return "xcodebuild"

        def xcodebuild(self, ctx, action, conf_name, conf, extra=(), destination=None, log_name=None, timeout=None,
                       dd=None):
            seen.append((action, self.scheme(ctx), self.derived_data(ctx, conf_name), destination))
            return {"ok": True, "_lines": ["Executed 3 tests, with 0 failures (0 unexpected)"]}
    entry = {"adapter": "xcode", "project": "App.xcodeproj", "scheme": "App",
             "configurations": {"sim": {"configuration": "Debug", "testDestination": "platform=iOS Simulator,name=X"}}}
    ctx = Context("worktree", SB, SB, {}, entry, SB + "/b58")
    r = Recording().test(ctx, {"scheme": "ModelsTests"})
    assert r["tests"] == 3 and seen[0][:2] == ("test", "ModelsTests"), (r, seen)
    Recording().test(Context("worktree", SB, SB, {}, entry, SB + "/b58"), {})
    assert seen[1][1] == "App" and seen[0][2] == seen[1][2], "another scheme must build into the same DerivedData: %s" % seen
    # Swift Testing prints its own summary next to XCTest's (which then says 0)
    lines = ["Executed 0 tests, with 0 failures (0 unexpected) in 0.000 (0.001) seconds",
             "\u2714 Test run with 16 tests in 0 suites passed after 0.105 seconds."]
    Recording.xcodebuild = lambda self, *a, **k: {"ok": True, "_lines": lines}
    r = Recording().test(Context("worktree", SB, SB, {}, entry, SB + "/b58"), {})
    assert r["tests"] == 16 and r["failures"] == 0 and r["failed"] == [], r
    lines = ["\u2718 Test displayNameFallsBackToHandle() failed after 0.002 seconds with 1 issue.",
             "\u2718 Test run with 16 tests in 0 suites failed after 0.105 seconds with 1 issue.",
             "Executed 2 tests, with 0 failures (0 unexpected) in 0.010 (0.011) seconds"]
    r = Recording().test(Context("worktree", SB, SB, {}, entry, SB + "/b58"), {})
    assert r["tests"] == 18 and r["failures"] == 1 and r["failed"] == ["displayNameFallsBackToHandle()"], r


@test
def t59_warm_leaves_a_current_slot_alone():
    E = toy_env(); repo = toy_repo("r59", slots=1)
    assert xcfs("pool", "warm", repo, env=E)["_code"] == 0
    r = xcfs("pool", "warm", repo, env=E)
    assert r["_code"] == 0 and r["slots"][0].get("already_warm"), "a slot warm at its baseline was warmed again: %s" % r
    c = xcfs("checkout", repo, "w59", env=E)
    assert c["_code"] == 0 and c["warm_at_baseline"], c
    assert xcfs("release", c["worktree"], "--no-warm", "--prune-branch", env=E)["_code"] == 0
    r = xcfs("pool", "warm", repo, env=E)
    assert r["_code"] == 0 and not r["slots"][0].get("already_warm"), "a slot used since its warm-up must be warmed: %s" % r
    r = xcfs("pool", "warm", repo, env=E)
    assert r["slots"][0].get("already_warm"), r
    r = xcfs("pool", "warm", repo, "--slot", slot_rows(repo) and list(slot_rows(repo))[0], env=E)
    assert r["_code"] == 0 and not r["slots"][0].get("already_warm"), "asked for one slot, it is warmed: %s" % r


@test
def t60_session_builds_the_tests_it_runs():
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))
    from xcfs.adapter import Context
    from xcfs.adapters import xcode_session as xs
    from xcfs.adapters.xcode import XcodeAdapter
    unit = "com.apple.product-type.bundle.unit-test"
    pif = [{"type": "target", "contents": {"name": n, "productTypeIdentifier": t}} for n, t in (
        ("App", "com.apple.product-type.application"), ("ModelsTests-product", unit), ("NetworkClientTests-product", unit),
        ("AppTests", unit))]
    root = SB + "/t60"
    ctx = Context("worktree", root, root, {}, {"adapter": "xcode"}, SB + "/b60")
    assert xs.test_targets(ctx, pif, "ModelsTests") == ["ModelsTests-product"]       # a package's test scheme
    assert xs.test_targets(ctx, pif, "NetworkClient") == ["NetworkClientTests-product"]   # a library's scheme
    assert xs.test_targets(ctx, pif, "Elsewhere") == []
    os.makedirs(root + "/A.xcodeproj/xcshareddata/xcschemes")
    open(root + "/A.xcodeproj/xcshareddata/xcschemes/App.xcscheme", "w").write(
        '<Scheme><TestAction><Testables><TestableReference skipped = "NO"><BuildableReference '
        'BlueprintName = "AppTests" BuildableName = "AppTests.xctest"></BuildableReference></TestableReference>'
        '</Testables></TestAction></Scheme>')
    assert xs.test_targets(ctx, pif, "App") == ["AppTests"], "a scheme file's testables come first"
    # with the session driver: the session builds, xcodebuild only runs, in the session's DerivedData
    calls = []

    class Session(XcodeAdapter):
        def driver(self, ctx): return "session"

        def xcodebuild(self, ctx, action, conf_name, conf, extra=(), destination=None, log_name=None, timeout=None, dd=None):
            calls.append((action, self.scheme(ctx), dd))
            return {"ok": True, "seconds": 5.0, "_lines": ["\u2714 Test run with 3 tests in 0 suites passed after 0.1 seconds."]}
    saved = xs.build_tests
    entry = {"adapter": "xcode", "scheme": "App", "configurations": {"sim": {"testDestination": "id=X"}}}
    try:
        xs.build_tests = lambda a, c, n, conf, args, scheme: {"ok": True, "seconds": 1.5, "derived_data": "/dd/swbuild",
                                                             "compile_tasks": 2, "test_targets": ["ModelsTests-product"]}
        r = Session().test(Context("worktree", root, root, {}, entry, SB + "/b60"), {"scheme": "ModelsTests"})
        assert calls[-1] == ("test-without-building", "ModelsTests", "/dd/swbuild") and r["tests"] == 3, (calls, r)
        assert r["built_by"] == "session" and r["seconds"] == 6.5, r
        xs.build_tests = lambda *a: {"ok": False, "seconds": 2.0, "derived_data": "/dd/swbuild",
                                     "diagnostics": [{"severity": "error", "message": "boom"}]}
        n = len(calls)
        r = Session().test(Context("worktree", root, root, {}, entry, SB + "/b60"), {"scheme": "ModelsTests"})
        assert not r["ok"] and len(calls) == n and r["diagnostics"][0]["message"] == "boom", "tests that do not build are not run"
        xs.build_tests = lambda *a: None
        r = Session().test(Context("worktree", root, root, {}, entry, SB + "/b60"), {"scheme": "Elsewhere"})
        assert calls[-1][0] == "test" and calls[-1][2] is None, "no test target found: xcodebuild builds and runs: %s" % calls
    finally:
        xs.build_tests = saved


@test
def t61_a_slot_warm_up_boots_its_simulator_once():
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))
    from xcfs.adapter import Context
    from xcfs.adapters import xcode as xa
    booted = []

    times = {}

    class Fake(xa.XcodeAdapter):
        def build(self, ctx, args):
            time.sleep(0.3); times["built"] = time.time()
            return {"ok": True, "seconds": 1.0, "diagnostics": []}
        def simulator(self, ctx):
            ctx.resources["simulator"] = "U1"
            return "U1"
        def boot(self, udid):
            times["boot"] = time.time(); booted.append(udid)
    saved = xa.run
    xa.run = lambda *a, **k: subprocess.CompletedProcess(a, 0, "", "")   # the shutdown
    try:
        entry = {"adapter": "xcode", "configurations": {"sim": {"destination": "generic/platform=iOS Simulator"}}}
        slot = Context("slot", SB, SB, {}, entry, SB + "/b61")
        r = Fake().warm(slot, {})
        assert r["ok"] and booted == ["U1"] and r["simulator_first_boot"] is not None, (r, booted)
        assert times["boot"] > times["built"], "without a shared cache the simulator boots after the build"
        assert Fake().warm(slot, {})["simulator_first_boot"] is None and booted == ["U1"], "booted a second time"
        wt = Context("worktree", SB, SB, {}, entry, SB + "/b61w")
        assert Fake().warm(wt, {})["simulator_first_boot"] is None, "only a slot's warm-up prepares a simulator"
        shared = Context("slot", SB, SB, {}, dict(entry, compileCache="all"), SB + "/b61s")
        assert Fake().warm(shared, {})["simulator_first_boot"] is not None and times["boot"] < times["built"], \
            "with a shared cache the simulator boots while the slot builds"
        mac = Context("slot", SB, SB, {}, {"adapter": "xcode", "configurations": {"m": {"destination": "platform=macOS"}}},
                      SB + "/b61m")
        n = len(booted)
        assert Fake().warm(mac, {})["simulator_first_boot"] is None and len(booted) == n, "no simulator for macOS"
    finally:
        xa.run = saved


@test
def t62_gc_reclaims_only_simulators_xcfs_made():
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))
    from xcfs.adapters import xcode as xa
    os.environ["XCFS_STATE"] = ENV["XCFS_STATE"]
    from xcfs import state
    with state.registry():   # exists however the tests are picked
        pass
    rec = xa.simulator_record()
    json.dump({"A": {"name": "xcfs a"}, "B": {"name": "xcfs b"}, "C": {"name": "xcfs c"}, "R": {"name": "xcfs r"}},
              open(rec, "w"))
    devs = [{"udid": "A", "name": "xcfs a"}, {"udid": "B", "name": "xcfs b"}, {"udid": "R", "name": "My renamed phone"},
            {"udid": "U", "name": "iPhone 18 Pro"}]          # C is gone; U is the user's own
    calls = []

    class Fake(xa.XcodeAdapter):
        def devices(self): return [d for d in devs if not any(c[-1] == d["udid"] and "delete" in c for c in calls)]
    saved = xa.run
    xa.run = lambda *a, **k: calls.append(a) or subprocess.CompletedProcess(a, 0, "", "")
    try:
        r = Fake().orphans([{"simulator": "A"}], apply=False)
        assert [x[1].split()[0] for x in r] == ["B"] and not calls, "dry run: list B only, delete nothing: %s %s" % (r, calls)
        r = Fake().orphans([{"simulator": "A"}], apply=True)
        deleted = [c[-1] for c in calls if "delete" in c]
        assert deleted == ["B"], "only the recorded, unused, still xcfs-named simulator goes: %s" % calls
        left = json.load(open(rec))
        assert set(left) == {"A", "R"}, "B (deleted) and C (gone) are forgotten; A (in use) and R (renamed) stay: %s" % left
    finally:
        xa.run = saved
    # gc passes what live checkouts hold to every adapter
    from xcfs import maint, adapter
    seen = {}

    class Probe(adapter.Adapter):
        name = "xcode"
        def orphans(self, in_use, apply):
            seen["in_use"], seen["apply"] = in_use, apply
            return [("simulator of a vanished checkout", "X")]
    reg_path = SB + "/state/registry.json"
    reg = json.load(open(reg_path))
    reg["builds"][SB] = {"dir": SB + "/nowhere", "resources": {"xcode": {"simulator": "LIVE"}}}
    reg["builds"][SB + "/gone"] = {"dir": SB + "/nowhere2", "resources": {"xcode": {"simulator": "DEAD"}}}
    json.dump(reg, open(reg_path, "w"))
    saved_all = maint.all_cache_adapters
    maint.all_cache_adapters = lambda cfg=None: [Probe()]
    try:
        from xcfs.cli import Args
        from xcfs.util import OUT
        OUT.json, OUT.result = True, {}
        maint.cmd_gc(Args(["--json"]))
        assert seen["in_use"] == [{"simulator": "LIVE"}] and seen["apply"] is False, seen
        assert ["simulator of a vanished checkout", "X"] in OUT.result["actions"], OUT.result
    finally:
        maint.all_cache_adapters = saved_all
        OUT.json, OUT.result = False, {}
        reg = json.load(open(reg_path))
        reg["builds"].pop(SB, None); reg["builds"].pop(SB + "/gone", None)
        json.dump(reg, open(reg_path, "w"))


@test
def t63_a_fresh_pool_warms_its_slots_side_by_side():
    E = toy_env(); repo = toy_repo("r63", slots=3)
    cfgdir = os.path.join(E.get("XDG_CONFIG_HOME", ENV.get("XDG_CONFIG_HOME", SB + "/config")), "xcfs")
    os.makedirs(cfgdir, exist_ok=True)
    settings = os.path.join(cfgdir, "settings.json")
    saved = open(settings).read() if os.path.exists(settings) else None
    try:
        for jobs, log in ((3, SB + "/t63-par.log"), (1, SB + "/t63-seq.log")):
            json.dump(dict(json.loads(saved) if saved else {}, warmJobs=jobs), open(settings, "w"))
            if jobs == 1:
                assert xcfs("pool", "resize", repo, "0", env=E)["_code"] == 0
                for p in list(slot_rows(repo)):
                    wait_gone(p)
            r = xcfs("pool", "warm", repo, "--slots", "3", env=dict(E, TOY_SLEEP="1.5", TOY_LOG=log))
            assert r["_code"] == 0 and len(r["slots"]) == 3 and all(x["ok"] for x in r["slots"]), r
            spans = sorted(tuple(map(float, l.split())) for l in open(log))
            overlap = any(spans[i + 1][0] < spans[i][1] for i in range(len(spans) - 1))
            assert len(spans) == 3 and overlap == (jobs > 1), "warmJobs=%d: builds %s" % (jobs, spans)
    finally:
        if saved is None:
            os.remove(settings)
        else:
            open(settings, "w").write(saved)


@test
def t64_parallel_warm_ups_share_state_safely():
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))
    import threading
    from xcfs.util import write_json, read_json
    target, errors = SB + "/t64/shared.json", []

    def writer(i):
        try:
            for k in range(50):
                write_json(target, {"writer": i, "k": k})
        except Exception as e:   # noqa: BLE001
            errors.append(e)
    ts = [threading.Thread(target=writer, args=(i,)) for i in range(6)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert not errors and read_json(target)["k"] == 49, "threads of one process lost a write: %s" % errors[:2]
    # three builds starting together run the session regression check once
    from xcfs.adapters import xcode_session as xs
    os.environ["XCFS_STATE"] = ENV["XCFS_STATE"]
    runs = []

    class A:
        def version(self): return "Xcode t64"

    def fake_regression(adapter):
        runs.append(1); time.sleep(0.5)
        rec = {"ok": True, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "check": xs.check_id()}
        data = read_json(xs.regression_file(), {}) or {}
        data[adapter.version()] = rec
        write_json(xs.regression_file(), data)
        return rec
    saved = (xs.regression, xs.swbuild_path, os.environ.pop("XCFS_DRIVER", None))
    xs.regression, xs.swbuild_path = fake_regression, (lambda: "/bin/true")
    try:
        class Ctx:
            entry = {}
        got = []
        ts = [threading.Thread(target=lambda: got.append(xs.choose_driver(A(), Ctx()))) for _ in range(3)]
        [t.start() for t in ts]; [t.join() for t in ts]
        assert got == ["session"] * 3 and len(runs) == 1, "the check ran %d times for %s" % (len(runs), got)
    finally:
        xs.regression, xs.swbuild_path = saved[0], saved[1]
        if saved[2] is not None:
            os.environ["XCFS_DRIVER"] = saved[2]


@test
def t65_driver_frontends_go_through_a_shared_gate():
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))
    from xcfs.adapters import swift_driver as sd
    tools = SB + "/t65/toolchain/usr/bin"
    os.makedirs(tools)
    for n in ("swift-frontend", "swift-symbolgraph-extract", "clang"):
        open(os.path.join(tools, n), "w").write("#!/bin/sh\n"); os.chmod(os.path.join(tools, n), 0o755)
    r = SB + "/t65/install"
    os.makedirs(r + "/bin")
    toolchain = {"frontend": tools + "/swift-frontend"}
    sd.refresh_wrapper(r, toolchain)
    sd.refresh_wrapper(r, toolchain)   # again: nothing changes, nothing fails
    w = open(r + "/bin/swiftc").read()
    assert 'XCFS_REAL_SWIFT_FRONTEND="%s"' % toolchain["frontend"] in w and "frontend-bin/swift-frontend" in w, w
    assert os.readlink(r + "/frontend-bin/swift-symbolgraph-extract") == tools + "/swift-symbolgraph-extract", \
        "the driver finds the other tools next to the frontend"
    gate = r + "/frontend-bin/swift-frontend"
    assert not os.path.islink(gate) and os.access(gate, os.X_OK)
    # one place: the second frontend starts when the first has ended
    log = SB + "/t65/runs.log"
    real = SB + "/t65/fake-frontend"
    open(real, "w").write("#!/bin/sh\necho start $(date +%s.%N 2>/dev/null || python3 -c 'import time;print(time.time())') >> " + log
                          + "\nsleep 1\n")
    os.chmod(real, 0o755)
    env = dict(os.environ, XCFS_REAL_SWIFT_FRONTEND=real, XCFS_FRONTEND_JOBS="1")
    t0 = time.time()
    ps = [subprocess.Popen([gate, "-frontend", "-c"], env=env) for _ in range(2)]
    [p.wait() for p in ps]
    assert all(p.returncode == 0 for p in ps) and time.time() - t0 >= 1.9, "two compile jobs ran at once with one place"
    t0 = time.time()
    ps = [subprocess.Popen([gate, "-frontend", "-emit-module"], env=env) for _ in range(2)]
    [p.wait() for p in ps]
    assert time.time() - t0 < 1.9, "emitting a module never waits"
    env["XCFS_FRONTEND_JOBS"] = "2"
    t0 = time.time()
    ps = [subprocess.Popen([gate, "-frontend", "-c"], env=env) for _ in range(2)]
    [p.wait() for p in ps]
    assert time.time() - t0 < 1.9, "two places: both at once"


@test
def t66_compile_cache_settings():
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))
    from xcfs.adapter import Context
    from xcfs.adapters import xcode as xa
    os.environ.pop("XCFS_COMPILE_CACHE", None)
    entry = {"adapter": "xcode", "project": "App.xcodeproj", "scheme": "App", "configurations": {"sim": {}}}
    ctx = lambda e: Context("slot", SB + "/slot-1/app", SB + "/app", {}, e, SB + "/slot-1/build")
    assert xa.compile_cache_settings(ctx(entry), {}) == {}, "off by default"
    dep = xa.compile_cache_settings(ctx(dict(entry, compileCache="dependencies")), {"OTHER_SWIFT_FLAGS": "-DX"})
    c = ctx(entry)
    assert dep["COMPILATION_CACHE_ENABLE_CACHING"] == "YES" and dep["SWIFT_ENABLE_PROJECT_PREFIX_MAPPING"] == "NO"
    assert dep["OTHER_SWIFT_FLAGS"].startswith("-DX "), "the project's own flags come first"
    assert "%s/App.xcodeproj=/^container" % c.toolchain_root in dep["OTHER_SWIFT_FLAGS"], dep
    assert "%s=/^build" % c.build_dir in dep["OTHER_SWIFT_FLAGS"] and "%s=/^checkout" % c.root not in dep["OTHER_SWIFT_FLAGS"]
    assert "-fdepscan-prefix-map=%s=/^build" % c.build_dir in dep["OTHER_CFLAGS"]
    full = xa.compile_cache_settings(ctx(dict(entry, compileCache="all")), {})
    assert full["OTHER_SWIFT_FLAGS"].startswith("$(inherited) ") and "%s=/^checkout" % c.root in full["OTHER_SWIFT_FLAGS"]
    # the experimental driver cannot use the cache: Xcode's own driver, said so
    a = xa.XcodeAdapter()
    assert a.swift_driver(dict(entry, compileCache="all")) is None and "compilation cache" in a.swift_driver_decision
    assert "compileCache must be" in " ".join(a.validate(dict(entry, compileCache="yes")))
    assert not [p for p in a.validate(dict(entry, compileCache="dependencies")) if "compileCache" in p]
    # a test failure's #filePath in the mapped form points back into the checkout
    assert xa.debug_hint(ctx(dict(entry, compileCache="all")), {})["lldb_source_map"] == \
        "settings set target.source-map /^checkout %s" % c.root
    assert "lldb_source_map" not in xa.debug_hint(ctx(dict(entry, compileCache="dependencies")), {})
    d = xa.unmap_paths(c, [{"file": "/^checkout/Tests/T.swift"}, {"file": "/^build/x.swift"}, {"file": "/elsewhere/y"}])
    assert [x["file"] for x in d] == [c.root + "/Tests/T.swift", c.build_dir + "/x.swift", "/elsewhere/y"], d


@test
def t67_a_shared_cache_warms_one_slot_first():
    E = toy_env(); repo = toy_repo("r67", slots=3)
    settings = os.path.join(ENV.get("XDG_CONFIG_HOME", SB + "/config"), "xcfs", "settings.json")
    os.makedirs(os.path.dirname(settings), exist_ok=True)
    saved = open(settings).read() if os.path.exists(settings) else None
    try:
        json.dump(dict(json.loads(saved) if saved else {}, warmJobs=3), open(settings, "w"))
        log = SB + "/t67.log"
        r = xcfs("pool", "warm", repo, "--slots", "3", env=dict(E, TOY_SLEEP="1.2", TOY_LOG=log, TOY_SHARES="1"))
        assert r["_code"] == 0 and all(x["ok"] for x in r["slots"]), r
        spans = sorted(tuple(map(float, l.split())) for l in open(log))
        assert len(spans) == 3 and spans[1][0] >= spans[0][1] and spans[2][0] >= spans[0][1], \
            "the others start once the first is built: %s" % spans
        assert spans[2][0] < spans[1][1], "and then side by side: %s" % spans
    finally:
        if saved is None:
            os.remove(settings)
        else:
            open(settings, "w").write(saved)


@test
def t68_init_detects_and_never_overwrites():
    r = SB + "/r68"
    sch = r + "/Shop.xcodeproj/xcshareddata/xcschemes"
    os.makedirs(sch)
    sh("git", "init", "-q", r)
    open(r + "/Shop.xcodeproj/project.pbxproj", "w").write("SDKROOT = iphoneos;\nSDKROOT = iphoneos;\n")
    app = '<Scheme><BuildAction><BuildActionEntries><BuildActionEntry><BuildableReference BuildableName = "%s">' \
          '</BuildableReference></BuildActionEntry></BuildActionEntries></BuildAction></Scheme>'
    open(sch + "/Widgets.xcscheme", "w").write(app % "Widgets.appex")
    open(sch + "/Shop.xcscheme", "w").write(app % "Shop.app")
    open(r + "/.gitignore", "w").write("Local.xcconfig\n")
    open(r + "/Local.xcconfig", "w").write("DEVELOPMENT_TEAM =\n")
    open(r + "/Secrets.xcconfig.template", "w").write("API_KEY =\n")
    sh("git", "-C", r, "add", "-A"); commit(r, "init")
    out = xcfs("init", r)
    t = out["config"]["toolchains"][0]
    assert t["project"] == "Shop.xcodeproj" and t["scheme"] == "Shop", t
    assert t["configurations"]["sim-debug"]["destination"] == "generic/platform=iOS Simulator", t
    assert out["config"]["carry"] == ["Local.xcconfig"], out["config"]
    assert any("Secrets.xcconfig.template" in n for n in out["notes"]), out["notes"]
    assert not os.path.exists(r + "/.xcfs.json") and out["written"] is None, "nothing is written without --write"
    w = xcfs("init", r, "--write")
    assert w["_code"] == 0 and os.path.isfile(r + "/.xcfs.json"), w
    again = xcfs("init", r, "--user")
    assert again["_code"] != 0 and "exists already" in again["error"], "an existing config is never overwritten"
    user = os.path.join(ENV.get("XDG_CONFIG_HOME", SB + "/config"), "xcfs", "r68.json")
    assert not os.path.exists(user)
    os.remove(r + "/.xcfs.json")
    u = xcfs("init", r, "--user")
    assert u["_code"] == 0 and os.path.isfile(user) and not os.path.exists(r + "/.xcfs.json"), u
    assert json.load(open(user))["toolchains"][0]["scheme"] == "Shop"
    assert xcfs("init", r, "--user", "--force")["_code"] == 0, "--force rewrites it"
    os.remove(user)
    # a macOS project: no simulator, no signing override
    open(r + "/Shop.xcodeproj/project.pbxproj", "w").write("SDKROOT = macosx;\n")
    m = xcfs("init", r)["config"]["toolchains"][0]
    assert m["configurations"] == {"debug": {"configuration": "Debug", "destination": "platform=macOS"}} and \
        "buildSettings" not in m, m
    pkg = SB + "/r68-package"
    os.makedirs(pkg); sh("git", "init", "-q", pkg)
    open(pkg + "/Package.swift", "w").write("// swift-tools-version: 5.9\nlet p = Package(name: \"P\", platforms: [.macOS(.v14)])\n")
    m = xcfs("init", pkg)["config"]["toolchains"][0]
    assert m["package"] == "." and m["configurations"]["debug"]["destination"] == "platform=macOS", m


@test
def t69_session_idle_time_is_a_setting():
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))
    from xcfs.adapters import xcode_session as xs
    d = SB + "/t69"; os.makedirs(d + "/session", exist_ok=True)
    ctx = type("Ctx", (), {"build_dir": d, "resources": {}})
    started, pings = [], []

    class P:
        pid = 4242
        def poll(self): return None

    def popen(argv, **kw):
        started.append(argv)
        return P()
    settings = os.path.join(ENV.get("XDG_CONFIG_HOME", SB + "/config"), "xcfs", "settings.json")
    os.makedirs(os.path.dirname(settings), exist_ok=True)
    saved_settings = open(settings).read() if os.path.exists(settings) else None
    saved = (xs.ping, xs.swbuild_path, xs.socket_path, xs.subprocess.Popen, os.environ.get("XDG_CONFIG_HOME"))
    xs.ping = lambda sp: pings.append(1) or len(pings) > 1   # nothing running at first, then the new one answers
    xs.swbuild_path, xs.socket_path = (lambda: "/bin/true"), (lambda c: d + "/s.sock")
    xs.subprocess.Popen = popen
    os.environ["XDG_CONFIG_HOME"] = ENV.get("XDG_CONFIG_HOME", SB + "/config")
    try:
        json.dump(dict(json.loads(saved_settings) if saved_settings else {}, sessionIdleMinutes=5), open(settings, "w"))
        xs.ensure_daemon(None, ctx, d, {"PATH": "/usr/bin"})
        assert started and started[0][-1] == "300", "the session is started with the configured idle time: %s" % started
        import inspect
        assert inspect.signature(xs.serve).parameters["idle_seconds"].default == 1800, "30 minutes by default"
    finally:
        xs.ping, xs.swbuild_path, xs.socket_path, xs.subprocess.Popen = saved[:4]
        if saved[4] is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = saved[4]
        if saved_settings is None:
            os.remove(settings)
        else:
            open(settings, "w").write(saved_settings)


@test
def t70_hooks_are_installed_and_removed_without_touching_others():
    home = SB + "/claude70"; os.makedirs(home)
    E = dict(toy_env(), CLAUDE_CONFIG_DIR=home)
    path = home + "/settings.json"
    mine = {"type": "command", "command": "say done"}
    old = "jq -r '.session_id // empty' | { read -r sid; [ -n \"$sid\" ] && xcfs reap --session \"$sid\" --apply; true; }"
    json.dump({"model": "x", "hooks": {"Stop": [{"hooks": [mine]}],
                                       "SessionEnd": [{"hooks": [mine, {"type": "command", "command": old}]}]}}, open(path, "w"))
    before = open(path).read()
    # a dry run by default: it says what it would do and writes nothing
    r = xcfs("hook", "install", env=E)
    assert r["_code"] == 0 and not r["applied"] and len(r["changes"]) == 3 and open(path).read() == before, r
    assert any(c.startswith("replace SessionEnd") for c in r["changes"]), r["changes"]
    r = xcfs("hook", "install", "--apply", env=E)
    got = json.load(open(path))
    assert r["applied"] and got["model"] == "x" and got["hooks"]["Stop"] == [{"hooks": [mine]}], got
    cmds = lambda ev: [h["command"] for g in got["hooks"].get(ev, []) for h in g["hooks"]]
    assert cmds("WorktreeCreate") == ["xcfs hook worktree-create"] and cmds("WorktreeRemove") == ["xcfs hook worktree-remove"]
    assert cmds("SessionEnd") == ["say done", "xcfs hook session-end"], "the earlier session-end line is replaced: %s" % cmds("SessionEnd")
    assert xcfs("hook", "status", env=E)["installed"] is True
    r = xcfs("hook", "install", "--apply", env=E)
    assert r["changes"] == [] and not r["applied"], "installing twice changes nothing: %s" % r
    # someone else's worktree hook: refused, nothing written
    got["hooks"]["WorktreeCreate"] = [{"hooks": [{"type": "command", "command": "other-tool make"}]}]
    json.dump(got, open(path, "w")); before = open(path).read()
    r = xcfs("hook", "install", "--apply", env=E)
    assert r["_code"] != 0 and "other-tool make" in r["error"] and open(path).read() == before, r
    # uninstall takes out xcfs's commands and nothing else
    r = xcfs("hook", "uninstall", "--apply", env=E)
    got = json.load(open(path))
    assert r["applied"] and cmds("WorktreeCreate") == ["other-tool make"] and cmds("SessionEnd") == ["say done"], got
    assert "WorktreeRemove" not in got["hooks"] and got["hooks"]["Stop"] == [{"hooks": [mine]}], got
    # a repository's own settings, created when missing; a settings file that is not JSON is left alone
    repo = mkrepo("r70")
    r = xcfs("hook", "install", repo, "--apply", env=E)
    assert r["applied"] and os.path.realpath(r["settings"]) == os.path.realpath(repo + "/.claude/settings.json"), r
    assert len(json.load(open(repo + "/.claude/settings.json"))["hooks"]) == 3
    open(path, "w").write("{ not json")
    r = xcfs("hook", "install", "--apply", env=E)
    assert r["_code"] != 0 and open(path).read() == "{ not json", r


@test
def t71_session_end_hook_returns_what_the_session_opened():
    E = toy_env(); repo = toy_repo("r71", slots=1)

    def hook(sub, data):
        r = subprocess.run([PY, XCFS, "hook", sub], input=data if isinstance(data, str) else json.dumps(data),
                           capture_output=True, text=True, env=E)
        return r.returncode, r.stdout, r.stderr
    clean = hook("worktree-create", {"name": "a", "cwd": repo, "session_id": "S71"})[1].strip()
    dirty = hook("worktree-create", {"name": "b", "cwd": repo, "session_id": "S71"})[1].strip()
    other = hook("worktree-create", {"name": "c", "cwd": repo, "session_id": "T71"})[1].strip()
    open(dirty + "/toy.txt", "a").write("wip\n")
    code, out, err = hook("session-end", {"session_id": "S71"})
    assert code == 0 and out == "", (code, out, err)
    assert not os.path.exists(clean) and os.path.isdir(dirty) and os.path.isdir(other), (clean, dirty, other)
    last = json.load(open(E["XCFS_STATE"] + "/last-reap.json"))
    assert last["session"] == "S71" and last["left"] == [dirty], last
    # it never fails: a session must be able to end
    assert hook("session-end", "not json")[0] == 0 and hook("session-end", {})[0] == 0


@test
def t72_uninstall_lists_then_removes_everything_but_unsaved_work():
    base = SB + "/u72"   # a state, cache and pool of its own: uninstall removes them whole
    E = dict(toy_env(), XCFS_STATE=base + "/state", XCFS_CACHE=base + "/cache", XCFS_POOL_ROOT=base + "/pool",
             CLAUDE_CONFIG_DIR=base + "/claude")
    repo = toy_repo("r72", slots=1)
    if xcfs("new", repo, "u72-probe", env=E)["_code"] == 3:
        assert xcfs("trust", repo, env=E)["_code"] == 0
    clean = xcfs("new", repo, "u72-a", env=E); assert clean["_code"] == 0, clean
    clean = clean["worktree"]
    dirty = xcfs("new", repo, "u72-b", env=E)["worktree"]
    assert xcfs("build", clean, env=E)["_code"] == 0
    assert xcfs("pool", "warm", repo, env=E)["_code"] == 0
    slot = xcfs("checkout", repo, "u72-c", env=E)["worktree"]
    assert xcfs("hook", "install", "--apply", env=E)["applied"]
    open(dirty + "/toy.txt", "a").write("wip\n")
    # a dry run by default: it lists, and removes nothing
    r = xcfs("uninstall", env=E)
    assert r["_code"] == 0 and not r["applied"] and clean in r["worktrees"] and slot in r["slots"] and len(r["hooks"]) == 3, r
    assert os.path.isdir(clean) and os.path.isdir(slot) and os.path.isdir(base + "/cache")
    # unsaved work is kept, and so is the state that knows about it
    r = xcfs("uninstall", "--apply", env=E)
    assert r["_code"] == 5 and [k["path"] for k in r["kept"]] == [dirty], r
    assert os.path.isdir(dirty) and not os.path.exists(clean) and not os.path.exists(slot), (clean, slot)
    assert dirty in json.load(open(base + "/state/registry.json"))["worktrees"]
    assert "wip" in open(dirty + "/toy.txt").read()
    # once the work is saved, everything goes: worktrees, pool, cache, state, hooks — not the repository or its config
    sh("git", "-C", dirty, "commit", "-qam", "saved")
    r = xcfs("uninstall", "--apply", env=E)
    assert r["_code"] == 0 and r["kept"] == [], r
    assert wait_for(lambda: not os.path.exists(dirty) and not os.path.exists(base + "/cache")), os.listdir(base)
    assert not os.path.exists(base + "/state") and wait_for(lambda: not os.listdir(base + "/pool") or
                                                             all(n.startswith(".") for n in os.listdir(base + "/pool")))
    assert "hooks" not in json.load(open(base + "/claude/settings.json"))
    assert os.path.isfile(repo + "/.xcfs.json") and "u72-b" in branches(repo), "the commit stays on its branch"
    # a directory that is not xcfs's own is never removed whole, wherever a variable points
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))
    from xcfs import maint
    assert not maint.own_dir(os.path.expanduser("~")) and not maint.own_dir("/") and not maint.own_dir("/Users")
    assert not maint.own_dir(os.path.dirname(os.path.expanduser("~")))


@test
def t73_first_use_hints_and_sizing_advice():
    E = toy_env(); repo = toy_repo("r73", slots=1)
    r = xcfs("checkout", repo, "h73", env=E)
    assert r["_code"] == 0 and not r["warm"] and "pool warm" in r["hint"] and "--slots 1" in r["hint"], r
    assert xcfs("release", r["worktree"], "--no-warm", env=E)["_code"] == 0
    assert xcfs("pool", "warm", repo, env=E)["_code"] == 0
    r = xcfs("checkout", repo, "h73b", env=E)
    assert r["_code"] == 0 and r["warm"] and r["hint"] is None, r
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))
    from xcfs import maint, state
    st = dict(state.DEFAULT_SETTINGS)
    small, big = maint.sizing_advice(st, mem=16), maint.sizing_advice(st, mem=64)
    assert "1 slot(s) at a time" in small[0] and any("less than 32 GB" in l for l in small), small
    assert "4 slot(s) at a time" in big[0] and not any("less than 32 GB" in l for l in big), big
    assert "2 slot(s) at a time" in maint.sizing_advice(dict(st, warmJobs=2), mem=16)[0]
    assert "advice" in xcfs("doctor", env=E)


@test
def t74_init_knows_generated_projects():
    r = SB + "/r74"; os.makedirs(r); sh("git", "init", "-q", r)
    open(r + "/project.yml", "w").write("name: Gen\n")
    open(r + "/Podfile.lock", "w").write("PODFILE CHECKSUM: 1\n")
    open(r + "/.gitignore", "w").write("*.xcodeproj\n*.xcworkspace\nPods/\n")
    sh("git", "-C", r, "add", "-A"); commit(r, "init")
    out = xcfs("init", r)   # not generated yet: nothing to detect, and a note saying what to run
    assert out["config"]["toolchains"] == [] and any("xcodegen generate" in n for n in out["notes"]), out
    for d in ("Gen.xcodeproj", "Gen.xcworkspace", "Pods"):
        os.makedirs(r + "/" + d)
    open(r + "/Gen.xcodeproj/project.pbxproj", "w").write("SDKROOT = iphoneos;\n")
    cfg = xcfs("init", r)
    c = cfg["config"]
    assert c["toolchains"][0]["workspace"] == "Gen.xcworkspace", c
    assert c["seed"] == ["Pods", "Gen.xcodeproj", "Gen.xcworkspace"] and c["seedKey"] == ["Podfile.lock", "project.yml"], c
    assert any("XcodeGen" in n and "afterSeed" in n for n in cfg["notes"]), cfg["notes"]
    t = SB + "/r74t"; os.makedirs(t + "/Tuist"); sh("git", "init", "-q", t)
    open(t + "/Project.swift", "w").write("import ProjectDescription\n")
    open(t + "/Tuist/Package.resolved", "w").write("{}\n")
    open(t + "/.gitignore", "w").write("*.xcodeproj\n*.xcworkspace\n")
    sh("git", "-C", t, "add", "-A"); commit(t, "init")
    assert any("tuist generate" in n for n in xcfs("init", t)["notes"])
    os.makedirs(t + "/App.xcodeproj")
    c = xcfs("init", t)["config"]
    assert c["seed"] == ["App.xcodeproj"] and c["seedKey"] == ["Tuist/Package.resolved", "Project.swift"], c


@test
def t75_pool_status_shows_a_warm_up_under_way():
    E = toy_env(); repo = toy_repo("r75", slots=1)
    first = dict(E, TOY_SLEEP="1.5", TOY_LOG=SB + "/toy75.log")
    assert xcfs("pool", "warm", repo, env=first)["_code"] == 0  # the first warm-up: its time is the estimate
    s = xcfs("checkout", repo, "w75", env=E)
    assert xcfs("release", s["worktree"], "--no-warm", env=E)["_code"] == 0
    slow = dict(E, TOY_SLEEP="4", TOY_LOG=SB + "/toy75.log")
    p = subprocess.Popen([PY, XCFS, "pool", "warm", repo, "--clean"], env=slow, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    try:
        assert wait_for(lambda: any(r.get("step") == "building" for r in xcfs("pool", "status", repo, env=E)["slots"]), 20)
        row = xcfs("pool", "status", repo, env=E)["slots"][0]
        assert row["state"] == "warming" and row["elapsed_seconds"] >= 0 and row["estimate_seconds"] >= 1, row
        w = subprocess.run([PY, XCFS, "pool", "status", repo, "--watch"], env=E, capture_output=True, text=True, timeout=60)
        assert w.returncode == 0 and "building" in w.stdout and "of about" in w.stdout, w.stdout[-600:]
    finally:
        p.wait(60)
    row = xcfs("pool", "status", repo, env=E)["slots"][0]
    assert row["state"] == "idle" and "step" not in row and row["warm"], row


@test
def t76_failures_say_what_to_do_next():
    E = toy_env(); repo = toy_repo("r76", slots=1)
    s = xcfs("checkout", repo, "n76", env=E)
    r = xcfs("checkout", repo, "n76b", env=E)
    assert r["_code"] == 10 and "--wait" in r["next"], r
    open(s["worktree"] + "/toy.txt", "w").write("BROKEN\n")
    r = xcfs("release", s["worktree"], env=E)
    assert r["_code"] == 5 and "never stashes" in r["next"], r
    h = subprocess.run([PY, XCFS, "release", s["worktree"]], env=E, capture_output=True, text=True)
    assert h.returncode == 5 and "next: commit the work" in h.stderr, h.stderr
    assert "next" not in xcfs("pool", "status", repo, env=E), "nothing to advise on success"
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))
    from xcfs.adapters.xcode import XcodeAdapter
    ad = XcodeAdapter()
    log = SB + "/x76.log"
    open(log, "w").write("...\nerror: module file '/dd/X.pcm' is out of date and needs to be rebuilt\n")
    assert "SwiftExplicitPrecompiledModules" in ad.advise({"ok": False, "log": log, "derived_data": "/dd"})
    assert "/dd" in ad.advise({"ok": False, "log": log, "derived_data": "/dd"})
    assert "CODE_SIGNING_ALLOWED" in ad.advise({"ok": False, "diagnostics": [
        {"message": "Signing for \"App\" requires a development team."}]})
    assert "simctl" in ad.advise({"ok": False, "error": "Unable to boot the Simulator."})
    assert ad.advise({"ok": False, "diagnostics": [{"message": "cannot find 'x' in scope"}]}) is None


@test
def t77_stats_from_the_local_history():
    E = dict(toy_env(), XCFS_STATE=SB + "/state77", XCFS_POOL_ROOT=SB + "/pool77")
    repo = toy_repo("r77", slots=1)
    if xcfs("new", repo, "s77-probe", env=E)["_code"] == 3:
        assert xcfs("trust", repo, env=E)["_code"] == 0
    assert xcfs("stats", env=E)["checkouts"] == 0
    s = xcfs("checkout", repo, "s77", env=E)            # cold
    assert xcfs("build", s["worktree"], env=E)["_code"] == 0
    assert xcfs("release", s["worktree"], env=E)["_code"] == 0
    assert xcfs("pool", "warm", repo, env=E)["_code"] == 0
    s = xcfs("checkout", repo, "s77b", env=E)           # built
    r = xcfs("stats", env=E)
    assert r["checkouts"] == 2 and r["checkouts_built"] == 1, r
    assert r["operations"]["build"]["count"] >= 1 and r["operations"]["warm"]["count"] >= 1, r
    assert xcfs("stats", "--days", "0", env=E)["checkouts"] == 0
    lines = open(SB + "/state77/history.jsonl").read().splitlines()
    assert all(set(json.loads(l)) <= {"event", "at", "kind", "ok", "seconds", "driver", "warm", "at_baseline",
                                      "full_build_seconds"} for l in lines), "no paths or names in the history"


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
