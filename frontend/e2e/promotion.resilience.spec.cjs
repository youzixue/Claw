const { test, expect } = require('@playwright/test')
const WEB_URL = process.env.CLAW_WEB_URL || 'http://127.0.0.1:5173'

function fixture() {
  const lane = { target_label: '首板', actual_count: 43, predicted_count: 12, hit_count: 0, precision: 1, recall: 0, pool_recall: 1 }
  return {
    'board-height': { height: 5, limit_up_count: 43, seal_rate: 100, promotion_rate: 1 },
    ladder: { ladder: [{ consecutive_days: 1, count: 43, seal_rate: 100, stocks: [] }] },
    candidates: {},
    'learning-review': {
      latest: { lane_metrics: { target_1: lane } },
      daily: [{ actual_trade_date: '2026-09-28', predicted_count: 12, limit_up_precision: 1 }],
    },
  }
}

function populatedFixture() {
  const data = fixture()
  const stocks = (count, offset = 0) => Array.from({ length: count }, (_, i) => ({
    code: String(600000 + offset + i), name: `测试样本${i + 1}`,
    tag: i % 5 === 0 ? 'observe_only' : 'tradeable', is_tradeable: i % 5 !== 0,
  }))
  data.ladder = { trade_date: '2026-09-29', status: 'ok', seal_rate_method: 'zero_break_share',
    scope: 'filtered_market_including_observation_boards',
    ladder: [
      { consecutive_days: 1, count: 43, seal_rate: 100, stocks: stocks(43) },
      { consecutive_days: 2, count: 6, seal_rate: 83.3, stocks: stocks(6, 100) },
      { consecutive_days: 3, count: 2, seal_rate: 50, stocks: stocks(2, 200) },
    ] }
  data['board-height'] = { trade_date: '2026-09-29', status: 'ok', height: 3, limit_up_count: 51,
    seal_rate: 92.7, seal_rate_method: 'verified_pool_state', promotion_rate: 0.16,
    previous_trade_date: '2026-09-28', promotion_rate_status: 'ok',
    scope: 'filtered_market_including_observation_boards',
    ladder_summary: [{ days: 1, count: 43 }, { days: 2, count: 6 }, { days: 3, count: 2 }] }
  const metrics = { sample_count: 12, predicted_count: 12, directional_evaluable_count: 10,
    directional_unknown_count: 2, directional_coverage: 10 / 12, directional_observed_precision: 0.8,
    directional_precision: null, directional_precision_lower_bound: 8 / 12, directional_precision_upper_bound: 10 / 12,
    directional_target_precision: 0.8, directional_target_met: null,
    evaluation_status: 'partial', evaluation_reasons: ['candidate_outcome_bar_missing'],
    strong_rise_precision: 0.4, limit_up_precision: 0.25, actionable_count: 5 }
  data['learning-review'].latest = { ...metrics, prediction_trade_date: '2026-09-28',
    actual_trade_date: '2026-09-29', snapshot_complete: true,
    lane_metrics: { target_1: { ...data['learning-review'].latest.lane_metrics.target_1, hit_count: 12, snapshot_complete: true } } }
  data['learning-review'].aggregate = { ...metrics, predicted_count: 168, directional_evaluable_count: 140, directional_unknown_count: 28,
    launch_precursor_metrics: { platform: { ...metrics, label: '平台突破证据（隔离测试）' },
      volume: { ...metrics, label: '量能启动证据（隔离测试）', actionable_count: 0 } } }
  return data
}

async function waitForChartSettled(page) {
  await expect(page.locator('.chart-card canvas')).toBeVisible()
  // Canvas animation is not affected by Playwright's CSS-animation screenshot option.
  // Require stable pixels after at least 1.5s; do not capture a partially grown bar.
  await page.waitForFunction(() => {
    const canvas = document.querySelector('.chart-card canvas')
    if (!canvas) return false
    const now = performance.now()
    const state = window.__promotionChartCapture ||= { start: now, changed: now, pixels: '' }
    const pixels = canvas.toDataURL()
    if (pixels !== state.pixels) { state.pixels = pixels; state.changed = now }
    return now - state.start >= 1500 && now - state.changed >= 500
  }, null, { polling: 100, timeout: 10000 })
  await page.evaluate(() => { delete window.__promotionChartCapture })
}

