/**
 * Shared browser-side proof for the generated native resource plugin
 * (qualification/native-resource-fixture), used by the real Desktop (Electron) and real
 * Dashboard (Chromium) scenarios. The UI image copies this file next to the specs as
 * apps/desktop/e2e/hermes-cpa-resource-ui.ts.
 *
 * Nothing here fakes a host: every function drives or observes the real launcher, the
 * trusted review dialog, the real presentation origin and the real native plugin. Facts a
 * page could forge about itself are cross-checked with an independent client of the same
 * presentation origin (Electron main-process `net.fetch` or a Node HTTP(S) client) and with
 * the plugin's own out-of-band effect log (a JSONL file inside the generated root).
 */
import * as fs from 'node:fs'
import * as http from 'node:http'
import * as https from 'node:https'
import * as path from 'node:path'
import { randomBytes } from 'node:crypto'
import { once } from 'node:events'
import { buffer } from 'node:stream/consumers'
import { gunzipSync } from 'node:zlib'
import { expect } from '@playwright/test'
import type {
  BrowserContext, ConsoleMessage, ElectronApplication, Frame, FrameLocator, Locator, Page, Request, Route,
  Response as NetworkResponse
} from '@playwright/test'

export const PLUGIN_ID = 'hermes-resource-fixture'
export const MENU_LABEL = 'Hermes Resource Fixture'
export const BASE_PATH = `/v0/resource/plugins/${PLUGIN_ID}`
export const ENTRY_PATH = `${BASE_PATH}/panel`
export const WRAPPER_PATH = '/__hermes_plugin_resource__'
/** The first installed build (version 1.0.0) is generation 1. */
export const FIXTURE_VERSION = '1.0.0'
export const FIXTURE_GENERATION = '1'
const EFFECT_LOG = 'hermes-resource-fixture-effects.jsonl'
const HANDLE = /^p-[a-f0-9]{48}$/
const LARGE_RESPONSE_BYTES = 24 * 1024 * 1024
const SLOW_EFFECT_MS = 20_000

const FUNCTIONAL_CHECKS = [
  'entry', 'script-relative', 'css-relative', 'image-relative', 'module-root-relative', 'module-dynamic',
  'gzip-script', 'gzip-module', 'fetch-json', 'fetch-query', 'fetch-binary', 'fetch-gzip',
  'redirect-relative', 'redirect-absolute-path', 'redirect-dot-segments'
] as const
const CONTAINMENT_CHECKS = [
  'headers-sanitized', 'redirect-outside', 'redirect-protocol-relative', 'redirect-unregistered',
  'outside-fetch', 'outside-image', 'outside-script', 'root-fallthrough', 'api-fallthrough',
  'plugin-api-fallthrough', 'management-fallthrough', 'unregistered-resource', 'frame-isolation',
  'storage-isolation', 'service-worker'
] as const
interface PopupAttempt {
  kind: 'popup'
  name: string
  button: string
}
/** `frame-src` is the wrapper's policy, `form-action` the guest document's: each names the grant origin only. */
type RefusalDirective = 'frame-src' | 'form-action'
interface NavigationAttempt {
  kind: 'navigation'
  name: string
  button: string
  /** The policy directive of the grant origin that must refuse this attempt's destination. */
  directive: RefusalDirective
  /** The destination exactly as Chromium names it in that refusal. */
  destination: RegExp
}
/**
 * A popup has no `allow-popups` in the sandbox, so Chromium refuses the new window before any navigation exists
 * and the guest page must stay. An attempt that navigates the guest frame itself (location, link, meta, and the
 * form submission) must be refused by the grant origin's own Content Security Policy; Chromium may then keep the
 * page or replace it with its error document, which the proof accepts only together with that refusal.
 */
type OutsideAttempt = PopupAttempt | NavigationAttempt
/** `frame-src` refusals name the destination by its origin only (Chromium drops the path). */
const OUTSIDE_FRAME_DESTINATION = /^https:\/\/outside\.invalid\/[^?#]*$/
/** The fixture's form is a GET form with one hidden field; `form-action` refusals name the full destination. */
const OUTSIDE_FORM_DESTINATION = /^https:\/\/outside\.invalid\/hermes-fixture-form\?probe=1$/
const OUTSIDE_NAVIGATION: ReadonlyArray<OutsideAttempt> = [
  { kind: 'navigation', name: 'location', button: 'nav-location-outside', directive: 'frame-src', destination: OUTSIDE_FRAME_DESTINATION },
  { kind: 'popup', name: 'open', button: 'nav-open-outside' },
  { kind: 'navigation', name: 'link', button: 'nav-link-outside', directive: 'frame-src', destination: OUTSIDE_FRAME_DESTINATION },
  { kind: 'popup', name: 'link-blank', button: 'nav-link-blank-outside' },
  { kind: 'navigation', name: 'form', button: 'nav-form-outside', directive: 'form-action', destination: OUTSIDE_FORM_DESTINATION },
  { kind: 'navigation', name: 'meta', button: 'nav-meta-outside', directive: 'frame-src', destination: OUTSIDE_FRAME_DESTINATION }
]
/** URL prefix of the document Chromium puts into a frame whose navigation it refused. */
const BLOCKED_FRAME_URL = 'chrome-error://chromewebdata'
/**
 * Chromium's console report of an enforced refusal (never a report-only one): `Framing '<destination>' violates
 * ... "frame-src <sources>"` and `Sending form data to '<destination>' violates ... "form-action <sources>"`.
 */
const CSP_REFUSAL =
  /^(Framing|Sending form data to) '([^']*)' violates the following Content Security Policy directive: "(frame-src|form-action) ([^"]*)"\. The request has been blocked\./

interface Refusal {
  directive: RefusalDirective
  destination: string
  /** The directive's source list (the grant origin for a host-owned policy). */
  sources: string[]
}

function parseRefusal(text: string): Refusal | undefined {
  const match = CSP_REFUSAL.exec(text)
  if (!match) return undefined
  const [, verb, destination, directive, sources] = match
  const framing = directive === 'frame-src'
  if ((verb === 'Framing') !== framing) return undefined
  return { directive: framing ? 'frame-src' : 'form-action', destination, sources: sources.split(' ') }
}

/** Playwright spells a CSP-blocked request's failure as Chromium's blocked reason, or as its net error. */
const CSP_BLOCKED = /^(csp|net::ERR_BLOCKED_BY_CSP)$/
/** Request headers a presentation host and owner must never give a plugin. */
const FORBIDDEN_REQUEST_HEADER = /^(cookie|authorization|proxy-authorization|x-hermes-|x-management-key|x-api-key|forwarded|x-forwarded-|x-real-ip|x-cpa-)/
/** Response headers a plugin must never be able to use against the host. */
const DROPPED_RESPONSE_HEADERS = [
  'set-cookie', 'www-authenticate', 'refresh', 'x-frame-options', 'access-control-allow-credentials',
  'cross-origin-resource-policy', 'location'
] as const

