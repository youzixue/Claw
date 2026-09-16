const { test, expect } = require('@playwright/test')

const WEB_URL = process.env.CLAW_WEB_URL || 'http://127.0.0.1:5173'
// 全部业务API/WebSocket隔离；此处数据只用于UI合同，不代表资金源恢复。
const fresh = {
  source: 'fund_flow', provider_source: 'tencent', source_version: 'tencent_hsfundtab_v1',
  clock_status: 'ok', is_stale: false,
  source_quote_at: '2026-09-10T10:00:00', received_at: '2026-09-10T10:00:05',
  observed_at: '2026-09-10T10:00:06', as_of: '2026-09-10T10:01:00',
  main_net_inflow: 120000000, main_net_inflow_pct: 12.5,
}
const book = {
  bid1_price: 6.06, ask1_price: 6.07, bid1_volume: 100, ask1_volume: 0,
  bid_depth_5: 300, ask_depth_5: 100, orderbook_imbalance: 0.5,
  support_strength_score: 72, seal_quality_score: 0, withdrawal_ratio: 0,
}
function row(index, detail = {}, overrides = {}) {
  return {
    code: String(600900 + index), key: String(index), name: '隔离样本' + index,
    event_type: 'breakthrough', event_types: ['breakthrough'],
    event_badges: [{ value: 'breakthrough', label: '突破' }], event_count: 1,
    primary_reason: '仅展示测试，不提供买入建议', secondary_reason: '观察',
    driver_detail_lines: [], reference_driver_lines: [], events: [], display_score: 80,
    setup_grade: 'A2 盘口确认后执行', latest_as_of: '2026-09-10T10:01:00',
    orderbook_display: { purpose: 'display_only', clock_status: 'ok', source_quote_at: '2026-09-10T10:01:00' },
    detail: { price: 6.06, change_pct: 2.54, ...detail }, ...overrides,
  }
}
async function isolatedPage(page, rows, rank = []) {
  const errors = [], writes = [], requests = []
  page.on('pageerror', error => errors.push(error.message))
  await page.routeWebSocket(/\/ws(?:\?|$)/, ws => ws.close())
  await page.route('**/api/v1/**', async route => {
    if (route.request().method() !== 'GET') writes.push(route.request().method())
    const url = new URL(route.request().url())
    requests.push(url)
    let json = {}
    if (url.pathname.endsWith('/tenbagger/anomalies')) {
      const eventType = url.searchParams.get('event_type')
      const filtered = eventType ? rows.filter(item => item.event_types.includes(eventType)) : rows
      json = { rows: filtered, total: filtered.length, snapshot_time: '2026-09-10T10:01:00' }
    } else if (url.pathname.endsWith('/tenbagger/rank')) json = { rank, total: rank.length }
    await route.fulfill({ status: 200, json })
  })
  await page.goto(WEB_URL + '/tenbagger', { waitUntil: 'domcontentloaded' })
  await expect(page.locator('.anomaly-table')).toBeVisible()
  return { errors, writes, requests }
}
function displaySnapshot(changes = {}) {
  return { ...fresh, schema: 'anomaly_fund_display_v1', purpose: 'display_only',
    basis: 'latest_snapshot', available: true, clock_status: 'stale', is_stale: true,
    trade_date: '2026-09-10', displayed_at: '2026-09-10T18:00:00', ...changes }
}