async function intercept(page, payloads, handler) {
  const errors = []
  page.on('pageerror', e => errors.push(e.message))
  const calls = {}
  // Intercept ALL API traffic, including the surrounding shell; never query live business APIs.
  await page.route('**/api/v1/**', async route => {
    const key = new URL(route.request().url()).pathname.split('/').pop()
    calls[key] = (calls[key] || 0) + 1
    if (handler && await handler(route, key, calls[key])) return
    await route.fulfill({ json: payloads[key] ?? {} })
  })
  return { errors, calls }
}

test('resumed high board stays visible with an explicit unresolved trading restriction', async ({ page }) => {
  const data = populatedFixture()
  data.ladder.scope = 'non_st_market_with_risk_annotations'
  data.ladder.ladder.unshift({ consecutive_days: 6, count: 1, seal_rate: 100,
    stocks: [{ code: '600825', name: '新华传媒', is_tradeable: false, tag: '停牌标签待核验 · 禁止交易' }] })
  data['board-height'] = { ...data['board-height'], height: 6, scope: data.ladder.scope,
    leader: { code: '600825', name: '新华传媒', days: 6 } }
  const state = await intercept(page, data)
  await page.goto(WEB_URL + '/promotion', { waitUntil: 'domcontentloaded' })
  await page.getByRole('tab', { name: '市场梯队', exact: true }).click()
  const ladder = page.getByTestId('promotion-ladder')
  await expect(ladder).toContainText('6板')
  await expect(ladder).toContainText('新华传媒(600825)')
  await expect(ladder).toContainText('停牌标签待核验 · 禁止交易')
  await expect(page.getByTestId('ladder-scope')).toContainText('展示不代表允许交易')
  expect(state.errors).toEqual([])
})

test('slow review never delays independent market, ladder or candidates', async ({ page }) => {
  let release
  const gate = new Promise(resolve => { release = resolve })
  const state = await intercept(page, fixture(), async (route, key) => {
    if (key !== 'learning-review') return false
    await gate
    await route.fulfill({ json: fixture()['learning-review'] })
    return true
  })
  try {
    await page.goto(WEB_URL + '/promotion', { waitUntil: 'domcontentloaded' })
    await expect(page.getByTestId('market-limit-up-count')).toContainText('43')
    await expect(page.getByTestId('promotion-ladder')).toContainText('100.0%')
  await page.getByRole('tab', { name: '预测复盘', exact: true }).click()
  await page.getByRole('tab', { name: '晋级候选', exact: true }).click()
    await expect(page.getByTestId('request-candidates')).toContainText('已加载')
    await expect(page.getByTestId('request-review')).toContainText('加载中')
    expect(state.calls['learning-review']).toBe(1)
  } finally { release() }
  await page.getByRole('tab', { name: '预测复盘', exact: true }).click()
  await expect(page.getByTestId('request-review')).toContainText('已加载')
  expect(state.errors).toEqual([])
})

test('failed region retries independently and suppresses repeated in-flight clicks', async ({ page }) => {
  let release
  const gate = new Promise(resolve => { release = resolve })
  const state = await intercept(page, fixture(), async (route, key, count) => {
    if (key !== 'learning-review') return false
    if (count === 1) await route.fulfill({ status: 503, json: { detail: 'isolated fixture unavailable' } })
    else { await gate; await route.fulfill({ json: fixture()['learning-review'] }) }
    return true
  })
  await page.goto(WEB_URL + '/promotion')
  await page.getByRole('tab', { name: '预测复盘', exact: true }).click()
  const status = page.getByTestId('request-review')
  await expect(status).toContainText('加载失败')
  await expect(page.getByTestId('market-limit-up-count')).toContainText('43')
  // Synchronous repeated DOM clicks exercise the guard before Vue updates the button.
  await status.getByRole('button', { name: '重试' }).evaluate(button => { button.click(); button.click(); button.click() })
  try {
    await expect(status).toContainText('加载中')
    expect(state.calls['learning-review']).toBe(2)
    expect(state.calls['board-height']).toBe(1)
  } finally { release() }
  await expect(status).toContainText('已加载')
  expect(state.errors).toEqual([])
})

