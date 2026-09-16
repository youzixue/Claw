const { test, expect } = require('@playwright/test')

const WEB_URL = process.env.CLAW_WEB_URL || 'http://127.0.0.1:5173'
const table = page => page.locator('.anomaly-table')
const filter = (page, value) => page.locator('.anomaly-filter-group').first().getByText(value, { exact: true })
const result = name => ({
  rows: [{
    key: '600901', code: '600901', name, event_types: ['breakthrough'],
    event_badges: [], events: [], detail: {}, driver_detail_lines: [],
    reference_driver_lines: [], primary_reason: '隔离请求测试',
  }],
  total: 120, snapshot_time: name, stock_summary: { total: 120 },
})

// 所有业务 API 和 WS 均截获，不连接真实后端，不执行 replay/push/DB 写入。
async function isolated(page) {
  const state = { requests: [], sockets: [], writes: [], errors: [] }
  page.on('pageerror', error => state.errors.push(error.message))
  await page.addInitScript(() => {
    const nativeInterval = window.setInterval.bind(window)
    const nativeClear = window.clearInterval.bind(window)
    const intervals = new Map()
    window.setInterval = (callback, delay, ...args) => {
      const id = nativeInterval(callback, delay, ...args)
      if (delay === 30000) intervals.set(id, () => callback(...args))
      return id
    }
    window.clearInterval = id => { intervals.delete(id); nativeClear(id) }
    window.__tickRadarIntervals = () => [...intervals.values()].forEach(callback => callback())
  })
  await page.routeWebSocket(/\/api\/v1\/ws(?:\?|$)/, socket => {
    state.sockets.push(socket)
    socket.onMessage(() => {}) // 心跳也留在隔离环境
  })
  await page.route('**/api/v1/**', async route => {
    if (route.request().method() !== 'GET') {
      state.writes.push(route.request().url())
      await route.abort()
      return
    }
    const url = new URL(route.request().url())
    if (url.pathname.endsWith('/tenbagger/anomalies')) {
      state.requests.push({ route, url })
      return
    }
    await route.fulfill({ json: {} })
  })
  await page.goto(WEB_URL + '/tenbagger', { waitUntil: 'domcontentloaded' })
  await expect.poll(() => state.requests.length).toBe(1)
  await expect(table(page)).toBeVisible()
  return state
}
async function reply(state, index, name, status = 200) {
  await state.requests[index].route.fulfill({ status, json: status === 200 ? result(name) : { detail: '隔离失败' } })
}
async function count(state, value) {
  await expect.poll(() => state.requests.length).toBe(value)
}
async function settled(page, state, expectedCount) {
  await page.waitForTimeout(400) // 跨过 Vue flush 与 WS 250ms 合并窗口
  expect(state.requests).toHaveLength(expectedCount)
  expect(state.writes).toEqual([])
  expect(state.errors).toEqual([])
}
async function rememberState(page) {
  // 仅测试中保留已卸载实例状态，验证不是仅仅 DOM 被移除了。
  await page.evaluate(() => {
    let instance = document.querySelector('.tenbagger-page').__vueParentComponent
    while (instance && !instance.setupState.loadAnomalies) instance = instance.parent
    if (!instance) throw new Error('radar instance unavailable')
    window.__radarState = instance.setupState
  })
}

