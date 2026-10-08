const { test, expect } = require('@playwright/test')
const WEB_URL = process.env.CLAW_WEB_URL || 'http://127.0.0.1:5173'

test('incomplete limit source shows unknown metrics, never a zero market', async ({ page }) => {
  const errors = []
  page.on('pageerror', error => errors.push(error.message))
  // All API requests are browser-isolated; no writes or production API reads.
  await page.route('**/api/v1/**', async route => {
    const data = route.request().url().includes('/promotion/board-height') ? {
      trade_date: '2026-09-28', height: null, limit_up_count: null,
      seal_rate: null, promotion_rate: null, ladder_summary: [],
      source_health: { ready: false, coverage: 0.95, detail_unknown_count: 3, stale_state_count: 1 },
    } : {}
    await route.fulfill({ json: data })
  })
  await page.goto(WEB_URL + '/promotion', { waitUntil: 'networkidle' })
  await expect(page.getByTestId('limit-source-health')).toContainText('不启用东财回退')
  await expect(page.getByTestId('limit-source-health')).toContainText('字段待补 3 只')
  for (const name of ['market-board-height', 'market-limit-up-count', 'market-seal-rate', 'market-promotion-rate']) {
    await expect(page.getByTestId(name)).toContainText('--')
  }
  await expect(page.getByTestId('market-scope-note')).toHaveCount(0)
  expect(errors).toEqual([])
})
