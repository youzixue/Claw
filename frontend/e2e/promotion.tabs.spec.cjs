const { test, expect } = require('@playwright/test')
const path = require('node:path')
const WEB_URL = process.env.CLAW_WEB_URL || 'http://127.0.0.1:5173'
const output = path.resolve(__dirname, '../../outputs/promotion_status_tabs_20260929')
async function setup(page) {
  const calls = {}, errors = []
  page.on('pageerror', e => errors.push(e.message))
  await page.route('**/api/v1/**', async route => {
    const key = new URL(route.request().url()).pathname.split('/').pop()
    calls[key] = (calls[key] || 0) + 1
    await route.fulfill({ json: key === 'board-height' ? { height: 3, limit_up_count: 20 }
      : key === 'ladder' ? { ladder: [{ consecutive_days: 3, count: 1, stocks: [{ code: '600001', name: '测试', tag: '禁止交易', is_tradeable: false }] }] }
      : key === 'candidates' ? { ranked_first_board_candidates: [{ code: '600002', name: '保留候选', probability: 0.5 }] } : {} })
  })
  return { calls, errors }
}
for (const [section, tab, endpoint] of [
  ['height', '市场梯队', 'board-height'], ['ladder', '市场梯队', 'ladder'],
  ['candidates', '晋级候选', 'candidates'], ['review', '预测复盘', 'learning-review'],
]) test(`${section}: isolated error and retry retain other sections`, async ({ page }) => {
  const state = await setup(page)
  let attempts = 0
  await page.route('**/api/v1/promotion/' + endpoint + '**', async route => {
    attempts++
    if (attempts === 1) await route.fulfill({ status: 503, json: { detail: 'isolated error' } })
    else await route.fulfill({ json: {} })
  })
  await page.goto(WEB_URL + '/promotion')
  await select(page, tab)
  const status = page.getByTestId('request-' + section)
  await expect(status).toContainText('加载失败')
  await status.getByRole('button', { name: '重试' }).click()
  await expect(status).toContainText('已加载')
  expect(attempts).toBe(2)
  if (section !== 'height') expect(state.calls['board-height']).toBe(1)
  expect(state.errors).toEqual([])
})
test('shared export stays available after switching out of candidates', async ({ page }) => {
  const state = await setup(page)
  await page.goto(WEB_URL + '/promotion')
  await expect(page.getByRole('button', { name: '导出', exact: true })).toBeDisabled()
  await select(page, '晋级候选')
  await expect(page.getByTestId('first-board-top12-table')).toContainText('保留候选')
  await select(page, '市场梯队')
  await page.getByRole('button', { name: '导出', exact: true }).click()
  const download = page.waitForEvent('download')
  await page.getByRole('menuitem', { name: '导出 CSV' }).click()
  expect((await download).suggestedFilename()).toContain('分赛道概率榜')
  expect(state.calls.candidates).toBe(1)
})
const select = (page, name) => page.getByRole('tab', { name, exact: true }).click()
for (const width of [1440, 1024, 390]) test(`tabs at ${width}: short first page, exclusive sections and retained data`, async ({ page }) => {
  await page.setViewportSize({ width, height: 900 })
  const state = await setup(page)
  await page.goto(WEB_URL + '/promotion')
  await expect(page.getByTestId('promotion-ladder')).toContainText('禁止交易')
  expect(state.calls.candidates || 0).toBe(0)
  expect(state.calls['learning-review'] || 0).toBe(0)
  await expect(page.getByRole('tabpanel').locator('.el-table')).toHaveCount(1)
  await expect(page.getByTestId('review-lanes')).toHaveCount(0)
  expect(await page.locator('.promotion-page').evaluate(el => el.getBoundingClientRect().height)).toBeLessThan(1500)
  for (const name of ['市场梯队', '晋级候选', '预测复盘', '研究观察']) {
    await select(page, name)
    await expect(page.getByRole('tab', { name, exact: true })).toHaveAttribute('aria-selected', 'true')
    await expect(page.getByRole('tabpanel')).toHaveCount(1)
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true)
    if (name !== '市场梯队') await expect(page.locator('.chart-card')).toHaveCount(0)
    if (name !== '研究观察') await expect(page.getByTestId('direction-research-panel')).toHaveCount(0)
    await page.screenshot({ path: path.join(output, `frontend-${width}-${['市场梯队','晋级候选','预测复盘','研究观察'].indexOf(name)}.png`) })
  }
  await select(page, '晋级候选')
  await expect(page.getByTestId('first-board-top12-table')).toContainText('保留候选')
  await page.getByPlaceholder('输入股票代码').fill('600002')
  await select(page, '研究观察')
  await page.getByText('展开预备池、弱观察与未入池诊断', { exact: true }).click()
  await expect(page.getByText('首板未入池诊断', { exact: true })).toBeVisible()
  await select(page, '晋级候选')
  await expect(page.getByPlaceholder('输入股票代码')).toHaveValue('600002')
  expect(state.calls.candidates).toBe(1)
  expect(state.calls['learning-review']).toBe(1)
  expect(state.calls['board-height']).toBe(1)
  expect(state.calls.ladder).toBe(1)
  expect(state.errors).toEqual([])
})
test('keyboard navigation preserves page state, refresh only current resources', async ({ page }) => {
  const state = await setup(page)
  await page.goto(WEB_URL + '/promotion')
  await select(page, '预测复盘')
  await expect(page.getByTestId('review-lanes')).toBeVisible()
  expect(state.calls.candidates || 0).toBe(0)
  await expect.poll(() => state.calls.ladder).toBe(1)
  const review = page.getByRole('tab', { name: '预测复盘', exact: true })
  await review.focus()
  await page.keyboard.press('ArrowRight')
  await expect(page.getByRole('tab', { name: '研究观察', exact: true })).toBeFocused()
  await expect(page.getByRole('tab', { name: '研究观察', exact: true })).toHaveAttribute('aria-selected', 'true')
  await page.keyboard.press('Home')
  await expect(page.getByTestId('promotion-ladder')).toBeVisible()
  await page.keyboard.press('End')
  await expect(page.getByTestId('direction-research-panel')).toBeVisible()
  await page.getByRole('button', { name: '刷新当前视图' }).click()
  await expect.poll(() => state.calls.candidates).toBe(2)
  expect(state.calls['learning-review']).toBe(1)
  expect(state.calls.ladder).toBe(1)
  expect(state.errors).toEqual([])
})
