const { test, expect } = require('@playwright/test')
const WEB_URL = process.env.CLAW_WEB_URL || 'http://127.0.0.1:5173'
const stocks = Array.from({ length: 25 }, (_, i) => ({
  code: String(600100 + i), name: '历史样本' + i,
  first_seen_at: '2026-09-11T09:40:00+08:00',
  last_seen_at: '2026-09-11T11:' + String(i).padStart(2, '0') + ':00+08:00',
  record_count: 2,
}))
const activity = (changes = {}) => ({
  status: 'ok', trade_date: '2026-09-11', as_of_at: '2026-09-11T12:00:00+08:00',
  basis: 'recorded_candidates_distinct_stock', stock_count: 25, record_count: 50,
  first_seen_at: stocks[0].first_seen_at, last_seen_at: stocks[24].last_seen_at, stocks,
  note: '交易日已落库候选生命周期，不承诺无漏记', ...changes,
})
const payload = (changes = {}) => ({
  rows: [], total: 0, stock_summary: { capital_count: 0, buy_point_count: 3 },
  snapshot_time: '2026-09-11T12:00:00', snapshot_stale: false,
  market_session: 'lunch_break', detection_paused: true, monitor_observation_only: true,
  capital_activity: activity(), ...changes,
})
async function isolated(page, initial, failure = false) {
  const state = { response: initial, failure, requests: [], writes: [], errors: [] }
  page.on('pageerror', e => state.errors.push(e.message))
  await page.routeWebSocket(/.*/, ws => ws.close())
  await page.route('**/api/v1/**', async route => {
    if (route.request().method() !== 'GET') {
      state.writes.push(route.request().url())
      return route.abort()
    }
    if (new URL(route.request().url()).pathname.endsWith('/tenbagger/anomalies')) {
      state.requests.push(route.request().url())
      return route.fulfill({ status: state.failure ? 500 : 200, json: state.failure ? { detail: 'isolated failure' }
        : typeof state.response === 'function' ? await state.response(new URL(route.request().url())) : state.response })
    }
    return route.fulfill({ json: {} })
  })
  await page.goto(WEB_URL + '/tenbagger')
  await expect(page.locator('.anomaly-table')).toBeVisible()
  await expect.poll(() => state.requests.length).toBe(1)
  return state
}
const count = page => page.locator('.capital-recorded-count')
const dialog = page => page.getByRole('dialog')
async function refresh(page) {
  await page.evaluate(async () => {
    let vm = document.querySelector('.tenbagger-page').__vueParentComponent
    while (vm && !vm.setupState.loadAnomalies) vm = vm.parent
    await vm.setupState.loadAnomalies()
  })
}
async function safe(state) {
  expect(state.writes).toEqual([])
  expect(state.errors).toEqual([])
}

// 仅隔离 UI 样本；按真实 GET 参数分页，绝不写入业务库。
function currentStock(i, type = 'capital') {
  return {
    code: String(600300 + i), key: 'current-' + i, name: (type === 'capital' ? '当前资金样本' : '其他事件样本') + i,
    event_types: [type, 'breakthrough'], event_badges: [{ value: type, label: type === 'capital' ? '资金异动' : '突破' }],
    event_count: 1, primary_reason: '隔离资金明细，不代表买点', secondary_reason: '仅观察',
    driver_detail_lines: [], reference_driver_lines: [], display_score: 80,
    latest_as_of: '2026-09-11T11:29:00',
    detail: { price: 6.06, change_pct: 2.54 },
    fund_display: { schema: 'anomaly_fund_display_v1', purpose: 'display_only', basis: 'latest_snapshot',
      available: true, source: 'fund_flow', provider_source: 'tencent', source_version: 'tencent_hsfundtab_v1',
      clock_status: 'ok', is_stale: false, main_net_inflow: 120000000, main_net_inflow_pct: 12.5,
      source_quote_at: '2026-09-11T11:29:00', received_at: '2026-09-11T11:29:05', observed_at: '2026-09-11T11:29:06' },
    events: [{ key: 'event-' + i, event_type: type, event_label: '资金异动',
      description: '隔离事件原始原因', price: 6.06, change_pct: 2.54, latest_as_of: '2026-09-11T11:28:00' }],
  }
}
function currentResponse(url) {
  const capital = url.searchParams.get('event_type') === 'capital'
  const total = capital ? 180 : 120
  const offset = (Number(url.searchParams.get('page') || 1) - 1) * 50
  return payload({ capital_activity: undefined, total,
    rows: Array.from({ length: Math.min(50, total - offset) }, (_, i) => currentStock(offset + i, capital ? 'capital' : 'breakthrough')),
    stock_summary: { capital_count: 180, buy_point_count: 3 } })
}

