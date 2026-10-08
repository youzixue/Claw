const { test, expect } = require('@playwright/test')

const WEB_URL = process.env.CLAW_WEB_URL || 'http://127.0.0.1:5173'

function formatPercent(value, scale = 100) {
  if (value == null || typeof value === 'boolean' || String(value).trim() === '') return '--'
  const number = Number(value)
  return Number.isFinite(number) && number >= 0 && number <= 100 / scale
    ? `${(number * scale).toFixed(1)}%` : '--'
}
const formatCount = value => value == null ? '--' : String(value)

test('promotion page renders the same audited values as live APIs', async ({ page }, testInfo) => {
  test.setTimeout(180_000)
  const browserIssues = []

  page.on('pageerror', error => browserIssues.push(`pageerror: ${error.message}`))
  page.on('console', message => {
    if (message.type() === 'error') browserIssues.push(`console: ${message.text()}`)
  })
  page.on('response', response => {
    if (response.url().includes('/api/') && response.status() >= 400) {
      browserIssues.push(`api: ${response.status()} ${response.url()}`)
    }
  })

  // Audit the responses actually rendered, not another expensive request round
  // that can race a newer quote/snapshot and hide the independent loading contract.
  const responses = ['board-height', 'learning-review', 'candidates'].map(endpoint =>
    page.waitForResponse(response => new URL(response.url()).pathname === `/api/v1/promotion/${endpoint}`
      && response.request().method() === 'GET', { timeout: 120_000 }))
  await page.goto(`${WEB_URL}/promotion`, { waitUntil: 'domcontentloaded', timeout: 120_000 })
  await expect(page.getByRole('heading', { name: '晋级预测' })).toBeVisible()
  await page.getByRole('tab', { name: '晋级候选', exact: true }).click()
  await page.getByRole('tab', { name: '预测复盘', exact: true }).click()
  const actualResponses = await Promise.all(responses)
  for (const response of actualResponses) expect(response.ok()).toBe(true)
  const [height, review, candidates] = await Promise.all(actualResponses.map(response => response.json()))
  const latest = review.latest || {}
  const first = latest.lane_metrics?.target_1 || {}
  const second = latest.lane_metrics?.target_2 || {}
  const ladder = height.ladder_summary || []
  const ladderCount = days => Number(ladder.find(item => Number(item.days) === days)?.count || 0)
  const firstMarketCount = ladderCount(1)
  const secondMarketCount = ladderCount(2)
  const higherMarketCount = Math.max(Number(height.limit_up_count || 0) - firstMarketCount - secondMarketCount, 0)

  await expect(page.getByTestId('market-board-height').locator('.stat-value')).toHaveText(formatCount(height.height))
  await expect(page.getByTestId('market-limit-up-count')).toContainText(`统计池涨停${formatCount(height.limit_up_count)}`)
  await expect(page.getByTestId('market-seal-rate').locator('.stat-value')).toHaveText(formatPercent(height.seal_rate, 1))
  await expect(page.getByTestId('market-promotion-rate').locator('.stat-value')).toHaveText(formatPercent(height.promotion_rate))
  if (height.trade_date && height.limit_up_count != null) {
    await expect(page.getByTestId('market-scope-note')).toContainText(
      `首板 ${firstMarketCount} + 二板 ${secondMarketCount} + 三板及以上 ${higherMarketCount} = ${height.limit_up_count}`,
    )
    await expect(page.getByTestId('market-scope-note')).toContainText('下方复盘按当前风险标签过滤主板首/二板')
    await expect(page.getByTestId('market-scope-note')).not.toContainText('该口径过滤 ST/停牌/退市')
  } else {
    await expect(page.getByTestId('market-scope-note')).toHaveCount(0)
  }

  await expect(page.getByTestId('review-first-actual')).toContainText(formatCount(first.actual_count))
  const missingFormal = first.predicted_count != null && Number(first.predicted_count) === 0
    && (first.snapshot_complete === false || (first.evaluation_reasons || []).includes('formal_ranked_predictions_unavailable'))
  await expect(page.getByTestId('review-first-predicted').locator('strong')).toHaveText(
    missingFormal ? '暂无记录' : formatCount(first.predicted_count))
  await expect(page.getByTestId('review-first-hit')).toContainText(formatCount(first.hit_count))
  await expect(page.getByTestId('review-first-pool-recall')).toContainText(formatPercent(first.pool_recall))
  await expect(page.getByTestId('review-second-actual')).toContainText(formatCount(second.actual_count))
  await expect(page.getByTestId('review-second-hit')).toContainText(formatCount(second.hit_count))
  await expect(page.getByTestId('review-first-precision')).toContainText(formatPercent(first.precision))
  await expect(page.getByTestId('review-first-recall')).toContainText(formatPercent(first.recall))

  if (first.recall_ranked_available === true) {
    await expect(page.getByTestId('review-first-top30-hit')).toContainText(formatCount(first.recall_hit_count))
  } else {
    await expect(page.getByTestId('review-first-top30-hit')).toContainText('旧版未记录')
  }

  await expect(page.getByTestId('review-scope-note')).toContainText('与上方竞价成交字段是否完整无关')
  if (latest.prediction_model_version) {
    await expect(page.getByTestId('review-prediction-model')).toContainText(latest.prediction_model_version)
  }
  await expect(page.getByTestId('current-prediction-model')).toContainText(candidates.prediction_model_version)
  await expect(page.getByTestId('review-recommendation')).toContainText(review.recommendation.headline)

  const auction = candidates.prediction_health?.auction_health || {}
  if (auction.trade_date) {
    const strip = page.getByTestId('auction-health-strip')
    await expect(strip).toContainText(`及时快照（仅时点） ${auction.timely_snapshot_count || 0}/${auction.latest_code_count || 0}`)
    await expect(strip).toContainText(`竞价四字段完整 ${auction.feed_complete_count || 0}/${auction.latest_code_count || 0}`)
    await expect(strip).toContainText(
      `强高开四字段齐全（非执行授权） ${auction.executable_strong_open_count || 0}/${auction.strong_open_count || 0}`,
    )
  }

  await page.getByRole('tab', { name: '晋级候选', exact: true }).click()
  const formalFirst = candidates.ranked_first_board_candidates || []
  const recallFirst = candidates.ranked_first_board_recall_candidates || []
  const formalSecond = candidates.ranked_second_board_candidates || []
  expect(recallFirst.slice(0, formalFirst.length).map(item => item.code)).toEqual(formalFirst.map(item => item.code))
  await expect(page.getByTestId('first-board-top12-table').locator('.el-table__body-wrapper tbody tr')).toHaveCount(formalFirst.length)
  await expect(page.getByTestId('first-board-top30-table').locator('.el-table__body-wrapper tbody tr')).toHaveCount(recallFirst.length)
  await expect(page.getByTestId('second-board-table').locator('.el-table__body-wrapper tbody tr')).toHaveCount(formalSecond.length)

  const forecastOnlyCount = recallFirst.filter(item => item.prediction_actionable !== true).length
  await expect(
    page.getByTestId('first-board-top30-table').getByText('仅预测·不可交易', { exact: true }),
  ).toHaveCount(forecastOnlyCount)

  await page.screenshot({ path: testInfo.outputPath('promotion-accuracy.png'), fullPage: true })
  expect(browserIssues, browserIssues.join('\n')).toEqual([])
})
