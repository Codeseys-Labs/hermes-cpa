'use strict'

// Generated-data Windows development proof, not packaged-release certification.
// Windows Electron -> private loopback relay -> network-none Linux CPA fixture.
// The guest sees only sandboxed documents; tokens and grant URLs remain in runner memory.
// Fresh paths prevent state reuse; bounded processes/protocols prevent hung cleanup.
// Only this run's container, Electron process, relay sockets, staging container and read-only final-probe container are stopped.
// The Electron bootstrap installs network and OS-opener containment before it imports the desktop.
const fs = require('node:fs/promises')
const path = require('node:path')
const net = require('node:net')
const { spawn } = require('node:child_process')
const { createRequire } = require('node:module')
const { randomBytes, createHash } = require('node:crypto')

const CONTAINER_ROOT = '/work/ui-windows'
const WRAPPER_PATH = '/__hermes_plugin_resource__'
const PANEL_PATH = '/v0/resource/plugins/hermes-resource-fixture/panel'
const PYTHON = '/opt/q/venv/bin/python'
const MAX_OUTPUT = 8 * 1024 * 1024
const MAX_PROTOCOL_BUFFER = 262_144
const MAX_STDERR_LINE = 16_384
// Image-owned fixture contract: the same check lists the Linux UI proof asserts.
const RESOURCE_CONTRACT = '/opt/q/host/apps/desktop/e2e/hermes-cpa-resource-ui.ts'
// Text logs under generated directories only; bounds keep an unexpected tree from stalling cleanup.
const LOG_FILE = /(?:\.(?:log|txt|jsonl|out|err)|^LOG(?:\.old)?)$/i
const LOG_SCAN = { entries: 20_000, depth: 10, fileBytes: 16 * 1024 * 1024, totalBytes: 256 * 1024 * 1024, chunk: 1024 * 1024, overlap: 128, deadline: 60_000 }
const SHELL_DENIED = ['openExternal', 'openPath', 'showItemInFolder', 'trashItem']
const GUARD_COUNTERS = ['guarded_sessions', 'observed', 'relay_http', 'relay_ws', 'blocked',
  'protocol_registration_blocked', 'replacement_refused', 'install_failures']

function requireCondition(condition) {
  if (!condition) throw new Error('Qualification condition failed')
}

async function bounded(promise, milliseconds) {
  let timer
  try {
    return await Promise.race([
      promise,
      new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('Deadline exceeded')), milliseconds) }),
    ])
  } finally {
    clearTimeout(timer)
  }
}