test('capital count opens its current list, clears extra filters and paginates every stock without history', async ({ page }) => {
  const state = await isolated(page, currentResponse)
  await page.locator('.anomaly-filter-group').first().getByText('突破信号', { exact: true }).click()
  await page.locator('.anomaly-filter-group').nth(1).getByText('打板型', { exact: true }).click()
  await page.locator('.buy-point-checkbox').click()
  await expect(page.getByRole('checkbox', { name: '只看买点' })).toBeChecked()
  await page.locator('.table-footer .el-pagination .number').filter({ hasText: /^3$/ }).click()
  await expect.poll(() => new URL(state.requests.at(-1)).searchParams.get('page')).toBe('3')
  await expect(page.locator('.anomaly-table .stock-name').first()).toHaveText('其他事件样本100')
  const before = state.requests.length
  await page.locator('.capital-count-button').click()
  await expect(page.locator('.capital-list-title')).toHaveText('资金异动股票清单 · 共 180 只')
  await expect(page.locator('.capital-filter-note')).toBeInViewport()
  await expect(page.locator('.capital-filter-note')).toBeFocused()
  await expect(page.locator('.anomaly-table .stock-name')).toHaveCount(50)
  await expect(page.locator('.anomaly-table .stock-name').first()).toHaveText('当前资金样本0')
  await expect(page.locator('.anomaly-table')).toContainText('600300')
  await expect(page.locator('.anomaly-table .snapshot-price').first()).toHaveText('¥6.06')
  await expect(page.locator('.anomaly-table .capital-value').first()).toHaveText('+1.20亿')
  await expect(page.locator('.capital-list-page')).toContainText('本页 50 只 · 第 1 页')
  await expect(page.getByRole('checkbox', { name: '只看买点' })).not.toBeChecked()
  const params = new URL(state.requests.at(-1)).searchParams
  expect(params.get('event_type')).toBe('capital')
  expect(params.get('page')).toBe('1')
  expect(params.get('min_score')).toBe('50')
  expect(params.get('view')).toBe('stock')
  expect(params.get('setup_track')).toBeNull()
  expect(params.get('buy_point_only')).toBeNull()
  expect(params.get('sort_by')).toBe('priority')
  expect(state.requests).toHaveLength(before + 1)
  await expect(page.getByRole('button', { name: '查看已记录' }).first()).toBeDisabled()
  await expect(dialog(page)).toHaveCount(0)
  await page.locator('.el-table__expand-icon').first().click()
  await expect(page.locator('.anomaly-event-item').first()).toContainText('隔离事件原始原因')
  await page.locator('.table-footer .el-pagination .number').filter({ hasText: /^4$/ }).click()
  await expect(page.locator('.anomaly-table .stock-name')).toHaveCount(30)
  await expect(page.locator('.anomaly-table .stock-name').last()).toHaveText('当前资金样本179')
  await expect(page.locator('.capital-list-page')).toContainText('本页 30 只 · 第 4 页')
  await page.locator('.anomaly-table .stock-name').first().click()
  await expect(page).toHaveURL(/\/stocks\/600450$/)
  await safe(state)
})

test('capital loading hides the previous unrelated list and unknown counts', async ({ page }) => {
  let release
  const pending = new Promise(resolve => { release = resolve })
  const state = await isolated(page, async url => {
    if (url.searchParams.get('event_type') === 'capital') await pending
    return currentResponse(url)
  })
  await expect(page.locator('.anomaly-table')).toContainText('其他事件样本0')
  await page.getByRole('button', { name: '查看当前清单', exact: true }).click()
  await expect(page.locator('.capital-list-title')).toContainText('加载中')
  await expect(page.locator('.anomaly-table')).not.toContainText('其他事件样本')
  await expect(page.locator('.table-footer__meta')).toContainText('共 -- 只股票')
  await expect(page.getByRole('button', { name: '导出', exact: true })).toBeDisabled()
  release()
  await expect(page.locator('.capital-list-title')).toContainText('共 180 只')
  await safe(state)
})

