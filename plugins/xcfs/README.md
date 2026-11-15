# xcfs plugin for Claude Code

The same three hooks `xcfs hook install` writes into a settings file, as a plugin: every worktree Claude Code creates
(`claude --worktree`, a subagent with `isolation: "worktree"`, `EnterWorktree`) comes from xcfs, and the session's end
returns what it opened. A skill tells the agent how to build, check and test through xcfs.

```
brew install PatrickSCLin/xcfs/xcfs          # the hooks call `xcfs` on PATH
claude plugin marketplace add PatrickSCLin/xcfs
claude plugin install xcfs@xcfs
```

Use either the plugin or `xcfs hook install`, not both: Claude Code runs every matching hook, so both would create a
worktree. `xcfs hook uninstall --apply` takes the settings-file hooks out.
