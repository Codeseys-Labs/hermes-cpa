import * as fs from 'node:fs'
import * as path from 'node:path'
import { randomBytes } from 'node:crypto'
import { spawn } from 'node:child_process'
import { once } from 'node:events'
import { createInterface } from 'node:readline'
import type { ElectronApplication, FrameLocator, Page } from '@playwright/test'

import { buildAppEnv, launchDesktop, type Sandbox } from './fixtures'
import { test, expect } from './test'
import {
  approveReview, closeResourceButton, countedEffects, declineReview, electronDriver, exercisePanel,
  expectedHost, expectLiveAcrossSameDocumentNavigation, expectNoCapabilityInLogs,
  expectRevocationDuringSlowEffect, expectRevoked, openNativeMenu, requestResourceReview,
  resourceFrameElement, type TrustedHostPolicy, waitForResourceFrames, watchPageNetwork,
} from './hermes-cpa-resource-ui'

/**
 * Network policy of this generated-data, Docker `--network none` scenario for the trusted Desktop host. Its Capabilities
 * page embeds two docs-site pickers (Skills Hub and Plugin Catalog) from one public origin, and the renderer may look the
 * plugin catalog up there. Those requests belong to the host, not to the CPA console or a plugin resource, and no socket
 * may open for them: they are refused before the network and counted. Attribution comes from the browser's frame tree and
 * the owning `<iframe>` element (exact `src` and `sandbox`), never from the destination. Nothing else is excused: a request
 * from the console or a plugin resource to any origin, this one included, is passed through untouched and must be refused
 * by a Content Security Policy, or it fails the run. Requests made before the watch starts are not observed.
 */
const HUB_ORIGIN = 'https://hermes-agent.nousresearch.com'
const HOST_NETWORK_POLICY: TrustedHostPolicy = {
  origin: HUB_ORIGIN,
  embeds: ['skills', 'plugins'].map(section => ({
    src: `${HUB_ORIGIN}/docs/${section}?embed=picker`, sandbox: 'allow-scripts allow-same-origin',
  })),
}

async function navigateConsole(frame: FrameLocator, label: string) {
  const link = frame.locator('aside').getByRole('link', { name: new RegExp(`^${label}`) })
  await expect(link).toBeAttached()
  // The mobile sidebar is translated offscreen, so its links still report visible.
  const expandSidebar = frame.getByRole('button', { name: 'Expand sidebar', exact: true })
  if (await expandSidebar.isVisible()) {
    await expandSidebar.click()
  }
  await link.click()
}

async function approveConfigRead(page: Page) {
  const review = page.getByRole('dialog', { name: 'Review CPA effect', exact: true })
  await expect(review).toBeVisible()
  await expect(review).toContainText('GET /v8/management/config.yaml')
  await expect(review.getByRole('listitem')).toHaveText(['config_export'])
  await review.getByRole('button', { name: 'Approve exact effects', exact: true }).click()
}

