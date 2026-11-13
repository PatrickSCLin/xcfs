#!/bin/bash
# Fail if a private term appears where it would be published.
#
#   scripts/check-private-terms.sh            every tracked + untracked-but-not-ignored file
#   scripts/check-private-terms.sh --staged   what is about to be committed (pre-commit hook)
#   scripts/check-private-terms.sh --message <file>   a commit message (commit-msg hook)
#
# The terms are regexes in plan/PRIVATE_TERMS.txt, which is git-ignored: the list names the things that must
# not be named. Without that file (a contributor's clone, CI) there is nothing to check and this exits 0.
set -euo pipefail
ROOT=$(git rev-parse --show-toplevel)
TERMS="${XCFS_PRIVATE_TERMS:-${ROOT}/plan/PRIVATE_TERMS.txt}"
[ -f "${TERMS}" ] || { echo "check-private-terms: no ${TERMS#"${ROOT}"/}; nothing to check"; exit 0; }
PATTERNS=$(grep -v '^[[:space:]]*#' "${TERMS}" | grep -v '^[[:space:]]*$' || true)
[ -n "${PATTERNS}" ] || exit 0
cd "${ROOT}"
hits=0
scan() {  # scan <label> <file-with-content>
  local out
  out=$(grep -n -i -E -f <(printf '%s\n' "${PATTERNS}") "$2" 2>/dev/null || true)
  if [ -n "${out}" ]; then
    printf '%s\n' "${out}" | sed "s|^|  $1:|" | cut -c1-200
    hits=$((hits + 1))
  fi
}
case "${1:-}" in
  --message) scan "commit message" "$2" ;;
  --staged)
    # The author and committer of a commit are published too.
    tmp=$(mktemp); { git var GIT_AUTHOR_IDENT; git var GIT_COMMITTER_IDENT; } > "${tmp}" 2>/dev/null || true
    scan "commit author/committer identity" "${tmp}"; rm -f "${tmp}"
    while IFS= read -r -d '' f; do
      tmp=$(mktemp); git show ":${f}" > "${tmp}" 2>/dev/null || true
      scan "${f}" "${tmp}"
      printf '%s\n' "${f}" > "${tmp}"; scan "(file name) ${f}" "${tmp}"; rm -f "${tmp}"
    done < <(git diff --cached --name-only --diff-filter=ACMR -z) ;;
  *)
    while IFS= read -r -d '' f; do
      [ -f "${f}" ] && scan "${f}" "${f}"
    done < <(git ls-files -z --cached --others --exclude-standard) ;;
esac
if [ "${hits}" -gt 0 ]; then
  echo "check-private-terms: ${hits} file(s) contain a private term. This repository is public: rename or remove them." >&2
  exit 1
fi
echo "check-private-terms: clean"
