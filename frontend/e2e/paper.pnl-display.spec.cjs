const { test, expect } = require('@playwright/test')

const WEB_URL = process.env.CLAW_WEB_URL || 'http://127.0.0.1:5173'

// 隔离UI契约样本，复现累计盈利但当前持仓多数浮亏；绝不请求线上业务接口。
function comparison() {
  return {
    generated_at: '2026-09-10T16:01:01',
    pairs: [{
      route_id: 'momentum_first_retest', label: '策略A2 · 强势股首次回踩',
      execution_mode: 'isolated_paper', hypothesis: '隔离展示回归测试',
      champion: { id: 2, total_return: 0.72, open_position_count: 0, nav: [] },
      challenger: {
        id: 12, account_configured: true, execution_enabled: true,
        initial_capital: 50000, total_assets: 50975.05, total_return: 1.95,
        trade_count: 12, open_position_count: 4, fee_drag: 78.95,
        trade_stats: { total_pnl: 1086.05 },
        return_breakdown: {
          total_pnl: 975.05, realized_pnl: 1086.05, fees_paid: 78.95,
          daily_pnl: 255.95, daily_return_pct: 0.50464,
          daily_trade_date: '2026-09-10', daily_status: 'ok',
          daily_note: '相对上一交易日净值估算，含已实现与未实现盈亏变化',
        },
        positions: [
          { code: '000811', name: '冰轮环境', buy_amount: 100, buy_price: 38.39, current_price: 37.75, profit_loss: -64, profit_pct: -1.67 },
          { code: '603123', name: '翠微股份', buy_amount: 400, buy_price: 11.52, current_price: 11.47, profit_loss: -20, profit_pct: -0.43 },
          { code: '603980', name: '吉华集团', buy_amount: 600, buy_price: 8.08, current_price: 8.15, profit_loss: 42, profit_pct: 0.87 },
          { code: '601208', name: '东材科技', buy_amount: 100, buy_price: 49.08, current_price: 48.59, profit_loss: -49, profit_pct: -1 },
        ],
        nav: [
          { date: '2026-09-09', nav: 1.014382 },
          { date: '2026-09-10', nav: 1.019501 },
        ],
      },
      event_counts: {}, evidence: {},
    }],
  }
}

async function isolatedPage(page, payload) {
  const errors = []
  const writes = []
  await page.routeWebSocket('**/*', ws => ws.close())
  page.on('pageerror', error => errors.push(error.message))
  await page.route('**/api/v1/**', async route => {
    if (route.request().method() !== 'GET') writes.push(route.request().method())
    const pathname = new URL(route.request().url()).pathname
    const json = pathname.endsWith('/paper/challengers/comparison') ? payload
      : pathname.endsWith('/paper/positions') ? { positions: payload.pairs[0].challenger.positions || [] }
      : {}
    await route.fulfill({ status: 200, json })
  })
  await page.goto(`${WEB_URL}/paper`, { waitUntil: 'domcontentloaded' })
  await page.getByRole('tab', { name: '策略对比' }).click()
  await expect(page.getByRole('heading', { name: '策略A2 · 强势股首次回踩', exact: true })).toBeVisible()
  await page.evaluate(() => document.fonts.ready)
  return { errors, writes }
}