test('full CPA configuration stays opaque, edits native keys, remains a single chat-side workspace, and presents an installed native plugin resource in isolation', async ({}, testInfo) => {
  test.setTimeout(900_000)
  const root = process.env.CPA_UI_ROOT
  test.skip(!root, 'Requires an explicitly prepared generated-data network-none CPA root')
  if (!root || !path.isAbsolute(root) || !fs.existsSync(path.join(root, '.desktop-smoke-used'))) {
    throw new Error('Prepared isolated CPA root required')
  }
  const userDataDir = path.join(root, 'electron-user-data')
  fs.mkdirSync(userDataDir, { recursive: true })
  fs.writeFileSync(path.join(userDataDir, 'window-state.json'), JSON.stringify({
    x: 0, y: 0, width: 1600, height: 1050, isMaximized: false,
  }))
  fs.writeFileSync(path.join(userDataDir, 'zoom-state.json'), JSON.stringify({ zoomLevel: 0 }))
  const sandbox: Sandbox = { root, hermesHome: path.join(root, '.hermes'), userDataDir, cleanup: () => {} }
  const fixture = spawn('/opt/q/venv/bin/python', [
    '/opt/q/qualification/ui_oauth_fixture.py', '--root', root, '--consent',
  ], { stdio: ['pipe', 'pipe', 'ignore'] })
  const lines = createInterface({ input: fixture.stdout })[Symbol.asyncIterator]()
  const readFixture = async (): Promise<Record<string, unknown>> => {
    const line = await lines.next()
    if (line.done) throw new Error('Generated OAuth authority exited')
    const value: unknown = JSON.parse(line.value)
    if (typeof value !== 'object' || value === null || Array.isArray(value) || 'error' in value) {
      throw new Error('Generated OAuth authority refused the scenario')
    }
    return value as Record<string, unknown>
  }
  const fixtureCommand = async (command: Record<string, unknown>) => {
    fixture.stdin.write(JSON.stringify(command) + '\n')
    return readFixture()
  }
  let desktopApp: ElectronApplication | undefined
  let desktopPage: Page | undefined
  try {
    const authority = await readFixture()
    if (authority.ready !== true || typeof authority.email !== 'string') throw new Error('Generated OAuth authority unavailable')
    const launched = await launchDesktop(buildAppEnv(sandbox, { HERMES_DESKTOP_PYTHON: '/opt/q/venv/bin/python' }))
    desktopApp = launched.app
    desktopPage = launched.page
    const { page } = launched
    // The installed Desktop half stays inert until the user enables its switch.
    const consoleNavigation = page.getByRole('button', { name: 'CPA Console', exact: true })
    await page.getByRole('button', { name: 'Capabilities', exact: true }).click()
    await page.getByRole('button', { name: 'Plugins', exact: true }).click()
    const desktopToggle = page.getByRole('switch', { name: 'Desktop: CLI Proxy API Console', exact: true })
    await expect(desktopToggle).not.toBeChecked()
    await expect(consoleNavigation).toHaveCount(0)
    await desktopToggle.click()
    await expect(desktopToggle).toBeChecked()
    await expect(consoleNavigation).toBeVisible()
    await page.screenshot({ path: testInfo.outputPath('desktop-plugin-opt-in.png') })
    const openConsole = () => consoleNavigation.click()
    await openConsole()
    await expect(page.getByRole('button', { name: 'Unlock CPA Console', exact: true })).toBeVisible({ timeout: 45_000 })
    const workspace = page.locator('[data-slot="pane-tab"]').filter({ hasText: 'CPA Console' })
    await expect(workspace).toHaveCount(1)
    await openConsole()
    await expect(workspace).toHaveCount(1)
    await page.getByRole('button', { name: 'Unlock CPA Console', exact: true }).click()
    await page.getByRole('button', { name: 'Unlock console', exact: true }).click()
    await expect(page.locator('[data-cpa-phase="ready"]')).toBeVisible({ timeout: 45_000 })
    const element = page.locator('iframe[title="CPA Management Console"]')
    await expect(element).toHaveAttribute('sandbox', 'allow-scripts')
    const consoleFrame = page.frameLocator('iframe[title="CPA Management Console"]')
    const child = await element.contentFrame()
    if (!child) throw new Error('CPA console frame unavailable')
    const isolation = await child.locator('body').evaluate(async () => {
      let parentDenied = false
      let storageDenied = false
      try { void parent.document.body } catch { parentDenied = true }
      try { void localStorage.length } catch { storageDenied = true }
      const blocked = new Promise<string>(resolve => {
        const timer = setTimeout(() => resolve('no-csp-event'), 3000)
        document.addEventListener('securitypolicyviolation', event => {
          clearTimeout(timer)
          resolve(event.effectiveDirective)
        }, { once: true })
      })
      void fetch('http://127.0.0.1:9/forbidden-console-network').catch(() => undefined)
      return { parentDenied, storageDenied, blocked: await blocked }
    })
    expect(isolation).toEqual({ parentDenied: true, storageDenied: true, blocked: 'connect-src' })

    await navigateConsole(consoleFrame, 'AI Providers')
    // The default family is Gemini; configured model names appear in the model filter.
    await consoleFrame.getByRole('button', { name: /^OpenAI Compatible\b/ }).click()
    await consoleFrame.getByRole('button', { name: 'All models', exact: true }).click()
    await expect(consoleFrame.getByText('qualification-model', { exact: true }).first()).toBeVisible()
    await consoleFrame.getByRole('button', { name: 'All models', exact: true }).click()
    await navigateConsole(consoleFrame, 'OAuth Login')
    await consoleFrame.getByRole('button', { name: 'Start Anthropic Login', exact: true }).click()
    await page.getByRole('button', { name: 'Approve exact effects', exact: true }).click()
    const authorization = consoleFrame.getByText(/^https:\/\/claude\.ai\/oauth\/authorize\?/)
    await expect(authorization).toBeVisible()
    const authorizationUrl = await authorization.textContent()
    if (!authorizationUrl) throw new Error('Native OAuth authorization URL unavailable')
    const issued = await fixtureCommand({ command: 'authorize', url: authorizationUrl.trim() })
    if (typeof issued.callback !== 'string') throw new Error('Generated callback unavailable')
    await consoleFrame.getByLabel('Callback URL', { exact: true }).fill(issued.callback)
    await consoleFrame.getByRole('button', { name: 'Submit Callback URL', exact: true }).click()
    await expect(consoleFrame.getByText('Authentication successful!', { exact: true }).first()).toBeVisible({ timeout: 20_000 })
    expect((await fixtureCommand({ command: 'counts' })).exchanges).toBe(1)
    await page.screenshot({ path: testInfo.outputPath('desktop-oauth-completed.png') })
    await navigateConsole(consoleFrame, 'Auth Files')
    await expect(consoleFrame.getByRole('button', { name: /upload/i }).first()).toBeVisible()
    await expect(consoleFrame.getByText(authority.email, { exact: false }).first()).toBeVisible()
    await navigateConsole(consoleFrame, 'Config Panel')
    await approveConfigRead(page)
    await consoleFrame.getByRole('tab', { name: 'Access & Authentication', exact: true }).click()
    await consoleFrame.getByRole('button', { name: 'Add API Key', exact: true }).click()
    const apiKeyDialog = consoleFrame.getByRole('dialog', { name: 'Add API Key', exact: true })
    const generatedKey = `generated-ui-${randomBytes(24).toString('hex')}`
    await apiKeyDialog.getByLabel('API Key', { exact: true }).fill(generatedKey)
    await apiKeyDialog.getByRole('button', { name: 'Add', exact: true }).click()
    await consoleFrame.getByRole('button', { name: 'Save changes', exact: true }).click()
    // Save preview and commit each read a fresh snapshot before their next review.
    await approveConfigRead(page)
    await expect(consoleFrame.getByRole('dialog', { name: 'Review Changes' })).toBeVisible()
    await consoleFrame.getByRole('button', { name: 'Confirm Save', exact: true }).click()
    await approveConfigRead(page)
    const writeReview = page.getByRole('dialog', { name: 'Review CPA effect', exact: true })
    await expect(writeReview.getByText('config_write', { exact: true })).toBeVisible()
    await page.screenshot({ path: testInfo.outputPath('desktop-config-review.png') })
    await writeReview.getByRole('button', { name: 'Approve exact effects', exact: true }).click()
    const configPath = path.join(sandbox.hermesHome, 'cpa', 'instances', 'default', 'config.yaml')
    await expect.poll(() => fs.readFileSync(configPath, 'utf8').includes(generatedKey), { timeout: 30_000 }).toBe(true)
    await approveConfigRead(page)
    await expect(consoleFrame.getByRole('dialog', { name: 'Review Changes' })).toHaveCount(0)
    await expect(consoleFrame.getByRole('group', { name: 'Unsaved changes', exact: true })).toHaveCount(0)
    await page.getByText('Operation receipts', { exact: true }).click()
    await expect(page.getByText(/response received \(HTTP 200\)/).first()).toBeVisible()
    await page.screenshot({ path: testInfo.outputPath('desktop-native-config-completed.png') })
    await workspace.hover()
    await workspace.getByRole('button', { name: 'Close', exact: true }).click()
    await expect(workspace).toHaveCount(0)
    await openConsole()
    await expect(workspace).toHaveCount(1)
    await expect(page.getByRole('button', { name: 'Unlock CPA Console', exact: true })).toBeVisible()
    await page.getByRole('button', { name: 'Unlock CPA Console', exact: true }).click()
    await page.getByRole('button', { name: 'Unlock console', exact: true }).click()
    await expect(page.locator('[data-cpa-phase="ready"]')).toBeVisible()
    await navigateConsole(consoleFrame, 'Config Panel')
    await approveConfigRead(page)
    await consoleFrame.getByRole('tab', { name: 'Access & Authentication', exact: true }).click()
    await expect(consoleFrame.getByRole('button', { name: 'Add API Key', exact: true })).toBeVisible()
    expect(fs.readFileSync(configPath, 'utf8')).toContain(generatedKey)
    await page.screenshot({ path: testInfo.outputPath('desktop-config-reopened.png') })

    // ── Installed native plugin resource: reviewed, isolated, effect-counted, revocable ──
    // The generated Go plugin was installed by the preparation step through CPA's own plugin
    // store and has never been invoked. Every observation below is of the real Desktop shell.
    const driver = electronDriver(launched.app)
    const network = await watchPageNetwork(page, expectedHost([]), HOST_NETWORK_POLICY)
    const secrets = [generatedKey, fs.readFileSync(path.join(root, 'session-token'), 'utf8').trim()]
    const windows = () => launched.app.windows().length
    const shot = (name: string) => page.screenshot({ path: testInfo.outputPath(`desktop-resource-${name}.png`) })
    const unlockConsole = async () => {
      await expect(page.getByRole('button', { name: 'Unlock CPA Console', exact: true })).toBeVisible()
      await page.getByRole('button', { name: 'Unlock CPA Console', exact: true }).click()
      await page.getByRole('button', { name: 'Unlock console', exact: true }).click()
      await expect(page.locator('[data-cpa-phase="ready"]')).toBeVisible({ timeout: 45_000 })
    }
    const openResource = async () => {
      await requestResourceReview(page, consoleFrame)
      await approveReview(page)
      return waitForResourceFrames(page)
    }
    expect(countedEffects(root)).toHaveLength(0)
    await openNativeMenu(consoleFrame)
    await shot('menu')
    // Declining the trusted review presents nothing and runs no native code.
    await requestResourceReview(page, consoleFrame)
    await shot('review')
    await declineReview(page)
    await expect(resourceFrameElement(page)).toHaveCount(0)
    expect(countedEffects(root)).toHaveLength(0)
    // Approval presents the plugin on its own host-owned origin; the full live proof follows.
    const first = await openResource()
    expect(new URL(first.wrapperUrl).protocol).toBe('hermes-plugin-resource:')
    expect(first.origin).not.toBe(new URL(page.url()).origin)
    await exercisePanel({
      host: page, frames: first, driver, root, secrets, windows, screenshot: name => shot(`first-${name}`),
    })
    // Explicit close revokes at once.
    await closeResourceButton(page).click()
    await expectRevoked(page, first, driver, root)
    await shot('closed')
    // Locking the console while a native effect is still running revokes without waiting for it.
    const second = await openResource()
    expect(second.handle).not.toBe(first.handle)
    await expect(second.panelLocator.locator('#fixture')).toHaveAttribute('data-state', 'complete', { timeout: 150_000 })
    // An in-app (same-document) route change while the console is docked is not a navigation away.
    const originalHash = await page.evaluate(() => window.location.hash)
    await expectLiveAcrossSameDocumentNavigation(second, driver, root, async () => {
      await page.evaluate(target => { window.location.hash = target }, originalHash === '#/' ? '#/settings' : '#/')
    })
    await page.evaluate(target => { window.location.hash = target }, originalHash)
    await expectRevocationDuringSlowEffect(page, second, driver, root,
      // Ordinary console work completes while a native effect is still running.
      async () => {
        await page.getByRole('button', { name: 'Show CPA Console', exact: true }).click()
        await navigateConsole(consoleFrame, 'AI Providers')
        await consoleFrame.getByRole('button', { name: /^OpenAI Compatible\b/ }).click()
        await consoleFrame.getByRole('button', { name: 'All models', exact: true }).click()
        await expect(consoleFrame.getByText('qualification-model', { exact: true }).first()).toBeVisible({ timeout: 30_000 })
        await consoleFrame.getByRole('button', { name: 'All models', exact: true }).click()
        await page.getByRole('button', { name: 'Show plugin resource', exact: true }).click()
        await expect(resourceFrameElement(page)).toBeVisible()
        expect(second.panel.isDetached(), 'switching panes preserves the approved resource document').toBe(false)
      },
      () => page.getByRole('button', { name: 'Lock CPA Console', exact: true }).click())
    await expect(page.locator('[data-cpa-phase="ended"]')).toBeVisible({ timeout: 30_000 })
    await shot('locked')
    // Closing the workspace tab revokes too.
    await workspace.hover()
    await workspace.getByRole('button', { name: 'Close', exact: true }).click()
    await expect(workspace).toHaveCount(0)
    await openConsole()
    await expect(workspace).toHaveCount(1)
    await unlockConsole()
    await openNativeMenu(consoleFrame)
    const third = await openResource()
    expect(third.handle).not.toBe(second.handle)
    await expect(third.panelLocator.locator('#fixture')).toHaveAttribute('data-state', 'complete', { timeout: 150_000 })
    await workspace.hover()
    await workspace.getByRole('button', { name: 'Close', exact: true }).click()
    await expect(workspace).toHaveCount(0)
    await expectRevoked(page, third, driver, root)
    // Nothing ran natively beyond the one clicked effect and the one deliberately slow effect.
    expect(countedEffects(root).map(entry => entry.route)).toEqual(['/api/effect', '/api/slow'])
    // Trusted-host requests refused before the network, by initiator; counts only, never origins or paths.
    await network.settled()
    const refused = network.trustedBlocked()
    testInfo.annotations.push({ type: 'trusted-host-network-refused', description: `host=${refused.host} embed=${refused.embed}` })
    console.log(`desktop trusted-host network refused before the network: host=${refused.host} embed=${refused.embed}`)
    expect(network.leaks()).toEqual([])
    expectNoCapabilityInLogs([userDataDir, path.join(root, '.hermes', 'logs')], secrets)
  } finally {
    if (desktopPage && !desktopPage.isClosed()) await desktopPage.screenshot({ path: testInfo.outputPath('desktop-final.png') }).catch(() => undefined)
    await desktopApp?.close()
    fixture.stdin.end()
    if (fixture.exitCode === null) {
      const timer = setTimeout(() => fixture.kill('SIGTERM'), 3000)
      try { await once(fixture, 'exit') } finally { clearTimeout(timer) }
    }
  }
})
