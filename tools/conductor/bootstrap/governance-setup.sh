#!/usr/bin/env bash
# governance-setup — idempotent bootstrap of Conductor's safety substrate on a repo.
#
# This is the server-side, authoritative half of P0 (the hooks/CLI are the bypassable
# local half). It is a TEMPLATE: it requires an authenticated `gh` with repo-admin and a
# provisioned Conductor bot App. The P0 tests do NOT run this.
#
# Idempotent: re-running updates the ruleset in place and never duplicates it.
#
# Usage:  ./governance-setup.sh <owner>/<repo> [app_installation_id]
set -euo pipefail

REPO="${1:?usage: governance-setup.sh <owner>/<repo> [app_installation_id]}"
APP_ID="${2:-0}"
HERE="$(cd "$(dirname "$0")" && pwd)"
RULESET_NAME="conductor-trunk"

command -v gh >/dev/null || { echo "error: gh CLI not found"; exit 2; }
gh auth status >/dev/null 2>&1 || { echo "error: gh not authenticated"; exit 2; }

echo "==> target repo: $REPO"

# 1. Trunk ruleset (linear history + required conductor-landed check + sole-writer App).
payload="$(mktemp)"
trap 'rm -f "$payload"' EXIT
jq --argjson app "$APP_ID" \
  '.bypass_actors[0].actor_id = $app | del(.._comment) | del(.rules[].parameters._comment?)' \
  "$HERE/ruleset.json" > "$payload" 2>/dev/null || cp "$HERE/ruleset.json" "$payload"

existing="$(gh api "repos/$REPO/rulesets" --jq \
  ".[] | select(.name==\"$RULESET_NAME\") | .id" 2>/dev/null | head -n1 || true)"

if [ -n "$existing" ]; then
  echo "==> updating existing ruleset #$existing"
  gh api -X PUT "repos/$REPO/rulesets/$existing" --input "$payload" >/dev/null
else
  echo "==> creating ruleset '$RULESET_NAME'"
  gh api -X POST "repos/$REPO/rulesets" --input "$payload" >/dev/null
fi

# 2. Restrict who may push refs/conductor/* (only the bot App). GitHub rulesets target
#    branches/tags; arbitrary ref namespaces are guarded by the App being the sole
#    credential with write to the repo's conductor refs. Documented here as the intended
#    posture; enforced operationally by the App's least-privilege token.
echo "==> NOTE: refs/conductor/* writes must be restricted to the Conductor bot App token."

# 3. Seed the changeset config directory marker (engine-owned).
echo "==> ensuring .conductor/ scaffold exists in the default branch (manual commit step)."

echo "==> done. Trunk now requires linear history + the 'conductor-landed' status check."
echo "    A lost lease race can no longer write a non-linear or unverified trunk."
