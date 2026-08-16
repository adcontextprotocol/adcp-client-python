#!/usr/bin/env bash
# Run the four Emma backend tests in parallel via headless `claude -p`.
# Each backend boots in its own /tmp/emma-* working dir; reports land in
# /tmp/emma-matrix-<timestamp>/ and the aggregated punch list prints to
# stdout when all four complete.
#
# Usage:
#   ./scripts/run_emma_matrix.sh                    # run all 4 in parallel
#   ./scripts/run_emma_matrix.sh sales              # one backend by name
#   ./scripts/run_emma_matrix.sh sales signals      # specific subset
#
# Available backends: sales, signals, audiostack, stability
#
# Requires:
#   * `claude` CLI on PATH
#   * `uv` on PATH
#   * SDK at /Users/brianokelley/conductor/adcp-client-python (path can be
#     overridden via $ADCP_SDK_PATH)

set -uo pipefail

ADCP_SDK_PATH="${ADCP_SDK_PATH:-/Users/brianokelley/conductor/adcp-client-python}"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
REPORT_DIR="/tmp/emma-matrix-${TIMESTAMP}"
mkdir -p "${REPORT_DIR}"

if [[ ! -d "${ADCP_SDK_PATH}/src/adcp/decisioning" ]]; then
  echo "ERROR: SDK not found at ${ADCP_SDK_PATH}" >&2
  echo "  Override with ADCP_SDK_PATH=/path/to/adcp-client-python" >&2
  exit 1
fi

# ---- per-backend briefs ----

