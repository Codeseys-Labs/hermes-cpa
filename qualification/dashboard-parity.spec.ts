/**
 * Real Hermes Dashboard browser proof of the installed native plugin resource.
 *
 * Real Chromium (Playwright) drives the real Dashboard SPA, the real CPA launcher, the real
 * opaque CPA Management Center, the trusted review dialog and the real presentation origin
 * served by the Dashboard's own resource middleware. The native plugin is the generated Go
 * plugin installed by desktop_prepare.py through CPA's plugin store. Four scenarios, one per
 * fresh prepared root (select with HERMES_UI_DASHBOARD_MODE):
 *
 *   http         http://127.0.0.1:<port>, reserved loopback resource origins
 *                http://p-<handle>.localhost:<port>.
 *   http-remap   the same through a loopback TCP relay on another port (an `ssh -L` remap):
 *                resource origins must carry the port the browser used.
 *   https        generated TLS reverse proxy (X-Forwarded-Proto set), dashboard.public_url,
 *                dashboard.resource_origin_template on a distinct wildcard host, generated
 *                password login (the bundled `basic` provider).
 *   https-bare   the same without any forwarded-protocol header: the proxy only terminates TLS
 *                and remaps the port.
 *
 * Chromium resolves only the generated fixture names (host-resolver rules) and trusts only the
 * generated certificate by SPKI pin; there is no global TLS bypass. The browser context starts
 * empty and holds nothing but generated fixture credentials. The UI image copies this file to
 * apps/desktop/e2e/hermes-cpa-dashboard-parity.spec.ts.
 *
 * Nothing here may wait for ever. Every browser action and navigation is bounded (a Playwright
 * Test run otherwise gives contexts made with chromium.launch no timeout at all), every helper
 * reply is bounded, and a failure names the proof stage and the helper's own last step. The
 * helper, the browser and the qualification locks are released by a test.afterEach hook as well
 * as by the body, because a test cut off by its timeout is never unwound.
 */
import * as fs from 'node:fs'
import * as os from 'node:os'
import * as path from 'node:path'
import { randomBytes } from 'node:crypto'
import { spawn } from 'node:child_process'
import { createInterface } from 'node:readline'
import { setTimeout as sleep } from 'node:timers/promises'
import { chromium, expect, test } from '@playwright/test'
import type { Browser, BrowserContext, Page } from '@playwright/test'

import {
  approveReview, closeResourceButton, countedEffects, dashboardDriver, declineReview, effectLogPath,
  ENTRY_PATH, exercisePanel, expectedHost, expectLiveAcrossSameDocumentNavigation,
  expectNoCapabilityInLogs, expectRevocationDuringSlowEffect, expectRevocationWithUnreadResponse,
  expectRevoked, openConsoleSection, openNativeMenu, PLUGIN_ID, rawProbe, requestResourceReview,
  resourceFrameElement, waitForResourceFrames, watchContextNetwork,
} from './hermes-cpa-resource-ui'

type DashboardMode = 'http' | 'http-remap' | 'https' | 'https-bare'
const MODES: readonly DashboardMode[] = ['http', 'http-remap', 'https', 'https-bare']

interface ScenarioReady {
  ready: true
  mode: DashboardMode
  dashboard_origin: string
  backend_port: number
  relay_port?: number
  effect_log: string
  tls_port?: number
  username?: string
  password?: string
  ca_pem?: string
  spki?: string
  host_resolver_rules?: string
  resource_template?: string
  oauth_email?: string
}
interface ScenarioStopped {
  stopped: true
  log_clean: boolean
  log_bytes: number
  quiesced: boolean
}
interface ScenarioError {
  error: string
  stage?: string
}
interface OAuthCallback { callback: string }
interface OAuthCounts { exchanges: number; refreshes: number }
type ScenarioMessage = ScenarioReady | ScenarioStopped | ScenarioError | OAuthCallback | OAuthCounts