test('list reads separate latest funds while execution stays null, old event is not repainted', async ({ page }) => {
  const snapshot = displaySnapshot()
  const state = await isolatedPage(page, [row(1, {
    ...book, main_net_inflow: null, main_net_inflow_pct: null, source: 'unavailable',
  }, { fund_display: snapshot, buy_point_pushable: false, feishu_pushable: false,
    orderbook_display: { clock_status: 'historical', source_quote_at: '2026-09-10T15:05:00' },
    events: [{ key: 'legacy', event_label: '原异动', main_net_inflow: null,
      fund_display: displaySnapshot({ basis: 'event_snapshot', available: false, main_net_inflow: null }) }],
  })])
  await expect(cells(page)).toContainText('+1.20亿')
  await expect(cells(page)).toContainText('占比 +12.5%')
  await expect(cells(page)).toContainText('最近资金 · 2026-09-10 10:00:00')
  await expect(cells(page)).toContainText('盘口快照')
  await expect(cells(page)).not.toContainText('确认强')
  await expect(cells(page)).not.toContainText('低吸强')
  await expect(page.locator('.anomaly-table')).not.toContainText('买点已到')
  await cells(page).hover()
  await expect(tooltip(page)).toContainText(snapshot.received_at)
  await expect(tooltip(page)).toContainText('不是当前交易资金')
  await expect(tooltip(page)).toContainText('不是') // no inferred final close
  await page.locator('.el-table__expand-icon').first().click()
  await expect(page.locator('.anomaly-event-item')).toContainText('事件时点资金 --')
  expect(state.errors).toEqual([])
  expect(state.writes).toEqual([])
})

test('frozen event keeps original negative zero and timestamp after close', async ({ page }) => {
  const state = await isolatedPage(page, [
    row(1, fresh, { fund_display: displaySnapshot({ basis: 'event_snapshot',
      event_at: fresh.as_of, main_net_inflow: -120000000, main_net_inflow_pct: -12.5 }) }),
    row(2, fresh, { fund_display: displaySnapshot({ main_net_inflow: 0, main_net_inflow_pct: 0 }) }),
    row(3, fresh, { fund_display: displaySnapshot({ available: false, main_net_inflow: null }) }),
  ])
  await expect(cells(page).nth(0)).toContainText('-1.20亿')
  await expect(cells(page).nth(0)).toContainText('事件资金 · 2026-09-10 10:00:00')
  await expect(cells(page).nth(1)).toContainText('0.00亿')
  await expect(cells(page).nth(2).locator('.capital-value')).toHaveText('--') // no fallback to detail
  expect(state.errors).toEqual([])
  expect(state.writes).toEqual([])
})

test('unknown future and single-sided book cannot become confirmation labels', async ({ page }) => {
  const rows = ['unknown', 'future', 'single_sided', 'stale'].map((status, i) => row(i, {
    ...book, support_strength_score: 100,
  }, { fund_display: displaySnapshot(), orderbook_display: { clock_status: status } }))
  const state = await isolatedPage(page, rows)
  for (const [i, label] of ['盘口待核', '盘口时钟异常', '单边盘口', '盘口过期'].entries()) {
    await expect(cells(page).nth(i)).toContainText(label)
    await expect(cells(page).nth(i)).not.toContainText('确认强')
    await expect(cells(page).nth(i)).not.toContainText('承接偏强')
  }
  expect(state.errors).toEqual([])
  expect(state.writes).toEqual([])
})

const cells = page => page.locator('.anomaly-table .capital-cell')
const tooltip = page => page.locator('.el-popper:visible .anomaly-tooltip').filter({ hasText: '资金与盘口明细' })

test('Tencent alias shows real provider, yuan amount, percent and three source clocks', async ({ page }) => {
  await page.setViewportSize({ width: 2048, height: 1100 })
  const state = await isolatedPage(page, [row(1, { ...fresh, ...book })])
  await expect(page.locator('.fund-source-legend')).toContainText('腾讯主力资金')
  await expect(page.locator('.fund-source-legend')).not.toContainText('降级为')
  await expect(cells(page)).toContainText('+1.20亿')
  await expect(cells(page)).toContainText('腾讯主力资金')
  await expect(cells(page)).toContainText('占比 +12.5% · 承接评分 72')
  await expect(cells(page)).toContainText('封单评分 0 · 撤单 0%')
  await expect(cells(page).locator('.capital-value')).toHaveClass(/text-up/)
  await expect(cells(page).getByText('资金流入', { exact: true }).locator('..')).toHaveClass(/el-tag--danger/)
  await cells(page).hover()
  await expect(tooltip(page)).toContainText('tencent_hsfundtab_v1')
  for (const clock of [fresh.source_quote_at, fresh.received_at, fresh.observed_at]) {
    await expect(tooltip(page)).toContainText(clock)
  }
  await expect(tooltip(page)).toContainText('主力 = 超大单 + 大单')
  await expect(tooltip(page)).toContainText('盘口失衡：+50.0%')
  await expect(tooltip(page)).toContainText('买/卖五档合计：300手 / 100手')
  await expect(tooltip(page)).toContainText('100手 / 0手')
  await page.screenshot({ path: 'test-results/tenbagger-fund-display-tooltip-isolated.png' })
  expect(state.errors).toEqual([])
  expect(state.writes).toEqual([])
})

