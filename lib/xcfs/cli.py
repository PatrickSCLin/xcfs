"""xcfs — worktrees, a pool of pre-built worktrees and build sessions for parallel (agent) development on macOS.

Worktrees
  xcfs new <repo> <branch> [dest] [--from REF] [--session ID]   worktree (+ the config's repository set) with the
                                                                dependency layer cloned in (APFS clonefile)
  xcfs rm <worktree> [--apply] [--discard-ignored] [--prune-branch]   remove it and every cache it owns
  xcfs reap [--session ID | --older-than DAYS | --all] [--apply] [--prune-branch]
                                                                remove the CLEAN worktrees (and return the clean pool
                                                                slots) of a session / of that age
  xcfs adopt <worktree> [--session ID | --release]              hand a worktree to a session, take one made with plain
                                                                `git worktree add` under management, or exempt it from reap
  xcfs ls [--session ID] [--status] [--sizes]                   worktrees xcfs manages

Pool (slots at fixed paths, kept built at their baseline)
  xcfs checkout <repo> <branch> [--from REF] [--session ID] [--wait SECONDS] [--build]
                                                                lease a slot and switch it to <branch>; prints its path
  xcfs release [path] [--prune-branch] [--discard-ignored] [--no-warm]
                                                                return the slot (refuses if anything would be lost)
  xcfs pool status [repo]                                       slots, leases, warm state
  xcfs pool warm <repo> [--slots N] [--slot PATH] [--clean] [--fetch] [--background]
                                                                create missing slots; bring idle ones to their baseline
                                                                and build them
  xcfs pool resize <repo> N                                     change the number of slots (idle extras are retired)

Building (in a worktree, a slot, or any checkout; output never inside the source tree)
  xcfs build [path] [--config NAME] [--toolchain NAME] [--timeout SECONDS]
  xcfs check <file>... [--config NAME]                          compile-check these files
  xcfs test [path] [--config NAME] [--only TARGET/CLASS[/METHOD]]...
  xcfs run [path] [--config NAME]                               build, install on the checkout's simulator, launch

Maintenance
  xcfs gc [--apply] [--foreign] [--stale-days N]                reclaim what xcfs owns; --foreign also looks at caches
                                                                it did not create (a toolchain's global caches)
  xcfs doctor [path] [--session-check]                          environment, hygiene and toolchain checks
  xcfs init [repo] [--write] [--force]                          suggest a config (.xcfs.json) for a repository
  xcfs trust <repo>                                             approve the commands a repository's config runs

Every command takes --json and then prints exactly one JSON object. Nothing is deleted without --apply.

The session id comes from --session, else $XCFS_SESSION, else $CLAUDE_CODE_SESSION_ID. An agent session that ends runs
    xcfs reap --session <id> --apply --prune-branch

Project knowledge lives in a config, never in xcfs: <repo>/.xcfs.json, else $XDG_CONFIG_HOME/xcfs/<repo name>.json.
Machine settings: $XDG_CONFIG_HOME/xcfs/settings.json (poolRoot, slots, slotDiskGB, leaseHours, warmOnRelease,
buildTimeout). State: $XCFS_STATE, else $XDG_STATE_HOME/xcfs, else ~/.local/state/xcfs.

Exit codes: 0 ok · 1 error · 2 usage · 3 config not trusted · 4 not one APFS volume · 5 uncommitted work · 6 in use
            7 ignored files would be lost · 8 build or tests failed · 9 timed out · 10 no free slot · 11 disk full
"""
import json, os, sys

from . import VERSION, builds, config, maint, pool, state, worktree
from .util import Fail, OUT, USAGE, phys, say


class Args:
    VALUE = {"session", "from", "stale-days", "older-than", "config", "toolchain", "timeout", "only", "slot", "slots",
             "wait"}
    MULTI = {"only"}
    FLAGS = {"apply", "dry-run", "json", "discard-ignored", "foreign", "sizes", "status", "all", "prune-branch",
             "release", "write", "force", "clean", "fetch", "background", "no-warm", "build", "help", "session-check"}

    def __init__(self, argv):
        self.pos, self.opt, self.flags = [], {}, set()
        i = 0
        while i < len(argv):
            x = argv[i]
            if x.startswith("--") and len(x) > 2:
                k, eq, v = x[2:].partition("=")
                if k in self.VALUE:
                    if not eq:
                        if i + 1 >= len(argv):
                            raise Fail("--%s needs a value" % k, USAGE)
                        v = argv[i + 1]
                        i += 1
                    if k in self.MULTI:
                        self.opt.setdefault(k, []).append(v)
                    else:
                        self.opt[k] = v
                elif k in self.FLAGS and not eq:
                    self.flags.add(k)
                else:
                    raise Fail("unknown flag: --%s" % k, USAGE)
            elif x == "-h":
                self.flags.add("help")
            else:
                self.pos.append(x)
            i += 1

    def flag(self, k):
        return k in self.flags

    def session(self):
        return self.opt.get("session") or os.environ.get("XCFS_SESSION") or os.environ.get("CLAUDE_CODE_SESSION_ID", "")

    def result(self, **kw):
        OUT.result.update(kw)

    def num(self, k, default=None, kind=int):
        if k not in self.opt:
            return default
        try:
            return kind(self.opt[k])
        except ValueError:
            raise Fail("--%s needs a number" % k, USAGE)


def timeout_of(a):
    return a.num("timeout", state.settings()["buildTimeout"], float)


def build_like(op, a, path, extra=None):
    args = dict({"config": a.opt.get("config"), "timeout": timeout_of(a)}, **(extra or {}))
    res = builds.operate(op, path, args, a.opt.get("toolchain"))
    a.result(**res)
    return builds.report(res, op)


