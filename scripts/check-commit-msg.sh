#!/usr/bin/env bash
# Validates commit subjects against release-please parser constraints.
# Embedded ( ) or " in the description silently drop commits from the CHANGELOG.
# NOTE: this hook runs on direct commits only — squash-merge subjects (set by
# GitHub from the PR title) bypass it. Keep the PR title clean as the primary fix.
#
# Enable: pre-commit install --hook-type commit-msg

set -euo pipefail

msg_file="$1"
subject=$(head -1 "$msg_file")

# Only validate conventional commit messages: type(scope): desc or type: desc
cc_pattern='^[a-z]+(\([^)]*\))?(!)?: '
if echo "$subject" | grep -qE "$cc_pattern"; then
    description="${subject#*: }"
    if echo "$description" | grep -qE '[()"]'; then
        echo ""
        echo "commit-msg: release-please will silently drop this commit from the CHANGELOG."
        echo ""
        echo "  Subject: $subject"
        echo ""
        echo '  The description (text after "type(scope):") contains ( ) or " characters,'
        echo "  which break release-please's conventional commit parser."
        echo ""
        echo "  Fix: reword to avoid parentheses and quotes in the description."
        echo "  Move code snippets and type names with parens to the PR body instead."
        echo ""
        echo "  Example:"
        echo '    Wrong: fix(auth): synthesize AuthInfo(kind="bearer") in _build_request_context'
        echo "    Right: fix(auth): synthesize bearer AuthInfo in _build_request_context"
        echo ""
        exit 1
    fi
fi

exit 0
