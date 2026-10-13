const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {spawnSync} = require('node:child_process');

const root = path.resolve(__dirname, '../..');
const source = fs.readFileSync(path.join(root, 'desktop/plugin.js'), 'utf8');
const code = source.replace(/^import .*;$/gm, '').replace('export default', 'const plugin =');
const context = vm.createContext({TextEncoder});
vm.runInContext(code + '\nglobalThis.projectUsageData = data => project("usage.read", data);', context);
const project = data => JSON.parse(JSON.stringify(context.projectUsageData(data)));
const providers = ['gemini', 'gemini-interactions', 'vertex', 'codex', 'claude', 'xai', 'meta', 'other'];
const max = Number.MAX_SAFE_INTEGER;
const buckets = (success = 0, failed = 0) => Array.from({length: 20}, () => ({success, failed}));
const unavailable = () => ({available: false, scope: 'api_key_credentials', credential_entries: null,
  total_success: null, total_failed: null, providers: [],
  window: {available: false, bucket_seconds: 600, buckets: []}});
const group = (provider = 'codex', success = 40, failed = 20) =>
  ({provider, credential_entries: 1, success, failed, buckets: buckets(2, 1)});
function usage(rows = [group()], recent = true) {
  return {available: true, scope: 'api_key_credentials',
    credential_entries: rows.reduce((sum, row) => sum + row.credential_entries, 0),
    total_success: rows.reduce((sum, row) => sum + row.success, 0),
    total_failed: rows.reduce((sum, row) => sum + row.failed, 0),
    providers: rows.map(row => ({...row, buckets: recent ? row.buckets : []})),
    window: {available: recent, bucket_seconds: 600, buckets: recent ? buckets().map((_, index) => ({
      success: rows.reduce((sum, row) => sum + row.buckets[index].success, 0),
      failed: rows.reduce((sum, row) => sum + row.buckets[index].failed, 0),
    })) : []}};
}
function reject(data) {
  assert.throws(() => project(data), error => error.message === 'Unavailable');
}

test('unavailable, empty, unavailable recent, quiet and active usage remain distinct', () => {
  const quiet = {...group('codex', 0, 0), buckets: buckets()};
  for (const data of [unavailable(), usage([], false), usage([group()], false), usage([quiet]), usage()]) {
    assert.deepEqual(project(data), data);
  }
});

test('every provider uses canonical order and only allowlisted fields survive', () => {
  const expected = usage(providers.map(name => group(name)));
  const input = usage(providers.slice().reverse().map(name => group(name)));
  Object.assign(input, {timestamp: 'CANARY', unit: 'CANARY', secret: 'CANARY', error: 'CANARY'});
  for (const row of input.providers) {
    Object.assign(row, {model: 'CANARY', credential: 'CANARY', name: 'CANARY'});
    for (const bucket of row.buckets) Object.assign(bucket, {time: 'CANARY', token: 'CANARY'});
  }
  input.window.label = 'CANARY';
  for (const bucket of input.window.buckets) bucket.time = 'CANARY';
  const output = project(input);
  assert.deepEqual(output, expected);
  assert.ok(!JSON.stringify(output).includes('CANARY'));
  input.providers[0].buckets[0].success = 99;
  assert.equal(output.providers[7].buckets[0].success, 2);
});

test('safe integer and credential count boundaries are inclusive', () => {
  const row = {...group('codex', max, 0), credential_entries: 1000, buckets: buckets()};
  row.buckets[19].success = max;
  const data = usage([row]);
  assert.deepEqual(project(data), data);
  row.success = max - 1;
  row.failed = 1;
  row.buckets[19] = {success: max - 1, failed: 1};
  assert.deepEqual(project(usage([row])), usage([row]));
});

for (const value of [true, false, null, undefined, '1', -1, 0.5, NaN, Infinity, -Infinity, max + 1]) {
  test('non-count values are rejected at every numeric boundary: ' + String(value), () => {
    for (const mutate of [
      d => { d.credential_entries = value; },
      d => { d.total_success = value; },
      d => { d.total_failed = value; },
      d => { d.providers[0].credential_entries = value; },
      d => { d.providers[0].success = value; },
      d => { d.providers[0].failed = value; },
      d => { d.providers[0].buckets[0].success = value; },
      d => { d.providers[0].buckets[0].failed = value; },
      d => { d.window.buckets[0].success = value; },
      d => { d.window.buckets[0].failed = value; },
    ]) {
      const data = usage(); mutate(data); reject(data);
    }
  });
}