test('null and omitted counts stay unknown while genuine zero remains zero', async ({ page }) => {
  const data = fixture()
  data.ladder.ladder[0] = { consecutive_days: 1, count: null, seal_rate: null }
  data['learning-review'] = {
    latest: { lane_metrics: { target_1: {
      target_label: '首板', actual_count: null, predicted_count: 0, hit_count: null,
      precision: null, recall_ranked_available: true, recall_hit_count: null, recall_ranked_count: null,
    } } },
    aggregate: { launch_precursor_metrics: { example: { label: '缺失样本', sample_count: null, actionable_count: 0 } } },
    daily: [{ actual_trade_date: '2026-09-28', actual_target_limit_up_count: null, predicted_count: 0 }],
  }
  const state = await intercept(page, data)
  await page.goto(WEB_URL + '/promotion')
  await page.getByRole('tab', { name: '预测复盘', exact: true }).click()
  await expect(page.getByTestId('request-review')).toContainText('已加载')
  const cells = page.getByTestId('review-lanes').locator('.el-table__body-wrapper tbody tr').first().locator('td .cell')
  await expect(cells.nth(1)).toHaveText('--')
  await expect(cells.nth(2)).toHaveText('0')
  await expect(cells.nth(3)).toHaveText('--')
  await expect(page.getByTestId('review-first-top30-hit')).toContainText('--')
  await expect(page.getByTestId('review-lanes')).toContainText('--/--')
  const cohortCells = page.getByTestId('review-directional-cohorts').locator('.el-table__body-wrapper tbody tr td .cell')
  await expect(cohortCells.nth(0)).toHaveText('缺失样本')
  await expect(cohortCells.nth(1)).toHaveText('--')
  await expect(cohortCells.nth(15)).toHaveText('0')
  await page.getByRole('tab', { name: '市场梯队', exact: true }).click()
  const ladder = page.getByTestId('promotion-ladder')
  await expect(ladder.locator('.el-table__body-wrapper tbody tr td .cell').nth(1)).toHaveText('--')
  await expect(ladder).not.toContainText('0.0%')
  await page.getByRole('tab', { name: '预测复盘', exact: true }).click()
  await expect(page.getByTestId('review-directional-daily').locator('.el-table__body-wrapper tbody tr td .cell').nth(1)).toHaveText('--')
  expect(state.errors).toEqual([])
})

test('empty payloads retain empty states and malformed response has a recoverable error', async ({ page }) => {
  const state = await intercept(page, { ladder: { ladder: 'invalid' } })
  await page.goto(WEB_URL + '/promotion')
  await expect(page.getByTestId('request-ladder')).toContainText('加载失败')
  await page.getByRole('tab', { name: '预测复盘', exact: true }).click()
  await expect(page.getByTestId('request-review')).toContainText('已加载')
  await expect(page.getByTestId('review-first-actual')).toContainText('--')
  await expect(page.getByTestId('review-directional-daily')).toContainText('暂无逐日复盘')
  await page.getByRole('tab', { name: '晋级候选', exact: true }).click()
  await expect(page.getByTestId('first-board-top12-table')).toContainText('暂无首板观察标的')
  expect(state.errors).toEqual([])
})

