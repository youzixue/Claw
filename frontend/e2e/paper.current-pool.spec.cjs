const { test, expect } = require('@playwright/test')

const WEB_URL = process.env.CLAW_WEB_URL || 'http://127.0.0.1:5173'
test.use({ timezoneId: 'America/New_York' }) // Exchange-local expiry must ignore browser timezone.

function comparison() {
  return {
    generated_at: '2026-09-07T10:00:00',
    pairs: [{
      route_id: 'c3_mainline_first_board', label: '策略C3 · 主线首板盘中确认',
      execution_mode: 'evidence_only', champion: {}, challenger: {},
      event_counts: { confirmed: 9 }, evidence: {},
      current_pool: {
        read_model_version: 'c3_current_pool_v1', valid: true, expired: false,
        as_of: '2026-09-07T10:00:00', expires_at: '2026-09-07T10:01:15',
        cumulative_confirmed_today: 9, structural_count: 1, eligible_count: 1, confirmed_count: 1,
        reason: '隔离测试扫描帧', coverage: { valid_quote_count: 3000, allowed_count: 3000 },
        members: [{ code: '600201', name: '隔离测试股', eligible: true, currently_confirmed: true, reason: '逐帧连续确认' }],
        invalidated: [], note: '当前池与累计证据分离', execution_enabled: false,
      },
    }],
  }
}

async function isolatedPage(page, getPayload) {
  const errors = []
  const writeRequests = []
  page.on('pageerror', error => errors.push(error.message))
  // Every business API is intercepted: these fixture tests cannot create accounts,
  // mutate the running database, or submit a simulated/real trade.
  await page.route('**/api/v1/**', async route => {
    if (route.request().method() !== 'GET') writeRequests.push(route.request().method())
    const isComparison = route.request().url().includes('/paper/challengers/comparison')
    await route.fulfill({ status: 200, json: isComparison ? getPayload() : {} })
  })
  await page.clock.install({ time: new Date('2026-09-07T02:00:00Z') })
  await page.goto(`${WEB_URL}/paper`, { waitUntil: 'domcontentloaded' })
  await page.locator('.el-radio-button').filter({ hasText: 'C·主线扩散' }).click()
  await page.getByRole('tab', { name: '策略对比' }).click()
  await expect(page.getByRole('heading', { name: '策略C3 · 主线首板盘中确认', exact: true })).toBeVisible()
  return { errors, writeRequests }
}

for (const width of [1440, 390]) {
  test(`current pool expires without altering cumulative evidence at width ${width}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 })
    const { errors, writeRequests } = await isolatedPage(page, comparison)
    const pool = page.locator('.same-day-funnel-box').filter({ hasText: '当前有效结构 / 资格 / 确认' })
    await expect(pool).toContainText('1 / 1 / 1')
    await expect(pool).toContainText('隔离测试股')
    await expect(pool).toContainText('当日累计确认（不可变证据）9')
    await expect(page.getByText('只采证 · 不撮合', { exact: true })).toBeVisible()
    await pool.scrollIntoViewIfNeeded()
    await page.screenshot({ path: testInfo.outputPath('current-pool.png') })
    const noBodyOverflow = await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)
    expect(noBodyOverflow).toBe(true)
    const tableLayout = await pool.locator('.current-pool-table-wrap').evaluate(el => ({
      width: el.clientWidth, parent: el.parentElement.clientWidth,
      nowrap: getComputedStyle(el.querySelector('td .cell')).whiteSpace,
      codeFits: el.querySelector('td .cell').scrollWidth <= el.querySelector('td .cell').clientWidth,
    }))
    expect(tableLayout.width).toBeGreaterThan(tableLayout.parent - 40)
    expect(tableLayout.nowrap).toBe('nowrap')
    expect(tableLayout.codeFits).toBe(true)
    await page.clock.fastForward(76000)
    await expect(pool).toContainText('0 / 0 / 0')
    await expect(pool).not.toContainText('隔离测试股')
    await expect(pool).toContainText('当日累计确认（不可变证据）9')
    await expect(pool).toContainText('已过期')
    expect(errors).toEqual([])
    expect(writeRequests).toEqual([])
  })
}

test('legacy backend without a current pool is not presented as zero current signals', async ({ page }) => {
  const payload = comparison()
  delete payload.pairs[0].current_pool
  const { errors, writeRequests } = await isolatedPage(page, () => payload)
  await expect(page.getByText('当前有效结构 / 资格 / 确认')).toHaveCount(0)
  await expect(page.getByText('当前路线版本历史累计（非当前有效池）')).toBeVisible()
  expect(errors).toEqual([])
  expect(writeRequests).toEqual([])
})
