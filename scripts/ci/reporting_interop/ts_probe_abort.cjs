'use strict';

/* Shared cancellation boundary for installed-package HTTP probes.
 *
 * A request timeout is only a deadline.  It does not prove that sibling calls
 * or signal-interrupted work stopped.  This helper supplies one AbortSignal to
 * every call in a probe and aborts the full probe on the first call failure or
 * process signal.  The owner still performs its normal connection drain and
 * process-group cleanup; cancellation is not treated as cleanup proof.
 */

function createProbeAbort(label) {
  const controller = new AbortController();
  const abort = reason => {
    if (controller.signal.aborted) return;
    const error = reason instanceof Error ? reason : new Error(`${label}: ${String(reason)}`);
    controller.abort(error);
  };
  const onSigint = () => abort('received SIGINT');
  const onSigterm = () => abort('received SIGTERM');
  process.once('SIGINT', onSigint);
  process.once('SIGTERM', onSigterm);
  return {
    abort,
    signal: controller.signal,
    dispose() {
      process.removeListener('SIGINT', onSigint);
      process.removeListener('SIGTERM', onSigterm);
    },
  };
}

async function callWithProbeAbort(boundary, invoke) {
  try {
    return await invoke(boundary.signal);
  } catch (error) {
    boundary.abort(error);
    throw error;
  }
}

module.exports = { callWithProbeAbort, createProbeAbort };
