const { test, expect } = require('@playwright/test')

const WEB_URL = process.env.CLAW_WEB_URL || 'http://127.0.0.1:5173'

// Fixtures are browser-isolated; no production records or API writes.
async function openReview(page, metrics, empty = false, candidates = {}) {
  const errors = []
  page.on('pageerror', error => errors.push(error.message))
  const row = { actual_trade_date: '2026-09-09', prediction_trade_date: '2026-09-08', ...metrics }
  await page.route('**/api/v1/**', async route => {
    const review = {
      latest: row,
      aggregate: { ...row, launch_precursor_metrics: empty ? {} : { fixture: { label: '测试分组', sample_count: 10, ...metrics } } },
      daily: empty ? [] : [row],
    }
    const url = route.request().url()
    await route.fulfill({ json: url.includes('/learning-review') ? review : url.includes('/candidates') ? candidates : {} })
  })
  await page.goto(`${WEB_URL}/promotion`, { waitUntil: 'networkidle' })
  await page.getByRole('tab', { name: '预测复盘', exact: true }).click()
  await expect(page.getByTestId('review-directional-latest')).toBeVisible()
  return errors
}

test('partial results retain denominator, unknowns and bounds without claiming full precision', async ({ page }) => {
  const errors = await openReview(page, {
    predicted_count: 10, directional_evaluable_count: 8, directional_unknown_count: 2,
    directional_coverage: 0.8, directional_observed_precision: 1,
    directional_precision: null, directional_precision_lower_bound: 0.8,
    directional_precision_upper_bound: 1, directional_target_precision: 0.8,
    directional_target_met: null, evaluation_status: 'partial', evaluation_reasons: ['missing_outcome'],
  })
  for (const key of ['latest', 'aggregate']) {
    const summary = page.getByTestId(`review-directional-${key}`)
    await expect(summary).toContainText('完整上涨实际率 --')
    await expect(summary).toContainText('8 / 10')
    await expect(summary).toContainText('未知 2')
    await expect(summary).toContainText('覆盖率 80.0%')
    await expect(summary).toContainText('已评价子集上涨率 100.0%')
    await expect(summary).toContainText('80.0% ～ 100.0%')
    await expect(summary).toContainText('数据不足 · 达标未知')
    await expect(summary).toContainText('missing_outcome')
  }
  for (const key of ['daily', 'cohorts']) {
    await expect(page.getByTestId(`review-directional-${key}`)).toContainText('8 / 10')
    await expect(page.getByTestId(`review-directional-${key}`)).toContainText('数据不足 · 达标未知')
  }
  expect(errors).toEqual([])
})

test('legacy, null and empty rates remain unavailable on mobile with preserved empty states', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  const errors = await openReview(page, {
    directional_precision: '', limit_up_precision: null, brier_score: null,
    lane_metrics: { target_1: { precision: null, pool_recall: '', recall: null } },
  }, true)
  for (const key of ['latest', 'aggregate']) {
    const summary = page.getByTestId(`review-directional-${key}`)
    await expect(summary).toContainText('完整上涨实际率 --')
    await expect(summary).toContainText('覆盖率 --')
    await expect(summary).toContainText('数据不足 · 达标未知')
    await expect(summary).not.toContainText('0.0%')
    expect(await summary.evaluate(el => el.scrollWidth <= el.clientWidth + 1)).toBe(true)
  }
  await expect(page.getByTestId('review-first-precision')).toContainText('--')
  await expect(page.getByTestId('review-first-pool-recall')).toContainText('--')
  await expect(page.getByTestId('review-directional-daily')).toContainText('暂无逐日复盘')
  await expect(page.getByTestId('review-directional-cohorts')).toContainText('等待新版正式快照积累样本')
  await expect(page.getByTestId('review-directional-contract')).toContainText('未承诺、未认证')
  await page.getByRole('tab', { name: '晋级候选', exact: true }).click()
  await expect(page.getByTestId('first-board-top12-table')).toContainText('暂无首板观察标的')
  await expect(page.getByRole('button', { name: '导出', exact: true })).toBeAttached()
  expect(errors).toEqual([])
})

for (const [precision, met, label] of [[0, false, '本样本未达目标'], [0.8, true, '本样本达到目标（非认证）']]) {
  test(`complete rate ${precision} and boolean target ${met} remain explicit`, async ({ page }) => {
    await openReview(page, {
      predicted_count: 10, directional_evaluable_count: 10, directional_unknown_count: 0,
      directional_coverage: 1, directional_precision: precision, directional_observed_precision: precision,
      directional_precision_lower_bound: precision, directional_precision_upper_bound: precision,
      directional_target_precision: 0.8, directional_target_met: met, evaluation_status: 'complete',
    })
    const summary = page.getByTestId('review-directional-latest')
    await expect(summary).toContainText(`完整上涨实际率 ${(precision * 100).toFixed(1)}%`)
    await expect(summary).toContainText(label)
    await expect(summary).toContainText('未知 0')
  })
}

test('complete directional results remain visible despite missing limit-up outcomes', async ({ page }) => {
  await openReview(page, {
    predicted_count: 10, directional_evaluable_count: 10, directional_unknown_count: 0,
    directional_coverage: 1, directional_precision: 0.8, directional_target_precision: 0.8,
    directional_target_met: true, evaluation_status: 'partial',
    evaluation_reasons: ['outcome_limit_up_pool_missing'],
  })
  for (const key of ['latest', 'aggregate']) {
    const summary = page.getByTestId(`review-directional-${key}`)
    await expect(summary).toContainText('完整上涨实际率 80.0%')
    await expect(summary).toContainText('覆盖率 100.0%')
    await expect(summary).toContainText('部分可评价')
    await expect(summary).toContainText('outcome_limit_up_pool_missing')
    await expect(summary).toContainText('本样本达到目标（非认证）')
  }
  for (const key of ['daily', 'cohorts']) {
    const table = page.getByTestId(`review-directional-${key}`)
    await expect(table).toContainText('80.0%')
    await expect(table).toContainText('部分可评价')
    await expect(table).toContainText('outcome_limit_up_pool_missing')
  }
})