for (const status of ['source_incomplete', 'missing']) test(`source ${status} is unknown, previous trade date is not substituted`, async ({ page }) => {
  const data = fixture()
  data.ladder = { trade_date: '2026-09-29', status, ladder: [], seal_rate_method: 'zero_break_share' }
  data['board-height'] = { trade_date: '2026-09-29', height: 5, limit_up_count: 43,
    promotion_rate: null, promotion_rate_status: 'previous_pool_missing_or_incomplete', previous_trade_date: '2026-09-28',
    seal_rate: null, seal_rate_method: 'unknown' }
  const state = await intercept(page, data)
  await page.goto(WEB_URL + '/promotion')
  await expect(page.getByTestId('ladder-scope')).toContainText('2026-09-29')
  await expect(page.getByTestId('ladder-source-health')).toContainText('不是零涨停')
  await expect(page.getByTestId('promotion-ladder')).toContainText('梯队未知')
  await expect(page.getByTestId('promotion-ladder')).not.toContainText('暂无数据')
  await expect(page.getByTestId('market-promotion-rate').locator('.stat-value')).toHaveText('--')
  await expect(page.getByTestId('promotion-rate-status')).toContainText('前交易日统计池缺失或不完整')
  await expect(page.getByTestId('promotion-rate-status')).toContainText('2026-09-28')
  await expect(page.getByTestId('market-seal-rate')).toContainText('--')
  await expect(page.getByTestId('market-scope-note')).toContainText('非全市场')
  expect(state.errors).toEqual([])
})

test('ladder shows all stock tags and distinguishes zero-break from verified seal rate', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 900 })
  const data = fixture()
  data.ladder.trade_date = '2026-09-29'
  data.ladder.status = 'ok'
  data.ladder.seal_rate_method = 'zero_break_share'
  data.ladder.ladder[0].count = 12
  data.ladder.source_health = { ready: true }
  data.ladder.ladder[0].stocks = Array.from({ length: 12 }, (_, i) => ({ code: String(600000 + i), name: '测试股票' }))
  data.ladder.scope = 'filtered_market_including_observation_boards'
  data.ladder.ladder[0].stocks[0] = { code: '301190', name: '测试观察股', tag: 'observe_only', is_tradeable: false }
  data.ladder.ladder[0].stocks[1].is_tradeable = false
  data['board-height'].seal_rate_method = 'verified_pool_state'
  const state = await intercept(page, data)
  await page.goto(WEB_URL + '/promotion')
  await page.getByRole('tab', { name: '市场梯队', exact: true }).click()
  const ladder = page.getByTestId('promotion-ladder')
  await expect(ladder).toContainText('零开板率')
  await expect(ladder).toContainText('共 12 只 · 返回 12 只')
  await expect(ladder.locator('.stock-tag')).toHaveCount(12)
  await expect(ladder.locator('.stock-tag').first()).toContainText('仅观察')
  await expect(ladder.locator('.stock-tag').nth(1)).toContainText('仅观察')
  await expect(page.getByTestId('ladder-scope')).toContainText('含观察板块')
  await expect(page.getByTestId('ladder-scope')).not.toContainText('filtered_market')
  await expect(page.getByTestId('market-seal-rate')).toContainText('源验证封板率')
  expect(await ladder.locator('.ladder-stock-tags').evaluate(el => getComputedStyle(el).flexWrap)).toBe('wrap')
  expect(await ladder.locator('.stock-tag').first().evaluate(el => getComputedStyle(el).whiteSpace)).toBe('nowrap')
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true)
  expect(state.errors).toEqual([])
})

for (const [height, count, summary, expected] of [
  [0, 0, [], '首板 0 + 二板 0 + 三板及以上 0 = 0'],
  [1, 3, [{ days: 1, count: 3 }], '首板 3 + 二板 0 + 三板及以上 0 = 3'],
  [1, 3, undefined, '首板 -- + 二板 -- + 三板及以上 -- = 3'],
]) test(`complete zero versus absent ladder summary: ${expected}`, async ({ page }) => {
  const data = fixture()
  data['board-height'] = { trade_date: '2026-09-29', status: 'ok', height,
    limit_up_count: count, ladder_summary: summary, scope: 'filtered_market_including_observation_boards',
    promotion_rate: null, promotion_rate_status: 'future_unrecognized_status' }
  const state = await intercept(page, data)
  await page.goto(WEB_URL + '/promotion')
  await expect(page.getByTestId('market-scope-note')).toContainText(expected)
  await expect(page.getByTestId('market-scope-note')).not.toContainText('filtered_market')
  await expect(page.getByTestId('promotion-rate-status')).toContainText('口径状态未知')
  await expect(page.getByTestId('promotion-rate-status')).not.toContainText('future_unrecognized')
  expect(state.errors).toEqual([])
})