// ── Independent effect log ──────────────────────────────────────────────────────────────

export interface EffectEvent {
  event: string
  seq: number
  route: string
  detail: string
  generation: string
  instance_nonce: string
}

interface EffectWire {
  event?: unknown
  seq?: unknown
  route?: unknown
  detail?: unknown
  generation?: unknown
  instance_nonce?: unknown
}

export function effectLogPath(root: string): string {
  return path.join(root, '.hermes', 'cpa', 'instances', 'default', EFFECT_LOG)
}

/** Every event the native plugin appended (a trailing partial line is not yet an event). */
export function readEffectLog(root: string): EffectEvent[] {
  let text: string
  try {
    text = fs.readFileSync(effectLogPath(root), 'utf8')
  } catch (error) {
    if (error instanceof Error && 'code' in error && error.code === 'ENOENT') return []
    throw error
  }
  const lines = text.split('\n')
  if (!text.endsWith('\n')) lines.pop()
  return lines.filter(line => line.length > 0).map((line): EffectEvent => {
    const wire = JSON.parse(line) as EffectWire
    if (typeof wire.event !== 'string') throw new Error('Effect log is corrupt')
    return {
      event: wire.event, seq: Number(wire.seq), route: String(wire.route ?? ''), detail: String(wire.detail ?? ''),
      generation: String(wire.generation ?? ''), instance_nonce: String(wire.instance_nonce ?? '')
    }
  })
}

/** Counted native effects only (a slow effect's later "finished" line is not another effect). */
export function countedEffects(root: string): EffectEvent[] {
  return readEffectLog(root).filter(entry => entry.event === 'effect')
}

// ── Independent clients of one presentation origin ──────────────────────────────────────

export interface RawResponse {
  status: number
  /** Lower-case names; repeated headers are joined with ", ". */
  headers: Record<string, string>
  body: Buffer
}
export type PresentationProbe = (
  method: string, requestPath: string, headers?: Record<string, string>
) => Promise<RawResponse>
export interface PresentationDriver {
  probe: (origin: string) => PresentationProbe
  /**
   * How a gzip resource reaches this client. `gzip`: as the host sent it (a raw HTTP(S) client keeps the
   * compressed bytes and `Content-Encoding: gzip`). `decoded`: through Electron's `net.fetch` and the host's
   * protocol handler, which has consumed the encoding: the body is plain and no `Content-Encoding` remains.
   */
  encoding: 'gzip' | 'decoded'
}

function flattenHeaders(raw: http.IncomingHttpHeaders): Record<string, string> {
  const headers: Record<string, string> = {}
  for (const [name, value] of Object.entries(raw)) {
    if (value !== undefined) headers[name.toLowerCase()] = Array.isArray(value) ? value.join(', ') : value
  }
  return headers
}

/**
 * Send one request to 127.0.0.1 on the origin's port (the backend for http, the TLS ingress
 * for https), stating the origin's authority as Host so no DNS is involved. HTTPS trusts only
 * the generated certificate. Resolves at the response headers; the body is returned unread.
 */
async function startRequest(
  origin: URL, ca: string | undefined, method: string, requestPath: string, headers: Record<string, string>
): Promise<http.IncomingMessage> {
  const port = Number(origin.port)
  if (!Number.isInteger(port) || port < 1) throw new Error('Presentation origin needs an explicit port')
  const options: http.RequestOptions = {
    host: '127.0.0.1', port, method, path: requestPath, agent: false,
    headers: { host: origin.host, accept: '*/*', ...headers }
  }
  const request = ca === undefined
    ? http.request(options)
    : https.request({ ...options, ca, servername: origin.hostname, rejectUnauthorized: true })
  request.setTimeout(60_000, () => { request.destroy(new Error('Presentation request timed out')) })
  request.end()
  const [response] = await once(request, 'response') as [http.IncomingMessage]
  return response
}

/** Raw HTTP(S) client of a Dashboard presentation origin; bodies are returned undecoded. */
export function rawProbe(origin: string, ca?: string): PresentationProbe {
  const target = new URL(origin)
  return async (method, requestPath, headers = {}) => {
    const response = await startRequest(target, ca, method, requestPath, headers)
    return { status: response.statusCode ?? 0, headers: flattenHeaders(response.headers), body: await buffer(response) }
  }
}

interface MainFetchResult {
  status: number
  headers: Record<string, string>
  body: number[]
  error: string
}

/** One request through the Electron main process, i.e. the real registered protocol handler. */
export async function electronMainFetch(
  app: ElectronApplication, target: string, method: string, headers: Record<string, string>
): Promise<RawResponse> {
  const result: MainFetchResult = await app.evaluate(
    async ({ net }, input: { target: string; method: string; headers: Record<string, string> }) => {
      try {
        const response: Response = await net.fetch(input.target, { method: input.method, headers: input.headers })
        const bytes = new Uint8Array(await response.arrayBuffer())
        const collected: Record<string, string> = {}
        response.headers.forEach((value, name) => { collected[name.toLowerCase()] = value })
        return { status: response.status, headers: collected, body: Array.from(bytes), error: '' }
      } catch (error) {
        return { status: 0, headers: {} as Record<string, string>, body: [] as number[], error: String(error) }
      }
    },
    { target, method, headers }
  )
  if (result.error) throw new Error(`Electron main-process fetch failed: ${result.error}`)
  return { status: result.status, headers: result.headers, body: Buffer.from(result.body) }
}

export function electronDriver(app: ElectronApplication): PresentationDriver {
  return {
    probe: origin => (method, requestPath, headers = {}) => electronMainFetch(app, origin + requestPath, method, headers),
    encoding: 'decoded'
  }
}

export function dashboardDriver(ca?: string): PresentationDriver {
  return { probe: origin => rawProbe(origin, ca), encoding: 'gzip' }
}

// ── Host chrome: menu, review, approval ─────────────────────────────────────────────────

function approveButton(host: Page): Locator {
  return host.getByRole('button', { name: 'Approve exact effects', exact: true })
}

export function resourceFrameElement(host: Page): Locator {
  return host.locator('iframe[title="CPA plugin resource"]')
}

export function closeResourceButton(host: Page): Locator {
  return host.getByRole('button', { name: 'Close plugin resource', exact: true })
}

/** Select the installed plugin's native menu entry in the opaque CPA Management Center sidebar. */
export async function openNativeMenu(consoleFrame: FrameLocator): Promise<void> {
  const link = consoleFrame.locator('aside').getByRole('link', { name: new RegExp(`^${MENU_LABEL}`) })
  // The plugin list is fetched after the console connects: wait for the entry, then expand if needed.
  await link.waitFor({ state: 'attached', timeout: 90_000 })
  const expand = consoleFrame.getByRole('button', { name: 'Expand sidebar', exact: true })
  if (await expand.isVisible()) await expand.click()
  await link.click()
  await expect(consoleFrame.getByRole('button', { name: 'Open plugin page', exact: true })).toBeVisible({ timeout: 30_000 })
}