test('real zero is flat, negative is outflow, missing is never 0.0 percent', async ({ page }) => {
  const state = await isolatedPage(page, [
    row(1, { ...fresh, main_net_inflow: 0, main_net_inflow_pct: 0 }),
    row(2, { ...fresh, main_net_inflow: -120000000, main_net_inflow_pct: -12.5 }),
    row(3, { main_net_inflow: null, main_net_inflow_pct: null, source: 'unavailable', is_stale: true, clock_status: 'unknown' }),
  ])
  await expect(cells(page).nth(0)).toContainText('0.00亿')
  await expect(cells(page).nth(0)).toContainText('资金持平')
  await expect(cells(page).nth(0)).toContainText('占比 0.0%')
  await expect(cells(page).nth(1)).toContainText('-1.20亿')
  await expect(cells(page).nth(1)).toContainText('资金流出')
  await expect(cells(page).nth(1).locator('.capital-value')).toHaveClass(/text-down/)
  await expect(cells(page).nth(2)).toContainText('资金缺失')
  await expect(cells(page).nth(2)).toContainText('占比 -- · 承接评分 --')
  await expect(cells(page).nth(2)).not.toContainText('0.0%')
  expect(state.errors).toEqual([])
  expect(state.writes).toEqual([])
})

test('stale, invalid clock, unknown provider and legacy book difference cannot confirm money', async ({ page }) => {
  const state = await isolatedPage(page, [
    row(1, { ...fresh, source: 'fund_flow_stale', clock_status: 'stale', is_stale: true }),
    row(2, { ...fresh, clock_status: 'future' }),
    row(3, { ...fresh, provider_source: null, source_version: null }),
    row(4, { ...fresh, source: 'stock_spot', provider_source: null, source_version: null, ...book }),
    row(5, { ...fresh, clock_status: 'unknown', is_stale: true }),
    row(6, { ...fresh, source: 'eastmoney_main_fund', provider_source: 'eastmoney', source_version: 'individual_fund_flow_v3_f124' }),
  ])
  for (const [index, status] of ['资金过期', '时钟异常', '资金待核', '资金缺失', '资金待核'].entries()) {
    await expect(cells(page).nth(index)).toContainText(status)
    await expect(cells(page).nth(index).locator('.capital-value')).toHaveText('--')
    await expect(cells(page).nth(index)).toContainText('占比 --')
  }
  await expect(cells(page).nth(2)).not.toContainText('同花顺')
  await expect(cells(page).nth(3)).toContainText('承接评分 72') // book remains independent
  await expect(cells(page).nth(5)).toContainText('东方财富主力资金')
  await expect(cells(page).nth(5)).toContainText('+1.20亿')
  expect(state.errors).toEqual([])
  expect(state.writes).toEqual([])
})

