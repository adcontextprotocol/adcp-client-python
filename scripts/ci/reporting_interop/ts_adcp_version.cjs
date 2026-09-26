'use strict';

/* Convert an exact SDK release identifier to the protocol's major/minor wire
 * precision. The release patch is never carried on the wire; any prerelease
 * suffix remains attached to the major/minor version. Malformed or imprecise
 * input is rejected rather than guessed.
 */
function wireAdcpVersion(value) {
  const match = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$/.exec(value);
  if (!match) throw new Error(`AdCP version must use release precision: ${String(value)}`);
  const [, major, minor, , prerelease = ''] = match;
  if (prerelease.slice(1).split('.').some(identifier => /^0\d+$/.test(identifier))) {
    throw new Error(`AdCP version must use release precision: ${String(value)}`);
  }
  return `${major}.${minor}${prerelease}`;
}

/* This is deliberately an assertion for harness-owned fixture documents. It
 * requires the fixture's singleton supported_versions declaration and must not
 * be reused as a general validator for remote capability documents.
 */
function assertFixtureCapabilityVersion(capability, exactRelease) {
  const wire = wireAdcpVersion(exactRelease);
  if (capability?.adcp_version !== wire ||
      !Array.isArray(capability?.adcp?.supported_versions) ||
      capability.adcp.supported_versions.length !== 1 ||
      capability.adcp.supported_versions[0] !== wire) {
    throw new Error('capability wire version does not match the selected exact SDK release');
  }
  return wire;
}

module.exports = { assertFixtureCapabilityVersion, wireAdcpVersion };
