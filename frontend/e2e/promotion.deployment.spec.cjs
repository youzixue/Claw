const { test, expect } = require('@playwright/test')

const WEB_URL = process.env.CLAW_WEB_URL || 'http://127.0.0.1:5173'
const percent = value => value == null ? '--' : `${(value * 100).toFixed(1)}%`

// Live acceptance: no route mocks, no model activation or scheduled snapshot calls.
test('deployed direction review and research board match real API responses', async ({ page }, testInfo) => {
  test.setTimeout(180_000)
  const errors = []
  page.on('pageerror', error => errors.push(error.message))
  page.on('response', response => {
    if (response.url().includes('/api/') && response.status() >= 400) errors.push(`${response.status()} ${response.url()}`)
  })
  await page.goto(`${WEB_URL}/promotion`, { waitUntil: 'networkidle', timeout: 120_000 })
  const reviewResponse = page.waitForResponse(r => new URL(r.url()).pathname === '/api/v1/promotion/learning-review', { timeout: 120_000 })
  await page.getByRole('tab', { name: '预测复盘', exact: true }).click()
  const review = await (await reviewResponse).json()
  const pendingCandidates = page.waitForResponse(r => new URL(r.url()).pathname === '/api/v1/promotion/candidates', { timeout: 120_000 })
  await page.getByRole('tab', { name: '研究观察', exact: true }).click()
  const candidatesResponse = await pendingCandidates
  expect(candidatesResponse.ok()).toBe(true)
  const candidates = await candidatesResponse.json()
  expect(candidates.prediction_model_version).toBe('promotion_v20260829_27_governed')
  await page.getByRole('tab', { name: '预测复盘', exact: true }).click()
  for (const name of ['latest', 'aggregate']) {
    const data = review[name]
    expect(Object.prototype.hasOwnProperty.call(data, 'directional_unknown_count')).toBe(true)
    expect(data.directional_evaluable_count + data.directional_unknown_count).toBe(data.predicted_count)
    expect(data.directional_target_precision).toBe(0.8)
    const panel = page.getByTestId(`review-directional-${name}`)
    const missingFormal = data.predicted_count === 0
      && (data.evaluation_reasons || []).includes('formal_ranked_predictions_unavailable')
    if (missingFormal) {
      await expect(panel).toContainText('-- / 暂无记录')
      await expect(panel).toContainText('未知 --')
      await expect(panel).toContainText('覆盖率 --')
    } else {
      await expect(panel).toContainText(`${data.directional_evaluable_count} / ${data.predicted_count}`)
      await expect(panel).toContainText(`未知 ${data.directional_unknown_count}`)
      await expect(panel).toContainText(`覆盖率 ${percent(data.directional_coverage)}`)
    }
    await expect(panel).toContainText(`完整上涨实际率 ${percent(data.directional_precision)}`)
    if (data.directional_unknown_count || !data.predicted_count) {
      expect(data.directional_precision).toBeNull()
      expect(data.directional_target_met).toBeNull()
      await expect(panel).toContainText('数据不足 · 达标未知')
    }
  }
  await page.getByRole('tab', { name: '研究观察', exact: true }).click()
  const research = candidates.direction_research
  expect(research).toBeTruthy()
  expect(research.scope).toBe('research_only')
  expect(research.production_unchanged).toBe(true)
  expect(research.manual_review_eligible).toBe(false)
  await expect(page.getByTestId('direction-research-panel')).toContainText('次日上涨研究榜Top12')
  await expect(page.getByTestId('direction-research-panel')).toContainText('不是买入建议')
  await expect(page.getByTestId('direction-research-panel')).toContainText(research.version)
  const rows = research.candidates || []
  await expect(page.getByTestId('direction-research-table').locator('.el-table__body-wrapper tbody tr')).toHaveCount(rows.length)
  for (let i = 1; i < rows.length; i++) expect(rows[i - 1].direction_probability).toBeGreaterThanOrEqual(rows[i].direction_probability)
  if (research.status === 'blocked') expect(rows).toEqual([])
  expect(errors).toEqual([])
  await page.screenshot({ path: testInfo.outputPath('promotion-deployed-desktop.png'), fullPage: true })
  await page.setViewportSize({ width: 390, height: 844 })
  await page.reload({ waitUntil: 'networkidle', timeout: 120_000 })
  await page.getByRole('tab', { name: '预测复盘', exact: true }).click()
  await expect(page.getByTestId('review-directional-latest')).toBeVisible()
  await expect(page.getByTestId('direction-research-panel')).toHaveCount(0)
  const summary = page.getByTestId('review-directional-latest')
  expect(await summary.evaluate(el => el.scrollWidth <= el.clientWidth + 1)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('promotion-deployed-mobile.png'), fullPage: true })
  await page.getByRole('tab', { name: '研究观察', exact: true }).click()
  await expect(page.getByTestId('direction-research-panel')).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true)
  expect(errors).toEqual([])
})
