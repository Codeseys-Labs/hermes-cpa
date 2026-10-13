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
vm.runInContext(code + '\nglobalThis.projectLogs = data => project("logs.read", data);', context);
const projectLogs = data => JSON.parse(JSON.stringify(context.projectLogs(data)));

// Exercise the unchanged desktop projection with the real backend projection,
// fed only generated output from the exact disposable pinned Go formatter.
test('pinned formatter -> backend -> desktop preserves every finite severity', () => {
  const fixture = JSON.parse(fs.readFileSync(path.join(root, 'tests/backend/fixtures/pinned_formatter_logs.json'), 'utf8'));
  const python = process.env.HERMES_CPA_TEST_PYTHON || 'python3';
  const backend = spawnSync(python, ['-c',
    'import json,sys; from hermes_cpa.management_features import project_feature; print(json.dumps(project_feature("logs", json.load(sys.stdin))))'], {
    cwd: root,
    env: {...process.env, PYTHONPATH: path.join(root, 'src'), PYTHONDONTWRITEBYTECODE: '1'},
    input: JSON.stringify({lines: fixture.rows.map(row => row.line.replace(/[\r\n]+$/, ''))}),
    encoding: 'utf8',
  });
  assert.equal(backend.status, 0, backend.stderr || String(backend.error));
  const result = projectLogs(JSON.parse(backend.stdout));
  assert.deepEqual(result, {available: true, withheld: null,
    items: fixture.rows.map((row, index) => ({ordinal: index + 1, severity: row.severity}))});
  assert.ok(result.items.some(row => row.severity === 'warning'));
  assert.ok(!JSON.stringify(result).includes('generated-canary'));
});

test('desktop severity enum is not expanded to upstream warn or arbitrary strings', () => {
  const result = projectLogs({available: true, items: [
    {severity: 'warning', message: 'generated-canary'},
    {severity: 'warn', request_id: 'generated-canary'},
    {severity: 'warning-generated-canary', path: 'generated-canary'},
  ]});
  assert.deepEqual(result.items, [
    {ordinal: 1, severity: 'warning'}, {ordinal: 2, severity: 'unknown'},
    {ordinal: 3, severity: 'unknown'},
  ]);
  assert.ok(!JSON.stringify(result).includes('generated-canary'));
});
