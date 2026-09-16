const { test, expect } = require('@playwright/test')

const WEB_URL = process.env.CLAW_WEB_URL || 'http://127.0.0.1:5173'

test('switching strategies resets to account overview and ignores stale responses', async ({ page }) => {
  test.setTimeout(30_000)
  const browserIssues = []

  page.on('pageerror', error => browserIssues.push(`pageerror: ${error.message}`))
  page.on('console', message => {
    if (message.type() === 'error') browserIssues.push(`console: ${message.text()}`)
  })

  // Keep only the initial strategy-A account response in flight. Without a request
  // generation guard it arrives after B and overwrites B's banner and metrics.
  await page.route('**/api/v1/paper/account?*', async route => {
    if (route.request().url().includes('account_name=default')) {
      await new Promise(resolve => setTimeout(resolve, 1_500))
    }
    await route.continue()
  })

  await page.goto(`${WEB_URL}/paper`, { waitUntil: 'domcontentloaded' })
  await expect(page.getByRole('heading', { name: '模拟盘' })).toBeVisible()

  await page.getByRole('tab', { name: '交易记录' }).click()
  await expect(page.getByRole('tab', { name: '交易记录' })).toHaveAttribute('aria-selected', 'true')

  await page.locator('.el-radio-button').filter({ hasText: 'B·晋级二板' }).click()
  await expect(page.getByRole('tab', { name: '账户概览' })).toHaveAttribute('aria-selected', 'true')
  await expect(page.locator('.strategy-banner-text strong')).toHaveText('策略B · 晋级二板')

  // Wait until the deliberately slow A response has arrived; B must remain visible.
  await page.waitForTimeout(1_800)
  await expect(page.locator('.account-switch .el-radio-button.is-active')).toContainText('B·晋级二板')
  await expect(page.getByRole('tab', { name: '账户概览' })).toHaveAttribute('aria-selected', 'true')
  await expect(page.locator('.strategy-banner-text strong')).toHaveText('策略B · 晋级二板')
  expect(browserIssues, browserIssues.join('\n')).toEqual([])
})

test('auto tab separates simulated fills from candidates and reports enabled continuous buys', async ({ page }) => {
  await page.goto(`${WEB_URL}/paper`, { waitUntil: 'domcontentloaded' })
  await page.locator('.el-radio-button').filter({ hasText: 'B·晋级二板' }).click()
  await page.getByRole('tab', { name: '自动执行' }).click()

  await expect(page.locator('.auto-summary')).toContainText('当前 / 上限')
  await expect(page.locator('.auto-summary')).toContainText('今日模拟成交')
  await expect(page.locator('.auto-summary')).toContainText('今日演练候选')
  await expect(page.locator('.auto-summary')).toContainText('自动模拟买卖已启用')
  await expect(page.locator('.auto-summary')).toContainText('下一健康轮次')
})

test('necessary-data waiting is not displayed as no candidate', async ({ page }) => {
  await page.route('**/api/v1/paper/auto/status**', async route => {
    const response = await route.fetch()
    const payload = await response.json()
    // 明确的UI契约测试，不作为生产数据恢复或交易证据。
    payload.daily_outcome = {
      trade_date: '2026-09-08', terminal_status: 'data_limited',
      reason_code: 'necessary_data_wait_no_fill',
    }
    await route.fulfill({ response, json: payload })
  })
  await page.goto(`${WEB_URL}/paper`, { waitUntil: 'domcontentloaded' })
  await page.getByRole('tab', { name: '自动执行' }).click()
  await expect(page.locator('.auto-summary')).toContainText('必要数据等待·无成交')
})

test('auto and trade tables scroll internally on mobile', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto(`${WEB_URL}/paper`, { waitUntil: 'domcontentloaded' })
  await page.locator('.el-radio-button').filter({ hasText: 'B·晋级二板' }).click()
  await page.getByRole('tab', { name: '自动执行' }).click()
  await expect(page.locator('.auto-log-table')).toBeVisible()

  const autoWidths = await page.locator('.auto-log-table').evaluate(el => ({
    body: document.documentElement.scrollWidth,
    viewport: window.innerWidth,
    container: el.parentElement.clientWidth,
    content: el.parentElement.scrollWidth,
  }))
  expect(autoWidths.body).toBeLessThanOrEqual(autoWidths.viewport)
  expect(autoWidths.content).toBeGreaterThan(autoWidths.container)

  await page.getByRole('tab', { name: '交易记录' }).click()
  await expect(page.locator('.trade-record-table')).toBeVisible()
  const tradeWidths = await page.locator('.trade-record-table').evaluate(el => ({
    body: document.documentElement.scrollWidth,
    viewport: window.innerWidth,
    container: el.parentElement.clientWidth,
    content: el.parentElement.scrollWidth,
  }))
  expect(tradeWidths.body).toBeLessThanOrEqual(tradeWidths.viewport)
  expect(tradeWidths.content).toBeGreaterThan(tradeWidths.container)
})