const invalidShapes = [
  ['truthy availability', d => { d.available = 1; }],
  ['missing scope', d => { delete d.scope; }],
  ['arbitrary scope', d => { d.scope = 'CANARY'; }],
  ['legacy providers map', d => { d.providers = {}; }],
  ['oversized provider array', d => { d.providers = Array(1001).fill(group()); }],
  ['sparse provider array', d => { d.providers = new Array(1); }],
  ['null provider', d => { d.providers[0] = null; }],
  ['unknown provider', d => { d.providers[0].provider = 'CANARY'; }],
  ['provider suffix', d => { d.providers[0].provider = 'codex-CANARY'; }],
  ['obsolete provider alias', d => { d.providers[0].provider = 'interactions'; }],
  ['duplicate providers', d => { d.providers.push({...d.providers[0]}); d.credential_entries *= 2; d.total_success *= 2; d.total_failed *= 2; }],
  ['zero-entry provider', d => { d.providers[0].credential_entries = 0; d.credential_entries = 0; }],
  ['too many credentials', d => { d.providers[0].credential_entries = 1001; d.credential_entries = 1001; }],
  ['missing window', d => { delete d.window; }],
  ['null window', d => { d.window = null; }],
  ['truthy window availability', d => { d.window.available = 'CANARY'; }],
  ['wrong interval', d => { d.window.bucket_seconds = 60; }],
  ['provider buckets not array', d => { d.providers[0].buckets = {}; }],
  ['aggregate buckets not array', d => { d.window.buckets = {}; }],
  ['short provider window', d => { d.providers[0].buckets.pop(); }],
  ['long provider window', d => { d.providers[0].buckets.push({success: 0, failed: 0}); }],
  ['short aggregate window', d => { d.window.buckets.pop(); }],
  ['long aggregate window', d => { d.window.buckets.push({success: 0, failed: 0}); }],
  ['null bucket', d => { d.providers[0].buckets[0] = null; }],
  ['sparse aggregate buckets', d => { delete d.window.buckets[0]; }],
  ['sparse provider buckets', d => { delete d.providers[0].buckets[0]; }],
  ['unavailable window with data', d => { d.window.available = false; }],
  ['missing provider window with aggregate data', d => { d.providers[0].buckets = []; }],
  ['missing aggregate window with provider data', d => { d.window.buckets = []; }],
  ['credential mismatch', d => { d.credential_entries += 1; }],
  ['success mismatch', d => { d.total_success += 1; }],
  ['failure mismatch', d => { d.total_failed += 1; }],
  ['bucket success mismatch', d => { d.window.buckets[3].success += 1; }],
  ['bucket failure mismatch', d => { d.window.buckets[3].failed += 1; }],
  ['bucket index mismatch', d => { d.providers[0].buckets[0].success += 1; d.window.buckets[1].success += 1; }],
];
for (const [name, mutate] of invalidShapes) {
  test('rejects ' + name, () => { const data = usage(); mutate(data); reject(data); });
}

test('all unavailable fields must agree rather than hide contradictory data', () => {
  for (const mutate of [
    d => { d.total_success = 0; }, d => { d.total_failed = 0; },
    d => { d.credential_entries = 0; }, d => { d.providers = [group()]; },
    d => { d.window.available = true; }, d => { d.window.buckets = buckets(); },
  ]) {
    const data = unavailable(); mutate(data); reject(data);
  }
  reject(usage([], true));
  const data = usage([group()], false);
  data.providers[0].buckets = buckets();
  reject(data);
  for (const legacy of [null, [], {}, {available: true, total_success: 0, total_failed: 0}, {usage: usage()}]) reject(legacy);
});

