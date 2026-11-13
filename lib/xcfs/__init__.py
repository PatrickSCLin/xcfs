"""xcfs — worktrees, a pool of pre-built worktrees and build sessions for parallel (agent) development on macOS.

The core (every module directly in this package) knows no toolchain and no project. Toolchain knowledge lives in
adapters (xcfs.adapters.*, or external `xcfs-adapter-<name>` executables); project knowledge lives in the project's
config.
"""
VERSION = "0.2.0"
