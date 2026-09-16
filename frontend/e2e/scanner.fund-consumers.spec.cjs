const { test, expect } = require('@playwright/test')
const WEB_URL = process.env.CLAW_WEB_URL || 'http://127.0.0.1:5173'

test('resonance separates current zero negative unknown and dated research evidence', async ({ page }, testInfo) => {
  const errors = [], writes = []
  page.on('pageerror', error => errors.push(error.message))
  await page.routeWebSocket('**/*', ws => ws.close())
  const resonance = [
    { code: '600001', main_net_inflow: 0, main_fund_status: 'ok' },
    { code: '600002', main_net_inflow: -120000000, main_fund_status: 'ok' },
    { code: '600003', main_net_inflow: null, main_fund_status: 'stale',
      main_fund_display: { available: true, purpose: 'display_only', main_net_inflow: 230000000,
        source_quote_at: '2026-09-14T15:00:00' } },
    { code: '600004', main_net_inflow: 990000000, main_fund_status: 'unknown' },
  ].map(row => ({ name: '隔离样本', stock_change: 2, best_resonance_score: 60,
    best_level: 'weak_resonance', purpose: 'research_only', sectors: [], ...row }))
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url())
    if (!url.pathname.startsWith('/api/')) return route.fallback() // keep Vite /src/api modules
    if (route.request().method() !== 'GET') writes.push(route.request().method())
    const json = url.pathname.endsWith('/tenbagger/resonance') ? { resonance }
      : url.pathname.endsWith('/tenbagger/anomalies') ? { rows: [], total: 0 } : {}
    await route.fulfill({ status: 200, json })
  })
  await page.goto(WEB_URL + '/tenbagger', { waitUntil: 'domcontentloaded' })
  await page.getByRole('tab', { name: '共振分析' }).click()
  const cells = page.locator('.resonance-fund-evidence')
  await expect(cells).toHaveCount(4)
  await expect(cells.nth(0)).toContainText('当前资金 0.00亿')
  await expect(cells.nth(1)).toContainText('当前资金 -1.20亿')
  await expect(cells.nth(2)).toContainText('当前资金 --')
  await expect(cells.nth(2)).toContainText('日期研究快照 +2.30亿')
  await expect(cells.nth(2)).toContainText('2026-09-14T15:00:00')
  await expect(cells.nth(3)).toContainText('当前资金 --')
  await expect(cells.nth(3)).not.toContainText('9.90')
  await expect(page.locator('.fund-research-note')).toContainText('不代表当前交易资金')
  await page.screenshot({ path: testInfo.outputPath('resonance-funds.png'), fullPage: true })
  expect(errors).toEqual([])
  expect(writes).toEqual([])
})