test('legacy valid precision stays visible without fabricating new coverage fields', async ({ page }) => {
  await openReview(page, { predicted_count: 10, directional_precision: 0.6, evaluation_status: 'partial' })
  const summary = page.getByTestId('review-directional-latest')
  await expect(summary).toContainText('完整上涨实际率 60.0%')
  await expect(summary).toContainText('可评价 / 原预测 -- / 10')
  await expect(summary).toContainText('覆盖率 --')
  await expect(summary).toContainText('数据不足 · 达标未知')
})

const researchCandidate = (code, probability, rank, method = 'neutral_prior_insufficient_sample') => ({
  code, name: `研究${code}`, direction_probability: probability, limit_up_probability: 0.02,
  probability_factors: { direction_research: { rank_position: rank, probability_method: method } },
})

test('research ranking stays separate from production and reports probability method', async ({ page }) => {
  const rows = [researchCandidate('600002', 0.7, 1), researchCandidate('600001', 0.5, 2)]
  await openReview(page, {}, true, {
    ranked_first_board_candidates: [{ code: '600099', name: '原生产榜', probability: 0.9 }],
    direction_research: {
      scope: 'research_only', status: 'insufficient_candidates', candidates: rows,
      rank_limit: 12, selected_count: 2, target_precision: 0.8, candidate_count: 4,
      eligible_count: 2, missing_probability_count: 0,
      version: 'direction_rank_research_v1', label_version: 'next_day_close_up_v1',
      production_unchanged: true, manual_review_eligible: false,
    },
  })
  await page.getByRole('tab', { name: '研究观察', exact: true }).click()
  const panel = page.getByTestId('direction-research-panel')
  const table = page.getByTestId('direction-research-table')
  await expect(panel).toContainText('候选不足 · 不补齐旧榜')
  await expect(panel).toContainText('不是买入建议')
  await expect(table.locator('.el-table__body-wrapper tbody tr')).toHaveCount(2)
  await expect(table.locator('.el-table__body-wrapper tbody tr').first()).toContainText('600002')
  await expect(table).toContainText('70.0%')
  await expect(table).toContainText('2.0%')
  await expect(table).toContainText('neutral_prior_insufficient_sample / 样本不足')
  await expect(table).not.toContainText('原生产榜')
  await page.getByRole('tab', { name: '晋级候选', exact: true }).click()
  await expect(page.getByTestId('first-board-top12-table')).toContainText('原生产榜')
})

test('old API does not backfill research ranking from the production table', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await openReview(page, {}, true, {
    ranked_first_board_candidates: [{ code: '600099', name: '仅原榜', probability: 0.9 }],
  })
  await page.getByRole('tab', { name: '研究观察', exact: true }).click()
  await expect(page.getByTestId('direction-research-table')).toContainText('等待新冻结批次，不补旧榜')
  await expect(page.getByTestId('direction-research-table')).not.toContainText('仅原榜')
  await page.getByRole('tab', { name: '晋级候选', exact: true }).click()
  await expect(page.getByTestId('first-board-top12-table')).toContainText('仅原榜')
})

test('blocked ranking cannot expose stale candidates and null direction never uses limit-up probability', async ({ page }) => {
  await openReview(page, {}, true, { direction_research: {
    version: 'direction_rank_research_v1', label_version: 'next_day_close_up_v1',
    production_unchanged: true, manual_review_eligible: false,
    scope: 'research_only', status: 'blocked', candidates: [researchCandidate('600001', 0.7, 1)],
    reason: 'eligible_direction_probability_missing', missing_probability_count: 1,
  } })
  await page.getByRole('tab', { name: '研究观察', exact: true }).click()
  await expect(page.getByTestId('direction-research-table')).toContainText('研究榜阻塞')
  await expect(page.getByTestId('direction-research-table')).not.toContainText('600001')
  await openReview(page, {}, true, { direction_research: {
    version: 'direction_rank_research_v1', label_version: 'next_day_close_up_v1',
    production_unchanged: true, manual_review_eligible: false, selected_count: 1,
    scope: 'research_only', status: 'insufficient_candidates', candidates: [researchCandidate('600001', null, 1, 'unknown')],
  } })
  await page.getByRole('tab', { name: '研究观察', exact: true }).click()
  const row = page.getByTestId('direction-research-table').locator('.el-table__body-wrapper tbody tr').first()
  await expect(row.locator('td').nth(2)).toHaveText('--')
  await expect(row.locator('td').nth(3)).toHaveText('2.0%')
  await expect(row).toContainText('概率方法未知')
})

test('invalid rates and nonboolean target cannot become zero or a success', async ({ page }) => {
  await openReview(page, {
    directional_precision: ' ', directional_coverage: 'invalid',
    directional_observed_precision: false, directional_precision_lower_bound: -0.1,
    directional_precision_upper_bound: 2, directional_target_met: 'false',
    evaluation_status: 'unavailable',
  })
  const summary = page.getByTestId('review-directional-latest')
  await expect(summary).toContainText('覆盖率 --')
  await expect(summary).toContainText('已评价子集上涨率 --')
  await expect(summary).toContainText('原名单上涨率上下界 --')
  await expect(summary).toContainText('数据不足 · 达标未知')
  await expect(summary).not.toContainText('0.0%')
})
