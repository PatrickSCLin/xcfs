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
    src = open(os.path.join(ctx["toolchainRoot"], "toy.txt")).read()
    os.makedirs(ctx["buildDir"], exist_ok=True)
    marker = os.path.join(ctx["buildDir"], "out.txt"); had = os.path.exists(marker)
    open(marker, "w").write(src)
    res = dict(ctx["resources"]); res["count"] = res.get("count", 0) + 1
    if "broken" in src:
        out(ok=False, seconds=0.01, resources=res, diagnostics=[{"file": os.path.join(ctx["toolchainRoot"], "toy.txt"),
            "line": 1, "column": 1, "severity": "error", "message": "broken"}])
    out(ok=True, seconds=0.01, diagnostics=[], resources=res, count=res["count"], had_previous=had, build_dir=ctx["buildDir"])
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
    assert c["toolchains"] == [{"adapter": "xcode", "project": "App.xcodeproj"}] and c["seed"] == ["Pods"], c
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
    return "error-then-hang detected in %.1fs" % (time.time() - t0)


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
def t47_experimental_swift_driver_auto_only_when_built_for_this_swift():
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))
    from xcfs.adapters.xcode import XcodeAdapter
    from xcfs.adapters import swift_driver as sd
    a = XcodeAdapter()
    for v in ("auto", "native", "api-digests"):
        assert a.validate({"adapter": "xcode", "swiftDriver": v}) == [], v
    assert a.validate({"adapter": "xcode", "swiftDriver": "fast"}), "an unknown swiftDriver value must be refused"
    ctx = lambda **e: type("Ctx", (), {"entry": dict(e, sharedXcodeCaches=False)})
    have = "Apple Swift version %s (swiftlang-x)" % sd.SWIFT
    started = []
    saved = (os.environ.get("XCFS_CACHE"), os.environ.pop("XCFS_SWIFT_DRIVER", None), sd.start_build)
    os.environ["XCFS_CACHE"] = SB + "/cache-driver"
    sd.start_build = lambda force=False: started.append(force) or SB + "/build.log"
    try:
        sd._swift = have
        assert "SWIFT_EXEC" not in a.build_settings(ctx(), {}), "not built yet: Xcode's own driver"
        assert started == [True], "auto mode starts building the driver for a matching Swift"
        assert a.swift_driver_decision.startswith("native: building it"), a.swift_driver_decision
        root = sd.root()
        os.makedirs(root + "/bin")
        wrapper = root + "/bin/swiftc"
        open(wrapper, "w").write("#!/bin/sh\n"); os.chmod(wrapper, 0o755)
        json.dump({"swiftVersion": "swift-driver version: 1.0 " + have}, open(root + "/toolchain.json", "w"))
        s = a.build_settings(ctx(buildSettings={"OTHER": "1"}), {})
        assert s.get("SWIFT_EXEC") == wrapper and s.get("SWIFT_USE_INTEGRATED_DRIVER") == "NO" and s["OTHER"] == "1", s
        assert a.swift_driver_decision == "api-digests"
        assert "SWIFT_EXEC" not in a.build_settings(ctx(swiftDriver="native"), {}), "native turns it off"
        os.environ["XCFS_SWIFT_DRIVER"] = "native"
        assert "SWIFT_EXEC" not in a.build_settings(ctx(swiftDriver="api-digests"), {}), "the environment overrides"
        del os.environ["XCFS_SWIFT_DRIVER"]
        sd._swift = have.replace("swiftlang-x", "swiftlang-y")       # same Swift, another build: rebuild, not use
        assert "SWIFT_EXEC" not in a.build_settings(ctx(), {}) and len(started) == 2
        sd._swift = "Apple Swift version 9.9 (swiftlang-z)"          # another release: never built, never used
        assert "SWIFT_EXEC" not in a.build_settings(ctx(swiftDriver="api-digests"), {}) and len(started) == 2
        assert "9.9" in a.swift_driver_decision
    finally:
        sd._swift, sd.start_build = None, saved[2]
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
