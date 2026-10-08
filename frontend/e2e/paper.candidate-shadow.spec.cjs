const { test, expect } = require('@playwright/test')
const path = require('node:path')
const WEB_URL = process.env.CLAW_WEB_URL || 'http://127.0.0.1:5173'
const SCREEN_DIR = process.env.CLAW_SHADOW_EVIDENCE_DIR || path.resolve('../outputs/strategy_shadow_live_20260924/frontend')

async function setup(page, response) {
  const requests = []
  const browserErrors = []
  page.on('pageerror', error => browserErrors.push(error.message))
  page.on('close', () => expect(browserErrors).toEqual([]))
  // Every application API is isolated; no running database is queried.
  await page.route('**/api/v1/**', async route => {
    const url = new URL(route.request().url())
    expect(route.request().method()).toBe('GET')
    if (url.pathname.endsWith('/paper/research/candidate-shadow')) {
      const strategy = url.searchParams.get('route')
      requests.push(strategy)
      expect(url.searchParams.get('limit')).toBe('200')
      await route.fulfill({ json: await response(strategy, url) })
    } else {
      await route.fulfill({ json: {} })
    }
  })
  await page.goto(WEB_URL + '/paper')
  return requests
}
const empty = route => ({
  trade_date: '2026-09-24', status: { enabled: true, running: false },
  rows: [], coverage: { status: 'not_open', reason: '未开盘，无自然前向证据' },
  warnings: ['覆盖不全'], truncated: false,
})

test('manual-only family research stays inside comparison, throttles and preserves missing state', async ({ page }) => {
  const requests = await setup(page, empty)
  await expect(page.getByTestId('candidate-shadow-panel')).toHaveCount(0)
  expect(requests).toEqual([])
  await page.getByRole('tab', { name: '策略对比', exact: true }).click()
  const panel = page.getByTestId('candidate-shadow-panel')
  await expect(panel).toContainText('尚未接收研究报告')
  expect(requests).toEqual([])
  await panel.getByPlaceholder('请选择研究交易日').fill('2026-09-24')
  await panel.getByPlaceholder('请选择研究交易日').press('Enter')
  await panel.getByRole('button', { name: '手动刷新实验' }).click()
  await expect(panel).toContainText('未开盘，无自然前向证据')
  expect(requests).toEqual(['A', 'A2'])
  await panel.getByPlaceholder('请选择研究交易日').fill('2026-09-24')
  await panel.getByPlaceholder('请选择研究交易日').press('Enter')
  await panel.getByRole('button', { name: '手动刷新实验' }).click()
  await expect(panel).toContainText('刷新限频')
  expect(requests).toEqual(['A', 'A2'])
  await page.getByRole('tab', { name: '当前持仓', exact: true }).click()
  await expect(panel).toHaveCount(0)
  await page.locator('.el-radio-button').filter({ hasText: 'C·主线扩散' }).click()
  await page.getByRole('tab', { name: '策略对比', exact: true }).click()
  await expect(panel).toContainText('C + C2 + C3')
  await panel.getByPlaceholder('请选择研究交易日').fill('2026-09-24')
  await panel.getByPlaceholder('请选择研究交易日').press('Enter')
  await panel.getByRole('button', { name: '手动刷新实验' }).click()
  await expect(panel.locator('.shadow-route')).toHaveCount(3)
  expect(requests).toEqual(['A', 'A2', 'C', 'C2', 'C3'])
  expect(await panel.innerText()).not.toContain('0.00%')
})

test('malformed and cross-route data cannot masquerade as a zero result', async ({ page }) => {
  await setup(page, route => route === 'A' ? { rows: null } : {
    ...empty(route), rows: [null, { route: 'C3', code: 'SHOULD_NOT_SHOW' }],
  })
  await page.getByRole('tab', { name: '策略对比', exact: true }).click()
  const panel = page.getByTestId('candidate-shadow-panel')
  await panel.getByPlaceholder('请选择研究交易日').fill('2026-09-24')
  await panel.getByPlaceholder('请选择研究交易日').press('Enter')
  await panel.getByRole('button', { name: '手动刷新实验' }).click()
  await expect(panel).toContainText('研究响应结构异常')
  await expect(panel).toContainText('当前覆盖不完整')
  await expect(panel).not.toContainText('SHOULD_NOT_SHOW')
})