test('failed capital query does not masquerade stale rows as the list; clicking count retries', async ({ page }) => {
  const state = await isolated(page, currentResponse)
  await expect(page.locator('.anomaly-table')).toContainText('其他事件样本0')
  state.failure = true
  await page.getByRole('button', { name: '查看当前清单', exact: true }).click()
  await expect(page.locator('.capital-list-title')).toContainText('查询失败，请重试')
  await expect(page.locator('.anomaly-table')).not.toContainText('其他事件样本')
  await expect(page.locator('.table-footer__meta')).toContainText('共 -- 只股票')
  await expect(page.getByRole('button', { name: '导出', exact: true })).toBeDisabled()
  state.failure = false
  await page.locator('.capital-count-button').click()
  await expect(page.locator('.capital-list-title')).toContainText('共 180 只')
  await expect(page.locator('.anomaly-table')).toContainText('当前资金样本0')
  await safe(state)
})

test('mobile count supports keyboard and scrolls directly to detailed current list', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  const state = await isolated(page, currentResponse)
  await page.locator('.capital-count-button').focus()
  await page.locator('.capital-count-button').press('Enter')
  await expect(page.locator('.capital-list-title')).toContainText('共 180 只')
  await expect(page.locator('.capital-list-title')).toBeInViewport()
  await expect(page.locator('.anomaly-table .stock-name').first()).toBeInViewport()
  await expect(page.locator('.capital-filter-note')).toBeFocused()
  await expect(dialog(page)).toHaveCount(0)
  await safe(state)
})

test('morning snapshot remains distinct; history pagination is local and metadata only', async ({ page }) => {
  const state = await isolated(page, payload({
    market_session: 'morning', detection_paused: false, monitor_observation_only: false,
    stock_summary: { capital_count: 2, buy_point_count: 3 },
  }))
  await expect(page.locator('.inflow .stat-value')).toHaveText('2')
  await expect(count(page)).toHaveText('本交易日已记录 25只')
  await expect(page.getByRole('button', { name: '推送买点' })).toBeEnabled()
  await page.getByRole('button', { name: '查看已记录' }).first().click()
  await expect(dialog(page)).toContainText('股票 25 只 · 身份记录 50 条')
  await expect(dialog(page)).toContainText('非买点、非成交，记录数不是股票数')
  await expect(dialog(page)).toContainText('2026-09-11')
  await expect(dialog(page).locator('tbody tr')).toHaveCount(20)
  await expect(dialog(page).locator('tbody tr').first()).toContainText('历史样本24')
  await expect(dialog(page).getByRole('link').first()).toHaveAttribute('href', '/stocks/600124')
  await dialog(page).locator('.el-pagination .number').filter({ hasText: /^2$/ }).click()
  await expect(dialog(page).locator('tbody tr')).toHaveCount(5)
  expect(state.requests).toHaveLength(1)
  await safe(state)
})

test('lunch zero snapshot explains capital empty filter without mixing history or buy counts', async ({ page }) => {
  const state = await isolated(page, payload())
  await expect(page.locator('.anomaly-session-note')).toContainText('午休')
  await expect(page.locator('.anomaly-session-note')).toContainText('午休 · 实时买点暂停')
  await expect(page.locator('.anomaly-session-note')).toContainText('历史记录仍可查看（不代表后台采集停机）')
  await expect(page.locator('.inflow .stat-value')).toHaveText('0')
  await expect(count(page)).toContainText('25只')
  await page.locator('.anomaly-filter-group').first().getByText('资金异动', { exact: true }).click()
  await expect(page.locator('.capital-filter-note')).toContainText('不包含交易日历史记录')
  await expect(page.locator('.capital-filter-note').getByRole('button')).toBeVisible()
  await expect(page.locator('.anomaly-table')).not.toContainText('历史样本')
  await expect(page.locator('.buy-point .stat-value')).toHaveText('3')
  await expect(page.getByRole('button', { name: '推送买点' })).toBeDisabled()
  await page.evaluate(async () => {
    let vm = document.querySelector('.tenbagger-page').__vueParentComponent
    while (vm && !vm.setupState.handlePushBuyPoints) vm = vm.parent
    await vm.setupState.handlePushBuyPoints()
  })
  await safe(state)
})

