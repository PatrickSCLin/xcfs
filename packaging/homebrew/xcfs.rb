# Homebrew formula for xcfs. It lives in a tap (a repository named homebrew-xcfs, as Formula/xcfs.rb); this copy is
# the source. After tagging a release, scripts/homebrew-formula.sh <version> fills in url and sha256.
class Xcfs < Formula
  desc "Pool of pre-built worktrees and build sessions for parallel agents on Xcode"
  homepage "https://github.com/PatrickSCLin/xcfs"
  url "https://github.com/PatrickSCLin/xcfs/archive/refs/tags/v0.3.0.tar.gz"
  sha256 "375f3137063aa5e1f8117f87cbdcd50c157ca89c7ac7e5a746a5f4e3791baca9"
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
    # a --HEAD build reports the version in the code, not "HEAD-<sha>"
    expected = version.head? ? /xcfs \d+\.\d+\.\d+/ : "xcfs #{version}"
    assert_match expected, shell_output("#{bin}/xcfs --version")
    system "git", "init", "-q", testpath/"repo"
    (testpath/"repo/Package.swift").write "// swift-tools-version: 5.9\n"
    assert_match "\"package\"", shell_output("#{bin}/xcfs init #{testpath}/repo --json")
  end
end