test('null blank boolean and non-finite values never coerce into measurements', async ({ page }) => {
  const values = [null, '', ' ', false, true, 'NaN', 'Infinity']
  const state = await isolatedPage(page, values.map((value, i) => row(i, {
    ...fresh, ...book, main_net_inflow: value, main_net_inflow_pct: value,
    withdrawal_ratio: value, support_strength_score: value, seal_quality_score: value,
    orderbook_imbalance: value,
  })))
  for (let i = 0; i < values.length; i++) {
    await expect(cells(page).nth(i)).toContainText('资金缺失')
    await expect(cells(page).nth(i)).toContainText('占比 -- · 承接评分 --')
    await expect(cells(page).nth(i)).toContainText('封单评分 -- · 撤单 --')
    await expect(cells(page).nth(i)).not.toContainText('确认强')
  }
  await cells(page).first().hover()
  await expect(tooltip(page)).toContainText('盘口失衡：--')
  expect(state.errors).toEqual([])
  expect(state.writes).toEqual([])
})

test('limit up without fund or book is not strong; measured seal remains independent', async ({ page }) => {
  const limit = { event_type: 'limit_up', event_types: ['limit_up'] }
  const state = await isolatedPage(page, [
    row(1, {}, limit),
    row(2, { ...book, seal_quality_score: 80 }, limit),
    row(3, { ...fresh, ...book, main_net_inflow: 0, main_net_inflow_pct: 0 }, limit),
    row(4, { ...fresh, ...book, main_net_inflow: -100000000 }, limit),
  ])
  await expect(cells(page).nth(0)).toContainText('资金缺失')
  await expect(cells(page).nth(0)).toContainText('盘口缺失')
  await expect(cells(page).nth(0)).not.toContainText('封板强')
  await expect(cells(page).nth(1)).toContainText('资金缺失')
  await expect(cells(page).nth(1)).toContainText('封单强')
  await expect(cells(page).nth(2)).toContainText('资金持平')
  await expect(cells(page).nth(2)).toContainText('净额 0.00亿 · 占比 0.0%')
  await expect(cells(page).nth(3)).toContainText('资金分歧')
  expect(state.errors).toEqual([])
  expect(state.writes).toEqual([])
})

test('expanded events distinguish original fund evidence from legacy synthetic zero', async ({ page }) => {
  const state = await isolatedPage(page, [row(1, fresh, { events: [
    { key: 'new', event_label: '合格事件', ...fresh, price: 6.06, change_pct: 2.54 },
    { key: 'old', event_label: '旧版缺证据', main_net_inflow: 0, price: 6.06, change_pct: 2.54 },
  ] })])
  await page.locator('.el-table__expand-icon').first().click()
  const events = page.locator('.anomaly-event-item')
  await expect(events.nth(0)).toContainText('事件时点资金 +1.20亿')
  await expect(events.nth(0)).toContainText('腾讯主力资金')
  await expect(events.nth(1)).toContainText('事件时点资金 --')
  await expect(events.nth(1)).not.toContainText('0.00亿')
  expect(state.errors).toEqual([])
  expect(state.writes).toEqual([])
})

test('filter empty state and CSV export still work without a push or trade', async ({ page }) => {
  const state = await isolatedPage(page, [row(1, fresh)])
  await page.getByText('涨停', { exact: true }).filter({ visible: true }).last().click()
  await expect(page.locator('.anomaly-table')).toContainText('暂无异动数据')
  expect(state.requests.some(url => url.searchParams.get('event_type') === 'limit_up')).toBe(true)
  await page.getByText('全部', { exact: true }).click()
  await expect(cells(page)).toHaveCount(1)
  await page.getByRole('button', { name: /导出/ }).click()
  const download = page.waitForEvent('download')
  await page.getByText('导出 CSV', { exact: true }).filter({ visible: true }).click()
  expect((await download).suggestedFilename()).toContain('异动监控')
  expect(state.errors).toEqual([])
  expect(state.writes).toEqual([])
})