test('post-close metadata separates display freshness from signal TTL and preserves zero counts', async ({ page }) => {
  const state = await isolated(page)
  await state.requests[0].route.fulfill({ json: {
    ...result('盘后样本'), snapshot_time: '2026-09-11T15:05:00',
    snapshot_trade_date: '2026-09-11', snapshot_age_seconds: 120,
    snapshot_stale: true, snapshot_cache_policy: 'post_close_read_only',
    stock_summary: { total: 0, limit_up_count: 0, capital_count: 0,
      rapid_rise_count: 0, breakthrough_count: 0, buy_point_count: 0 },
    summary: { total: 99, limit_up_count: 9, capital_count: 9, rapid_rise_count: 9, breakthrough_count: 9 },
    b1_summary: { pushable: 9 },
  } })
  const note = page.locator('.anomaly-snapshot-note')
  await expect(note).toContainText('盘后快照 · 2026-09-11 15:05:00')
  await expect(note).toContainText('交易日 2026-09-11')
  await expect(note).toContainText('响应时快照年龄 120 秒')
  await expect(note).toContainText('快照已超过实时刷新窗口')
  await expect(note).toContainText('资金展示与买点时效分别校验；展示缓存不代表信号有效期')
  await rememberState(page)
  expect(await page.evaluate(() => ({ ...window.__radarState.marketTemp }))).toEqual({
    anomaly_total: 0, limit_up: 0, capital_anomaly: 0, rapid_rise: 0, breakthrough: 0, buy_point: 0,
  })
  await expect.poll(() => state.sockets.length).toBeGreaterThan(0)
  await page.evaluate(() => window.__tickRadarIntervals())
  await count(state, 2)
  await expect(note).toContainText('刷新中')
  await state.requests[1].route.fulfill({ json: {
    ...result('实时样本'), snapshot_time: '2026-09-11T15:08:00',
    snapshot_age_seconds: 0, snapshot_stale: false, snapshot_cache_policy: 'live',
    stock_summary: {}, summary: { total: 8 }, b1_summary: { pushable: 2 },
  } })
  await expect(note).toContainText('异动快照 · 2026-09-11 15:08:00')
  await expect(note).not.toContainText('盘后快照')
  await expect(note).not.toContainText('刷新中')
  await expect(note).not.toContainText('超过实时刷新窗口')
  expect(await page.evaluate(() => window.__radarState.marketTemp.buy_point)).toBe(2)
  expect(await page.evaluate(() => window.__radarState.marketTemp.anomaly_total)).toBe(8)
  await settled(page, state, 2)
})

test('page 2 changing filter sends exactly one page 1 request', async ({ page }) => {
  const state = await isolated(page)
  await reply(state, 0, '初始')
  await expect(table(page)).toContainText('初始')
  await page.locator('.el-pagination .number').filter({ hasText: /^2$/ }).click()
  await count(state, 2)
  expect(state.requests[1].url.searchParams.get('page')).toBe('2')
  await reply(state, 1, '第二页')
  await filter(page, '涨停').click()
  await count(state, 3)
  expect(state.requests[2].url.searchParams.get('page')).toBe('1')
  expect(state.requests[2].url.searchParams.get('event_type')).toBe('limit_up')
  await reply(state, 2, '涨停结果')
  await expect(table(page)).toContainText('涨停结果')
  await settled(page, state, 3)
})

test('first request pending accepts last filter and ignores out-of-order older results', async ({ page }) => {
  const state = await isolated(page)
  await expect(page.getByRole('status')).toContainText('正在加载')
  await filter(page, '涨停').click()
  await count(state, 2)
  await filter(page, '资金异动').click()
  await count(state, 3)
  await reply(state, 2, '最新资金')
  await expect(table(page)).toContainText('最新资金')
  await reply(state, 1, '过期涨停')
  await reply(state, 0, '过期初始')
  await expect(table(page)).toContainText('最新资金')
  await expect(table(page)).not.toContainText('过期')
  await settled(page, state, 3)
})

test('synchronous filter style buy-point and sort updates use one server query', async ({ page }) => {
  const state = await isolated(page)
  await reply(state, 0, '初始')
  await expect(table(page)).toContainText('初始')
  await rememberState(page)
  await page.evaluate(() => {
    const radar = window.__radarState
    radar.activeAnomalyFilter = 'capital'
    radar.activeSetupTrackFilter = '趋势/资金型'
    radar.activeBuyPointOnly = true
    radar.activeSortKey = 'net_inflow'
  })
  await count(state, 2)
  const params = state.requests[1].url.searchParams
  expect(params.get('event_type')).toBe('capital')
  expect(params.get('setup_track')).toBe('趋势/资金型')
  expect(params.get('buy_point_only')).toBe('true')
  expect(params.get('sort_by')).toBe('net_inflow')
  expect(params.get('page_size')).toBe('50')
  await reply(state, 1, '联合筛选')
  await expect(table(page)).toContainText('联合筛选')
  await settled(page, state, 2)
})

test('returning to same inflight parameters reuses that request', async ({ page }) => {
  const state = await isolated(page)
  await filter(page, '涨停').click()
  await count(state, 2)
  await filter(page, '全部').click()
  await settled(page, state, 2)
  await reply(state, 0, '最终全部')
  await reply(state, 1, '旧涨停')
  await expect(table(page)).toContainText('最终全部')
  await expect(table(page)).not.toContainText('旧涨停')
})