test('leaving comparison cancels stale research and has no automatic retry', async ({ page }) => {
  let release
  const gate = new Promise(resolve => { release = resolve })
  const requests = await setup(page, async route => {
    await gate
    return { ...empty(route), rows: [{ route, code: 'STALE_RESULT' }] }
  })
  await page.getByRole('tab', { name: '策略对比', exact: true }).click()
  const panel = page.getByTestId('candidate-shadow-panel')
  await panel.getByPlaceholder('请选择研究交易日').fill('2026-09-24')
  await panel.getByPlaceholder('请选择研究交易日').press('Enter')
  await panel.getByRole('button', { name: '手动刷新实验' }).click()
  await expect.poll(() => requests.length).toBe(2)
  await page.getByRole('tab', { name: '当前持仓', exact: true }).click()
  release()
  await page.getByRole('tab', { name: '策略对比', exact: true }).click()
  await expect(panel).toContainText('尚未接收研究报告')
  await expect(panel).not.toContainText('STALE_RESULT')
  expect(requests.length).toBe(2)
})

test('empty candidates retain source-blocked receipts without raw records', async ({ page }) => {
  await setup(page, route => ({
    ...empty(route), raw_evidence_omitted: true,
    coverage: { status: 'unknown', recent_receipts: [{
      route, stage: 'source_blocked', reason: '竞价早中段来源未提供',
      scan_id: 'receipt-20260924', predicate_asof: '2026-09-24T09:25:01',
      counts: { eligible: null, scanned: 0 },
    }] },
  }))
  await page.locator('.el-radio-button').filter({ hasText: 'D·竞价强攻' }).click()
  await page.getByRole('tab', { name: '策略对比', exact: true }).click()
  const panel = page.getByTestId('candidate-shadow-panel')
  await panel.getByPlaceholder('请选择研究交易日').fill('2026-09-24')
  await panel.getByPlaceholder('请选择研究交易日').press('Enter')
  await panel.getByRole('button', { name: '手动刷新实验' }).click()
  await expect(panel.getByText('竞价早中段来源未提供', { exact: true }).first()).toBeVisible()
  await expect(panel).toContainText('eligible：未知')
  await expect(panel).toContainText('receipt-20260924')
  await expect(panel).toContainText('不是当前路线完整覆盖')
  expect(await panel.innerText()).not.toContain('0.00%')
})

test('full bounded report shows independent identities clocks anchors and reference changes', async ({ page }) => {
  const fixture = require('./fixtures/candidate-shadow.json')
  await setup(page, route => route === 'A' ? fixture : empty(route))
  await page.getByRole('tab', { name: '策略对比', exact: true }).click()
  const panel = page.getByTestId('candidate-shadow-panel')
  await panel.getByPlaceholder('请选择研究交易日').fill('2026-09-24')
  await panel.getByPlaceholder('请选择研究交易日').press('Enter')
  await panel.getByRole('button', { name: '手动刷新实验' }).click()
  await expect(panel).toContainText('原确认已观察（不代表成交）')
  await expect(panel).toContainText('实验条件满足（不下单）')
  await expect(panel).toContainText('提前观察（非买点）')
  await expect(panel).toContainText('实验过滤 / 条件未满足')
  await expect(panel).toContainText('2.500 秒')
  await expect(panel).toContainText('源报价：2026-09-24T09:30:01')
  await expect(panel).toContainText('原谓词观察：2026-09-24T09:30:03')
  await expect(panel).toContainText('研究记录生成：2026-09-24T09:30:05.500')
  await expect(panel).toContainText('记录生成减观察，非持久化完成、发送或成交延迟')
  await expect(panel).not.toContainText('研究落地')
  await expect(panel).toContainText('结果已截断')
  await expect(panel).toContainText('budget_exceeded')
  await expect(panel).toContainText('复用首状态锚点；涨跌不以本帧参考价重算')
  await expect(panel.locator('.shadow-up').first()).toHaveText('+2.50%')
  await expect(panel.locator('.shadow-down').first()).toHaveText('-1.20%')
  await expect(panel).toContainText('0.00%')
  await expect(panel).toContainText('未到期 / 未接收有效标签')
  await panel.screenshot({ path: path.join(SCREEN_DIR, 'desktop-full.png') })
  await panel.locator('.shadow-route').first().locator('.el-table .el-scrollbar__wrap').evaluateAll(elements => elements.forEach(element => { element.scrollLeft = element.scrollWidth }))
  await panel.screenshot({ path: path.join(SCREEN_DIR, 'desktop-labels.png') })
  await page.setViewportSize({ width: 390, height: 844 })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true)
  await panel.screenshot({ path: path.join(SCREEN_DIR, 'mobile-full.png') })
})

