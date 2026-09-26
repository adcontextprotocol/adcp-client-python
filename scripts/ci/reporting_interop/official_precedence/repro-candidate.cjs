'use strict';

// Preserve the rc.42-adapted semantic runner unchanged while selecting the
// separately pinned candidate identity for this process. Node caches JSON
// module objects, so mutating the already-loaded historical pin object makes
// the existing runner consume pins-rc45.json without altering its source or
// the byte-exact fixture.
const historicalPins = require('./pins.json');
const candidatePins = require('./pins-rc45.json');
for (const key of Object.keys(historicalPins)) delete historicalPins[key];
Object.assign(historicalPins, candidatePins);
require('./repro-rc42.cjs');