/** Ask the guest to open the plugin page and require the full trusted effect review. */
export async function requestResourceReview(host: Page, consoleFrame: FrameLocator): Promise<void> {
  await consoleFrame.getByRole('button', { name: 'Open plugin page', exact: true }).click()
  await expect(approveButton(host)).toBeVisible({ timeout: 30_000 })
  await expect(host.getByText(/GET is not read-only/).first()).toBeVisible()
  await expect(host.getByText(new RegExp(`installed plugin ${PLUGIN_ID}`)).first()).toBeVisible()
  await expect(host.getByText(/No console, management or inference authority/).first()).toBeVisible()
  await expect(host.getByRole('note').first()).toContainText(/read or change credentials/)
  await expect(host.getByText('plugin_code', { exact: true })).toBeVisible()
}

export async function declineReview(host: Page): Promise<void> {
  const cancel = host.getByRole('button', { name: 'Cancel', exact: true })
  if (await cancel.count() > 0) await cancel.click()
  else await host.keyboard.press('Escape')
  await expect(approveButton(host)).toHaveCount(0)
}

export async function approveReview(host: Page): Promise<void> {
  await approveButton(host).click()
}

// ── The presentation itself ─────────────────────────────────────────────────────────────

export interface ResourceFrames {
  wrapper: Frame
  panel: Frame
  panelLocator: FrameLocator
  /** scheme://host[:port] of the grant-specific presentation origin. */
  origin: string
  handle: string
  wrapperUrl: string
  panelUrl: string
}

/** Wait for the host-owned wrapper and the sandboxed native entry page nested inside it. */
export async function waitForResourceFrames(host: Page): Promise<ResourceFrames> {
  const element = resourceFrameElement(host)
  await expect(element).toHaveCount(1, { timeout: 60_000 })
  await expect(element).toHaveAttribute('sandbox', 'allow-scripts allow-forms')
  const handle = await element.elementHandle()
  const wrapper = await handle?.contentFrame()
  if (!wrapper) throw new Error('Resource wrapper frame unavailable')
  await expect.poll(() => wrapper.url(), { timeout: 60_000 }).toContain(WRAPPER_PATH)
  await expect.poll(() => wrapper.childFrames().length, { timeout: 60_000 }).toBe(1)
  const panel = wrapper.childFrames()[0]
  await expect.poll(() => panel.url(), { timeout: 60_000 }).toContain(ENTRY_PATH)
  const parsed = new URL(wrapper.url())
  const origin = `${parsed.protocol}//${parsed.host}`
  const handleId = parsed.hostname.split('.')[0]
  expect(handleId).toMatch(HANDLE)
  expect(wrapper.url()).toBe(origin + WRAPPER_PATH)
  expect(panel.url()).toBe(origin + ENTRY_PATH)
  const panelElement = await panel.frameElement()
  expect(await panelElement.getAttribute('sandbox')).toBe('allow-scripts allow-forms')
  return {
    wrapper, panel, origin, handle: handleId, wrapperUrl: wrapper.url(), panelUrl: panel.url(),
    panelLocator: host.frameLocator('iframe[title="CPA plugin resource"]').frameLocator('iframe[title="Plugin resource"]')
  }
}

export interface PanelReport {
  plugin_id: string
  generation: string
  location: { href: string; origin: string; protocol: string; pathname: string }
  violations: Array<{ directive: string; blocked: string }>
  failures: number
  leaks: number
}

/** Every automatic functional check passed and every containment check was contained. */
export async function expectPanelComplete(frames: ResourceFrames): Promise<PanelReport> {
  const fixture = frames.panelLocator.locator('#fixture')
  await expect(fixture).toHaveAttribute('data-state', 'complete', { timeout: 150_000 })
  await expect(fixture).toHaveAttribute('data-failures', '0')
  await expect(fixture).toHaveAttribute('data-leaks', '0')
  await expect(fixture).toHaveAttribute('data-plugin-id', PLUGIN_ID)
  await expect(fixture).toHaveAttribute('data-version', FIXTURE_VERSION)
  await expect(fixture).toHaveAttribute('data-generation', FIXTURE_GENERATION)
  for (const name of FUNCTIONAL_CHECKS) await expect(fixture, name).toHaveAttribute(`data-check-${name}`, 'pass')
  for (const name of CONTAINMENT_CHECKS) await expect(fixture, name).toHaveAttribute(`data-check-${name}`, 'contained')
  await expect(fixture).not.toHaveAttribute('data-check-watchdog', /.*/)
  // The plugin's own scripts and modules (genuine gzip, root-relative and dynamic ES modules) ran.
  const root = frames.panelLocator.locator('html')
  await expect(root).toHaveAttribute('data-module', 'loaded')
  await expect(root).toHaveAttribute('data-module-static', `static-ok-g${FIXTURE_GENERATION}`)
  await expect(root).toHaveAttribute('data-module-dynamic', `dynamic-ok-g${FIXTURE_GENERATION}`)
  await expect(root).toHaveAttribute('data-gzip-script', `gzip-script-ok-g${FIXTURE_GENERATION}`)
  await expect(root).toHaveAttribute('data-gzip-module', `gzip-module-ok-g${FIXTURE_GENERATION}`)
  expect(await frames.panel.title()).toBe('fixture:complete:0:0')
  const report = JSON.parse(await frames.panelLocator.locator('#fixture-report').textContent() ?? '') as PanelReport
  expect(report.plugin_id).toBe(PLUGIN_ID)
  expect(report.location.href).toBe(frames.panelUrl)
  expect(report.location.pathname).toBe(ENTRY_PATH)
  expect(await frames.panel.evaluate(() => window.origin === 'null'),
    'sandboxed document must have an opaque security origin').toBe(true)
  expect(report.failures).toBe(0)
  expect(report.leaks).toBe(0)
  // The host policy never blocked the plugin's own resources.
  expect(report.violations.filter(entry => String(entry.blocked).startsWith(frames.origin))).toEqual([])
  return report
}

interface HeaderReport {
  status: number
  report: { names: string[]; forbidden_present: string[] }
}