test('latest missing snapshot is not confused with the aggregate denominator', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 1100 })
  const data = fixture()
  data['learning-review'] = {
    lookback_days: 10,
    latest: { prediction_trade_date: '2026-09-28', actual_trade_date: '2026-09-29',
      predicted_count: 0, snapshot_complete: false,
      evaluation_reasons: ['formal_ranked_predictions_unavailable'],
      lane_metrics: { target_1: { target_label: '首板', predicted_count: 0, snapshot_complete: false } } },
    aggregate: { predicted_count: 168, directional_evaluable_count: 168 },
  }
  const state = await intercept(page, data)
  await page.goto(WEB_URL + '/promotion')
  await page.getByRole('tab', { name: '预测复盘', exact: true }).click()
  await expect(page.getByTestId('review-latest-scope')).toContainText('累计原预测 168 条')
  await expect(page.getByTestId('review-latest-scope')).toContainText('不代表确认预测了零只')
  await expect(page.getByTestId('review-first-predicted')).toContainText('暂无记录')
  await expect(page.getByTestId('review-first-predicted')).toContainText('原始记录数 0')
  await expect(page.getByTestId('review-lanes')).toContainText('暂无记录')
  await expect(page.getByTestId('review-directional-aggregate')).toContainText('168 / 168')
  await page.getByTestId('review-directional-latest').scrollIntoViewIfNeeded()
  await page.screenshot({ path: testInfo.outputPath('review-scope-1440.png') })
  expect(state.errors).toEqual([])
})

for (const width of [1440, 1024, 390]) {
  test(`width ${width}: numbers and headers do not wrap, wide tables scroll locally`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 })
    const state = await intercept(page, populatedFixture())
    await page.goto(WEB_URL + '/promotion')
    await expect(page.getByTestId('promotion-ladder')).toContainText('100.0%')
      await page.getByRole('tab', { name: '预测复盘', exact: true }).click()
    await expect(page.getByTestId('review-lanes')).toContainText('100.0%')
    for (const id of ['promotion-ladder', 'review-lanes']) {
      await page.getByRole('tab', { name: id === 'promotion-ladder' ? '市场梯队' : '预测复盘', exact: true }).click()
      const table = page.getByTestId(id)
      const header = table.locator('th .cell').filter({ hasText: id === 'review-lanes' ? /^实际$/ : /^个数$/ })
      expect(await header.evaluate(el => getComputedStyle(el).whiteSpace)).toBe('nowrap')
      expect(await header.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true)
      const number = table.locator('.el-table__body-wrapper td .cell').filter({ hasText: /^100.0%$/ }).first()
      expect(await number.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true)
      const count = table.locator('.el-table__body-wrapper td .cell').filter({ hasText: /^43$/ }).first()
      expect(await count.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true)
      expect(await number.evaluate(el => {
        const range = document.createRange()
        range.selectNodeContents(el)
        const rects = Array.from(range.getClientRects()).filter(r => r.width > 0)
        return new Set(rects.map(r => Math.round(r.y))).size
      })).toBe(1)
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true)
    const daily = page.getByTestId('review-directional-daily')
    expect(await daily.locator('.el-scrollbar__wrap').first().evaluate(el => {
      if (el.scrollWidth <= el.clientWidth) return false
      el.scrollLeft = 150
      return el.scrollLeft > 0
    })).toBe(true)
      await page.getByRole('tab', { name: '市场梯队', exact: true }).click()
    await page.locator('.chart-card').scrollIntoViewIfNeeded()
    await waitForChartSettled(page)
    await page.screenshot({ path: testInfo.outputPath(`ladder-${width}.png`) })
    await page.getByTestId('promotion-ladder').screenshot({ path: testInfo.outputPath(`ladder-stocks-${width}.png`) })
      await page.getByRole('tab', { name: '预测复盘', exact: true }).click()
    await page.getByTestId('review-directional-cohorts').screenshot({ path: testInfo.outputPath(`learning-cohorts-${width}.png`) })
    expect(state.errors).toEqual([])
  })
}