brief_sales() {
  cat <<EOF
You are an AI engineer at a small DSP wanting to sell inventory through AdCP. Stand up the sales-direct seller agent.

Working dir: /tmp/emma-sales/. Don't touch the SDK source tree.

SDK at ${ADCP_SDK_PATH}. Setup:
  cd /tmp/emma-sales && uv init --bare --no-workspace && uv add --editable ${ADCP_SDK_PATH} && uv add httpx mcp

Build:
1. DecisioningPlatform subclass claiming \`sales-non-guaranteed\`.
2. Stub all 5 required sales methods with believable in-memory state.
3. Rely on the conformant \`auto_emit_completion_webhooks=False\` default; do not enable the legacy sync-completion compatibility mode.
4. Boot via \`from adcp.decisioning import serve\` and hit it with: tools/list, get_products, create_media_buy, sync_creatives, get_media_buy_delivery via the mcp client.
5. Verify tools/list shows ONLY sales tools (per-specialism filter from PR #339) — should NOT include build_creative, acquire_rights, check_governance, get_signals.

Reference: ${ADCP_SDK_PATH}/examples/hello_seller.py is your template.

Capture every moment of friction. Report under 600 words:

## Sales-Direct Emma Backend Test (post-#337/#338/#339/#340)
**Verdict**: X/10 (was 2/10 pre-fixes)
**Time to first tools/list**: <duration>
**Time to first create_media_buy**: <duration>
**Per-specialism filter narrowed correctly**: Y/N
### What worked smoothly
### Friction (P0/P1/P2)
### Tools tested (each ✅/❌)
- get_products
- create_media_buy
- update_media_buy
- sync_creatives
- get_media_buy_delivery
- tools/list narrowing (sales only, no leaks)
### What I'd ship as PRs
### Verbatim error messages worth fixing
EOF
}

brief_signals() {
  cat <<EOF
You are an AI engineer at a hypothetical signal marketplace. Stand up an MCP/A2A AdCP seller agent exposing signal discovery + activation.

Working dir: /tmp/emma-signals/. Don't touch the SDK source tree.

SDK at ${ADCP_SDK_PATH}. Setup:
  cd /tmp/emma-signals && uv init --bare --no-workspace && uv add --editable ${ADCP_SDK_PATH} && uv add httpx mcp

Build:
1. DecisioningPlatform subclass claiming \`signal-marketplace\`.
2. get_signals returns a small catalog (3 signals: demographic, in-market, purchase-intent).
3. activate_signal sync-success arm.
4. CRITICAL: on a SECOND activate_signal call, return a TaskHandoff via ctx.handoff_to_task. Verify framework projects to {task_id, status:"submitted"}.
5. Rely on the conformant \`auto_emit_completion_webhooks=False\` default.
6. Boot, hit tools/list + get_signals + activate_signal (sync) + activate_signal (handoff).
7. Verify tools/list narrows to just signals tools (per-specialism filter from PR #339).

Reference: ${ADCP_SDK_PATH}/examples/hello_seller_signals.py.

Report under 600 words:

## Signals-Marketplace Emma Backend Test (post-#337/#338/#339/#340)
**Verdict**: X/10 (was 8/10 pre-fixes)
**TaskHandoff projection works**: Y/N
**Per-specialism filter narrowed correctly**: Y/N
### What worked smoothly
### Friction (P0/P1/P2)
### Tools tested (each ✅/❌)
- get_signals (sync)
- activate_signal (sync)
- activate_signal (handoff)
- tools/list narrowing
### TaskHandoff DX notes
### What I'd ship as PRs
### Verbatim error messages worth fixing
EOF
}

brief_audiostack() {
  cat <<EOF
You're an AI engineer at AudioStack (audiostack.ai). Stand up an MCP/A2A AdCP seller agent exposing AudioStack's text-to-audio capability.

Working dir: /tmp/emma-audiostack/. Don't touch the SDK source tree.

SDK at ${ADCP_SDK_PATH}. Setup:
  cd /tmp/emma-audiostack && uv init --bare --no-workspace && uv add --editable ${ADCP_SDK_PATH} && uv add httpx mcp

Build:
1. DecisioningPlatform subclass claiming \`creative-generative\`.
2. build_creative wires AudioStack Generate API (mock the key, stub realistic responses).
3. Rely on the conformant \`auto_emit_completion_webhooks=False\` default.
4. Boot, hit tools/list and build_creative.
5. Verify tools/list narrows to creative tools only (no sales/signals/governance leaks).

Reference: ${ADCP_SDK_PATH}/examples/hello_seller_creative.py is your template — note the AudioContent (not AudioAsset) callout.

Report under 600 words:

## AudioStack Emma Backend Test (post-#337/#338/#339/#340)
**Verdict**: X/10 (was 6/10 pre-fixes)
**INTERNAL_ERROR breadcrumb visible on intentional crash**: Y/N (try raising AttributeError inside build_creative — should see caused_by.type='AttributeError' on wire)
**Per-specialism filter narrowed correctly**: Y/N
### What worked smoothly
### Friction (P0/P1/P2)
### What I'd ship as PRs
### Verbatim error messages worth fixing
EOF
}

brief_stability() {
  cat <<EOF
You're an AI engineer at Stability AI. Stand up an MCP/A2A AdCP seller agent exposing text-to-image generation.

Working dir: /tmp/emma-stability/. Don't touch the SDK source tree.

SDK at ${ADCP_SDK_PATH}. Setup:
  cd /tmp/emma-stability && uv init --bare --no-workspace && uv add --editable ${ADCP_SDK_PATH} && uv add httpx mcp

Build:
1. DecisioningPlatform subclass claiming \`creative-generative\`.
2. build_creative wires Stability /v2beta/stable-image/generate (mock key, realistic shape).
3. Rely on the conformant \`auto_emit_completion_webhooks=False\` default.
4. Test the SHORTHAND ergonomic arm: return a bare CreativeManifest (Pydantic model), NOT a fully-shaped BuildCreativeSuccessResponse.
5. CRITICAL: deliberately make a mistake on the first attempt (e.g., omit width/height on ImageContent). Verify the error response is FOCUSED (just ImageAsset.width / ImageAsset.height) — NOT the 60-line dump that 5/10 verdict reported pre-PR-#340.
6. Boot, hit tools/list and build_creative.

Reference: ${ADCP_SDK_PATH}/examples/hello_seller_creative.py.

Report under 600 words:

## Stability AI Emma Backend Test (post-#337/#338/#339/#340)
**Verdict**: X/10 (was 5/10 pre-fixes)
**Bare-manifest projection arm works**: Y/N (wire response shape?)
**Discriminated-union error narrowed (#340)**: Y/N (count of errors before vs after — should be 2 not 26)
**Per-specialism filter narrowed correctly**: Y/N
### What worked smoothly
### Friction (P0/P1/P2)
### What I'd ship as PRs
### Verbatim error messages worth fixing
EOF
}

# ---- runner ----

run_backend() {
  local name="$1"
  local brief_fn="$2"
  local workdir="/tmp/emma-${name}"
  local report="${REPORT_DIR}/${name}.md"

  # Pre-create the per-backend working dir BEFORE spawning claude — the
  # agent's first ``cd /tmp/emma-${name}`` would otherwise fail in the
  # sandbox even if the dir is added via ``--add-dir`` (Claude Code's
  # path allow-list permits operations IN existing dirs but won't
  # create the dir itself when it's not the launch cwd).
  mkdir -p "${workdir}"

  echo "[${name}] starting → ${report}" >&2
  # ``--add-dir`` grants the agent read+write under the per-backend
  # working dir AND read of the SDK source (so it can find
  # ``hello_seller_*.py`` references and follow imports for
  # ``DecisioningPlatform`` / ``CreativeManifest`` / etc.).
  #
  # ``--dangerously-skip-permissions`` bypasses the per-tool approval
  # prompts. Headless ``claude -p`` has no human approver — without
  # this flag every ``uv``/``Bash`` invocation hangs as "requires
  # approval" and the brief never gets started. Safe here because:
  # 1) the script is run-from-disk by an authenticated developer, and
  # 2) the agent is sandboxed to the working dir + SDK read via
  # ``--add-dir`` regardless of this flag.
  #
  # Path canonicalization note (macOS): ``/tmp`` symlinks to
  # ``/private/tmp``. Pass the realpath so the agent's allow-list
  # checks succeed against the resolved path (Claude Code's
  # canonicalizer compares pre-realpath agent input against post-
  # realpath allow-list entries; pre-resolving here sidesteps the
  # mismatch).
  local resolved_workdir
  resolved_workdir="$(cd "${workdir}" && pwd -P)"
  ${brief_fn} | claude -p \
    --add-dir "${resolved_workdir}" \
    --add-dir "${ADCP_SDK_PATH}" \
    --dangerously-skip-permissions \
    > "${report}" 2>&1
  echo "[${name}] done" >&2
}

# Default to all 4; allow subsetting via positional args.
if [[ $# -eq 0 ]]; then
  BACKENDS=(sales signals audiostack stability)
else
  BACKENDS=("$@")
fi

PIDS=()
for backend in "${BACKENDS[@]}"; do
  case "${backend}" in
    sales) run_backend sales brief_sales & PIDS+=($!) ;;
    signals) run_backend signals brief_signals & PIDS+=($!) ;;
    audiostack) run_backend audiostack brief_audiostack & PIDS+=($!) ;;
    stability) run_backend stability brief_stability & PIDS+=($!) ;;
    *)
      echo "ERROR: unknown backend ${backend}" >&2
      echo "  available: sales, signals, audiostack, stability" >&2
      exit 1
      ;;
  esac
done

echo "Running ${#PIDS[@]} backends in parallel; PIDs: ${PIDS[*]}" >&2
echo "Reports → ${REPORT_DIR}/" >&2

# Wait for all and capture exit codes.
EXIT_CODE=0
for pid in "${PIDS[@]}"; do
  if ! wait "${pid}"; then
    EXIT_CODE=1
    echo "WARN: backend pid ${pid} exited non-zero" >&2
  fi
done

# Aggregate.
AGG="${REPORT_DIR}/aggregate.md"
{
  echo "# Emma Backend Matrix — ${TIMESTAMP}"
  echo
  echo "Run from ${ADCP_SDK_PATH}"
  echo
  for backend in "${BACKENDS[@]}"; do
    if [[ -f "${REPORT_DIR}/${backend}.md" ]]; then
      cat "${REPORT_DIR}/${backend}.md"
      echo
      echo "---"
      echo
    fi
  done
} > "${AGG}"

echo
echo "================================================================="
echo "Aggregated report: ${AGG}"
echo "================================================================="
cat "${AGG}"
exit "${EXIT_CODE}"