test('poll and WS inflight notifications merge to one trailing refresh and retain new data', async ({ page }) => {
  const state = await isolated(page)
  await reply(state, 0, '首轮')
  await expect.poll(() => state.sockets.length).toBeGreaterThan(0)
  await page.evaluate(() => window.__tickRadarIntervals())
  await count(state, 2)
  for (const socket of state.sockets) socket.send(JSON.stringify({ channel: 'anomaly', data: { summary: { total: 888 } } }))
  await page.waitForTimeout(300)
  await page.evaluate(() => { window.__tickRadarIntervals(); window.__tickRadarIntervals() })
  await settled(page, state, 2)
  await reply(state, 1, '通知前快照')
  await count(state, 3)
  await reply(state, 2, '通知后新快照')
  await expect(table(page)).toContainText('通知后新快照')
  await settled(page, state, 3)
})

test('queued refresh for obsolete filter cannot replace the final user query', async ({ page }) => {
  const state = await isolated(page)
  await reply(state, 0, '初始')
  await expect.poll(() => state.sockets.length).toBeGreaterThan(0)
  await page.evaluate(() => window.__tickRadarIntervals())
  await count(state, 2)
  await page.evaluate(() => window.__tickRadarIntervals())
  await filter(page, '涨停').click()
  await count(state, 3)
  await reply(state, 2, '最新筛选')
  await reply(state, 1, '旧轮询')
  await expect(table(page)).toContainText('最新筛选')
  await settled(page, state, 3)
})

test('initial failure does not mark tab loaded and offers retry', async ({ page }) => {
  const state = await isolated(page)
  await rememberState(page)
  await reply(state, 0, '', 500)
  await expect(page.getByRole('alert').filter({ hasText: '已有数据保留' })).toBeVisible()
  expect(await page.evaluate(() => window.__radarState.tabLoaded.anomalies)).toBe(false)
  await page.getByRole('button', { name: '重试', exact: true }).click()
  await count(state, 2)
  await reply(state, 1, '重试成功')
  await expect(table(page)).toContainText('重试成功')
  expect(await page.evaluate(() => window.__radarState.tabLoaded.anomalies)).toBe(true)
  await expect(page.getByRole('button', { name: '重试', exact: true })).toHaveCount(0)
  await settled(page, state, 2)
})

test('failed refresh preserves existing rows and stops loading', async ({ page }) => {
  const state = await isolated(page)
  await reply(state, 0, '保留旧结果')
  await expect.poll(() => state.sockets.length).toBeGreaterThan(0)
  await page.evaluate(() => window.__tickRadarIntervals())
  await count(state, 2)
  await reply(state, 1, '', 500)
  await expect(table(page)).toContainText('保留旧结果')
  await expect(page.locator('.anomaly-table-wrap')).toHaveAttribute('aria-busy', 'false')
  await expect(page.getByRole('button', { name: '重试', exact: true })).toBeEnabled()
  await settled(page, state, 2)
})

test('unmount before initial response prevents late state writes and delayed polling setup', async ({ page }) => {
  const state = await isolated(page)
  await rememberState(page)
  await page.evaluate(() => document.querySelector('#app').__vue_app__.unmount())
  await reply(state, 0, '不应写入')
  await page.waitForTimeout(400)
  expect(await page.evaluate(() => ({
    rows: window.__radarState.anomalyRows.length,
    loaded: window.__radarState.tabLoaded.anomalies,
    snapshot: window.__radarState.anomalySnapshotTime,
  }))).toEqual({ rows: 0, loaded: false, snapshot: '' })
  await page.evaluate(() => window.__tickRadarIntervals())
  await settled(page, state, 1)
})

test('unmount cancels pending WS refresh and inflight trailing work', async ({ page }) => {
  const state = await isolated(page)
  await reply(state, 0, '卸载前')
  await expect.poll(() => state.sockets.length).toBeGreaterThan(0)
  await rememberState(page)
  await page.evaluate(() => window.__tickRadarIntervals())
  await count(state, 2)
  await page.evaluate(() => window.__tickRadarIntervals())
  for (const socket of state.sockets) socket.send(JSON.stringify({ channel: 'anomaly', data: {} }))
  await page.evaluate(() => document.querySelector('#app').__vue_app__.unmount())
  await reply(state, 1, '不应覆盖')
  await settled(page, state, 2)
  expect(await page.evaluate(() => window.__radarState.anomalySnapshotTime)).toBe('卸载前')
})