/** Longest the helper may take to answer, by message. `ready` starts a real backend; `stop` quiesces it. */
const HELPER_WAIT_MS = { ready: 240_000, reply: 30_000, stop: 300_000 }
/** Default for every browser action and navigation (Playwright Test would otherwise wait for ever). */
const BROWSER_TIMEOUT_MS = 60_000
/** The only helper stderr lines read: fixed step names. Anything else is dropped unread. */
const HELPER_STAGE_LINE = /^stage ([a-z_]+)$/

/** Resolves true when `settled` settles within `ms`, false otherwise. */
async function within(ms: number, settled: Promise<unknown>): Promise<boolean> {
  const stop = new AbortController()
  try {
    return await Promise.race([settled.then(() => true), sleep(ms, false, { signal: stop.signal })])
  } finally {
    stop.abort()
  }
}

function failedAt(stage: string, helperStage: string, error: unknown, routeError: unknown): Error {
  const reason = error instanceof Error ? error.message : String(error)
  const route = routeError === undefined ? '' :
    `; authorization route failed: ${routeError instanceof Error ? routeError.message : String(routeError)}`
  return new Error(`Dashboard proof failed at stage "${stage}" (helper stage ${helperStage}): ${reason}${route}`,
    { cause: error })
}

/** Releases the scenario in flight: browser, helper process, backend and qualification locks. */
let release: (() => Promise<void>) | undefined
// A test cut off by its timeout is never unwound (its body keeps awaiting), so this hook always runs.
test.afterEach(async () => {
  const pending = release
  release = undefined
  await pending?.()
})