test('group, lifetime, per-index and entire-window sums cannot exceed safe integers', () => {
  reject(usage([group('codex', max, 1)], false));
  reject(usage([group('codex', max, 0), group('claude', 1, 0)], false));
  reject(usage([group('codex', max, 0), group('claude', 0, 1)], false));
  const row = {...group('codex', max, 0), buckets: buckets()};
  row.buckets[0] = {success: max, failed: 1};
  reject(usage([row]));
  row.buckets[0] = {success: max, failed: 0};
  row.buckets[1] = {success: 1, failed: 0};
  reject(usage([row]));
  row.buckets[1] = {success: 0, failed: 1};
  reject(usage([row]));
  const second = {...group('claude', 0, 0), buckets: buckets()};
  row.buckets[1] = {success: 0, failed: 0};
  second.buckets[0].success = 1;
  reject(usage([row, second]));
  const first = {...group('codex', 0, 0), credential_entries: 1000};
  reject(usage([first, group('claude', 0, 0)], false));
});

test('multiple provider groups must agree with global lifetimes and every bucket', () => {
  const rows = [group('gemini', 60, 40), group('other', 80, 50)];
  rows[0].credential_entries = 2;
  rows[1].credential_entries = 3;
  rows[1].buckets = buckets(3, 2);
  const data = usage(rows);
  assert.equal(project(data).credential_entries, 5);
  assert.deepEqual(project(data).window.buckets, buckets(5, 3));
  for (const mutate of [
    d => { d.total_success = rows[0].success; },
    d => { d.total_failed = rows[0].failed; },
    d => { d.credential_entries = rows[0].credential_entries; },
    d => { d.window.buckets[19] = {...rows[0].buckets[19]}; },
  ]) { const changed = usage(rows); mutate(changed); reject(changed); }
});

test('actual Python normalization and desktop projection share the closed usage contract', () => {
  const rawEntry = (success, failed, recent = buckets(2, 1)) => ({success, failed,
    recent_requests: recent.map(bucket => ({...bucket, time: 'CANARY'})), secret: 'CANARY'});
  const raw = {
    codex: {'CANARY-key-a': rawEntry(40, 20)},
    'gemini-interactions': {'CANARY-key-b': rawEntry(50, 30)},
    'CANARY-provider-a': {'CANARY-key-c': rawEntry(60, 40)},
    'CANARY-provider-b': {'CANARY-key-d': rawEntry(70, 50)},
  };
  const missingWindow = {codex: {'CANARY-key': {success: 4, failed: 2}}};
  const malformedLifetime = {codex: {'CANARY-key': rawEntry(true, 0)}};
  const oversizedWindow = {codex: {'CANARY-key': rawEntry(40, 20, [...buckets(), {success: 0, failed: 0}])}};
  const overflowingWindow = buckets();
  overflowingWindow[0].success = max;
  overflowingWindow[1].failed = 1;
  const overflow = {codex: {'CANARY-key': rawEntry(40, 20, overflowingWindow)}};
  const quiet = {codex: {'CANARY-key': rawEntry(0, 0, buckets())}};
  const mixedWindows = {...missingWindow, claude: {'CANARY-valid-key': rawEntry(40, 20)}};
  const python = process.env.HERMES_CPA_TEST_PYTHON || 'python3';
  const backend = spawnSync(python, ['-c',
    'import json,sys; from hermes_cpa.telemetry import TelemetryCollector; print(json.dumps([TelemetryCollector.normalize_usage(raw) for raw in json.load(sys.stdin)]))'], {
    cwd: root,
    env: {...process.env, PYTHONPATH: path.join(root, 'src'), PYTHONDONTWRITEBYTECODE: '1'},
    input: JSON.stringify([raw, {}, null, missingWindow, malformedLifetime, oversizedWindow, overflow, quiet, mixedWindows]),
    encoding: 'utf8', timeout: 10000,
  });
  assert.equal(backend.status, 0, backend.stderr || String(backend.error));
  const normalized = JSON.parse(backend.stdout);
  assert.ok(!JSON.stringify(normalized).includes('CANARY'));
  const other = {...group('other', 130, 90), credential_entries: 2, buckets: buckets(4, 2)};
  const expected = [usage([group('gemini-interactions', 50, 30), group(), other]), usage([], false), unavailable(),
    usage([group('codex', 4, 2)], false), unavailable(), usage([group()], false), usage([group()], false),
    usage([{...group('codex', 0, 0), buckets: buckets()}]),
    usage([group('codex', 4, 2), group('claude')], false)];
  assert.deepEqual(normalized.map(project), expected);
});
