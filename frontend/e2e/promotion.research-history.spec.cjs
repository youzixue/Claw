const { test, expect } = require('@playwright/test')
const WEB = process.env.CLAW_WEB_URL || 'http://127.0.0.1:5173'
// Isolated transport fixtures. No production writes, ranking or model activation.
const blocked = { version: 'direction_rank_research_v1', label_version: 'next_day_close_up_v1',
  scope: 'research_only', production_unchanged: true, manual_review_eligible: false,
  candidate_count: 1428, eligible_count: 970, selected_count: 0, rank_limit: 12,
  missing_probability_count: 0, missing_recordable_count: 1, target_precision: .8,
  status: 'blocked', reason: 'eligible_candidates_not_recordable', candidates: [] }
const saved = { ...blocked, status: 'available', reason: null,
  candidate_count: 12, eligible_count: 12, selected_count: 12, missing_recordable_count: 0,
  frozen: true, persistence_status: 'immutable_ledger',
  candidates: Array.from({ length: 12 }, (_, i) => ({
    code: String(600001 + i), name: '原始冻结样本' + i, direction_probability: .8 - i / 100,
    limit_up_probability: .06,
    probability_factors: { direction_research: { rank_position: i + 1, probability_method: 'frozen_fixture' } },
  })) }
const run = id => ({ id, as_of_at: '2026-09-29T15:10:00', snapshot_context: 'promotion_1510',
  reference_trade_date: '2026-09-29', status: 'completed' })
async function setup(page, handler) {
  const calls = [], errors = []
  page.on('pageerror', e => errors.push(e.message))
  await page.route('**/api/v1/**', async route => {
    const url = new URL(route.request().url())
    calls.push(url.pathname + url.search)
    if (handler && await handler(route, url)) return
    const data = url.pathname.endsWith('/model-lab/runs') ? { runs: [run(794), run(792)] }
      : url.pathname.endsWith('/runs/792') ? { run: run(792), direction_research: saved }
      : url.pathname.endsWith('/runs/794') ? { run: run(794), direction_research: blocked }
      : url.pathname.endsWith('/candidates') ? { direction_research: blocked }
      : url.pathname.endsWith('/board-height') ? { trade_date: '2026-09-29' } : {}
    await route.fulfill({ json: data })
  })
  await page.goto(WEB + '/promotion')
  await page.getByRole('tab', { name: '研究观察', exact: true }).click()
  return { calls, errors }
}
async function options(page) {
  await page.getByRole('button', { name: '查看历史研究批次', exact: true }).click()
  await expect(page.getByLabel('研究批次')).toBeVisible()
}
const table = page => page.getByTestId('direction-research-table')
const linkedPayload = () => ({ direction_research: { ...blocked, missing_recordable_count: undefined },
  prediction_snapshot_source: 'schedule', prediction_snapshot_context: 'promotion_2000',
  prediction_model_version: 'frozen_fixture', first_board_trade_date: '2026-09-29',
  prediction_health: { persistence: { ledger_recorded: true, ledger: { run_id: 794, snapshot_count: 1473 } } } })
const linkedDetail = () => ({ run: { ...run(794), snapshot_context: 'promotion_2000',
  candidate_count: 1473, model_version: 'frozen_fixture' }, direction_research: blocked })

for (const mismatch of ['', 'id', 'date', 'model', 'candidate-count', 'proof-count', 'probability-gap', 'network']) {
  test('current missing diagnostic follows only exact immutable pointer: ' + (mismatch || 'valid'), async ({ page }) => {
    const payload = linkedPayload(), detail = structuredClone(linkedDetail())
    if (mismatch === 'id') detail.run.id = 792
    if (mismatch === 'date') detail.run.reference_trade_date = '2026-09-28'
    if (mismatch === 'model') detail.run.model_version = 'other'
    if (mismatch === 'candidate-count') detail.run.candidate_count = 1472
    if (mismatch === 'proof-count') detail.direction_research.eligible_count = 969
    if (mismatch === 'probability-gap') detail.direction_research.missing_recordable_count = true
    const { calls, errors } = await setup(page, async (route, url) => {
      if (url.pathname.endsWith('/candidates')) { await route.fulfill({ json: payload }); return true }
      if (url.pathname.endsWith('/runs/794')) {
        await route.fulfill(mismatch === 'network' ? { status: 503, json: {} } : { json: detail })
        return true
      }
    })
    const note = page.getByTestId('current-research-evidence')
    await expect(note).toContainText(mismatch ? '保持未知' : '缺口计数已核验（原批次 #794）')
    await expect(page.getByTestId('direction-research-recordability-gap')).toHaveText('无法完整记录 ' + (mismatch ? '--' : '1'))
    await expect(table(page)).toContainText('研究榜阻塞')
    await expect(page.getByTestId('research-history-context')).toHaveCount(0)
    expect(calls.filter(x => x.includes('/runs/792'))).toHaveLength(0)
    expect(calls.filter(x => x.includes('/model-lab/runs?'))).toHaveLength(0)
    expect(errors).toEqual([])
  })
}

