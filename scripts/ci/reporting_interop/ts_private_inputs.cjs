'use strict';

/*
 * Private per-run inputs for the TypeScript reporting fixtures.
 *
 * Environment variables are not a secrecy boundary against the same user or
 * root.  This seam only avoids exposing credentials and startup proofs in the
 * process argument vector and in ordinary command/error logging.
 */

const CORE_PRIVATE_INPUT_ENV = Object.freeze({
  tokenA: 'ADCP_INTEROP_TS_CORE_AUTH_TOKEN_A',
  tokenB: 'ADCP_INTEROP_TS_CORE_AUTH_TOKEN_B',
  startupProof: 'ADCP_INTEROP_TS_CORE_STARTUP_PROOF',
});

const STORYBOARD_PRIVATE_INPUT_ENV = Object.freeze({
  authToken: 'ADCP_INTEROP_TS_STORYBOARD_AUTH_TOKEN',
  startupProof: 'ADCP_INTEROP_TS_STORYBOARD_STARTUP_PROOF',
});

function requiredPrivateInput(environment, variable, label) {
  const value = environment?.[variable];
  if (typeof value !== 'string' || value.length < 32) {
    throw new Error(`${label} must be supplied as a strong per-run private input`);
  }
  return value;
}

function corePrivateInputs(environment = process.env) {
  const tokenA = requiredPrivateInput(
    environment, CORE_PRIVATE_INPUT_ENV.tokenA, 'Core seller account A token',
  );
  const tokenB = requiredPrivateInput(
    environment, CORE_PRIVATE_INPUT_ENV.tokenB, 'Core seller account B token',
  );
  const startupProof = requiredPrivateInput(
    environment, CORE_PRIVATE_INPUT_ENV.startupProof, 'Core seller startup proof',
  );
  if (tokenA === tokenB) {
    throw new Error('Core seller authentication requires distinct per-run tokens');
  }
  return { tokenA, tokenB, startupProof };
}

function storyboardPrivateInputs(environment = process.env) {
  return {
    authToken: requiredPrivateInput(
      environment,
      STORYBOARD_PRIVATE_INPUT_ENV.authToken,
      'Storyboard seller authentication token',
    ),
    startupProof: requiredPrivateInput(
      environment,
      STORYBOARD_PRIVATE_INPUT_ENV.startupProof,
      'Storyboard seller startup proof',
    ),
  };
}

module.exports = {
  CORE_PRIVATE_INPUT_ENV,
  STORYBOARD_PRIVATE_INPUT_ENV,
  corePrivateInputs,
  storyboardPrivateInputs,
};