for (const width of [1440, 390]) {
  test(`separates cumulative daily and position PnL at width ${width}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 950 })
    const { errors, writes } = await isolatedPage(page, comparison())
    const cumulative = page.locator('.comparison-metric').filter({ hasText: '候选账户成立以来收益' })
    await expect(cumulative).toContainText('1.95%')
    await expect(cumulative).toContainText('+975.05 元 · 非今日收益')
    await expect(page.locator('.daily-pnl-metric')).toContainText('+255.95')
    await expect(page.locator('.daily-pnl-metric')).toContainText('+0.50%')
    await expect(page.locator('.daily-pnl-metric > strong')).toHaveClass(/text-up/)
    await expect(page.locator('.position-pnl-metric')).toContainText('-91.00')
    await expect(page.locator('.position-pnl-metric > strong')).toHaveClass(/text-down/)
    await page.locator('.daily-pnl-metric').scrollIntoViewIfNeeded()
    await page.screenshot({ path: testInfo.outputPath('pnl-metrics-isolated.png') })

    const summary = page.locator('.challenger-position-summary')
    await expect(summary).toContainText('持仓 4 只')
    await expect(summary).toContainText('数量 1,200 股')
    await expect(summary).toContainText('总市值 18112.00 元')
    await expect(summary).toContainText('浮盈亏 -91.00 元')
    await expect(summary).toContainText('盈利 1 只 · 亏损 3 只 · 持平 0 只')

    const table = page.locator('.challenger-position-table')
    await expect(table).toContainText('持股数量（股）')
    await expect(table).toContainText('持仓市值（元）')
    await expect(table).toContainText('浮盈亏（元）')
    await expect(table).toContainText('浮盈亏（%）')
    const rows = table.locator('.el-table__body-wrapper tbody tr')
    await expect(rows).toHaveCount(4)
    await expect(rows.nth(0)).toContainText('3775.00')
    await expect(rows.nth(0)).toContainText('-64.00')
    await expect(rows.nth(2)).toContainText('+42.00')
    await summary.scrollIntoViewIfNeeded()
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))))
    // Wait for ECharts' initial canvas animation after the chart scrolls into view.
    await page.waitForTimeout(1200)
    await page.screenshot({ path: testInfo.outputPath('positions-isolated.png'), animations: 'disabled' })
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true)
    const layout = await table.evaluate(el => ({
      container: el.parentElement.clientWidth, content: el.parentElement.scrollWidth,
      cellStyle: getComputedStyle(el.querySelector('td .cell')).whiteSpace,
    }))
    expect(layout.content).toBeGreaterThan(layout.container)
    expect(layout.cellStyle).toBe('nowrap')
    const headingsFit = await table.locator('th .cell').evaluateAll(cells =>
      cells.filter(cell => ['持股数量（股）', '浮盈亏（%）', '浮盈亏（元）'].includes(cell.textContent.trim()))
        .every(cell => cell.scrollWidth <= cell.clientWidth))
    expect(headingsFit).toBe(true)
    expect(errors).toEqual([])
    expect(writes).toEqual([])
  })
}

test('missing yesterday and empty positions are different from unknown position data', async ({ page }) => {
  const payload = comparison()
  payload.pairs[0].challenger.positions = []
  payload.pairs[0].challenger.return_breakdown = {
    daily_status: 'missing_previous_nav', daily_pnl: null, daily_return_pct: null,
    daily_note: '缺少上一交易日净值，不能计算今日收益',
  }
  const { errors, writes } = await isolatedPage(page, payload)
  await expect(page.locator('.daily-pnl-metric > strong')).toHaveText('--')
  await expect(page.locator('.daily-pnl-metric')).toContainText('缺少上一交易日净值')
  await expect(page.locator('.challenger-position-summary')).toContainText('持仓 0 只')
  await expect(page.locator('.challenger-position-summary')).toContainText('总市值 0.00 元')
  await expect(page.locator('.challenger-position-table')).toContainText('暂无隔离持仓')
  expect(errors).toEqual([])
  expect(writes).toEqual([])
})

test('invalid and legacy missing values never become zero profit or zero shares', async ({ page }) => {
  const payload = comparison()
  payload.pairs[0].challenger.positions = [{
    code: '000811', name: '测试缺失', buy_amount: null, current_price: 'NaN',
    profit_loss: null, profit_pct: null,
  }]
  delete payload.pairs[0].challenger.return_breakdown
  const { errors, writes } = await isolatedPage(page, payload)
  await expect(page.locator('.daily-pnl-metric > strong')).toHaveText('--')
  await expect(page.locator('.daily-pnl-metric')).toContainText('缺少日收益口径数据')
  await expect(page.locator('.challenger-position-summary')).toContainText('数量 -- 股')
  await expect(page.locator('.challenger-position-summary')).toContainText('总市值 -- 元')
  await expect(page.locator('.challenger-position-summary')).toContainText('浮盈亏 -- 元')
  await expect(page.locator('.position-pnl-metric > strong')).toHaveText('--')
  expect(errors).toEqual([])
  expect(writes).toEqual([])
})

test('explicit unknown accounting values are not replaced with legacy partial totals', async ({ page }) => {
  const payload = comparison()
  payload.pairs[0].challenger.return_breakdown = {
    total_pnl: null, realized_pnl: null, fees_paid: null,
    daily_pnl: null, daily_return_pct: null, daily_status: 'invalid_data',
    daily_note: '账户数据不完整',
  }
  const { errors, writes } = await isolatedPage(page, payload)
  const card = page.locator('.challenger-card')
  await expect(card.getByText('账户累计盈亏（元）').locator('..')).toContainText('--')
  await expect(card.getByText('累计已实现账本值（元，旧值可能漏买费）').locator('..')).not.toContainText('1086.05')
  await expect(card.getByText('累计已付费用（元，已计入收益）').locator('..')).not.toContainText('78.95')
  await expect(page.locator('.daily-pnl-metric > strong')).toHaveText('--')
  expect(errors).toEqual([])
  expect(writes).toEqual([])
})

test('evidence-only route has no fabricated account PnL or holdings', async ({ page }) => {
  const payload = comparison()
  payload.pairs[0].execution_mode = 'evidence_only'
  payload.pairs[0].challenger = { account_configured: false }
  const { errors, writes } = await isolatedPage(page, payload)
  await expect(page.locator('.daily-pnl-metric')).toHaveCount(0)
  await expect(page.locator('.position-pnl-metric')).toHaveCount(0)
  await expect(page.locator('.challenger-position-table')).toHaveCount(0)
  await expect(page.getByText('只采证，不用 0% 冒充收益')).toBeVisible()
  expect(errors).toEqual([])
  expect(writes).toEqual([])
})

test('cent-level offsetting PnL remains zero rather than a rounding-induced profit', async ({ page }) => {
  const payload = comparison()
  payload.pairs[0].challenger.positions = [0.1, 0.2, -0.3].map((profit_loss, index) => ({
    code: String(index), name: '分位样本', buy_amount: 100, current_price: 10, profit_loss,
  }))
  const { errors, writes } = await isolatedPage(page, payload)
  await expect(page.locator('.position-pnl-metric > strong')).toHaveText('0.00')
  await expect(page.locator('.position-pnl-metric > strong')).toHaveClass(/text-flat/)
  expect(errors).toEqual([])
  expect(writes).toEqual([])
})

test('normal positions tab reuses shares market value and PnL columns', async ({ page }) => {
  const { errors, writes } = await isolatedPage(page, comparison())
  await page.getByRole('tab', { name: '当前持仓' }).click()
  const panel = page.getByRole('tabpanel', { name: '当前持仓', exact: true })
  await expect(panel).toContainText('数量 1,200 股')
  await expect(panel).toContainText('总市值 18112.00 元')
  await expect(panel).toContainText('浮盈亏 -91.00 元')
  await expect(panel).toContainText('持股数量（股）')
  await expect(panel).toContainText('浮盈亏（元）')
  expect(errors).toEqual([])
  expect(writes).toEqual([])
})