/** From inside the guest: what the native handler saw of the request, and what the guest can reach. */
export async function expectGuestContained(frames: ResourceFrames): Promise<void> {
  const body = frames.panelLocator.locator('body')
  const seen = await body.evaluate(async (): Promise<HeaderReport> => {
    const response = await fetch('api/headers', { cache: 'no-store' })
    return { status: response.status, report: await response.json() as HeaderReport['report'] }
  })
  expect(seen.status).toBe(200)
  expect(seen.report.forbidden_present).toEqual([])
  expect(seen.report.names.filter(name => FORBIDDEN_REQUEST_HEADER.test(name))).toEqual([])
  const isolation = await body.evaluate(() => {
    const result = { parent: 'contained', top: 'contained', storage: 'contained', cookie: 'contained' }
    try { void window.parent.document.body; result.parent = 'LEAK' } catch { /* denied */ }
    try { void window.top?.document.body; result.top = 'LEAK' } catch { /* denied */ }
    try { void window.localStorage.length; result.storage = 'LEAK' } catch { /* denied */ }
    try { void document.cookie; result.cookie = 'LEAK' } catch { /* denied */ }
    return { ...result, framed: window.top !== window, orphan: window.opener === null, referrer: document.referrer }
  })
  expect(isolation).toEqual({
    parent: 'contained', top: 'contained', storage: 'contained', cookie: 'contained',
    framed: true, orphan: true, referrer: ''
  })
}

/** No generated credential or Hermes/CPA authority appears in anything the guest can see. */
export async function expectNoAuthorityInGuest(frames: ResourceFrames, secrets: string[]): Promise<void> {
  const html = await frames.panelLocator.locator('html').evaluate(element => element.outerHTML)
  const wrapper = await frames.wrapper.content()
  for (const secret of secrets) {
    expect(secret.length, 'a credential to look for must be long enough to be meaningful').toBeGreaterThanOrEqual(16)
    for (const haystack of [html, wrapper, frames.wrapperUrl, frames.panelUrl]) {
      expect(haystack).not.toContain(secret)
    }
  }
}

function decoded(body: Buffer): Buffer {
  return body.length > 2 && body[0] === 0x1f && body[1] === 0x8b ? gunzipSync(body) : body
}

function expectHostOwnedHeaders(headers: Record<string, string>, origin: string, kind: 'resource' | 'wrapper'): void {
  expect(Object.keys(headers).filter(name => name.startsWith('x-hermes-'))).toEqual([])
  for (const name of DROPPED_RESPONSE_HEADERS) expect(headers[name], `${name} must be dropped`).toBeUndefined()
  expect(headers['cache-control']).toBe('no-store')
  expect(headers['x-content-type-options']).toBe('nosniff')
  expect(headers['referrer-policy']).toBe('no-referrer')
  expect(headers['access-control-allow-origin']).toBe('*')
  const policy = headers['content-security-policy'] ?? ''
  for (const directive of [
    "default-src 'none'", "base-uri 'none'", "object-src 'none'", "worker-src 'none'",
    `script-src 'unsafe-inline' ${origin}`, `connect-src ${origin}`, `form-action ${origin}`,
    `frame-src ${kind === 'wrapper' ? origin : "'none'"}`
  ]) expect(policy).toContain(directive)
  // The host policy replaces the plugin's permissive one; it never merges it.
  expect(policy).not.toMatch(/unsafe-eval|\*|outside\.invalid/)
}

/** What the browser's own origin returns for exact native bytes, as seen by an independent client. */
export async function inspectPresentation(frames: ResourceFrames, driver: PresentationDriver): Promise<void> {
  const probe = driver.probe(frames.origin)
  const entry = await probe('GET', ENTRY_PATH)
  expect(entry.status).toBe(200)
  expect(entry.headers['content-type']).toMatch(/^text\/html/)
  expectHostOwnedHeaders(entry.headers, frames.origin, 'resource')
  const page = entry.body.toString('utf8')
  expect(page).toContain(`data-plugin-id="${PLUGIN_ID}"`)
  // Root-relative module URLs reach the browser exactly as the plugin wrote them.
  expect(page).toContain(`<script type="module" src="${BASE_PATH}/module.js">`)
  expect(page).not.toContain(frames.origin)

  const wrapper = await probe('GET', WRAPPER_PATH)
  expect(wrapper.status).toBe(200)
  expectHostOwnedHeaders(wrapper.headers, frames.origin, 'wrapper')
  expect(wrapper.body.toString('utf8')).toContain('<iframe title="Plugin resource" sandbox="allow-scripts allow-forms"')
  expect(wrapper.body.toString('utf8')).not.toContain('allow-same-origin')

  /** A gzip resource as this client receives it; a length stated for a body is that body's length either way. */
  const expectGzipResource = (response: RawResponse): void => {
    if (driver.encoding === 'gzip') {
      expect(response.headers['content-encoding']).toBe('gzip')
    } else {
      expect(['identity', undefined], 'the consumed Content-Encoding must not remain on a decoded body')
        .toContain(response.headers['content-encoding'])
      expect(response.body[0] === 0x1f && response.body[1] === 0x8b, 'a decoded body is not gzip').toBe(false)
    }
    if (response.headers['content-length'] !== undefined) {
      expect(Number(response.headers['content-length'])).toBe(response.body.length)
    }
  }
  for (const gzipPath of ['/gzip-asset.js', '/gzip-module.js']) {
    const gzip = await probe('GET', BASE_PATH + gzipPath)
    expect(gzip.status).toBe(200)
    expectGzipResource(gzip)
    expectHostOwnedHeaders(gzip.headers, frames.origin, 'resource')
    expect(decoded(gzip.body).toString('utf8')).toContain(`-ok-g${FIXTURE_GENERATION}`)
  }
  const gzipJson = await probe('GET', BASE_PATH + '/api/gzip')
  expectGzipResource(gzipJson)
  expect(JSON.parse(decoded(gzipJson.body).toString('utf8'))).toMatchObject({ gzip: true, generation: FIXTURE_GENERATION })

  const module = await probe('GET', BASE_PATH + '/module.js')
  expect(module.status).toBe(200)
  expect(module.headers['content-type']).toMatch(/javascript/)
  expect(module.body.toString('utf8')).toContain(`import('${BASE_PATH}/dynamic.js')`)

  const binary = await probe('GET', BASE_PATH + '/api/binary')
  expect(binary.status).toBe(200)
  expect(binary.headers['content-encoding']).toBeUndefined()
  expect(binary.body.length).toBe(1024)
  expect(binary.body.every((byte, index) => byte === (index & 255))).toBe(true)

  interface QueryReport { query: unknown }
  const queryResponse = await probe('GET', `${BASE_PATH}/api/query?a=1&a=2&b=%25&c=x+y&d=%C3%A9&e=a%26b`)
  const query = JSON.parse(queryResponse.body.toString('utf8')) as QueryReport
  expect(query.query).toEqual({ a: ['1', '2'], b: ['%'], c: ['x y'], d: ['é'], e: ['a&b'] })

  const preflight = await probe('OPTIONS', ENTRY_PATH)
  expect(preflight.status).toBe(204)
  expect(preflight.headers['access-control-allow-origin']).toBe('*')
  expect(preflight.headers['access-control-allow-credentials']).toBeUndefined()
  // HEAD is not answered by replaying a GET, and nothing else is a resource method.
  expect((await probe('HEAD', ENTRY_PATH)).status).toBe(405)
  expect((await probe('POST', ENTRY_PATH)).status).toBe(405)
}

