// Offline assertions on the actual source-built artifact; not an E2E substitute.
const fs = require('node:fs');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const file = process.argv[2] || 'upstream-console/dist/index.html';
const bytes = fs.readFileSync(file);
const html = bytes.toString('utf8');
assert(bytes.length < 2*1024*1024, 'Upstream shell console size regressed');
// Isolation posture only: guest network is denied by CSP. Operation names, wire literals and UI
// wording are behavior, covered by the bridge and guest tests rather than pinned here.
for (const token of ['connect-src \'none\'', 'default-src \'none\'']) assert(html.includes(token), token);
assert(!/<script[^>]+src=/.test(html), 'External script in offline asset');
assert(!html.includes('HERMES_CPA_MANAGEMENT_KEY'), 'Secret name in guest');
assert(!html.includes('localStorage'), 'Persistent storage path in guest');
console.log(JSON.stringify({file, bytes:bytes.length, sha256:crypto.createHash('sha256').update(bytes).digest('hex'), checks: 'passed'}));