test('rank amounts preserve zero and CSV no longer hardcodes Eastmoney', async ({ page }) => {
  const state = await isolatedPage(page, [], [
    { code: '600901', name: '排行零值', main_net_inflow_billion: 0 },
    { code: '600902', name: '排行缺失', main_net_inflow_billion: null },
  ])
  await page.getByRole('tab', { name: '强势排行', exact: true }).click()
  await expect(page.locator('.rank-table')).toContainText('资金 0.00亿')
  await expect(page.locator('.rank-table')).toContainText('资金 --')
  await page.getByRole('button', { name: /导出/ }).filter({ visible: true }).click()
  const downloaded = page.waitForEvent('download')
  await page.getByText('导出 CSV', { exact: true }).filter({ visible: true }).click()
  const file = await downloaded
  const stream = await file.createReadStream()
  const parts = []
  for await (const part of stream) parts.push(part)
  const csv = Buffer.concat(parts).toString('utf8')
  expect(csv).toContain('主力资金净额(亿)')
  expect(csv).not.toContain('东财主力')
  expect(state.errors).toEqual([])
  expect(state.writes).toEqual([])
})

test('five completed fund sessions disclose holiday boundaries, zero, missing and CSV', async ({ page }, testInfo) => {
  // Block every API namespace, not just the v1 fixtures registered by isolatedPage.
  await page.route('**/api/**', route => new URL(route.request().url()).pathname.startsWith('/api/')
    ? route.fulfill({ status: 200, json: {} }) : route.fallback())
  await page.routeWebSocket('**/*', ws => ws.close())
  const window = {
    version: 'five_confirmed_closed_sessions_v1', basis: 'dated_latest_not_pit',
    decision_at: '2026-10-12T15:01:00', through_date: '2026-10-12',
    session_dates: ['2026-09-24', '2026-09-28', '2026-09-29', '2026-09-30', '2026-10-12'],
    expected_count: 5,
  }
  const state = await isolatedPage(page, [], [
    { code: '600901', name: '会话零', fund_5d_billion: 0, fund_5d_complete: true,
      fund_5d_count: 5, fund_5d_status: 'dated_known', fund_5d_window: window },
    { code: '600902', name: '会话缺失', fund_5d_billion: null, fund_5d_complete: false,
      fund_5d_count: 4, fund_5d_status: 'incomplete', fund_5d_window: window },
    { code: '600903', name: '会话负值', fund_5d_billion: -1.2, fund_5d_complete: true,
      fund_5d_count: 5, fund_5d_status: 'dated_known', fund_5d_window: window },
  ])
  await page.getByRole('tab', { name: '强势排行', exact: true }).click()
  for (const mode of ['短线强势', '十倍潜力']) {
    await page.getByText(mode, { exact: true }).filter({ visible: true }).click()
    const windows = page.locator('.rank-table .rank-fund-window')
    await expect(windows.nth(0)).toHaveText('近5会话 0.00亿')
    await expect(windows.nth(1)).toHaveText('近5会话 未知')
    await expect(windows.nth(2)).toHaveText('近5会话 -1.20亿')
    await expect(windows.nth(0)).toHaveAttribute('title', /2026-09-24 至 2026-10-12/)
    await expect(windows.nth(1)).toHaveAttribute('title', /有效 4\/5/)
    await expect(windows.nth(0)).toHaveAttribute('title', /不是历史PIT还原/)
    await page.locator('.rank-tab').getByRole('button', { name: '导出', exact: true }).click()
    const downloadPromise = page.waitForEvent('download')
    await page.getByText('导出 CSV', { exact: true }).filter({ visible: true }).click()
    const download = await downloadPromise
    const csv = require('node:fs').readFileSync(await download.path(), 'utf8')
    expect(csv).toContain('近5个确认收盘会话净额')
    expect(csv).toContain('0.00亿')
    expect(csv).toContain('未知')
    expect(csv).toContain('dated_latest_not_pit')
    expect(csv).toContain('2026-09-24')
  }
  await page.screenshot({ path: testInfo.outputPath('main-fund-window-20260914-isolated.png') })
  expect(state.errors).toEqual([])
  expect(state.writes).toEqual([])
})