export function expectGone(response: RawResponse): void {
  expect(response.status).toBe(410)
  expect(response.headers['cache-control']).toBe('no-store')
  expect(response.headers['access-control-allow-origin']).toBeUndefined()
  expect(response.headers['set-cookie']).toBeUndefined()
  expect(response.body.toString('utf8')).toBe('Resource unavailable')
}

/**
 * A capability host that was never issued, and paths outside the grant's plugin on the live
 * one, never fall through to Hermes (SPA, login, API, management) and never reach native code.
 */
export async function expectNoFallthrough(frames: ResourceFrames, driver: PresentationDriver, root: string): Promise<void> {
  const before = countedEffects(root).length
  const unknown = driver.probe(frames.origin.replace(frames.handle, `p-${randomBytes(24).toString('hex')}`))
  for (const target of ['/', WRAPPER_PATH, ENTRY_PATH, '/api/status', `${BASE_PATH}/api/effect`]) {
    expectGone(await unknown('GET', target))
  }
  const live = driver.probe(frames.origin)
  for (const target of [
    '/', '/api/status', '/login', '/api/plugins/hermes-cpa/status', '/v8/management/config',
    '/v0/resource/plugins/another-plugin/panel', `${BASE_PATH}/%2e%2e/panel`
  ]) expectGone(await live('GET', target))
  expect(countedEffects(root).length, 'denied requests must not invoke native code').toBe(before)
}

interface FixtureState {
  effects: number
  instance_nonce: string
  hits: Record<string, number>
}

async function fixtureState(probe: PresentationProbe): Promise<FixtureState> {
  const response = await probe('GET', BASE_PATH + '/api/state')
  expect(response.status).toBe(200)
  const state = JSON.parse(decoded(response.body).toString('utf8')) as FixtureState
  if (typeof state.effects !== 'number' || typeof state.instance_nonce !== 'string' || !state.hits) {
    throw new Error('Fixture state is malformed')
  }
  return state
}

/** One click, one native effect: the page's counter, the plugin's memory and the JSONL log agree. */
export async function clickCountedEffectOnce(
  frames: ResourceFrames, driver: PresentationDriver, root: string
): Promise<void> {
  const probe = driver.probe(frames.origin)
  const fixture = frames.panelLocator.locator('#fixture')
  const before = countedEffects(root)
  const stateBefore = await fixtureState(probe)
  expect(stateBefore.effects, 'plugin memory and the out-of-band log agree before the click').toBe(
    before.filter(entry => entry.instance_nonce === stateBefore.instance_nonce).length)
  await frames.panelLocator.locator('#effect-button').click()
  await expect(fixture).toHaveAttribute('data-effect-state', 'done', { timeout: 60_000 })
  await expect(fixture).toHaveAttribute('data-effect-status', '200')
  const reported = Number(await fixture.getAttribute('data-effect-count'))
  await expect.poll(() => countedEffects(root).length, { timeout: 15_000 }).toBe(before.length + 1)
  const latest = countedEffects(root).at(-1)
  expect(latest).toMatchObject({
    event: 'effect', route: '/api/effect', detail: 'source=button',
    generation: FIXTURE_GENERATION, instance_nonce: stateBefore.instance_nonce
  })
  expect(reported).toBe(latest?.seq)
  // No retry or replay of the effectful GET follows.
  await frames.panel.waitForTimeout(3_000)
  expect(countedEffects(root)).toHaveLength(before.length + 1)
  const stateAfter = await fixtureState(probe)
  expect(stateAfter.effects).toBe(stateBefore.effects + 1)
  expect(stateAfter.hits['/api/effect'] ?? 0).toBe((stateBefore.hits['/api/effect'] ?? 0) + 1)
}

/** Compares two URLs without echoing a capability URL into a failure message. */
function expectSameUrl(actual: string, expected: string, what: string): void {
  expect(actual === expected, what).toBe(true)
}

/**
 * The approved entry page is loaded again in its guest frame. Only the exact URL the host approved is ever
 * loaded (never one read back from the page or from Chromium's error document), and the page must finish every
 * automatic check again with the same plugin, generation and no failure or leak.
 */
async function restoreGuest(frames: ResourceFrames): Promise<void> {
  expectSameUrl(frames.panelUrl, frames.origin + ENTRY_PATH, 'the entry to load again is the approved grant entry')
  await frames.panel.goto(frames.panelUrl, { waitUntil: 'load', timeout: 60_000 })
  expectSameUrl(frames.panel.url(), frames.panelUrl, 'the guest is back at the approved entry')
  const fixture = frames.panelLocator.locator('#fixture')
  await expect(fixture).toHaveAttribute('data-state', 'complete', { timeout: 150_000 })
  await expect(fixture).toHaveAttribute('data-failures', '0')
  await expect(fixture).toHaveAttribute('data-leaks', '0')
  await expect(fixture).toHaveAttribute('data-plugin-id', PLUGIN_ID)
  await expect(fixture).toHaveAttribute('data-generation', FIXTURE_GENERATION)
}

/**
 * Navigation, popup and form escapes the guest attempts all fail to leave its origin. Each is proved instead of
 * assumed. After every attempt: no document was requested from a foreign origin, no window opened, the wrapper
 * and host keep their URLs and the guest frame stays the only child of the wrapper inside the host's one
 * resource frame, and no native effect was spent. Then, by kind of attempt:
 *  - a popup (the sandbox has no `allow-popups`) leaves the guest page at its own URL;
 *  - an attempt to navigate the guest frame itself (location, link, meta: the wrapper's `frame-src`; the form
 *    submission: the guest document's `form-action`) must be refused by that directive of the grant origin for
 *    its exact outside destination, as an enforced Chromium console report made after the click. Chromium then
 *    either leaves the page at its own URL or replaces it with its own error document; any other page fails.
 *    The Chromium of the A8 image replaced the page for location, link and form (a refused form reported both
 *    its `form-action` refusal and the guest frame's `frame-src` refusal). The same approved entry is then
 *    loaded again for the next probe.
 */
