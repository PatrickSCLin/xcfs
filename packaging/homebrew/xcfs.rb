# Homebrew formula for xcfs. It lives in a tap (a repository named homebrew-xcfs, as Formula/xcfs.rb); this copy is
# the source. After tagging a release, scripts/homebrew-formula.sh <version> fills in url and sha256.
class Xcfs < Formula
  desc "Worktrees, a pool of pre-built slots and build sessions for parallel agents on Xcode projects"
  homepage "https://github.com/PatrickSCLin/xcfs"
  url "https://github.com/PatrickSCLin/xcfs/archive/refs/tags/v0.2.0.tar.gz"
  sha256 "4167e2be07179809cf6219599891079a583dc4b6375896b3ae81c97a00e0c21c"
  license "MIT"
  head "https://github.com/PatrickSCLin/xcfs.git", branch: "main"

  depends_on :macos

  def install
    # bin/xcfs finds its code in ../libexec/xcfs (it resolves its own symlink first)
    libexec.install "lib/xcfs"
    bin.install "bin/xcfs"
    doc.install "README.md", "docs"
    pkgshare.install "examples"
  end

  def caveats
    <<~EOS
      xcfs runs with the python3 of the Xcode command-line tools (3.9 or later) and drives the selected Xcode.
      Start with `xcfs init <repo>` in a repository, then `xcfs doctor <repo>`.
      For Claude Code's worktree and session-end hooks, see #{doc}/docs/agents.md.
    EOS
  end

  test do
    assert_match "xcfs #{version}", shell_output("#{bin}/xcfs --version")
    system "git", "init", "-q", testpath/"repo"
    (testpath/"repo/Package.swift").write "// swift-tools-version: 5.9\n"
    assert_match "\"package\"", shell_output("#{bin}/xcfs init #{testpath}/repo --json")
  end
end