test('rank funds disclose snapshot time and unknown across both ranking modes', async ({ page }) => {
  const state = await isolatedPage(page, [], [
    { code: '600901', name: '快照零值', main_net_inflow_billion: 0,
      main_fund_status: 'snapshot_known', main_fund_purpose: 'ranking_snapshot',
      main_fund_source_quote_at: '2026-09-14T10:00:00' },
    { code: '600902', name: '快照未知', main_net_inflow_billion: null, main_fund_status: 'unknown' },
  ])
  await page.getByRole('tab', { name: '强势排行', exact: true }).click()
  for (const label of ['短线强势', '十倍潜力']) {
    await page.getByText(label, { exact: true }).filter({ visible: true }).click()
    const cells = page.locator('.rank-table .rank-fund-context')
    await expect(cells.nth(0)).toHaveText('排行快照 · 2026-09-14 10:00:00')
    await expect(cells.nth(0)).toHaveAttribute('title', /不是当前交易资金确认/)
    await expect(cells.nth(1)).toHaveText('资金未知')
    await expect(page.locator('.rank-table')).toContainText('资金 0.00亿')
    await expect(page.locator('.rank-table')).toContainText('资金 --')
  }
  expect(state.errors).toEqual([])
  expect(state.writes).toEqual([])
})

for (const [status, amount, label, display] of [
  ['ok', 0, '本次查询合格', '0.00亿'],
  ['stale', null, '资金过期', '--'],
  ['future', null, '资金时钟异常', '--'],
  ['unknown', null, '资金未知', '--'],
]) {
  test('stock detail preserves current fund status: ' + status, async ({ page }) => {
    const errors = [], writes = []
    page.on('pageerror', error => errors.push(error.message))
    await page.routeWebSocket('**/*', ws => ws.close())
    await page.route('**/api/v1/**', async route => {
      if (route.request().method() !== 'GET') writes.push(route.request().method())
      const path = new URL(route.request().url()).pathname
      const json = path.endsWith('/stocks/spot/600901') ? {
        code: '600901', name: '隔离资金测试', price: 10, prev_close: 10,
        main_fund_status: status, main_fund_reason: status === 'unknown' ? 'missing' : status,
        main_fund_available: status === 'ok', main_net_inflow: amount,
      } : {}
      await route.fulfill({ status: 200, json })
    })
    await page.goto(WEB_URL + '/stocks/600901', { waitUntil: 'domcontentloaded' })
    await expect(page.locator('.spot-fund-value')).toHaveText(display)
    await expect(page.locator('.spot-fund-status')).toContainText(label)
    await expect(page.locator('.spot-fund-value')).toHaveAttribute('title', /不替代交易时重新校验/)
    expect(errors).toEqual([])
    expect(writes).toEqual([])
  })
}

test('narrow viewport keeps funds usable through the existing scrollable table', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 900 })
  const state = await isolatedPage(page, [row(1, fresh)])
  await cells(page).first().scrollIntoViewIfNeeded()
  // 使用现有表格横向滚动，避开固定股票列；仅 boundingBox 可见不足以验证未被遮挡。
  await cells(page).first().evaluate(el => {
    const table = el.closest('.el-table')
    const wrap = table.querySelector('.el-table__body-wrapper .el-scrollbar__wrap')
    const pinned = table.querySelectorAll('.el-table__body-wrapper td.el-table-fixed-column--left')
    const right = Math.max(...Array.from(pinned, td => td.getBoundingClientRect().right))
    wrap.scrollLeft += el.getBoundingClientRect().left - right - 10
  })
  await expect(cells(page)).toContainText('腾讯主力资金')
  await expect(cells(page).locator('.capital-source-line')).toBeInViewport()
  await expect.poll(() => cells(page).locator('.capital-source-line').evaluate(el => {
    const rect = el.getBoundingClientRect()
    const top = document.elementFromPoint(rect.left + 5, rect.top + rect.height / 2)
    return top === el || el.contains(top)
  })).toBe(true)
  await page.screenshot({ path: 'test-results/tenbagger-fund-display-mobile-isolated.png' })
  expect(state.errors).toEqual([])
  expect(state.writes).toEqual([])
})
