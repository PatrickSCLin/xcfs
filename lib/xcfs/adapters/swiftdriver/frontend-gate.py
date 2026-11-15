#!/usr/bin/python3 -I
"""swift-frontend for the experimental driver: run the real frontend once one of N machine-wide places is free.

Xcode's integrated driver lets the build service schedule every compile job, so a build never runs more frontends
than it has lanes. The experimental driver runs as a separate `swiftc` per target, and each one starts as many
frontends as there are cores: several targets compiling at once multiply that, and several cold builds at once (a
pool warming its slots side by side) ran three times the frontends, and needed three times the memory, of the same
builds with the integrated driver. This gate bounds them again, across every build that uses this install.

A place is an exclusive lock on one of N files; the lock stays with the frontend (the descriptor survives exec) and
goes when it exits, however it exits. N: $XCFS_FRONTEND_JOBS, else four per core (measured on a fresh pool of three Ice Cubes slots: 22 GB at the peak, 276 s; ungated 59 GB, 217 s; two per core 17 GB, 327 s). Only compile jobs (`-c`) wait:
emitting a module is what the next modules wait for, and queries and dependency scans are short; holding those
behind a queue of compile jobs made a fresh pool's warm-up almost twice as long.
"""
import fcntl, os, sys, time

real = os.environ.get("XCFS_REAL_SWIFT_FRONTEND", "")
if not real:
    sys.exit("swift-frontend gate: XCFS_REAL_SWIFT_FRONTEND is not set")
try:
    n = int(os.environ.get("XCFS_FRONTEND_JOBS") or 0)
except ValueError:
    n = 0
n = n if n > 0 else 4 * (os.cpu_count() or 4)
if "-c" not in sys.argv[1:]:
    os.execv(real, [real] + sys.argv[1:])
places = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "frontend-places")
os.makedirs(places, exist_ok=True)
first = os.getpid() % n
while True:
    for k in range(n):
        fd = os.open(os.path.join(places, str((first + k) % n)), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            continue
        os.set_inheritable(fd, True)
        os.execv(real, [real] + sys.argv[1:])
    time.sleep(0.02)