test('history is lazy, explicitly selected and never substitutes blocked latest', async ({ page }) => {
  const { calls, errors } = await setup(page)
  expect(calls.filter(x => x.includes('/model-lab/'))).toEqual([])
  await expect(table(page)).toContainText('研究榜阻塞')
  await options(page)
  expect(calls.filter(x => x.includes('/model-lab/')).length).toBe(1)
  expect(calls.find(x => x.includes('/model-lab/'))).toContain('compact=true')
  await expect(page.getByLabel('研究批次')).toHaveValue('')
  await expect(table(page)).not.toContainText('原始冻结样本')
  await page.getByLabel('研究批次').selectOption('792')
  await expect(table(page).locator('tbody tr')).toHaveCount(12)
  await expect(table(page)).toContainText('80.0%')
  await expect(page.getByTestId('research-history-context')).toContainText('不替代当前最新榜')
  await expect(page.getByTestId('direction-research-persistence')).toContainText('不认证训练资格')
  expect(calls.find(x => x.includes('/runs/792'))).toContain('direction_only=true')
  await page.getByLabel('研究批次').selectOption('')
  await expect(table(page)).toContainText('研究榜阻塞')
  await expect(table(page)).not.toContainText('原始冻结样本')
  expect(errors).toEqual([])
})

test('blocked historical batch remains blocked without fallback to valid older rows', async ({ page }) => {
  await setup(page); await options(page)
  await page.getByLabel('研究批次').selectOption('792')
  await expect(table(page).locator('tbody tr')).toHaveCount(12)
  await page.getByLabel('研究批次').selectOption('794')
  await expect(page.getByTestId('direction-research-recordability-gap')).toHaveText('无法完整记录 1')
  await expect(table(page)).toContainText('研究榜阻塞')
  await expect(table(page)).not.toContainText('原始冻结样本')
})

test('late historical response cannot overwrite the current selection', async ({ page }) => {
  let pending
  await setup(page, async (route, url) => {
    if (url.pathname.endsWith('/runs/792')) { pending = route; return true }
  })
  await options(page)
  await page.getByLabel('研究批次').selectOption('792')
  await expect.poll(() => Boolean(pending)).toBe(true)
  await page.getByLabel('研究批次').selectOption('')
  await pending.fulfill({ json: { run: run(792), direction_research: saved } })
  await expect(table(page)).toContainText('研究榜阻塞')
  await expect(page.getByTestId('research-history-context')).toHaveCount(0)
  await expect(table(page)).not.toContainText('原始冻结样本')
})

for (const failure of ['network', 'wrong-identity', 'missing-payload', 'invalid-clock']) {
  test('failed historical read is isolated and retries same batch: ' + failure, async ({ page }) => {
    let attempts = 0
    const { calls, errors } = await setup(page, async (route, url) => {
      if (url.pathname.endsWith('/runs/792') && ++attempts === 1) {
        if (failure === 'network') await route.fulfill({ status: 503, json: { detail: 'unavailable' } })
        else await route.fulfill({ json: failure === 'wrong-identity'
          ? { run: run(794), direction_research: saved } : failure === 'invalid-clock' ? { run: { ...run(792), as_of_at: 123 }, direction_research: saved }
          : { run: run(792) } })
        return true
      }
    })
    await options(page)
    await page.getByLabel('研究批次').selectOption('792')
    await expect(page.getByTestId('research-history-error')).toContainText('不回退其他榜单')
    await expect(table(page)).not.toContainText('原始冻结样本')
    await page.getByRole('button', { name: '重试所选批次', exact: true }).click()
    await expect(table(page).locator('tbody tr')).toHaveCount(12)
    expect(calls.filter(x => x.includes('/runs/792'))).toHaveLength(2)
    expect(calls.filter(x => x.includes('/runs/794'))).toHaveLength(0)
    expect(errors).toEqual([])
  })
}

test('list errors retry without clearing current research evidence', async ({ page }) => {
  let attempts = 0
  await setup(page, async (route, url) => {
    if (url.pathname.endsWith('/model-lab/runs') && ++attempts === 1) {
      await route.fulfill({ json: { runs: null } }); return true
    }
  })
  await page.getByRole('button', { name: '查看历史研究批次', exact: true }).click()
  await expect(page.getByText('历史批次读取失败，可重试；未切换当前榜单')).toBeVisible()
  await expect(table(page)).toContainText('研究榜阻塞')
  await options(page)
  await expect(page.getByLabel('研究批次')).toHaveValue('')
})

test('mobile historical selector and research rows stay within page width', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  const { errors } = await setup(page)
  await options(page)
  await page.getByLabel('研究批次').selectOption('792')
  await expect(table(page).locator('tbody tr')).toHaveCount(12)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true)
  const select = await page.getByLabel('研究批次').boundingBox()
  expect(select.x + select.width).toBeLessThanOrEqual(391)
  await page.getByLabel('研究批次').selectOption('')
  await expect(table(page)).toContainText('研究榜阻塞')
  expect(await table(page).locator('.el-table__empty-text').evaluate(el => parseFloat(getComputedStyle(el).lineHeight))).toBeLessThan(30)
  expect(errors).toEqual([])
})
