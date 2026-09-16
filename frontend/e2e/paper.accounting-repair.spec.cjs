const { test, expect } = require('@playwright/test')
const WEB_URL = process.env.CLAW_WEB_URL || 'http://127.0.0.1:5173'

async function isolated(page, missing = false, accountingOverride = {}) {
  const errors = [], nonGet = [], requests = []
  page.on('pageerror', e => errors.push(e.message))
  // Close ALL websocket routes, including business WS and Vite HMR.
  await page.routeWebSocket('**/*', ws => ws.close())
  const accounting = missing ? null : {
    cash_reconciliation_residual: 0, asset_reconciliation_residual: 0,
    status: 'ok', realized_ledger_pnl: 362.01, realized_net_pnl: 357.98,
    today_realized_net_pnl: 0, net_unrealized_pnl: -18, remaining_entry_fees: 5,
    legacy_entry_fee_adjustment: -4.03, unexplained_realized_adjustment: 0,
    today_mtm: { daily_pnl: -18, daily_status: 'ok', daily_note: '相对上一交易日实际净值估算；费用已含在收益内，不重复扣除' },
    closed_cycle_performance: { sample_count: 2, win_rate: 50, excluded_cycle_count: 1 },
    ...accountingOverride,
  }
  const positions = [{ id: 54, code: '003021', name: '兆威机电', buy_amount: 100,
    buy_price: 67.96, current_price: 67.83, profit_loss: -13, profit_pct: -.1913,
    accounting: missing ? null : { net_unrealized_pnl: -18, remaining_entry_fees: 5, cycle_total_net_pnl: -18 } }]
  const account = { id: 2, account_name: 'default', initial_capital: 50000, total_assets: 50339.98,
    total_return: .68, accounting }
  const challengerAccounting = missing ? null : {
    ...accounting, realized_net_pnl: 212.69, net_unrealized_pnl: 114.99,
    today_realized_net_pnl: -873.36, legacy_entry_fee_adjustment: 0,
  }
  await page.route('**/api/v1/**', async route => {
    const req = route.request()
    if (req.method() !== 'GET') nonGet.push(req.method() + ' ' + req.url())
    const path = new URL(req.url()).pathname
    requests.push(path)
    let json = {}
    if (path.endsWith('/paper/account')) json = { account }
    if (path.endsWith('/paper/positions')) json = { positions }
    if (path.endsWith('/paper/nav')) json = { nav: [] }
    if (path.endsWith('/paper/trades')) json = { trades: [] }
    if (path.endsWith('/paper/challengers/comparison')) json = {
      pairs: [{ route_id: 'momentum_first_retest', label: '策略A2 · 强势股首次回踩',
        execution_mode: 'isolated_paper', champion: account, event_counts: {}, evidence: {},
        challenger: { id: 12, account_configured: true, positions, accounting: challengerAccounting,
          return_breakdown: { daily_pnl: missing ? null : -75.37, daily_return_pct: -.1495,
            daily_note: '独立候选净值变动，不与主账户合账' } },
      }],
    }
    await route.fulfill({ status: 200, json })
  })
  await page.goto(WEB_URL + '/paper', { waitUntil: 'domcontentloaded' })
  return { errors, nonGet, requests }
}

test('unreconciled cash and assets are visible and never deducted again', async ({ page }) => {
  const checks = await isolated(page, false, {
    status: 'unreconciled', issues: [], cash_reconciliation_residual: 2, asset_reconciliation_residual: -3,
  })
  const panel = page.getByTestId('account-accounting')
  await expect(page.getByTestId('account-reconciliation')).toContainText('现金对账差额 +2.00 元；资产对账差额 -3.00 元')
  await expect(panel).toContainText('现金或资产对账不平衡')
  await expect(panel).not.toContainText('经济口径未完整对账：缺少数据')
  await expect(panel).toContainText('今日净值变动 -18.00 元')
  await expect(panel).toContainText('持仓净浮盈 -18.00 元')
  await page.getByRole('tab', { name: '策略对比', exact: true }).click()
  await expect(page.getByTestId('challenger-reconciliation')).toContainText('+2.00 / -3.00')
  await expect(page.locator('.challenger-card')).toContainText('现金或资产对账不平衡')
  await expect(page.locator('.daily-pnl-metric')).toContainText('-75.37')
  await expect(page.getByTestId('challenger-net-unrealized')).toContainText('+114.99')
  expect(checks.errors).toEqual([])
  expect(checks.nonGet).toEqual([])
})

for (const width of [1440, 390]) {
  test('economic accounting stays isolated, width ' + width, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 950 })
    const checks = await isolated(page)
    const panel = page.getByTestId('account-accounting')
    await expect(panel).toContainText('今日净值变动 -18.00 元')
    await expect(panel).toContainText('今日卖出净实现 0.00 元')
    await expect(panel).toContainText('累计净已实现 +357.98 元')
    await expect(panel).toContainText('旧买费展示调整 -4.03 元')
    await expect(panel).toContainText('不改历史金额')
    await panel.scrollIntoViewIfNeeded()
    await page.screenshot({ path: info.outputPath('account-accounting.png') })
    await page.getByRole('tab', { name: '当前持仓', exact: true }).click()
    const positions = page.getByRole('tabpanel', { name: '当前持仓', exact: true })
    await expect(positions).toContainText('浮盈亏 -13.00 元')
    await expect(positions).toContainText('净浮盈（元）')
    await expect(positions).toContainText('-18.00')
    await page.getByRole('tab', { name: '策略对比', exact: true }).click()
    await expect(page.getByTestId('challenger-net-realized')).toContainText('+212.69')
    await expect(page.getByTestId('challenger-net-unrealized')).toContainText('+114.99')
    await expect(page.locator('.daily-pnl-metric')).toContainText('-75.37')
    await expect(page.locator('.challenger-card')).toContainText('-873.36')
    expect(checks.errors).toEqual([])
    expect(checks.nonGet).toEqual([])
    expect(checks.requests.some(p => p.endsWith('/paper/account'))).toBeTruthy()
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy()
  })
}

test('missing economic basis never becomes zero or gross fallback', async ({ page }) => {
  const checks = await isolated(page, true)
  await expect(page.getByTestId('account-reconciliation')).toContainText('现金对账差额 -- 元；资产对账差额 -- 元')
  await expect(page.getByTestId('account-accounting')).toContainText('持仓净浮盈 -- 元')
  await expect(page.getByTestId('account-accounting')).toContainText('经济口径未完整对账')
  await page.getByRole('tab', { name: '策略对比', exact: true }).click()
  await expect(page.getByTestId('challenger-net-unrealized')).toContainText('--')
  await expect(page.getByTestId('challenger-net-realized')).toContainText('--')
  expect(checks.errors).toEqual([])
  expect(checks.nonGet).toEqual([])
})