function start(executable, args, options = {}) {
  const child = spawn(executable, args, { windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'], ...options })
  const exited = new Promise((resolve, reject) => {
    child.once('error', reject)
    child.once('close', (code, signal) => resolve({ code, signal }))
  })
  // Every owner awaits this promise, including cleanup after partial setup.
  void exited.catch(() => {})
  child.stdin.on('error', () => {})
  return { child, exited }
}

async function run(executable, args, { input = '', timeout = 120_000, ...options } = {}) {
  const process = start(executable, args, options)
  const chunks = []
  let size = 0
  let overflow = false
  process.child.stdout.on('data', chunk => {
    size += chunk.length
    if (size > MAX_OUTPUT) {
      overflow = true
      process.child.kill()
    } else chunks.push(chunk)
  })
  process.child.stderr.resume()
  process.child.stdin.end(input)
  try {
    const result = await bounded(process.exited, timeout)
    requireCondition(result.code === 0 && !overflow)
    return Buffer.concat(chunks).toString('utf8')
  } finally {
    if (process.child.exitCode === null) process.child.kill()
    await bounded(process.exited, 10_000).catch(() => {})
  }
}

// One pending request at a time; bounded input, queue and replies. Failure is terminal.
class JsonProtocol {
  constructor(stream, input, exited, onFailure = () => {}) {
    this.input = input
    this.onFailure = onFailure
    this.buffer = ''
    this.queue = []
    this.pending = null
    this.failure = null
    stream.setEncoding('utf8')
    stream.on('data', chunk => this.receive(chunk))
    void exited.then(() => this.fail(), () => this.fail())
  }

  receive(chunk) {
    // After failure the stream is drained and discarded: nothing is retained or parsed.
    if (this.failure) return
    if (this.buffer.length + chunk.length > MAX_PROTOCOL_BUFFER) return this.fail()
    this.buffer += chunk
    let newline
    while ((newline = this.buffer.indexOf('\n')) !== -1) {
      const line = this.buffer.slice(0, newline)
      this.buffer = this.buffer.slice(newline + 1)
      try {
        const value = JSON.parse(line)
        requireCondition(value && typeof value === 'object' && !Array.isArray(value) && !value.error)
        if (this.pending) {
          const pending = this.pending
          this.pending = null
          pending.resolve(value)
        } else {
          requireCondition(this.queue.length < 8)
          this.queue.push(value)
        }
      } catch { return this.fail() }
    }
  }

  fail() {
    if (this.failure) return
    this.failure = new Error('Protocol unavailable')
    this.buffer = ''
    this.queue.length = 0
    if (this.pending) this.pending.reject(this.failure)
    this.pending = null
    this.onFailure()
  }

  async next(timeout = 30_000) {
    if (this.queue.length) return this.queue.shift()
    if (this.failure) throw this.failure
    requireCondition(!this.pending)
    try {
      return await bounded(new Promise((resolve, reject) => { this.pending = { resolve, reject } }), timeout)
    } catch (error) {
      this.fail()
      throw error
    }
  }

  async command(value, timeout = 30_000) {
    requireCondition(!this.pending && !this.failure && this.queue.length === 0)
    const reply = this.next(timeout)
    this.input.write(JSON.stringify(value) + '\n', error => { if (error) this.fail() })
    return reply
  }
}

const PREPARE = String.raw`
import json, os, pathlib, subprocess, sys
root = '/work/ui-windows'
q = [sys.executable, '/opt/q/scripts/qualification.py']
try:
    assert os.getuid() == 10001
    assert {p.name for p in pathlib.Path('/sys/class/net').iterdir()} == {'lo'}
    assert not pathlib.Path(root).exists()
    for command in (q + ['prepare', '--root', root],
                    q + ['permit', '--root', root, '--hours', '2'],
                    [sys.executable, '/opt/q/qualification/desktop_prepare.py', '--root', root, '--consent']):
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=240)
    os.execv(sys.executable, [sys.executable, '-u', '/opt/q/qualification/dashboard_scenario.py',
                            '--root', root, '--mode', 'http', '--consent'])
except BaseException:
    print(json.dumps({'error': 'windows_preparation_failed'}), flush=True)
    sys.exit(1)
`

const TCP_RELAY = String.raw`
import os, socket, sys, threading
upstream = socket.create_connection(('127.0.0.1', int(sys.argv[1])), timeout=15)
upstream.settimeout(None)
failed = threading.Event()
def upload():
    try:
        while True:
            data = os.read(0, 65536)
            if not data:
                break
            upstream.sendall(data)
    except OSError:
        failed.set()
    finally:
        try:
            upstream.shutdown(socket.SHUT_WR)
        except OSError:
            pass
thread = threading.Thread(target=upload, daemon=True)
thread.start()
try:
    while True:
        data = upstream.recv(65536)
        if not data:
            break
        view = memoryview(data)
        while view:
            sent = os.write(1, view)
            if sent <= 0:
                raise OSError('closed stdout')
            view = view[sent:]
finally:
    upstream.close()
sys.exit(1 if failed.is_set() else 0)
`

async function createRelay(docker, container, port) {
  const sockets = new Set()
  const children = new Set()
  const counters = { accepted: 0, peak: 0, rejected: 0 }
  const server = net.createServer({ allowHalfOpen: true }, socket => {
    if (sockets.size >= 24) { counters.rejected++; socket.destroy(); return }
    sockets.add(socket)
    counters.accepted++
    counters.peak = Math.max(counters.peak, sockets.size)
    socket.setTimeout(120_000, () => socket.destroy())
    const process = start(docker.executable, [...docker.args, 'exec', '-i', container,
      PYTHON, '-u', '-c', TCP_RELAY, String(port)])
    children.add(process)
    process.child.stderr.resume()
    socket.on('error', () => socket.destroy())
    socket.once('close', () => {
      sockets.delete(socket)
      process.child.stdin.destroy()
      if (process.child.exitCode === null) process.child.kill()
    })
    process.child.stdout.on('error', () => socket.destroy())
    socket.pipe(process.child.stdin)
    process.child.stdout.pipe(socket, { end: false })
    void process.exited.then(result => {
      children.delete(process)
      if (result.code === 0) socket.end()
      else socket.destroy()
    }, () => { children.delete(process); socket.destroy() })
  })
  await new Promise((resolve, reject) => {
    server.once('error', reject)
    server.listen(0, '127.0.0.1', resolve)
  })
  const address = server.address()
  requireCondition(address && typeof address !== 'string')
  return {
    port: address.port,
    counters,
    async close() {
      const closed = new Promise(resolve => server.close(resolve))
      for (const socket of sockets) socket.destroy()
      const pending = [...children]
      for (const process of pending) process.child.kill()
      await bounded(Promise.all([closed, ...pending.map(process => process.exited.catch(() => {}))]), 15_000)
    },
  }
}

async function createElectronControl(id, nonce) {
  let socket
  let acceptConnection
  let refuseConnection
  const connected = new Promise((resolve, reject) => { acceptConnection = resolve; refuseConnection = reject })
  void connected.catch(() => {})
  const server = net.createServer(candidate => {
    if (socket) { candidate.destroy(); return }
    socket = candidate
    server.close()
    candidate.on('error', () => candidate.destroy())
    const exited = new Promise(resolve => candidate.once('close', resolve))
    const protocol = new JsonProtocol(candidate, candidate, exited, () => candidate.destroy())
    void protocol.next(15_000).then(hello => {
      requireCondition(hello.ready === true && hello.nonce === nonce && Object.keys(hello).length === 2)
      acceptConnection(protocol)
    }).catch(error => {
      candidate.destroy()
      refuseConnection(error)
    })
  })
  const closed = new Promise(resolve => server.once('close', resolve))
  server.on('error', refuseConnection)
  await new Promise((resolve, reject) => {
    server.once('error', reject)
    server.listen('\\\\.\\pipe\\hermes-cpa-qualification-' + id, resolve)
  })
  return {
    connected,
    async close() {
      if (server.listening) server.close()
      socket?.destroy()
      await bounded(closed, 10_000)
    },
  }
}

// One network policy for the pre-import Electron guard (embedded via toString) and the later Playwright guard.
// Only the generated relay origin is reachable, and its only WebSocket is the gateway socket (/api/ws).
function allowedNetworkUrl(value, relayOrigin) {
  let url
  try { url = new URL(value) } catch { return false }
  if (['file:', 'blob:', 'data:', 'about:', 'hermes-plugin-resource:'].includes(url.protocol)) return true
  if (url.username || url.password) return false
  const relay = new URL(relayOrigin)
  if (url.protocol === 'http:') return url.origin === relay.origin
  return url.protocol === 'ws:' && url.host === relay.host && url.pathname === '/api/ws'
}

// The bootstrap's final guard line is a fixed-shape counter set; anything else is no evidence.
function parseGuardFinal(text) {
  try {
    const value = JSON.parse(text)
    const count = item => Number.isInteger(item) && item >= 0 && item <= 1_000_000_000
    requireCondition(value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).length === GUARD_COUNTERS.length + 3)
    for (const name of GUARD_COUNTERS) requireCondition(count(value[name]))
    requireCondition(typeof value.default_session_guarded === 'boolean')
    requireCondition(Array.isArray(value.blocked_hosts) && value.blocked_hosts.length <= 8 &&
      value.blocked_hosts.every(host => ['http://[redacted-host]', 'https://[redacted-host]',
        'ws://[redacted-host]', 'wss://[redacted-host]', 'other-scheme', 'invalid-url'].includes(host)))
    const denied = value.shell_denied
    requireCondition(denied && typeof denied === 'object' && !Array.isArray(denied) &&
      Object.keys(denied).length === SHELL_DENIED.length && SHELL_DENIED.every(name => count(denied[name])))
    return {
      ...Object.fromEntries(GUARD_COUNTERS.map(name => [name, value[name]])),
      default_session_guarded: value.default_session_guarded,
      blocked_hosts: value.blocked_hosts,
      shell_denied: Object.fromEntries(SHELL_DENIED.map(name => [name, denied[name]])),
    }
  } catch { return null }
}

// The closed list the fixture UI proof asserts: no template, comment or computed entry is accepted.
function checkList(source, name) {
  const match = source.match(new RegExp('const ' + name + ' = \\[([^\\]]*)\\] as const'))
  requireCondition(match)
  const names = [...match[1].matchAll(/'([a-z][a-z0-9-]*)'/g)].map(item => item[1])
  requireCondition(names.length > 0 && new Set(names).size === names.length)
  requireCondition(match[1].replace(/'[a-z][a-z0-9-]*'/g, '').replace(/[\s,]/g, '') === '')
  return names
}

const receiptRows = page => page.locator('details')
  .filter({ has: page.getByText('Operation receipts', { exact: true }) }).locator('li')

// Bounded read-only scan of this run's generated Windows directories after Electron has exited.
// Only fixed credential classes are searched; findings name the file and class, never the value.
async function scanWindowsLogs(root, secrets) {
  const started = Date.now()
  const report = { files: 0, bytes: 0, complete: true, leaked: [] }
  const buffer = Buffer.allocUnsafe(LOG_SCAN.chunk)
  const pending = ['home/.hermes', 'desktop-user-data', 'appdata', 'localappdata', 'tmp']
    .map(name => ({ directory: path.join(root, ...name.split('/')), depth: 0, logs: false }))
  let entries = 0
  // Runner-staged plugin source is image content, not a Windows log.
  const staged = path.join(root, 'home', '.hermes', 'plugins')
  const scanFile = async file => {
    const handle = await fs.open(file, 'r')
    try {
      const { size } = await handle.stat()
      if (size > LOG_SCAN.fileBytes || report.bytes + size > LOG_SCAN.totalBytes) { report.complete = false; return }
      const found = new Set()
      let carry = ''
      for (let offset = 0; offset < size;) {
        const { bytesRead } = await handle.read(buffer, 0, Math.min(buffer.length, size - offset), offset)
        if (bytesRead === 0) break
        offset += bytesRead
        const text = carry + buffer.toString('latin1', 0, bytesRead)
        for (const [kind, secret] of secrets) if (secret && text.includes(secret)) found.add(kind)
        if (/p-[a-f0-9]{48}/.test(text)) found.add('capability')
        carry = text.slice(-LOG_SCAN.overlap)
      }
      report.files++
      report.bytes += size
      for (const kind of found) {
        // Storage paths can embed a resource handle; the finding names the file shape, never a credential.
        if (report.leaked.length < 8) report.leaked.push({ file: path.relative(root, file).replace(/p-[a-f0-9]{48}|[a-f0-9]{64}/g, '[redacted]'), kind })
      }
    } finally { await handle.close() }
  }
  while (pending.length) {
    const { directory, depth, logs } = pending.pop()
    let listing
    try { listing = await fs.readdir(directory, { withFileTypes: true }) } catch (error) {
      if (error.code !== 'ENOENT') report.complete = false
      continue
    }
    for (const entry of listing) {
      if (++entries > LOG_SCAN.entries || Date.now() - started > LOG_SCAN.deadline) { report.complete = false; return report }
      const target = path.join(directory, entry.name)
      if (entry.isDirectory()) {
        if (target === staged) continue
        if (depth < LOG_SCAN.depth) pending.push({ directory: target, depth: depth + 1, logs: logs || entry.name.toLowerCase() === 'logs' })
        else report.complete = false
      } else if (entry.isFile() && (logs || LOG_FILE.test(entry.name))) {
        try { await scanFile(target) } catch { report.complete = false }
      }
    }
  }
  return report
}

const BOOTSTRAP = String.raw`
'use strict'
const fs = require('node:fs')
const path = require('node:path')
const { pathToFileURL } = require('node:url')
const controlNet = require('node:net')
const { app, net, session, shell } = require('electron')
const source = path.join(__dirname, 'desktop')
const userData = path.join(__dirname, 'desktop-user-data')
const remote = new URL(process.env.HERMES_DESKTOP_REMOTE_URL || 'about:blank')
const controlId = process.env.HERMES_CPA_QUALIFICATION_CONTROL_ID || ''
const controlNonce = process.env.HERMES_CPA_QUALIFICATION_CONTROL_NONCE || ''
if (!app || process.env.HERMES_DESKTOP_ISOLATED_BACKEND !== '1' ||
    process.env.HERMES_DESKTOP_USER_DATA_DIR !== userData ||
    remote.protocol !== 'http:' || remote.hostname !== '127.0.0.1' || !remote.port ||
    remote.username || remote.password || remote.pathname !== '/' || remote.search || remote.hash ||
    !/^[a-f0-9]{64}$/.test(process.env.HERMES_DESKTOP_REMOTE_TOKEN || '') ||
    !/^[a-f0-9]{16}$/.test(controlId) || !/^[a-f0-9]{64}$/.test(controlNonce) ||
    app.getVersion() !== JSON.parse(fs.readFileSync(path.join(source, 'package.json'), 'utf8')).version) {
  app.exit(1)
  throw new Error('Qualification guard refused')
}
${allowedNetworkUrl.toString()}
// Runner-visible evidence is fixed-format stderr lines: never URLs, paths or credentials.
const emitLine = (kind, value) => {
  try { process.stderr.write('qualification-' + kind + ':' + value + '\n') } catch { /* The runner treats missing evidence as failure. */ }
}
const lifecycle = event => emitLine('lifecycle', event)
const violation = kind => emitLine('violation', kind)
const guard = {
  sessions: 0, defaultSession: false, observed: 0, relayHttp: 0, relayWs: 0, blocked: 0, blockedHosts: [],
  shellDenied: { openExternal: 0, openPath: 0, showItemInFolder: 0, trashItem: 0 },
  protocolRegistrationBlocked: 0, replacementRefused: 0, installFailures: 0,
}
const snapshot = () => ({
  guarded_sessions: guard.sessions, default_session_guarded: guard.defaultSession, observed: guard.observed,
  relay_http: guard.relayHttp, relay_ws: guard.relayWs, blocked: guard.blocked, blocked_hosts: guard.blockedHosts,
  shell_denied: guard.shellDenied, protocol_registration_blocked: guard.protocolRegistrationBlocked,
  replacement_refused: guard.replacementRefused, install_failures: guard.installFailures,
})
const blockedName = value => {
  try {
    const url = new URL(value)
    return /^(?:https?|wss?):$/.test(url.protocol) ? url.protocol + '//[redacted-host]' : 'other-scheme'
  } catch { return 'invalid-url' }
}
// Every session, including partitions the desktop creates later, filters requests before any renderer uses it.
const guardedSessions = new WeakSet()
const guardSession = candidate => {
  if (!candidate || guardedSessions.has(candidate)) return
  try {
    const webRequest = candidate.webRequest
    const install = webRequest.onBeforeRequest.bind(webRequest)
    let downstream = null
    install((details, callback) => {
      guard.observed++
      if (!allowedNetworkUrl(details.url, remote.origin)) {
        guard.blocked++
        const name = blockedName(details.url)
        if (guard.blockedHosts.length < 8 && !guard.blockedHosts.includes(name)) guard.blockedHosts.push(name)
        callback({ cancel: true })
        return
      }
      if (details.url.startsWith('http:')) guard.relayHttp++
      else if (details.url.startsWith('ws:')) guard.relayWs++
      if (downstream) downstream(details, callback)
      else callback({})
    })
    // Electron keeps one onBeforeRequest listener per session. A later desktop listener may only narrow
    // what this guard already allowed; replacing or clearing the guard fails closed.
    Object.defineProperty(webRequest, 'onBeforeRequest', {
      configurable: false,
      writable: false,
      value: (...args) => {
        if (args.length === 1 && typeof args[0] === 'function') { downstream = args[0]; return }
        guard.replacementRefused++
        violation('network-guard-replacement')
        throw new Error('Qualification network guard cannot be replaced')
      },
    })
    guardedSessions.add(candidate)
    guard.sessions++
  } catch {
    guard.installFailures++
    violation('network-guard-install')
    app.exit(1)
  }
}
// The OS opener is denied and counted; an attempted launch fails the proof and never reaches a real browser.
const denyShell = (name, outcome) => {
  const denied = () => {
    guard.shellDenied[name]++
    violation(name)
    return outcome()
  }
  shell[name] = denied
  if (shell[name] !== denied) throw new Error('Qualification OS opener guard refused')
}
const registrationBlocked = () => {
  guard.protocolRegistrationBlocked++
  return false
}
try {
  denyShell('openExternal', () => Promise.reject(new Error('Qualification denied an OS open')))
  denyShell('openPath', () => Promise.resolve('Qualification denied an OS open'))
  denyShell('showItemInFolder', () => undefined)
  denyShell('trashItem', () => Promise.reject(new Error('Qualification denied an OS trash')))
  app.setAsDefaultProtocolClient = registrationBlocked
  app.removeAsDefaultProtocolClient = registrationBlocked
  app.on('session-created', guardSession)
  app.on('web-contents-created', (_event, contents) => guardSession(contents.session))
  const guardDefaultSession = () => {
    guardSession(session.defaultSession)
    guard.defaultSession = guardedSessions.has(session.defaultSession)
  }
  if (app.isReady()) guardDefaultSession()
  else app.once('ready', guardDefaultSession)
} catch {
  violation('guard-install')
  app.exit(1)
}
const writeFinal = () => emitLine('final', JSON.stringify(snapshot()))
app.once('quit', writeFinal)
process.once('exit', writeFinal)
app.setPath('userData', userData)
app.setPath('sessionData', userData)
app.setAppPath(source)
const control = controlNet.connect('\\\\.\\pipe\\hermes-cpa-qualification-' + controlId)
const emit = value => control.write(JSON.stringify(value) + '\n')
control.once('connect', () => emit({ ready: true, nonce: controlNonce }))
control.on('error', () => app.exit(1))
app.on('browser-window-created', () => lifecycle('window-created'))
app.on('before-quit', () => lifecycle('before-quit'))
// Windows GUI Electron closes stdin; this private runner pipe is not renderer IPC.
let buffer = ''
let busy = false
control.setEncoding('utf8')
control.on('data', async chunk => {
  buffer += chunk
  if (buffer.length > 4096 || busy) { app.exit(1); return }
  const newline = buffer.indexOf('\n')
  if (newline < 0) return
  const line = buffer.slice(0, newline)
  buffer = buffer.slice(newline + 1)
  busy = true
  try {
    const request = JSON.parse(line)
    if (request.command === 'quit' && Object.keys(request).length === 1) {
      control.write(JSON.stringify({ quitting: true }) + '\n', () => app.quit())
    } else if (request.command === 'probe' && Object.keys(request).length === 2) {
      const target = new URL(request.url)
      if (target.protocol !== 'hermes-plugin-resource:' || !/^p-[a-f0-9]{48}$/.test(target.hostname) ||
          target.port || target.username || target.password || target.search || target.hash ||
          !['/__hermes_plugin_resource__', '/v0/resource/plugins/hermes-resource-fixture/panel'].includes(target.pathname)) {
        throw new Error('Probe refused')
      }
      const response = await net.fetch(target.href, { method: 'GET', signal: AbortSignal.timeout(15000) })
      const status = response.status
      await response.body?.cancel()
      emit({ status })
    } else throw new Error('Command refused')
  } catch { emit({ error: 'bootstrap_command_failed' }) }
  finally { busy = false }
})
control.on('close', () => { lifecycle('control-closed'); app.quit() })
import(pathToFileURL(path.join(source, 'dist', 'electron-main.mjs')).href)
  .then(() => lifecycle('main-imported'))
  .catch(() => { lifecycle('main-import-failed'); app.exit(1) })
`

async function parseOptions() {
  requireCondition(process.platform === 'win32')
  const values = new Map()
  const args = process.argv.slice(2)
  for (let index = 0; index < args.length; index++) {
    const name = args[index]
    requireCondition(['--image', '--windows-desktop', '--root', '--consent'].includes(name) && !values.has(name))
    if (name === '--consent') values.set(name, true)
    else {
      requireCondition(index + 1 < args.length && !args[index + 1].startsWith('--'))
      values.set(name, args[++index])
    }
  }
  requireCondition(values.size === 4 && values.get('--consent') === true)
  const image = values.get('--image')
  requireCondition(typeof image === 'string' && /^sha256:[a-f0-9]{64}$/.test(image))
  const root = values.get('--root')
  const source = values.get('--windows-desktop')
  for (const value of [root, source]) {
    requireCondition(typeof value === 'string' && /^[A-Za-z]:[\\/]/.test(value) && !value.slice(2).includes(':'))
    requireCondition(!value.includes('\0') && path.resolve(value).toLowerCase() === path.normalize(value).toLowerCase())
  }
  const parent = path.dirname(root)
  requireCondition(parent !== root && (await fs.realpath(parent)).toLowerCase() === path.resolve(parent).toLowerCase())
  try { await fs.lstat(root); throw new Error('Existing root refused') } catch (error) {
    if (error.code !== 'ENOENT') throw error
  }
  const desktop = await fs.realpath(source)
  requireCondition(desktop.toLowerCase() === path.resolve(source).toLowerCase())
  for (const relative of ['package.json', 'node_modules/electron/dist/electron.exe', 'dist/native/win32-x64']) {
    await fs.access(path.join(desktop, relative))
  }
  return { image, root: path.resolve(root), desktop }
}

async function navigateConsole(frame, label) {
  const link = frame.locator('aside').getByRole('link', { name: new RegExp('^' + label) })
  await link.waitFor({ state: 'attached', timeout: 90_000 })
  const expand = frame.getByRole('button', { name: 'Expand sidebar', exact: true })
  if (await expand.isVisible()) await expand.click()
  await link.click()
}

async function approveRead(page, expect) {
  const review = page.getByRole('dialog', { name: 'Review CPA effect', exact: true })
  await expect(review).toContainText('GET /v8/management/config.yaml')
  await expect(review.getByRole('listitem')).toHaveText(['config_export'])
  await review.getByRole('button', { name: 'Approve exact effects', exact: true }).click()
}

const CONFIG_PROBE = String.raw`
import hashlib, json, pathlib, sys
key = sys.stdin.buffer.readline(256).strip()
with pathlib.Path('/work/ui-windows/.hermes/cpa/instances/default/config.yaml').open('rb') as stream:
    raw = stream.read(16 * 1024 * 1024 + 1)
assert len(key) > 32 and len(raw) <= 16 * 1024 * 1024
print(json.dumps({'contains_key': key in raw, 'sha256': hashlib.sha256(raw).hexdigest()}))
`

async function main() {
  let stage = 'arguments'
  let options
  let docker
  let rootOwned = false
  let staging = false
  let volumeCreated = false
  let helper
  let helperProtocol
  let electron
  let electronProtocol
  let electronControl
  let relay
  let page
  let token = ''
  let key = ''
  let failed = false
  let cleanupFailed = false
  let logsClean = true
  let errorName = ''
  let errorDetail = ''
  let contract = null
  let backendQuiesced = false
  let playwrightBlocked = 0
  let windowsLogs = null
  const guardEvents = { violations: {}, final: null }
  const desktopExit = { quit_requested: false, quit_acknowledged: false, code: null, signal: null, forced: false }
  const suffix = randomBytes(8).toString('hex')
  const controlNonce = randomBytes(32).toString('hex')
  const container = 'hermes-cpa-parity-ui-windows-' + suffix
  const volume = container + '-data'
  const stagingName = container + '-stage'
  const finalName = container + '-final'
  const result = { development_proof: true, packaged_certification: false, desktop_lifecycle: [] }
  const dockerRun = (args, settings) => run(docker.executable, [...docker.args, ...args], settings)
  const setStage = name => { stage = name; process.stdout.write(JSON.stringify({ stage }) + '\n') }
  const scanLogs = stream => {
    let previous = ''
    stream.on('data', chunk => {
      const text = previous + chunk.toString('utf8')
      if ((token && text.includes(token)) || (key && text.includes(key)) || text.includes(controlNonce) || /p-[a-f0-9]{48}/.test(text)) logsClean = false
      previous = text.slice(-256)
    })
  }
  const redact = text => {
    let value = String(text)
    for (const secret of [token, key, controlNonce]) if (secret) value = value.replaceAll(secret, '[redacted]')
    return value.replace(/(?:https?|wss?):\/\/\S+/g, '[url]').replace(/[a-f0-9]{64}|p-[a-f0-9]{48}/g, '[redacted]')
  }
  // Receipt rows are shown as the console renders them; they are not tied to any one mutation here.
  const observeReceipts = async () => (await bounded(receiptRows(page).allTextContents(), 5_000))
    .slice(0, 16).map(row => redact(row.replace(/\s+/g, ' ').trim()).slice(0, 240))
  try {
    options = await parseOptions()
    const requireDesktop = createRequire(path.join(options.desktop, 'package.json'))
    const { chromium } = requireDesktop('playwright')
    const { expect: baseExpect } = requireDesktop('@playwright/test')
    const expect = baseExpect.configure({ timeout: 30_000 })
    const systemRoot = process.env.SystemRoot || process.env.WINDIR
    requireCondition(systemRoot && path.isAbsolute(systemRoot))
    docker = { executable: path.join(systemRoot, 'System32', 'wsl.exe'), args: ['-d', 'Ubuntu', '--exec', 'docker'] }
    const windowsToWsl = value => '/mnt/' + value[0].toLowerCase() + value.slice(2).replaceAll('\\', '/')
    setStage('stage_current_image')
    await fs.mkdir(options.root)
    rootOwned = true
    const stagedDesktop = path.join(options.root, 'desktop')
    const home = path.join(options.root, 'home')
    const userData = path.join(options.root, 'desktop-user-data')
    const evidence = path.join(options.root, 'evidence')
    const contractDirectory = path.join(options.root, 'contract')
    for (const directory of [stagedDesktop, path.join(home, '.hermes', 'plugins'), userData, evidence,
      path.join(options.root, 'appdata'), path.join(options.root, 'localappdata'), path.join(options.root, 'tmp'), contractDirectory]) {
      await fs.mkdir(directory, { recursive: true })
    }
    staging = true
    await dockerRun(['create', '--name', stagingName, '--network', 'none', '--entrypoint', PYTHON, options.image, '-c', 'pass'])
    for (const [source, destination] of [
      ['/opt/q/host/apps/desktop/dist', stagedDesktop],
      ['/opt/q/host/apps/desktop/package.json', stagedDesktop],
      ['/opt/q/hermes-cpa', path.join(home, '.hermes', 'plugins')],
      [RESOURCE_CONTRACT, contractDirectory],
    ]) await dockerRun(['cp', stagingName + ':' + source, windowsToWsl(destination)])
    await dockerRun(['rm', stagingName])
    staging = false
    await fs.symlink(path.join(options.desktop, 'node_modules'), path.join(stagedDesktop, 'node_modules'), 'junction')
    const nativeTarget = path.join(stagedDesktop, 'dist', 'native', 'win32-x64')
    await fs.rm(nativeTarget, { recursive: true, force: true })
    await fs.cp(path.join(options.desktop, 'dist', 'native', 'win32-x64'), nativeTarget, { recursive: true })
    const metadata = JSON.parse(await fs.readFile(path.join(stagedDesktop, 'package.json'), 'utf8'))
    requireCondition(typeof metadata.version === 'string' && metadata.version.length < 100)
    await fs.writeFile(path.join(options.root, 'package.json'), JSON.stringify({
      name: container, version: metadata.version, main: 'bootstrap.cjs', private: true,
    }))
    await fs.writeFile(path.join(options.root, 'bootstrap.cjs'), BOOTSTRAP)
    await fs.writeFile(path.join(userData, 'window-state.json'), JSON.stringify({ x: 0, y: 0, width: 1600, height: 1050, isMaximized: false }))
    await fs.writeFile(path.join(userData, 'zoom-state.json'), JSON.stringify({ zoomLevel: 0 }))
    result.desktop_main_sha256 = createHash('sha256').update(await fs.readFile(path.join(stagedDesktop, 'dist', 'electron-main.mjs'))).digest('hex')
    result.launcher_sha256 = createHash('sha256').update(await fs.readFile(path.join(home, '.hermes', 'plugins', 'hermes-cpa', 'desktop', 'plugin.js'))).digest('hex')
    const contractBytes = await fs.readFile(path.join(contractDirectory, path.basename(RESOURCE_CONTRACT)))
    requireCondition(contractBytes.length > 0 && contractBytes.length < 2 * 1024 * 1024)
    const contractSource = contractBytes.toString('utf8')
    contract = { functional: checkList(contractSource, 'FUNCTIONAL_CHECKS'), containment: checkList(contractSource, 'CONTAINMENT_CHECKS') }
    requireCondition(new Set([...contract.functional, ...contract.containment]).size === contract.functional.length + contract.containment.length)
    result.native_resource_contract_sha256 = createHash('sha256').update(contractBytes).digest('hex')
    await fs.writeFile(path.join(evidence, 'run.json'), JSON.stringify({ image: options.image, container, volume, ...result }, null, 2))

    setStage('prepare_network_none_backend')
    await dockerRun(['volume', 'create', volume])
    volumeCreated = true
    helper = start(docker.executable, [...docker.args, 'run', '--rm', '--init', '-i', '--name', container,
      '--network', 'none', '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
      '--pids-limit', '1024', '--memory', '5g', '--shm-size', '512m', '--user', '10001:10001',
      '--tmpfs', '/tmp:rw,exec,mode=1777,size=2g', '-v', volume + ':/work',
      '--entrypoint', PYTHON, options.image, '-u', '-c', PREPARE])
    helper.child.stderr.resume()
    helperProtocol = new JsonProtocol(helper.child.stdout, helper.child.stdin, helper.exited)
    const ready = await helperProtocol.next(300_000)
    requireCondition(ready.ready === true && Number.isInteger(ready.backend_port) && ready.backend_port > 0 && ready.backend_port <= 65535)
    requireCondition(typeof ready.oauth_email === 'string' && ready.oauth_email.length < 256)
    token = (await dockerRun(['exec', container, 'cat', CONTAINER_ROOT + '/session-token'])).trim()
    requireCondition(/^[a-f0-9]{64}$/.test(token))
    relay = await createRelay(docker, container, ready.backend_port)
    const remoteUrl = 'http://127.0.0.1:' + relay.port

    setStage('launch_isolated_windows_desktop')
    const environment = {
      SystemRoot: systemRoot, WINDIR: systemRoot,
      PATH: [path.join(systemRoot, 'System32'), systemRoot, path.join(systemRoot, 'System32', 'Wbem')].join(';'),
      APPDATA: path.join(options.root, 'appdata'), LOCALAPPDATA: path.join(options.root, 'localappdata'),
      HOME: home, USERPROFILE: home, TEMP: path.join(options.root, 'tmp'), TMP: path.join(options.root, 'tmp'),
      HERMES_HOME: path.join(home, '.hermes'), HERMES_DESKTOP_USER_DATA_DIR: userData,
      HERMES_DESKTOP_APP_NAME: container, HERMES_DESKTOP_IGNORE_EXISTING: '1',
      HERMES_DESKTOP_ISOLATED_BACKEND: '1', HERMES_DESKTOP_SKIP_QUIT_CONFIRM: '1',
      HERMES_DESKTOP_REMOTE_URL: remoteUrl, HERMES_DESKTOP_REMOTE_TOKEN: token,
      HERMES_CPA_QUALIFICATION_CONTROL_ID: suffix, HERMES_CPA_QUALIFICATION_CONTROL_NONCE: controlNonce,
    }
    electronControl = await createElectronControl(suffix, controlNonce)
    electron = start(path.join(options.desktop, 'node_modules', 'electron', 'dist', 'electron.exe'),
      [options.root, '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0'], { cwd: options.root, env: environment })
    scanLogs(electron.child.stdout)
    scanLogs(electron.child.stderr)
    let lifecycleBuffer = ''
    electron.child.stderr.on('data', chunk => {
      lifecycleBuffer += chunk.toString('utf8')
      const lines = lifecycleBuffer.split('\n')
      const rest = lines.pop()
      lifecycleBuffer = rest.length > MAX_STDERR_LINE ? '' : rest
      for (const line of lines) {
        if (line.length > MAX_STDERR_LINE) continue
        const event = line.match(/^qualification-lifecycle:(window-created|before-quit|control-closed|main-imported|main-import-failed)\r?$/)
        if (event && result.desktop_lifecycle.length < 32) result.desktop_lifecycle.push({ event: event[1], during: stage })
        const violation = line.match(/^qualification-violation:([A-Za-z][A-Za-z-]{0,39})\r?$/)
        if (violation) {
          const count = Object.hasOwn(guardEvents.violations, violation[1]) ? guardEvents.violations[violation[1]] : 0
          if (count < 1_000 && (count > 0 || Object.keys(guardEvents.violations).length < 16)) guardEvents.violations[violation[1]] = count + 1
        }
        const final = line.match(/^qualification-final:(\{.*\})\r?$/)
        if (final) guardEvents.final = parseGuardFinal(final[1])
      }
    })
    const devtoolsFound = bounded(new Promise((resolve, reject) => {
      let buffer = ''
      const receive = chunk => {
        buffer = (buffer + chunk.toString('utf8')).slice(-8192)
        const match = buffer.match(/DevTools listening on (ws:\/\/127\.0\.0\.1:\d+\/devtools\/browser\/[a-f0-9-]+)/)
        if (match) { electron.child.stderr.off('data', receive); resolve(match[1]) }
      }
      electron.child.stderr.on('data', receive)
      void electron.exited.then(() => reject(new Error('Electron exited')), reject)
    }), 90_000)
    const controlReady = bounded(electronControl.connected, 60_000).then(protocol => { electronProtocol = protocol })
    // A refused control handshake fails the proof at once; it must not wait for DevTools discovery.
    void devtoolsFound.catch(() => {})
    void controlReady.catch(() => {})
    const [devtools] = await Promise.all([devtoolsFound, controlReady])
    setStage('connect_windows_browser')
    const browser = await chromium.connectOverCDP(devtools, { timeout: 60_000 })
    const context = browser.contexts()[0]
    requireCondition(context)
    context.setDefaultTimeout(30_000)
    context.setDefaultNavigationTimeout(30_000)
    // Additional to the pre-import Electron guard: the same exact-relay policy at the browser-context boundary.
    const relayGuard = value => allowedNetworkUrl(value, remoteUrl)
    await context.route('**/*', route => {
      if (relayGuard(route.request().url())) return route.continue()
      playwrightBlocked++
      return route.abort('blockedbyclient')
    })
    await context.routeWebSocket('**/*', socket => {
      if (relayGuard(socket.url())) socket.connectToServer()
      else { playwrightBlocked++; socket.close() }
    })
    setStage('wait_windows_window')
    page = context.pages().find(candidate => candidate.url().startsWith('file:')) || context.pages()[0] || await context.waitForEvent('page')
    await page.getByRole('button', { name: 'Capabilities', exact: true }).waitFor({ state: 'visible' })
    const screenshot = name => page.screenshot({ path: path.join(evidence, name + '.png') })

    setStage('desktop_opt_in')
    const consoleButton = page.getByRole('button', { name: 'CPA Console', exact: true })
    await page.getByRole('button', { name: 'Capabilities', exact: true }).click()
    await page.getByRole('button', { name: 'Plugins', exact: true }).click()
    const toggle = page.getByRole('switch', { name: 'Desktop: CLI Proxy API Console', exact: true })
    await expect(toggle).not.toBeChecked()
    await expect(consoleButton).toHaveCount(0)
    await toggle.click()
    await expect(toggle).toBeChecked()
    await expect(consoleButton).toBeVisible()
    result.desktop_opt_in = true
    await screenshot('windows-plugin-opt-in')
    const workspace = page.locator('[data-slot="pane-tab"]').filter({ hasText: 'CPA Console' })
    const unlock = async () => {
      await page.getByRole('button', { name: 'Unlock CPA Console', exact: true }).click()
      await page.getByRole('button', { name: 'Unlock console', exact: true }).click()
      await expect(page.locator('[data-cpa-phase="ready"]')).toBeVisible({ timeout: 60_000 })
    }
    await consoleButton.click()
    await expect(workspace).toHaveCount(1)
    await consoleButton.click()
    await expect(workspace).toHaveCount(1)
    await unlock()
    const consoleElement = page.locator('iframe[title="CPA Management Console"]')
    const consoleFrame = page.frameLocator('iframe[title="CPA Management Console"]')
    await expect(consoleElement).toHaveAttribute('sandbox', 'allow-scripts')
    const isolation = await consoleFrame.locator('body').evaluate(() => {
      let parentDenied = false
      let storageDenied = false
      try { void parent.document.body } catch { parentDenied = true }
      try { void localStorage.length } catch { storageDenied = true }
      return { parentDenied, storageDenied }
    })
    requireCondition(isolation.parentDenied && isolation.storageDenied)
    requireCondition(!(await consoleFrame.locator('html').evaluate(element => element.outerHTML)).includes(token))
    result.console_isolated = true

    setStage('generated_oauth')
    await navigateConsole(consoleFrame, 'OAuth Login')
    await consoleFrame.getByRole('button', { name: 'Start Anthropic Login', exact: true }).click()
    const oauthReview = page.getByRole('dialog', { name: 'Review CPA effect', exact: true })
    await expect(oauthReview.getByText('oauth_start', { exact: true })).toBeVisible()
    await oauthReview.getByRole('button', { name: 'Approve exact effects', exact: true }).click()
    const authorization = consoleFrame.getByText(/^https:\/\/claude\.ai\/oauth\/authorize\?/)
    await expect(authorization).toBeVisible()
    const authorizationUrl = (await authorization.textContent()).trim()
    const callback = await helperProtocol.command({ command: 'oauth-authorize', url: authorizationUrl })
    requireCondition(typeof callback.callback === 'string' && callback.callback.length < 8192)
    await consoleFrame.getByLabel('Callback URL', { exact: true }).fill(callback.callback)
    await consoleFrame.getByRole('button', { name: 'Submit Callback URL', exact: true }).click()
    // The state-bound callback has no new effect review. Only OAuth start requires approval.
    await expect(consoleFrame.getByText('Authentication successful!', { exact: true }).first()).toBeVisible({ timeout: 60_000 })
    const counts = await helperProtocol.command({ command: 'oauth-counts' })
    requireCondition(counts.exchanges === 1 && counts.refreshes === 0)
    await navigateConsole(consoleFrame, 'Auth Files')
    await expect(consoleFrame.getByText(ready.oauth_email, { exact: false }).first()).toBeVisible()
    result.oauth_exchanges = counts.exchanges
    result.oauth_refreshes = counts.refreshes
    result.oauth_credential_visible = true
    await screenshot('windows-oauth-completed')

    setStage('configuration_save')
    await navigateConsole(consoleFrame, 'Config Panel')
    await approveRead(page, expect)
    await consoleFrame.getByRole('tab', { name: 'Access & Authentication', exact: true }).click()
    await consoleFrame.getByRole('button', { name: 'Add API Key', exact: true }).click()
    key = 'generated-windows-ui-' + randomBytes(24).toString('hex')
    const keyDialog = consoleFrame.getByRole('dialog', { name: 'Add API Key', exact: true })
    await keyDialog.getByLabel('API Key', { exact: true }).fill(key)
    await keyDialog.getByRole('button', { name: 'Add', exact: true }).click()
    await consoleFrame.getByRole('button', { name: 'Save changes', exact: true }).click()
    await approveRead(page, expect)
    await expect(consoleFrame.getByRole('dialog', { name: 'Review Changes', exact: true })).toBeVisible()
    await consoleFrame.getByRole('button', { name: 'Confirm Save', exact: true }).click()
    await approveRead(page, expect)
    const writeReview = page.getByRole('dialog', { name: 'Review CPA effect', exact: true })
    await expect(writeReview.getByText('config_write', { exact: true })).toBeVisible()
    await expect(writeReview.getByText('authentication_changed', { exact: true })).toBeVisible()
    await screenshot('windows-config-review')
    await writeReview.getByRole('button', { name: 'Approve exact effects', exact: true }).click()
    // API-key changes live-apply; the console must approve its fresh post-save read.
    await approveRead(page, expect)
    await expect(consoleFrame.getByRole('dialog', { name: 'Review Changes', exact: true })).toHaveCount(0)
    await expect(consoleFrame.getByRole('group', { name: 'Unsaved changes', exact: true })).toHaveCount(0)
    const saved = JSON.parse(await dockerRun(['exec', '-i', container, PYTHON, '-c', CONFIG_PROBE], { input: key + '\n' }))
    requireCondition(saved.contains_key === true && /^[a-f0-9]{64}$/.test(saved.sha256))
    // A file observation after the UI reports the save, not a receipt-correlated completion.
    // An uncertain outcome stays unknown and this mutation is never repeated automatically.
    result.config_key_observed_after_save = true
    result.config_sha256_after_save = saved.sha256
    await page.getByText('Operation receipts', { exact: true }).click()
    await expect(receiptRows(page).first()).toBeVisible()
    result.operation_receipts_observed = await observeReceipts()
    await screenshot('windows-config-completed')
    await workspace.hover()
    await workspace.getByRole('button', { name: 'Close', exact: true }).click()
    await expect(workspace).toHaveCount(0)
    await consoleButton.click()
    await unlock()
    await navigateConsole(consoleFrame, 'Config Panel')
    await approveRead(page, expect)
    await consoleFrame.getByRole('tab', { name: 'Access & Authentication', exact: true }).click()
    await expect(consoleFrame.getByRole('button', { name: 'Add API Key', exact: true })).toBeVisible()
    await screenshot('windows-config-reopened')

    setStage('native_resource_presentation')
    await navigateConsole(consoleFrame, 'Hermes Resource Fixture')
    await consoleFrame.getByRole('button', { name: 'Open plugin page', exact: true }).click()
    const resourceReview = page.getByRole('dialog', { name: 'Review CPA effect', exact: true })
    await expect(resourceReview).toContainText('installed plugin hermes-resource-fixture')
    await expect(resourceReview).toContainText('GET is not read-only')
    await expect(resourceReview).toContainText('No console, management or inference authority')
    await expect(resourceReview.getByRole('note')).toContainText(/read or change credentials/)
    await expect(resourceReview.getByText('plugin_code', { exact: true })).toBeVisible()
    await screenshot('windows-resource-review')
    await resourceReview.getByRole('button', { name: 'Approve exact effects', exact: true }).click()
    const resourceElement = page.locator('iframe[title="CPA plugin resource"]')
    await expect(resourceElement).toHaveCount(1, { timeout: 60_000 })
    await expect(resourceElement).toHaveAttribute('sandbox', 'allow-scripts allow-forms')
    const wrapper = page.frameLocator('iframe[title="CPA plugin resource"]')
    const nested = wrapper.locator('iframe[title="Plugin resource"]')
    await expect(nested).toHaveAttribute('sandbox', 'allow-scripts allow-forms')
    const panel = wrapper.frameLocator('iframe[title="Plugin resource"]')
    const fixture = panel.locator('#fixture')
    await expect(fixture).toHaveAttribute('data-state', 'complete', { timeout: 150_000 })
    const markers = await fixture.evaluate((element, names) => names.map(name => [name, element.getAttribute('data-check-' + name)]),
      [...contract.functional, ...contract.containment])
    const deviations = markers.filter(([name, state]) => state !== (contract.functional.includes(name) ? 'pass' : 'contained'))
    // Aggregate counters alone can hide an inconclusive containment check; every named marker must be exact.
    result.native_resource_checks = {
      required: markers.length,
      deviations: deviations.map(([name, state]) => ({ name, state: /^[A-Za-z-]{1,16}$/.test(state ?? '') ? state : 'missing' })),
    }
    requireCondition(deviations.length === 0)
    await expect(fixture).toHaveAttribute('data-failures', '0')
    await expect(fixture).toHaveAttribute('data-leaks', '0')
    await expect(fixture).toHaveAttribute('data-plugin-id', 'hermes-resource-fixture')
    await expect(fixture).toHaveAttribute('data-version', '1.0.0')
    const resourceUrl = await wrapper.locator('html').evaluate(() => location.href)
    const parsed = new URL(resourceUrl)
    requireCondition(parsed.protocol === 'hermes-plugin-resource:' && /^p-[a-f0-9]{48}$/.test(parsed.hostname) && parsed.pathname === WRAPPER_PATH)
    const guest = await panel.locator('html').evaluate(element => {
      let parentDenied = false
      let storageDenied = false
      try { void parent.document.body } catch { parentDenied = true }
      try { void localStorage.length } catch { storageDenied = true }
      return { html: element.outerHTML, parentDenied, storageDenied }
    })
    requireCondition(guest.parentDenied && guest.storageDenied && !guest.html.includes(token) && !guest.html.includes(key))
    requireCondition((await electronProtocol.command({ command: 'probe', url: resourceUrl })).status === 200)
    result.native_resource_complete = true
    result.native_resource_isolated = true
    await screenshot('windows-native-page')
    await page.getByRole('button', { name: 'Close plugin resource', exact: true }).click()
    await expect(resourceElement).toHaveCount(0)
    requireCondition((await electronProtocol.command({ command: 'probe', url: resourceUrl })).status === 410)
    requireCondition((await electronProtocol.command({ command: 'probe', url: `${parsed.protocol}//${parsed.host}${PANEL_PATH}` })).status === 410)
    result.native_resource_revoked = true
    const effects = JSON.parse(await dockerRun(['exec', container, PYTHON, '-c', String.raw`
import json, pathlib
p = pathlib.Path('/work/ui-windows/.hermes/cpa/instances/default/hermes-resource-fixture-effects.jsonl')
rows = [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []
print(json.dumps({'effects': sum(row.get('event') == 'effect' for row in rows)}))
`]))
    requireCondition(effects.effects === 0)
    result.native_effects = effects.effects
    await screenshot('windows-native-closed')
    setStage('complete')
  } catch (error) {
    failed = true
    errorName = error instanceof Error && /^[A-Za-z][A-Za-z0-9]{0,63}$/.test(error.name) ? error.name : 'Error'
    errorDetail = redact(String(error?.message ?? '').split('\n')[0]).slice(0, 500)
    if (page && !page.isClosed() && options) {
      try {
        result.console_unknown = await page.locator('[data-cpa-phase="unknown"]').isVisible()
        const receipts = page.locator('details').filter({ has: page.getByText('Operation receipts', { exact: true }) })
        if (await receipts.isVisible() && await receipts.getAttribute('open') === null) {
          await bounded(receipts.getByText('Operation receipts', { exact: true }).click(), 5_000)
        }
        result.operation_receipts_observed = await observeReceipts()
      } catch { /* Failure evidence must not prevent cleanup. */ }
      await bounded(page.screenshot({ path: path.join(options.root, 'evidence', 'failure.png') }), 15_000).catch(() => {})
    }
  } finally {
    if (electron) {
      const alive = electron.child.exitCode === null && electron.child.signalCode === null
      try {
        if (alive) {
          desktopExit.quit_requested = true
          if (electronProtocol && !electronProtocol.failure) {
            const acknowledgement = await electronProtocol.command({ command: 'quit' }, 10_000)
            desktopExit.quit_acknowledged = acknowledgement.quitting === true && Object.keys(acknowledgement).length === 1
          } else if (electronControl) await electronControl.close()
          electron.child.stdin.end()
        }
        const closed = await bounded(electron.exited, 20_000)
        desktopExit.code = closed.code
        desktopExit.signal = closed.signal
      } catch {
        try {
          if (electron.child.exitCode === null && electron.child.signalCode === null) {
            desktopExit.forced = true
            await run(path.join(process.env.SystemRoot || process.env.WINDIR, 'System32', 'taskkill.exe'),
              ['/PID', String(electron.child.pid), '/T', '/F'], { timeout: 15_000 })
          }
          const closed = await bounded(electron.exited, 10_000)
          desktopExit.code = closed.code
          desktopExit.signal = closed.signal
        } catch { cleanupFailed = electron.child.exitCode === null && electron.child.signalCode === null }
      }
    }
    if (electronControl) await electronControl.close().catch(() => { cleanupFailed = true })
    // Electron has exited and its pipes are closed: its file logs are flushed and final.
    if (electron && options) {
      try { windowsLogs = await scanWindowsLogs(options.root, [['token', token], ['control_nonce', controlNonce]]) }
      catch { windowsLogs = { files: 0, bytes: 0, complete: false, leaked: [] } }
    }
    if (relay) await relay.close().catch(() => { cleanupFailed = true })
    if (helper) {
      try {
        const stopped = await helperProtocol.command({ command: 'stop' }, 180_000)
        requireCondition(stopped.stopped === true && stopped.quiesced === true && stopped.log_clean === true)
        helper.child.stdin.end()
        requireCondition((await bounded(helper.exited, 30_000)).code === 0)
        backendQuiesced = true
        result.backend_quiesced = true
      } catch {
        cleanupFailed = true
        await dockerRun(['stop', '--time', '20', container], { timeout: 60_000 }).catch(() => {})
        helper.child.stdin.end()
        if (helper.child.exitCode === null) helper.child.kill()
        await bounded(helper.exited, 15_000).catch(() => {})
      }
    }
    // The final key observation reads the retained volume read-only after every writer is gone.
    if (key && backendQuiesced) {
      try {
        const finalState = JSON.parse(await dockerRun(['run', '--rm', '-i', '--name', finalName,
          '--network', 'none', '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
          '--pids-limit', '64', '--memory', '256m', '--user', '10001:10001',
          '-v', volume + ':/work:ro', '--entrypoint', PYTHON, options.image, '-c', CONFIG_PROBE],
        { input: key + '\n', timeout: 60_000 }))
        requireCondition(typeof finalState.contains_key === 'boolean' && /^[a-f0-9]{64}$/.test(finalState.sha256))
        result.config_key_present_final = finalState.contains_key
        result.config_sha256_final = finalState.sha256
      } catch {
        await dockerRun(['rm', '-f', finalName]).catch(() => {})
        try { requireCondition((await dockerRun(['ps', '-aq', '--filter', 'name=^' + finalName + '$'])).trim() === '') } catch { cleanupFailed = true }
      }
    }
    if (staging) await dockerRun(['rm', '-f', stagingName]).catch(() => { cleanupFailed = true })
    result.relay_accepted = relay?.counters.accepted ?? 0
    result.relay_peak = relay?.counters.peak ?? 0
    result.relay_rejected = relay?.counters.rejected ?? 0
    result.process_logs_clean = logsClean
    result.playwright_blocked_requests = playwrightBlocked
    const proofFailures = []
    if (!logsClean) proofFailures.push('process_logs_leak')
    if (electron) {
      let outcome = 'clean_quit'
      if (desktopExit.forced) outcome = 'forced'
      else if (desktopExit.code === null && desktopExit.signal === null) outcome = 'unobserved'
      else if (desktopExit.code !== 0 || desktopExit.signal !== null) outcome = 'abnormal'
      else if (!desktopExit.quit_requested) outcome = 'unrequested'
      else if (!desktopExit.quit_acknowledged) outcome = 'unacknowledged'
      result.desktop_exit = { ...desktopExit, outcome }
      if (outcome !== 'clean_quit') proofFailures.push('desktop_exit_' + outcome)
      const guardFinal = guardEvents.final
      result.desktop_guard = guardFinal
      result.desktop_guard_violations = guardEvents.violations
      if (!guardFinal) proofFailures.push('desktop_guard_final_missing')
      else {
        if (!guardFinal.default_session_guarded || guardFinal.guarded_sessions < 1) proofFailures.push('network_guard_not_installed')
        if (guardFinal.observed < 1) proofFailures.push('network_guard_unobserved')
        if (guardFinal.replacement_refused > 0 || guardFinal.install_failures > 0) proofFailures.push('network_guard_compromised')
        if (SHELL_DENIED.some(name => guardFinal.shell_denied[name] > 0)) proofFailures.push('os_launch_attempted')
      }
      if (Object.keys(guardEvents.violations).length) proofFailures.push('desktop_guard_violation_reported')
      result.windows_log_scan = windowsLogs
      result.windows_logs_clean = Boolean(windowsLogs && windowsLogs.complete && windowsLogs.leaked.length === 0)
      if (!windowsLogs || !windowsLogs.complete) proofFailures.push('windows_log_scan_incomplete')
      else if (windowsLogs.leaked.length) proofFailures.push('windows_logs_leak')
    }
    if (key && result.config_key_present_final === undefined) proofFailures.push('config_key_final_unobserved')
    else if (key && result.config_key_present_final !== true) proofFailures.push('config_key_final_absent')
    result.cleanup_complete = !cleanupFailed
    result.evidence_retained = volumeCreated
    const report = {
      ok: !failed && !cleanupFailed && proofFailures.length === 0,
      stage,
      ...(failed ? { error: errorName, detail: errorDetail } : {}),
      ...(proofFailures.length ? { proof_failures: proofFailures } : {}),
      ...result,
    }
    if (rootOwned) await fs.writeFile(path.join(options.root, 'evidence', 'result.json'), JSON.stringify(report, null, 2)).catch(() => {})
    process.stdout.write(JSON.stringify(report) + '\n')
    token = ''
    key = ''
    process.exitCode = report.ok ? 0 : 1
  }
}

void main().catch(() => {
  process.stdout.write(JSON.stringify({ ok: false, stage: 'runner', error: 'Error' }) + '\n')
  process.exitCode = 1
})