def cmd_build(a):
    return build_like("build", a, a.pos[0] if a.pos else ".")


def cmd_check(a):
    if not a.pos:
        raise Fail("usage: xcfs check <file>... [--config NAME]", USAGE)
    files = [phys(f) for f in a.pos]
    return build_like("check", a, os.path.dirname(files[0]), {"files": files})


def cmd_test(a):
    return build_like("test", a, a.pos[0] if a.pos else ".", {"only": a.opt.get("only", [])})


def cmd_run(a):
    return build_like("run", a, a.pos[0] if a.pos else ".")


def cmd_checkout(a):
    if len(a.pos) < 2:
        raise Fail("usage: xcfs checkout <repo> <branch> [--from REF] [--session ID] [--wait SECONDS] [--build]", USAGE)
    repo = config.toplevel(phys(a.pos[0]))
    repo = config.main_checkout(repo)
    res = pool.checkout(repo, a.pos[1], a.opt.get("from"), a.session(), state.settings(), a.num("wait", 0, float))
    a.result(**res)
    if not res["session"]:
        say("  no session id: `xcfs reap --session` will not return this slot")
    if a.flag("build"):
        r = builds.operate("build", res["worktree"], {"config": a.opt.get("config"), "timeout": timeout_of(a)})
        a.result(build=r)
        return builds.report(r, "build")


def cmd_release(a):
    slot, row = pool.find_slot(a.pos[0] if a.pos else ".")
    a.result(**pool.release(slot, row, state.settings(), a.flag("prune-branch"), a.flag("discard-ignored"),
                            rewarm=not a.flag("no-warm")))


def cmd_pool(a):
    sub = a.pos[0] if a.pos else "status"
    rest = a.pos[1:]
    settings = state.settings()
    if sub == "status":
        repo = config.main_checkout(config.toplevel(phys(rest[0]))) if rest else None
        a.result(**pool.status(repo))
    elif sub == "warm":
        if not rest:
            raise Fail("usage: xcfs pool warm <repo> [--slots N] [--slot PATH] [--clean] [--fetch] [--background]", USAGE)
        repo = config.main_checkout(config.toplevel(phys(rest[0])))
        cfg = config.load_config(repo)
        slot = pool.find_slot(a.opt["slot"])[0] if a.opt.get("slot") else None
        if a.flag("background"):
            config.require_trust(repo, cfg)
            argv = ["pool", "warm", repo] + (["--slot", slot] if slot else []) + \
                   (["--slots", a.opt["slots"]] if "slots" in a.opt else []) + \
                   [f for f in ("--clean", "--fetch") if a.flag(f[2:])]
            from .util import self_argv, spawn_detached
            log = state.log_file("warm-%s" % os.path.basename(repo))
            pid = spawn_detached(self_argv() + argv, log, dict(os.environ, XCFS_BACKGROUND="1"))
            say("warming in the background (pid %d, log %s)" % (pid, log))
            a.result(pid=pid, log=log)
            return
        if os.environ.get("XCFS_BACKGROUND"):
            os.nice(10)  # background warm-ups yield the CPU to builds an agent is waiting for
        results = pool.warm(repo, cfg, settings, slot, a.num("slots"), a.flag("clean"), a.flag("fetch"), timeout_of(a))
        a.result(slots=results)
        if any(not r["ok"] for r in results):
            raise Fail("%d slot(s) failed to warm" % sum(1 for r in results if not r["ok"]), 8)
    elif sub == "resize":
        if len(rest) < 2 or not rest[1].isdigit():
            raise Fail("usage: xcfs pool resize <repo> N", USAGE)
        repo = config.main_checkout(config.toplevel(phys(rest[0])))
        a.result(**pool.resize(repo, int(rest[1]), config.load_config(repo, validate=False), settings))
    else:
        raise Fail("unknown pool command: %s (status, warm, resize)" % sub, USAGE)


COMMANDS = {"new": worktree.cmd_new, "rm": worktree.cmd_rm, "reap": worktree.cmd_reap, "adopt": worktree.cmd_adopt,
            "ls": worktree.cmd_ls, "gc": maint.cmd_gc, "doctor": maint.cmd_doctor, "init": maint.cmd_init,
            "trust": maint.cmd_trust, "build": cmd_build, "check": cmd_check, "test": cmd_test, "run": cmd_run,
            "checkout": cmd_checkout, "release": cmd_release, "pool": cmd_pool}


def main(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0
    if argv[0] in ("--version", "version"):
        print("xcfs " + VERSION)
        return 0
    name, code = argv[0], 0
    OUT.json = "--json" in argv
    try:
        if name not in COMMANDS:
            raise Fail("unknown command: %s (see xcfs --help)" % name, USAGE)
        a = Args(argv[1:])
        if a.flag("help"):
            print(__doc__.strip())
            return 0
        OUT.json = a.flag("json")
        code = COMMANDS[name](a) or 0
    except Fail as e:
        code = e.code
        OUT.result["error"] = str(e)
        OUT.result.update(e.extra)
        if not OUT.json:
            print("error: %s" % e, file=sys.stderr)
    except KeyboardInterrupt:
        code = 130
        OUT.result["error"] = "interrupted"
    except Exception as e:  # a bug in xcfs: still answer in the promised format, never with a bare traceback
        import traceback
        code = 1
        OUT.result["error"] = "internal error: %s: %s" % (type(e).__name__, e)
        OUT.result["traceback"] = traceback.format_exc().splitlines()[-3:]
        if not OUT.json:
            print("error: %s\n%s" % (OUT.result["error"], traceback.format_exc()), file=sys.stderr)
    if OUT.json:
        OUT.result["ok"] = code == 0
        print(json.dumps(OUT.result, ensure_ascii=False))
    return code