for (const scenario of ['legacy', 'failure', 'unavailable', 'empty']) {
  test(scenario + ' does not fabricate historical zero', async ({ page }) => {
    const capital = scenario === 'legacy' ? undefined : scenario === 'unavailable'
      ? activity({ status: 'unavailable', stock_count: null, record_count: null, stocks: [], note: '台账查询不可用' })
      : activity({ status: 'empty', stock_count: 0, record_count: 0, stocks: [], last_seen_at: null })
    const state = await isolated(page, payload({ capital_activity: capital, market_session: undefined, detection_paused: undefined }), scenario === 'failure')
    await expect(count(page)).toHaveText(scenario === 'empty' ? '本交易日已记录 0只' : '本交易日已记录 --')
    const historyButton = page.getByRole('button', { name: '查看已记录' }).first()
    if (scenario === 'empty') {
      await historyButton.click()
      await expect(dialog(page)).toContainText('本交易日尚无已记录')
    } else {
      await expect(historyButton).toBeDisabled()
      await expect(historyButton).toHaveAttribute('title', /不可用/)
      await expect(dialog(page)).toHaveCount(0)
      await expect(page.getByRole('button', { name: '查看当前清单', exact: true })).toBeEnabled()
    }
    await expect(page.getByRole('button', { name: '推送买点' })).toBeDisabled()
    await safe(state)
  })
}

test('lunch to afternoon only fresh normal response restores action; failed refresh fails closed', async ({ page }) => {
  const state = await isolated(page, payload())
  const button = page.getByRole('button', { name: '推送买点' })
  await expect(button).toBeDisabled()
  state.response = payload({ market_session: 'afternoon', detection_paused: false, monitor_observation_only: false, snapshot_stale: true })
  await refresh(page)
  await expect(button).toBeDisabled()
  state.response = { ...state.response, snapshot_stale: false, snapshot_time: '2026-09-11T13:00:01' }
  await refresh(page)
  await expect(button).toBeEnabled()
  await expect(page.locator('.anomaly-session-note')).toContainText('下午交易时段')
  state.failure = true
  await refresh(page)
  await expect(button).toBeDisabled()
  await expect(count(page)).toHaveText('本交易日已记录 --')
  await safe(state)
})

test('lunch read-only snapshot keeps original morning count and time', async ({ page }) => {
  const state = await isolated(page, payload({
    snapshot_cache_policy: 'lunch_read_only', snapshot_time: '2026-09-11T11:29:00',
    stock_summary: { capital_count: 7, buy_point_count: 0 },
  }))
  await expect(page.locator('.anomaly-session-note')).toContainText('午休只读快照')
  await expect(page.locator('.anomaly-session-note')).toContainText('当前快照事件不代表当前有效买点')
  await expect(page.locator('.anomaly-snapshot-note')).toContainText('午休只读快照 · 2026-09-11 11:29:00')
  await expect(page.locator('.inflow .stat-label')).toContainText('当前快照')
  await expect(page.locator('.inflow .stat-value')).toHaveText('7')
  await expect(page.locator('.buy-point .stat-value')).toHaveText('0')
  await expect(count(page)).toContainText('25只')
  await expect(page.getByRole('button', { name: '推送买点' })).toBeDisabled()
  await safe(state)
})

test('desktop long ledger note stays in dialog and does not stretch capital card', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  const note = '详细历史口径说明：仅为已落库候选生命周期，非买点非成交，不承诺无漏记。'.repeat(12)
  const state = await isolated(page, payload({ capital_activity: activity({ note }) }))
  await expect(page.locator('.inflow .stat-hint')).toHaveText('已记录≠当前买点')
  await expect(page.locator('.inflow')).not.toContainText(note)
  await page.screenshot({ path: '../outputs/radar-capital-session-20260911-desktop-cards.png', fullPage: true })
  const box = await page.locator('.inflow').boundingBox()
  expect(box.height).toBeLessThan(240)
  await page.getByRole('button', { name: '查看已记录' }).first().click()
  await expect(dialog(page)).toContainText(note)
  await safe(state)
})

test('closed mobile stays observation only with accessible history', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  const state = await isolated(page, payload({ market_session: 'closed' }))
  await expect(page.locator('.anomaly-session-note')).toContainText('休市')
  await page.getByRole('button', { name: '查看已记录' }).first().click()
  await expect(dialog(page)).toBeVisible()
  const box = await dialog(page).boundingBox()
  expect(box.width).toBeLessThanOrEqual(390)
  await page.waitForTimeout(350) // Let Element Plus dialog entrance transition settle for visual evidence.
  await page.screenshot({ path: '../outputs/radar-capital-session-20260911-mobile-history.png', animations: 'disabled' })
  await safe(state)
})
