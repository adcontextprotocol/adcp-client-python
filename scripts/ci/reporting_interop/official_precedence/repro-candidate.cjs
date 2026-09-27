'use strict';

// Preserve the rc.42-adapted semantic runner unchanged while selecting the
// selected candidate identity for this process. Node caches JSON module
// objects, so mutating the already-loaded historical pin object makes the
// existing runner consume the exact central candidate pin without altering
// its source or the byte-exact fixture.
const historicalPins = require('./pins.json');
const selected = require('../pins.json').typescript.candidate_rc48;
const candidatePins = {
  package: selected.package,
  version: selected.version,
  integrity: selected.integrity,
  shasum: selected.shasum,
  tarballSha256: selected.tarball_sha256,
  gitHead: selected.registry_git_head,
  sourceTree: selected.source_tree,
  defaultAdcpVersion: selected.protocol,
  fixtureSha256: historicalPins.fixtureSha256,
  historicalPin: historicalPins.historicalPin,
};
for (const key of Object.keys(historicalPins)) delete historicalPins[key];
Object.assign(historicalPins, candidatePins);
require('./repro-rc42.cjs');