test('mobile empty-state is contained and date is required before any request', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  const requests = await setup(page, empty)
  await page.getByRole('tab', { name: '策略对比', exact: true }).click()
  const panel = page.getByTestId('candidate-shadow-panel')
  await panel.getByRole('button', { name: '手动刷新实验' }).click()
  await expect(panel).toContainText('请先选择研究交易日')
  expect(requests).toEqual([])
  await panel.getByPlaceholder('请选择研究交易日').fill('2026-09-24')
  await panel.getByPlaceholder('请选择研究交易日').press('Enter')
  await panel.getByRole('button', { name: '手动刷新实验' }).click()
  await expect(panel.locator('.shadow-route')).toHaveCount(2)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true)
  await panel.screenshot({ path: path.join(SCREEN_DIR, 'mobile-empty.png') })
})

test('v2 preserves v1, old missing state and expiry without trading permission', async ({ page }) => {
  await setup(page, route => ({ ...empty(route), rows: route === 'C2' ? [
    { route, code: '600001', name: '有效期研究样本', baseline: { value: true, status: 'observed' },
      candidate: { value: false, status: 'control', reason: 'flat_vwap_v1' },
      candidate_v2: { value: true, status: 'observed', reason: 'plateau_progress_research' },
      confirmation_freshness: { value: true, status: 'observed', reason: 'within_original_confirmation_window' } },
    { route, code: '600002', name: '已过期研究样本', baseline: { value: true, status: 'observed' },
      candidate: { value: true, status: 'observed' },
      candidate_v2: { value: false, status: 'control', reason: 'original_confirmation_expired' },
      confirmation_freshness: { value: false, status: 'control', reason: 'original_confirmation_expired' } },
    { route, code: '600003', name: '旧记录不补造' },
  ] : [] }))
  await page.locator('.el-radio-button').filter({ hasText: 'C·主线扩散' }).click()
  await page.getByRole('tab', { name: '策略对比', exact: true }).click()
  const panel = page.getByTestId('candidate-shadow-panel')
  await panel.getByPlaceholder('请选择研究交易日').fill('2026-09-24')
  await panel.getByPlaceholder('请选择研究交易日').press('Enter')
  await panel.getByRole('button', { name: '手动刷新实验' }).click()
  await expect(panel).toContainText('时效按该记录当时计算，刷新不重新判断')
  await expect(panel).toContainText('实验v1（保留对照）')
  await expect(panel).toContainText('v2研究条件满足（不下单）')
  await expect(panel).toContainText('原确认时效通过（非交易许可）')
  await expect(panel).toContainText('原确认时效未通过')
  await expect(panel).toContainText('v2条件未满足 / 已失效')
  await expect(panel).toContainText('unknown（证据不足 / 状态未确认）')
  await expect(panel).not.toContainText('现在买入')
  await panel.screenshot({ path: path.join(SCREEN_DIR, 'v2-expiry-unknown.png') })
})