export async function expectOutsideNavigationContained(
  host: Page, frames: ResourceFrames, windows: () => number, root: string
): Promise<void> {
  const fixture = frames.panelLocator.locator('#fixture')
  const hostUrl = host.url()
  const windowsBefore = windows()
  const effectsBefore = countedEffects(root).length
  const trusted = [new URL(hostUrl).origin, frames.origin]
  const refusals: Refusal[] = []
  const foreignDocuments: string[] = []
  const onConsole = (message: ConsoleMessage) => {
    const refusal = parseRefusal(message.text())
    if (refusal) refusals.push(refusal)
  }
  const onRequest = (request: Request) => {
    const url = new URL(request.url())
    if (request.isNavigationRequest() && /^https?:$/.test(url.protocol) && !trusted.includes(url.origin)) {
      foreignDocuments.push(url.origin)
    }
  }
  /** The grant origin's own policy refused this attempt's exact destination, after the attempt began. */
  const expectRefused = async (attempt: NavigationAttempt, from: number): Promise<void> => {
    const refused = () => refusals.slice(from).some(entry =>
      entry.directive === attempt.directive && entry.sources.includes(frames.origin) && attempt.destination.test(entry.destination))
    try {
      await expect.poll(refused, { timeout: 10_000 }).toBe(true)
    } catch (error) {
      const reported = refusals.slice(from).map(entry =>
        `${entry.directive} of ${entry.sources.includes(frames.origin) ? 'the grant origin' : 'another origin'} -> ${entry.destination}`)
      throw new Error(
        `outside navigation ${attempt.name}: no enforced ${attempt.directive} refusal of its destination by the grant origin ` +
        `(reported since the click: ${reported.join('; ') || 'none'})`, { cause: error })
    }
  }
  const expectContained = async (label: string): Promise<void> => {
    expect(frames.panel.isDetached(), `${label}: the guest frame must stay attached`).toBe(false)
    const children = frames.wrapper.childFrames()
    expect(children.length === 1 && children[0] === frames.panel, `${label}: the guest must stay the wrapper's only child frame`).toBe(true)
    expect(frames.panel.parentFrame() === frames.wrapper, `${label}: the guest must stay inside the wrapper`).toBe(true)
    expectSameUrl(frames.wrapper.url(), frames.wrapperUrl, `${label}: the wrapper must not move`)
    expectSameUrl(host.url(), hostUrl, `${label}: the host must not move`)
    await expect(resourceFrameElement(host), `${label}: the host keeps its one resource frame`).toHaveCount(1)
    expect(windows(), `${label}: no window may open`).toBe(windowsBefore)
    expect(foreignDocuments, `${label}: no document may be requested from a foreign origin`).toEqual([])
    expect(countedEffects(root).length, `${label}: no native effect may be spent`).toBe(effectsBefore)
  }
  host.on('console', onConsole)
  host.on('request', onRequest)
  try {
    for (const attempt of OUTSIDE_NAVIGATION) {
      const { name, button } = attempt
      const label = `outside navigation ${name}`
      expectSameUrl(frames.panel.url(), frames.panelUrl, `${label}: the guest starts at the approved entry`)
      const refusalsBefore = refusals.length
      await frames.panelLocator.locator(`#${button}`).click()
      // "attempted" becomes "stayed" 750 ms later only if the page survived the attempt.
      let outcome = ''
      await expect.poll(async () => {
        outcome = frames.panel.url().startsWith(BLOCKED_FRAME_URL)
          ? 'replaced'
          : (await fixture.getAttribute(`data-nav-${name}`, { timeout: 500 }).catch(() => 'unreadable')) ?? 'unset'
        return outcome
      }, { message: label, timeout: 10_000 }).toMatch(/^(stayed|blocked-exception|replaced)$/)
      if (outcome === 'replaced') {
        expect(attempt.kind, `${label} must leave the guest page in place`).toBe('navigation')
      } else {
        await frames.panel.waitForTimeout(1_000)
        expectSameUrl(frames.panel.url(), frames.panelUrl, `${label} must not move the guest`)
      }
      if (attempt.kind === 'navigation') await expectRefused(attempt, refusalsBefore)
      if (outcome === 'replaced') await restoreGuest(frames)
      await expectContained(label)
    }
    await frames.panel.waitForTimeout(1_500)
    expectSameUrl(frames.panel.url(), frames.panelUrl, 'outside navigation: the guest ends at the approved entry')
    await expectContained('outside navigation: all attempts')
  } finally {
    host.off('console', onConsole)
    host.off('request', onRequest)
  }
}

/** GET forms, relative links and an owner-approved exact-route redirect stay on the grant origin. */
export async function expectInOriginNavigation(frames: ResourceFrames): Promise<void> {
  const fixture = frames.panelLocator.locator('#fixture')
  await frames.panelLocator.locator('#submit-get-form').click()
  const result = frames.panelLocator.locator('#form-result')
  await expect(result).toHaveAttribute('data-state', 'complete', { timeout: 60_000 })
  expect(JSON.parse(await result.getAttribute('data-query') ?? 'null')).toEqual(
    { q: ['a b', 'c&d'], enc: ['é=%25;'] })
  expect(new URL(frames.panel.url()).pathname).toBe(`${BASE_PATH}/form`)
  await frames.panelLocator.locator('#back-to-panel').click()
  await expect(fixture).toHaveAttribute('data-state', 'complete', { timeout: 150_000 })
  await frames.panelLocator.locator('#redirect-link').click()
  await expect.poll(() => frames.panel.url(), { timeout: 60_000 }).toBe(
    `${frames.origin}${BASE_PATH}/redirect/target?hop=1&dup=a&dup=b`)
}

/**
 * The presentation is gone for good: the frames are removed, the capability origin answers 410,
 * and not even an effectful route reaches native code any more.
 */
export async function expectRevoked(
  host: Page, frames: ResourceFrames, driver: PresentationDriver, root: string
): Promise<void> {
  await expect(resourceFrameElement(host)).toHaveCount(0, { timeout: 30_000 })
  await expect.poll(() => frames.panel.isDetached(), { timeout: 30_000 }).toBe(true)
  const probe = driver.probe(frames.origin)
  await expect.poll(async () => (await probe('GET', WRAPPER_PATH)).status, { timeout: 30_000 }).toBe(410)
  const before = countedEffects(root).length
  for (const target of [WRAPPER_PATH, ENTRY_PATH, `${BASE_PATH}/api/state`, `${BASE_PATH}/api/effect?source=revoked-probe`]) {
    expectGone(await probe('GET', target))
  }
  expect(countedEffects(root).length, 'a revoked grant must make zero native invocations').toBe(before)
}

/**
 * Revocation while a native effect is still running: it takes effect without waiting for the
 * handler (which is still sleeping), the handler completes exactly once, and nothing replays it.
 */