for (const mode of MODES) {
  test(`Dashboard presents an installed native plugin resource in isolation (${mode})`, async ({}, testInfo) => {
    test.setTimeout(1_500_000)
    test.skip(process.env.HERMES_UI_DASHBOARD_MODE !== mode, 'Select one scenario with HERMES_UI_DASHBOARD_MODE')
    const root = process.env.CPA_UI_ROOT
    if (!root || !path.isAbsolute(root) || !fs.existsSync(path.join(root, '.desktop-smoke-used'))) {
      throw new Error('Prepared isolated CPA root required')
    }
    const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-ui-chromium-'))
    const helper = spawn('/opt/q/venv/bin/python', [
      '/opt/q/qualification/dashboard_scenario.py', '--root', root, '--mode', mode, '--consent',
    ], {
      stdio: ['pipe', 'pipe', 'pipe'],
      env: { PATH: '/opt/q/venv/bin:/usr/local/bin:/usr/bin:/bin', LANG: 'C.UTF-8' },
    })
    // Only fixed step names are read from the helper's stderr; it never prints anything else we keep.
    let helperStage = 'spawned'
    createInterface({ input: helper.stderr }).on('line', line => {
      const named = HELPER_STAGE_LINE.exec(line)
      if (named) helperStage = named[1]
    })
    helper.stdin.on('error', () => undefined)
    const exited = new Promise<void>(resolve => {
      helper.once('exit', () => resolve())
      helper.once('error', () => resolve())
    })
    const lines = createInterface({ input: helper.stdout })[Symbol.asyncIterator]()
    const nextMessage = async (what: string, limitMs: number): Promise<ScenarioMessage> => {
      const stop = new AbortController()
      const silent = sleep(limitMs, undefined, { signal: stop.signal }).then((): never => {
        throw new Error(`Dashboard scenario helper sent no ${what} within ${limitMs / 1000}s (helper stage ${helperStage})`)
      })
      let line: IteratorResult<string>
      try {
        line = await Promise.race([lines.next(), silent])
      } finally {
        stop.abort()
      }
      if (line.done) throw new Error(`Dashboard scenario helper exited before its ${what} (helper stage ${helperStage})`)
      const message = JSON.parse(line.value) as ScenarioMessage
      if ('error' in message) throw new Error(`Dashboard scenario failed at stage ${message.stage ?? 'unknown'} (waiting for ${what})`)
      return message
    }
    const command = async (value: Record<string, string>, what: string): Promise<ScenarioMessage> => {
      helper.stdin.write(JSON.stringify(value) + '\n')
      return nextMessage(what, HELPER_WAIT_MS.reply)
    }
    let browser: Browser | undefined
    let page: Page | undefined
    let opened: BrowserContext | undefined
    let stage = 'helper ready'
    const at = (name: string) => { stage = name }
    let routeError: unknown
    let disposal: Promise<void> | undefined
    const dispose = (): Promise<void> => {
      disposal ??= (async () => {
        if (page && !page.isClosed()) {
          await page.screenshot({ path: testInfo.outputPath(`dashboard-${mode}-final.png`), timeout: 15_000 })
            .catch(() => undefined)
        }
        // Close the context itself (not only the browser) so the runner stops and keeps its trace and network log.
        if (opened) await within(30_000, opened.close().catch(() => undefined))
        if (browser) await within(30_000, browser.close().catch(() => undefined))
        // stdin EOF makes the helper stop its backend and ingress and release the root locks itself.
        // SIGTERM runs the same teardown; SIGKILL is the last resort (it would orphan the backend).
        helper.stdin.end()
        for (const [signal, waitMs] of [[undefined, 240_000], ['SIGTERM', 240_000], ['SIGKILL', 30_000]] as const) {
          if (helper.pid === undefined || helper.exitCode !== null || helper.signalCode !== null) break
          if (signal) helper.kill(signal)
          await within(waitMs, exited)
        }
        fs.rmSync(scratch, { recursive: true, force: true })
      })()
      return disposal
    }
    release = dispose
    try {
      const ready = await nextMessage('ready', HELPER_WAIT_MS.ready)
      if (!('ready' in ready) || ready.mode !== mode || ready.effect_log !== effectLogPath(root)) {
        throw new Error('Dashboard scenario helper did not report the requested scenario')
      }
      const dashboard = new URL(ready.dashboard_origin)
      const ca = ready.ca_pem === undefined ? undefined : fs.readFileSync(ready.ca_pem, 'utf8')
      const secure = mode.startsWith('https')
      if (secure && (!ready.spki || !ready.host_resolver_rules || !ready.username || !ready.password || !ca)) {
        throw new Error('HTTPS scenario material is incomplete')
      }
      const flags = ['--disable-dev-shm-usage', '--no-proxy-server']
      if (secure) {
        // Only the generated fixture names resolve, and only the generated certificate is trusted.
        flags.push(`--host-resolver-rules=${ready.host_resolver_rules}`,
          `--ignore-certificate-errors-spki-list=${ready.spki}`)
      }
      const browserEnv: Record<string, string> = {
        PATH: '/usr/local/bin:/usr/bin:/bin', HOME: scratch, TMPDIR: scratch, LANG: 'C.UTF-8',
      }
      if (process.env.DISPLAY) browserEnv.DISPLAY = process.env.DISPLAY
      at('launch browser')
      browser = await chromium.launch({
        headless: true, executablePath: process.env.HERMES_UI_CHROMIUM || undefined, args: flags, env: browserEnv,
      })
      const context = await browser.newContext({
        viewport: { width: 1600, height: 1000 }, reducedMotion: 'reduce', locale: 'en-US', timezoneId: 'UTC',
        acceptDownloads: false,
      })
      opened = context
      context.setDefaultTimeout(BROWSER_TIMEOUT_MS)
      context.setDefaultNavigationTimeout(BROWSER_TIMEOUT_MS)
      page = await context.newPage()
      const shell = page
      const driver = dashboardDriver(ca)
      const network = watchContextNetwork(context,
        expectedHost([...(secure ? [dashboard.hostname, '.resource.fixture.invalid'] : []),
          ...(mode === 'http' ? ['claude.ai'] : [])]))
      const sessionToken = fs.readFileSync(path.join(root, 'session-token'), 'utf8').trim()
      const secrets = [sessionToken]
      const shot = (name: string) => shell.screenshot({ path: testInfo.outputPath(`dashboard-${mode}-${name}.png`) })

      if (secure) {
        // A remote public URL engages the Dashboard auth gate: sign in with the generated password.
        at('sign in')
        await shell.goto(`${ready.dashboard_origin}/`)
        await shell.getByLabel('Username', { exact: true }).fill(ready.username ?? '')
        await shell.getByLabel('Password', { exact: true }).fill(ready.password ?? '')
        await shot('login')
        await shell.getByRole('button', { name: 'Sign in', exact: true }).click()
        await shell.waitForURL(url => !url.pathname.startsWith('/login'), { timeout: 30_000 })
        const cookies = await context.cookies(ready.dashboard_origin)
        expect(cookies.length, 'the gated Dashboard issued a generated session').toBeGreaterThan(0)
        secrets.push(ready.password ?? '', ...cookies.map(cookie => cookie.value).filter(value => value.length >= 16))
      }

      const consoleFrame = shell.frameLocator('iframe[title="CPA Management Console"]')
      const openConsole = async () => {
        await shell.goto(`${ready.dashboard_origin}/cpa`)
        await expect(shell.getByRole('button', { name: 'Unlock CPA Console', exact: true })).toBeVisible({ timeout: 90_000 })
        await shell.getByRole('button', { name: 'Unlock CPA Console', exact: true }).click()
        await shell.getByRole('button', { name: 'Unlock console', exact: true }).click()
        await expect(shell.locator('[data-cpa-phase="ready"]')).toBeVisible({ timeout: 90_000 })
        // The launcher reports ready before the guest console has rendered its own navigation (it
        // connects and loads data first): acting on the sidebar sooner races that render.
        await expect(consoleFrame.locator('aside').getByRole('link', { name: /^Dashboard/ })
          .or(consoleFrame.getByRole('button', { name: 'Expand sidebar', exact: true })).first())
          .toBeVisible({ timeout: 90_000 })
      }
      const openResource = async () => {
        await requestResourceReview(shell, consoleFrame)
        await approveReview(shell)
        return waitForResourceFrames(shell)
      }

      at('open console')
      await openConsole()
      await expect(shell.locator('iframe[title="CPA Management Console"]')).toHaveAttribute('sandbox', 'allow-scripts')
      if (mode === 'http') {
        if (!ready.oauth_email) throw new Error('Generated Dashboard OAuth account unavailable')
        at('oauth: open login section')
        await openConsoleSection(consoleFrame, 'OAuth Login',
          consoleFrame.getByRole('button', { name: 'Start Anthropic Login', exact: true }))
        at('oauth: start login and approve')
        await consoleFrame.getByRole('button', { name: 'Start Anthropic Login', exact: true }).click()
        await approveReview(shell)
        const authorization = consoleFrame.getByText(/^https:\/\/claude\.ai\/oauth\/authorize\?/)
        await expect(authorization).toBeVisible()
        const authorizationUrl = (await authorization.textContent())?.trim()
        if (!authorizationUrl) throw new Error('Native OAuth authorization URL unavailable')
        await context.route(url => url.href === authorizationUrl, async route => {
          try {
            const issued = await command({ command: 'oauth-authorize', url: route.request().url() }, 'oauth callback')
            if (!('callback' in issued)) throw new Error('Generated authorization callback unavailable')
            const callback = issued.callback.replaceAll('&', '&amp;').replaceAll('"', '&quot;').replaceAll('<', '&lt;')
            await route.fulfill({
              contentType: 'text/html',
              body: '<!doctype html><meta charset="utf-8"><link rel="icon" href="data:,">' +
                '<title>Generated OAuth authority</title>' +
                `<a href="${callback}">Authorize generated account</a>`,
            })
          } catch (error) {
            // Never leave the popup navigation pending: record why, fail the request, let the proof report it.
            routeError = error
            await route.abort('failed').catch(() => undefined)
          }
        })
        at('oauth: external-open prompt')
        const prompt = shell.getByRole('group', { name: 'Open external page', exact: true })
        await consoleFrame.getByRole('button', { name: 'Open Link', exact: true }).click()
        await expect(prompt.locator('code')).toHaveText(authorizationUrl)
        expect(context.pages()).toHaveLength(1)
        await prompt.getByRole('button', { name: 'Do not open', exact: true }).click()
        await expect(prompt).toHaveCount(0)
        expect(context.pages()).toHaveLength(1)
        await consoleFrame.getByRole('button', { name: 'Open Link', exact: true }).click()
        const link = prompt.getByRole('link', { name: 'Open external page', exact: true })
        await expect(link).toHaveAttribute('href', authorizationUrl)
        at('oauth: popup opens the exact authority')
        const opened = context.waitForEvent('page')
        await link.click()
        const popup = await opened
        await expect(popup.getByRole('link', { name: 'Authorize generated account', exact: true })).toBeVisible()
        await expect(popup).toHaveURL(authorizationUrl)
        expect(await popup.evaluate(() => window.opener === null)).toBe(true)
        at('oauth: native callback completes login')
        await popup.getByRole('link', { name: 'Authorize generated account', exact: true }).click()
        await expect(consoleFrame.getByText('Authentication successful!', { exact: true }).first())
          .toBeVisible({ timeout: 30_000 })
        at('oauth: exchange counts')
        const counts = await command({ command: 'oauth-counts' }, 'oauth counts')
        expect('exchanges' in counts && counts.exchanges).toBe(1)
        if (!popup.isClosed()) await popup.close()
        at('oauth: auth files lists the account')
        await openConsoleSection(consoleFrame, 'Auth Files',
          consoleFrame.getByText(ready.oauth_email, { exact: false }).first())
        await shot('oauth-completed')
        const approveConfigRead = async () => {
          const review = shell.getByRole('dialog', { name: 'Review CPA effect', exact: true })
          await expect(review).toContainText('GET /v8/management/config.yaml')
          await expect(review.getByRole('listitem')).toHaveText(['config_export'])
          await review.getByRole('button', { name: 'Approve exact effects', exact: true }).click()
        }
        at('configuration: add public API key')
        await openConsoleSection(consoleFrame, 'Config Panel',
          shell.getByRole('dialog', { name: 'Review CPA effect', exact: true }))
        await approveConfigRead()
        await consoleFrame.getByRole('tab', { name: 'Access & Authentication', exact: true }).click()
        await consoleFrame.getByRole('button', { name: 'Add API Key', exact: true }).click()
        const keyDialog = consoleFrame.getByRole('dialog', { name: 'Add API Key', exact: true })
        const generatedKey = `generated-dashboard-${randomBytes(24).toString('hex')}`
        secrets.push(generatedKey)
        await keyDialog.getByLabel('API Key', { exact: true }).fill(generatedKey)
        await keyDialog.getByRole('button', { name: 'Add', exact: true }).click()
        await consoleFrame.getByRole('button', { name: 'Save changes', exact: true }).click()
        await approveConfigRead()
        await expect(consoleFrame.getByRole('dialog', { name: 'Review Changes' })).toBeVisible()
        await consoleFrame.getByRole('button', { name: 'Confirm Save', exact: true }).click()
        await approveConfigRead()
        const writeReview = shell.getByRole('dialog', { name: 'Review CPA effect', exact: true })
        await expect(writeReview.getByText('config_write', { exact: true })).toBeVisible()
        await shot('config-review')
        await writeReview.getByRole('button', { name: 'Approve exact effects', exact: true }).click()
        const configPath = path.join(root, '.hermes', 'cpa', 'instances', 'default', 'config.yaml')
        await expect.poll(() => fs.readFileSync(configPath, 'utf8').includes(generatedKey), { timeout: 30_000 }).toBe(true)
        await approveConfigRead()
        await expect(consoleFrame.getByRole('dialog', { name: 'Review Changes' })).toHaveCount(0)
        await expect(consoleFrame.getByRole('group', { name: 'Unsaved changes', exact: true })).toHaveCount(0)
        await shot('config-completed')
        at('configuration: lock and reopen retained key')
        await shell.getByRole('button', { name: 'Lock CPA Console', exact: true }).click()
        await expect(shell.locator('iframe[title="CPA Management Console"]')).toHaveCount(0)
        await shell.getByRole('button', { name: 'Reload', exact: true }).click()
        await shell.getByRole('button', { name: 'Unlock CPA Console', exact: true }).click()
        await shell.getByRole('button', { name: 'Unlock console', exact: true }).click()
        await expect(shell.locator('[data-cpa-phase="ready"]')).toBeVisible({ timeout: 90_000 })
        await openConsoleSection(consoleFrame, 'Config Panel',
          shell.getByRole('dialog', { name: 'Review CPA effect', exact: true }))
        await approveConfigRead()
        await consoleFrame.getByRole('tab', { name: 'Access & Authentication', exact: true }).click()
        await expect(consoleFrame.getByRole('button', { name: 'Add API Key', exact: true })).toBeVisible()
        expect(fs.readFileSync(configPath, 'utf8')).toContain(generatedKey)
        await shot('config-reopened')
      }
      expect(countedEffects(root), 'the native plugin has never run before the proof').toHaveLength(0)
      at('native menu')
      await openNativeMenu(consoleFrame)
      await shot('menu')

      // Declining the trusted review presents nothing and runs no native code.
      at('declined review')
      await requestResourceReview(shell, consoleFrame)
      await shot('review')
      await declineReview(shell)
      await expect(resourceFrameElement(shell)).toHaveCount(0)
      expect(countedEffects(root)).toHaveLength(0)

      // Approval presents the plugin on its own grant-specific origin served by the Dashboard.
      at('first resource: approve and present')
      const first = await openResource()
      const grant = new URL(first.origin)
      expect(grant.origin).not.toBe(dashboard.origin)
      expect(grant.hostname).not.toBe(dashboard.hostname)
      if (secure) {
        expect(grant.protocol).toBe('https:')
        expect(grant.hostname).toMatch(/^p-[a-f0-9]{48}\.resource\.fixture\.invalid$/)
        expect(grant.port).toBe(String(ready.tls_port))
        // The Hermes session exists in this browser, yet none of it belongs to the resource origin.
        expect(await context.cookies(ready.dashboard_origin)).not.toEqual([])
      } else {
        expect(grant.protocol).toBe('http:')
        expect(grant.hostname).toMatch(/^p-[a-f0-9]{48}\.localhost$/)
        // Origins use the port the browser itself used (a relayed port differs from the backend's).
        expect(grant.port).toBe(dashboard.port)
        if (mode === 'http') expect(grant.port).toBe(String(ready.backend_port))
        else expect(grant.port).not.toBe(String(ready.backend_port))
      }
      expect(await context.cookies(first.origin)).toEqual([])
      // The Dashboard's own authority never serves native resource bytes.
      const onDashboard = await rawProbe(ready.dashboard_origin, ca)('GET', ENTRY_PATH)
      expect(onDashboard.body.toString('utf8')).not.toContain(`data-plugin-id="${PLUGIN_ID}"`)
      at('first resource: isolation and containment')
      await exercisePanel({
        host: shell, frames: first, driver, root, secrets, windows: () => context.pages().length,
        screenshot: name => shot(`first-${name}`),
      })
      // Neither the forged plugin Set-Cookie nor anything else was stored for the resource origin.
      expect(await context.cookies(first.origin)).toEqual([])
      // Explicit close revokes at once.
      at('first resource: close revokes')
      await closeResourceButton(shell).click()
      await expectRevoked(shell, first, driver, root)
      await shot('closed')

      // Locking the console while a native effect is still running revokes without waiting for it.
      at('second resource: present')
      const second = await openResource()
      expect(second.handle).not.toBe(first.handle)
      await expect(second.panelLocator.locator('#fixture')).toHaveAttribute('data-state', 'complete', { timeout: 150_000 })
      // A same-document change of the Dashboard URL is not a navigation away: the grant stays live.
      at('second resource: same-document navigation')
      await expectLiveAcrossSameDocumentNavigation(second, driver, root, async () => {
        await shell.evaluate(() => { window.location.hash = '#same-document' })
      })
      at('second resource: lock revokes during slow effect')
      await expectRevocationDuringSlowEffect(shell, second, driver, root,
        // Ordinary console work completes while a native effect is still running.
        async () => {
          await shell.getByRole('button', { name: 'Show CPA Console', exact: true }).click()
          // The default provider family is Gemini; the fixture's model belongs to OpenAI Compatible and only
          // the "All models" filter lists model names.
          await openConsoleSection(consoleFrame, 'AI Providers',
            consoleFrame.getByRole('button', { name: /^OpenAI Compatible\b/ }))
          await consoleFrame.getByRole('button', { name: /^OpenAI Compatible\b/ }).click()
          await consoleFrame.getByRole('button', { name: 'All models', exact: true }).click()
          await expect(consoleFrame.getByText('qualification-model', { exact: true }).first()).toBeVisible({ timeout: 30_000 })
          await consoleFrame.getByRole('button', { name: 'All models', exact: true }).click()
          await shell.getByRole('button', { name: 'Show plugin resource', exact: true }).click()
          await expect(resourceFrameElement(shell)).toBeVisible()
          expect(second.panel.isDetached(), 'switching panes preserves the approved resource document').toBe(false)
        },
        () => shell.getByRole('button', { name: 'Lock CPA Console', exact: true }).click())
      await expect(shell.locator('[data-cpa-phase="ended"]')).toBeVisible({ timeout: 30_000 })
      await shot('locked')

      // Closing while a client holds an unread, large response.
      at('third resource: reopen console and present')
      await openConsole()
      await openNativeMenu(consoleFrame)
      const third = await openResource()
      expect(third.handle).not.toBe(second.handle)
      await expect(third.panelLocator.locator('#fixture')).toHaveAttribute('data-state', 'complete', { timeout: 150_000 })
      at('third resource: close revokes with unread response')
      await expectRevocationWithUnreadResponse(shell, third, driver, root, ca,
        () => closeResourceButton(shell).click())
      await shot('unread-response-revoked')

      // Leaving the CPA page (route change unmounts the launcher) revokes as well.
      at('fourth resource: present')
      const fourth = await openResource()
      expect(fourth.handle).not.toBe(third.handle)
      await expect(fourth.panelLocator.locator('#fixture')).toHaveAttribute('data-state', 'complete', { timeout: 150_000 })
      at('fourth resource: route change revokes')
      await shell.locator('nav a[href^="/"]:not([href="/cpa"])').first().click()
      await expect(shell).not.toHaveURL(/\/cpa(\?|$)/)
      await expectRevoked(shell, fourth, driver, root)
      await shot('route-changed')

      // Nothing ran natively beyond the clicked, the deliberately slow and the deliberately large effect.
      at('final checks')
      expect(countedEffects(root).map(entry => entry.route)).toEqual(['/api/effect', '/api/slow', '/api/large'])
      expect(network.leaks()).toEqual([])
      expect(context.pages()).toHaveLength(1)

      at('helper stop')
      helper.stdin.write('{"command":"stop"}\n')
      const stopped = await nextMessage('stop', HELPER_WAIT_MS.stop)
      if (!('stopped' in stopped)) throw new Error('Dashboard scenario helper did not stop')
      expect(stopped.log_clean, 'the Dashboard process output never held a capability host, query or credential').toBe(true)
      expectNoCapabilityInLogs([path.join(root, '.hermes', 'logs')], secrets)
    } catch (error) {
      throw failedAt(stage, helperStage, error, routeError)
    } finally {
      await dispose()
    }
  })
}
