#!/usr/bin/env bash
# scripts/ci/run_storyboard_reference_seller.sh
#
# Boot examples/seller_agent.py and run the media_buy_seller storyboard
# against it. Callable from Python CI and from cross-repo release-gating
# (adcp-client#1916) without duplicating the recipe.
#
# Required — set exactly one of:
#   ADCP_SDK_VERSION   @adcp/sdk version to install from npm ("latest" or
#                      a specific version such as "7.10.2")
#   ADCP_SDK_TARBALL   Absolute path to a candidate @adcp/sdk .tgz/.tar.gz
#
# Optional:
#   ADCP_PORT              Port for the seller agent (default: 3001)
#   STORYBOARD_RESULT_PATH  Path for JSON result output; omit to write to stdout
#
# Run from the root of an adcp-client-python checkout.
# Node.js (npm + adcp binary) and Python must be on PATH.

set -euo pipefail

# --- Validate inputs ---

if [[ -z "${ADCP_SDK_VERSION:-}" && -z "${ADCP_SDK_TARBALL:-}" ]]; then
  echo "Error: set exactly one of ADCP_SDK_VERSION or ADCP_SDK_TARBALL" >&2
  echo "  ADCP_SDK_VERSION=latest ${0}" >&2
  echo "  ADCP_SDK_VERSION=7.10.2 ${0}" >&2
  echo "  ADCP_SDK_TARBALL=/absolute/path/adcp-sdk.tgz ${0}" >&2
  exit 1
fi
if [[ -n "${ADCP_SDK_VERSION:-}" && -n "${ADCP_SDK_TARBALL:-}" ]]; then
  echo "Error: set only one of ADCP_SDK_VERSION or ADCP_SDK_TARBALL, not both" >&2
  exit 1
fi

ADCP_PORT="${ADCP_PORT:-3001}"

if [[ -n "${ADCP_SDK_TARBALL:-}" ]]; then
  if [[ "${ADCP_SDK_TARBALL}" != /* ]]; then
    echo "Error: ADCP_SDK_TARBALL must be an absolute path (got: ${ADCP_SDK_TARBALL})" >&2
    exit 1
  fi
  if [[ ! -f "${ADCP_SDK_TARBALL}" ]]; then
    echo "Error: ADCP_SDK_TARBALL not found: ${ADCP_SDK_TARBALL}" >&2
    exit 1
  fi
  case "${ADCP_SDK_TARBALL}" in
    *.tgz|*.tar.gz) ;;
    *) echo "Error: ADCP_SDK_TARBALL must be a .tgz or .tar.gz file" >&2; exit 1 ;;
  esac
fi

# --- Install @adcp/sdk ---

if [[ -n "${ADCP_SDK_VERSION:-}" ]]; then
  npm install -g "@adcp/sdk@${ADCP_SDK_VERSION}"
else
  npm install -g "${ADCP_SDK_TARBALL}"
fi
adcp --version

# --- Install Python deps in an isolated venv ---
# Uses the base package only — seller_agent.py does not need dev extras.

VENV_DIR=".ci-venv"
python -m venv "${VENV_DIR}"
"${VENV_DIR}/bin/pip" install --quiet --upgrade pip
"${VENV_DIR}/bin/pip" install --quiet -e .

# --- Boot seller agent with guaranteed cleanup on exit ---

AGENT_PID=""
_cleanup() {
  [[ -n "${AGENT_PID}" ]] && kill "${AGENT_PID}" 2>/dev/null || true
}
trap _cleanup EXIT

ADCP_PORT="${ADCP_PORT}" "${VENV_DIR}/bin/python" examples/seller_agent.py &
AGENT_PID=$!

echo "Waiting for seller agent (pid ${AGENT_PID}) on port ${ADCP_PORT}..."
for i in $(seq 1 60); do
  # Any HTTP response (including 405 on GET to a POST-only endpoint) means
  # the server is up and accepting connections.
  HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 1 \
    "http://127.0.0.1:${ADCP_PORT}/mcp" 2>/dev/null) || HTTP_CODE="000"
  if [[ "${HTTP_CODE}" != "000" ]]; then
    echo "Seller agent ready (HTTP ${HTTP_CODE}, pid ${AGENT_PID})"
    break
  fi
  if ! kill -0 "${AGENT_PID}" 2>/dev/null; then
    echo "Seller agent process died during startup" >&2
    exit 1
  fi
  if [[ "${i}" -eq 60 ]]; then
    echo "Seller agent failed to start within 30s" >&2
    exit 1
  fi
  sleep 0.5
done

# --- Run storyboard ---

_RESULT_DEST="${STORYBOARD_RESULT_PATH:-}"
if [[ -n "${_RESULT_DEST}" ]]; then
  adcp storyboard run \
    "http://127.0.0.1:${ADCP_PORT}/mcp" media_buy_seller \
    --json --allow-http \
    > "${_RESULT_DEST}"
  _ASSERT_FILE="${_RESULT_DEST}"
else
  _TMPFILE="$(mktemp)"
  adcp storyboard run \
    "http://127.0.0.1:${ADCP_PORT}/mcp" media_buy_seller \
    --json --allow-http \
    | tee "${_TMPFILE}"
  _ASSERT_FILE="${_TMPFILE}"
fi

# --- Assert result ---

"${VENV_DIR}/bin/python" - "${_ASSERT_FILE}" <<'PYEOF'
import json, sys, pathlib
p = pathlib.Path(sys.argv[1])
if not p.exists() or p.stat().st_size == 0:
    print("storyboard result missing or empty — runner produced no output")
    sys.exit(1)
with p.open() as f:
    d = json.load(f)
if d.get("overall_status") != "passing":
    print(json.dumps(d, indent=2))
    sys.exit(1)
if not d.get("controller_detected"):
    print("controller_detected was false; check DemoStore overrides (see #304)")
    sys.exit(1)
print("Storyboard passed.")
PYEOF