export async function expectRevocationDuringSlowEffect(
  host: Page, frames: ResourceFrames, driver: PresentationDriver, root: string,
  during: () => Promise<void>, revoke: () => Promise<void>
): Promise<void> {
  const probe = driver.probe(frames.origin)
  const slow = (event: string) => readEffectLog(root).filter(entry => entry.route === '/api/slow' && entry.event === event).length
  const started = slow('effect')
  const finished = slow('finished')
  const inFlight = probe('GET', `${BASE_PATH}/api/slow?ms=${SLOW_EFFECT_MS}`).then(response => response.status, () => 0)
  await expect.poll(() => slow('effect'), { timeout: 20_000 }).toBe(started + 1)
  // Ordinary console work (management reads and their receipts) is not starved by the running effect.
  await during()
  await revoke()
  await expect(resourceFrameElement(host)).toHaveCount(0, { timeout: 8_000 })
  await expect.poll(async () => (await probe('GET', WRAPPER_PATH)).status, { timeout: 8_000 }).toBe(410)
  expect(slow('finished'), 'revocation must not wait for the running native handler').toBe(finished)
  await inFlight
  await expect.poll(() => slow('finished'), { timeout: 30_000 }).toBe(finished + 1)
  expect(slow('effect'), 'the slow effect must run exactly once').toBe(started + 1)
  await expectRevoked(host, frames, driver, root)
}

/**
 * Revocation while a client holds an unread, large response: revocation is immediate, the
 * response is not delivered in full afterwards, and the native effect ran exactly once.
 * Dashboard only (a raw HTTP(S) client can stop reading; the Electron main process cannot).
 */
export async function expectRevocationWithUnreadResponse(
  host: Page, frames: ResourceFrames, driver: PresentationDriver, root: string, ca: string | undefined,
  revoke: () => Promise<void>
): Promise<void> {
  const before = countedEffects(root).length
  const unread = await startRequest(
    new URL(frames.origin), ca, 'GET', `${BASE_PATH}/api/large?bytes=${LARGE_RESPONSE_BYTES}`, {})
  expect(unread.statusCode).toBe(200)
  unread.pause()
  const concurrent = await driver.probe(frames.origin)('GET', `${BASE_PATH}/api/state`)
  expect(concurrent.status, 'an unread response must not starve other requests of the same grant').toBe(200)
  await revoke()
  await expectRevoked(host, frames, driver, root)
  let received = 0
  try {
    for await (const chunk of unread) received += chunk.length
  } catch {
    // An aborted delivery is the expected end of a revoked grant's response.
  }
  expect(received < LARGE_RESPONSE_BYTES || !unread.complete, 'a revoked grant must not deliver its whole response').toBe(true)
  expect(countedEffects(root)).toHaveLength(before + 1)
}

export interface PanelScenario {
  host: Page
  frames: ResourceFrames
  driver: PresentationDriver
  root: string
  /** Generated credentials that must never be visible to the guest. */
  secrets: string[]
  /** Number of top-level windows the host owns (a popup would raise it). */
  windows: () => number
  screenshot: (name: string) => Promise<unknown>
}

/**
 * The whole live-presentation proof for one grant, in an order that never navigates the guest
 * before the checks that need its original page: functional and containment markers, what the
 * guest and an independent client see, one counted effect, outside and in-origin navigation.
 */
export async function exercisePanel(scenario: PanelScenario): Promise<void> {
  const { host, frames, driver, root } = scenario
  await expectPanelComplete(frames)
  await scenario.screenshot('panel-complete')
  await expectGuestContained(frames)
  await expectNoAuthorityInGuest(frames, scenario.secrets)
  await inspectPresentation(frames, driver)
  await expectNoFallthrough(frames, driver, root)
  await clickCountedEffectOnce(frames, driver, root)
  await scenario.screenshot('effect-once')
  await expectOutsideNavigationContained(host, frames, scenario.windows, root)
  await scenario.screenshot('outside-navigation-contained')
  await expectInOriginNavigation(frames)
  await scenario.screenshot('in-origin-navigation')
}

/** Resolve one section of the opaque CPA Management Center from its sidebar and wait for its page. */
export async function openConsoleSection(consoleFrame: FrameLocator, label: string, evidence: Locator): Promise<void> {
  const link = consoleFrame.locator('aside').getByRole('link', { name: new RegExp(`^${label}`) })
  const expand = consoleFrame.getByRole('button', { name: 'Expand sidebar', exact: true })
  await expect(link.or(expand).first()).toBeVisible({ timeout: 90_000 })
  if (await expand.isVisible()) await expand.click()
  await link.click()
  await expect(evidence).toBeVisible({ timeout: 30_000 })
}

/**
 * Navigation inside the host document that is not a navigation away (an in-app route or hash
 * change while the console is docked) must leave a live presentation untouched.
 */
export async function expectLiveAcrossSameDocumentNavigation(
  frames: ResourceFrames, driver: PresentationDriver, root: string, navigate: () => Promise<void>
): Promise<void> {
  const before = countedEffects(root).length
  await navigate()
  await frames.panel.waitForTimeout(2_000)
  expect(frames.panel.isDetached()).toBe(false)
  expect((await driver.probe(frames.origin)('GET', WRAPPER_PATH)).status).toBe(200)
  expect(countedEffects(root)).toHaveLength(before)
}

function logFiles(directory: string): string[] {
  let entries: fs.Dirent[]
  try {
    entries = fs.readdirSync(directory, { withFileTypes: true })
  } catch (error) {
    if (error instanceof Error && 'code' in error && error.code === 'ENOENT') return []
    throw error
  }
  return entries.flatMap(entry => {
    const target = path.join(directory, entry.name)
    if (entry.isDirectory()) return /^(Cache|Code Cache|GPUCache|Local Storage|Session Storage|IndexedDB)$/.test(entry.name) ? [] : logFiles(target)
    return entry.isFile() && /\.(log|txt|jsonl)$/.test(entry.name) && entry.name !== EFFECT_LOG ? [target] : []
  })
}

/** No capability host, resource query or credential reached a log or error file of the host. */
export function expectNoCapabilityInLogs(directories: string[], secrets: string[]): void {
  const forbidden = [/p-[0-9a-f]{48}/, /hermes-plugin-resource:/, /a=1&a=2/, /source=button/, /revoked-probe/]
  for (const file of directories.flatMap(logFiles)) {
    const text = fs.readFileSync(file, 'latin1').slice(-8 * 1024 * 1024)
    for (const pattern of forbidden) expect(text, `${path.basename(file)} must not match ${pattern}`).not.toMatch(pattern)
    for (const secret of secrets) expect(text, `${path.basename(file)} must not hold a credential`).not.toContain(secret)
  }
}

// ── Network observation ─────────────────────────────────────────────────────────────────

/**
 * What a scenario's network policy knows about the trusted host: the one public origin its own renderer addresses and
 * the exact `<iframe>` elements it owns that may load it. A request is the host's only if the browser's frame tree and
 * the element that owns the frame say so; the destination alone never excuses anything.
 */
export interface TrustedHostPolicy {
  /** The exact origin, spelled as `URL.origin` spells it, that the host's own documents may address. It is refused before any socket opens. */
  origin: string
  /** The `src` and `sandbox` attributes, exactly as the host renders them, of each host-owned embed `<iframe>` that may load that origin. */
  embeds: ReadonlyArray<{ src: string; sandbox: string }>
}

export interface NetworkWatch {
  /**
   * Requests to unexpected hosts that were not blocked by a CSP, that the trusted-host policy did not refuse itself, or
   * that got a response. Each names its protocol, outcome and initiator; hostnames may contain secrets.
   */
  leaks: () => string[]
  /** Trusted-host requests the policy refused before the network, by who issued them. Counts only. */
  trustedBlocked: () => { host: number; embed: number }
  /** Resolves once every attribution and refusal started so far has finished; await it before reading the results. */
  settled: () => Promise<void>
}

/** True for hosts a scenario legitimately talks to: loopback, *.localhost and explicit names. */
export function expectedHost(extra: readonly string[]): (url: URL) => boolean {
  return url => !['http:', 'https:', 'ws:', 'wss:'].includes(url.protocol) ||
    ['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname) || url.hostname.endsWith('.localhost') ||
    extra.some(name => url.hostname === name || (name.startsWith('.') && url.hostname.endsWith(name)))
}

/**
 * Who issued a request, proved from the browser's frame tree and, for a direct child of the host document, from the
 * `<iframe>` element that owns it. `host`: the host document itself. `host-embed`: a pinned host-owned embed.
 * `guest`: every other document (the sandboxed console and plugin-resource pages, anything nested below another
 * frame). `unattributed`: no frame (a worker), a detached frame or a failed lookup. Only the first two are ever excused.
 */
type Initiator = 'host' | 'host-embed' | 'guest' | 'unattributed'

interface Observation { target: string; failure?: string; responded: boolean; initiator: Initiator; refused: boolean }

async function attributeFrame(frame: Frame, embeds: TrustedHostPolicy['embeds']): Promise<Initiator> {
  if (frame.isDetached()) return 'unattributed'
  const main = frame.page().mainFrame()
  if (frame === main) return 'host'
  // Only a direct child of the host document can be one of its embeds; deeper documents are the embed's own content.
  if (frame.parentFrame() !== main || embeds.length === 0) return 'guest'
  const owner = await frame.frameElement().catch(() => undefined)
  if (!owner) return 'unattributed'
  try {
    const [src, sandbox] = await Promise.all([owner.getAttribute('src'), owner.getAttribute('sandbox')])
    return embeds.some(embed => embed.src === src && embed.sandbox === sandbox) ? 'host-embed' : 'guest'
  } catch {
    return 'unattributed'
  } finally {
    await owner.dispose().catch(() => undefined)
  }
}

function observer(allowed: (url: URL) => boolean, trusted?: TrustedHostPolicy) {
  const embeds = trusted?.embeds ?? []
  const seen = new Map<Request, Observation>()
  const attributions = new Map<Request, Promise<Initiator>>()
  const inflight = new Set<Promise<unknown>>()
  const track = <T>(work: Promise<T>): Promise<T> => {
    const done = () => { inflight.delete(work) }
    inflight.add(work)
    void work.then(done, done)
    return work
  }
  // A service worker's request has no frame, and a lookup can lose a race with a detaching frame: neither is ever excused.
  const attribute = (request: Request): Promise<Initiator> => {
    let attribution = attributions.get(request)
    if (!attribution) {
      attribution = track((async (): Promise<Initiator> => {
        try { return await attributeFrame(request.frame(), embeds) } catch { return 'unattributed' }
      })())
      attributions.set(request, attribution)
    }
    return attribution
  }
  /** The request's observation, created on first sight (the route handler and the request event may arrive in either order). */
  const observe = (request: Request): Observation | undefined => {
    const known = seen.get(request)
    if (known) return known
    let url: URL
    try { url = new URL(request.url()) } catch { return undefined }
    if (allowed(url)) return undefined
    const protocol = ['http:', 'https:', 'ws:', 'wss:'].includes(url.protocol) ? url.protocol : 'other:'
    const entry: Observation = { target: `${protocol}//[redacted-host]`, responded: false, initiator: 'unattributed', refused: false }
    seen.set(request, entry)
    void attribute(request).then(initiator => { entry.initiator = initiator })
    return entry
  }
  return {
    request: observe,
    failed: (request: Request) => {
      const entry = seen.get(request)
      if (entry) entry.failure = request.failure()?.errorText ?? 'failed'
    },
    response: (response: NetworkResponse) => {
      const entry = seen.get(response.request())
      if (entry) entry.responded = true
    },
    /**
     * Refuses a request of the trusted origin only when the host document or a pinned host embed issued it. The same
     * origin from a guest (or from anything unattributed) passes untouched, so a guest attempt is never aborted here
     * and then excused: it must be refused by a CSP or it is a leak.
     */
    route: (pending: Route) => track((async () => {
      const request = pending.request()
      const entry = observe(request)
      const initiator = entry ? await attribute(request) : 'unattributed'
      if (entry && (initiator === 'host' || initiator === 'host-embed')) {
        entry.initiator = initiator
        await pending.abort('blockedbyclient').then(() => { entry.refused = true }, () => undefined)
        return
      }
      await pending.continue().catch(() => undefined)
    })()),
    watch: (): NetworkWatch => ({
      leaks: () => [...seen.values()]
        .filter(entry => entry.responded || !(entry.refused || CSP_BLOCKED.test(entry.failure ?? '')))
        .map(entry => `${entry.target}: ${entry.responded ? 'responded' : entry.failure ?? 'pending'} (${entry.initiator})`),
      trustedBlocked: () => {
        const refused = [...seen.values()].filter(entry => entry.refused)
        return {
          host: refused.filter(entry => entry.initiator === 'host').length,
          embed: refused.filter(entry => entry.initiator === 'host-embed').length
        }
      },
      settled: async () => {
        while (inflight.size) await Promise.allSettled([...inflight])
      }
    })
  }
}

/**
 * Observes every request of one page. With a trusted-host policy it also refuses, before the network, that policy's
 * origin when the host document or one of its pinned embeds asks for it. Nothing else is intercepted.
 */
export async function watchPageNetwork(
  page: Page, allowed: (url: URL) => boolean, trusted?: TrustedHostPolicy
): Promise<NetworkWatch> {
  const watcher = observer(allowed, trusted)
  page.on('request', watcher.request)
  page.on('requestfailed', watcher.failed)
  page.on('response', watcher.response)
  if (trusted) {
    const { origin } = trusted
    await page.route(url => url.origin === origin, watcher.route)
  }
  return watcher.watch()
}

export function watchContextNetwork(context: BrowserContext, allowed: (url: URL) => boolean): NetworkWatch {
  const watcher = observer(allowed)
  context.on('request', watcher.request)
  context.on('requestfailed', watcher.failed)
  context.on('response', watcher.response)
  return watcher.watch()
}
