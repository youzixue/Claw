// 隔离契约测试：本地构建产物+明确测试响应；不访问Claw运行API或启动替代服务。
const { test, expect } = require('@playwright/test')
const fs = require('node:fs/promises')
const path = require('node:path')
const DIST = path.resolve(__dirname, '../dist')
const ORIGIN = 'http://factor-contract.test'
test.use({ serviceWorkers: 'block' })

const results = {
  ma5_bias: { status: 'research_only', ic_summary: { ic_mean: 0, ic_std: 0.2, ir: 0, ic_positive_rate: 0, sample_count: 20 }, decay: { is_decaying: false } },
  rsi_14: { status: 'legacy_not_ic', ic_summary: {}, decay: { is_decaying: null } },
}

async function isolate(page, { empty = false, failCompute = false } = {}) {
  const requests = []
  const errors = []
  page.on('pageerror', error => errors.push(error.message))
  await page.routeWebSocket('**/*', socket => socket.close())
  await page.route('**/*', async route => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.pathname.startsWith('/api/')) {
      requests.push({ method: request.method(), path: url.pathname })
      let payload = {}
      if (url.pathname.endsWith('/factors/categories')) payload = { categories: [{ key: 'technical', count: 2, factors: [
        { name: 'ma5_bias', direction: 1, description: '均线偏离' },
        { name: 'rsi_14', direction: -1, description: 'RSI' },
      ] }] }
      if (url.pathname.endsWith('/factors/evaluate')) payload = { results: empty ? {} : results }
      if (url.pathname.endsWith('/performance/factor-eval')) payload = { factors: [
        { factor_name: 'ma5_bias', status: 'research_only', ic_mean: 0, ir: 0, is_decaying: false },
        { factor_name: 'rsi_14', status: 'legacy_not_ic', ic_mean: null, ir: null, is_decaying: null },
      ] }
      if (url.pathname.includes('/factors/compute/')) {
        if (failCompute) return route.fulfill({ status: 500, json: { detail: 'test failure' } })
        payload = { factors: {
          ma5_bias: { value: 0, pct: 0.75, rank: 1, confidence: 1, meta: {} },
          news_heat: { value: null, pct: null, rank: null, confidence: 0,
            meta: { input_issues: [{ field: 'news_count_1h', code: 'missing_context' }] } },
        } }
      }
      return route.fulfill({ json: payload })
    }
    if (url.origin !== ORIGIN) return route.abort()
    const relative = url.pathname.startsWith('/assets/') ? url.pathname.slice(1) : 'index.html'
    const target = path.resolve(DIST, relative)
    if (!target.startsWith(DIST + path.sep)) return route.abort()
    try {
      const body = await fs.readFile(target)
      const contentType = target.endsWith('.js') ? 'application/javascript' : target.endsWith('.css') ? 'text/css' : 'text/html'
      await route.fulfill({ body, contentType })
    } catch {
      await route.fulfill({ status: 404, body: 'test asset missing' })
    }
  })
  return { requests, errors }
}

test('opening and refreshing factors reads only; directions and legacy IC are explicit', async ({ page }) => {
  const { requests, errors } = await isolate(page)
  await page.goto(ORIGIN + '/factors')
  await expect(page.getByRole('heading', { name: '因子引擎' })).toBeVisible()
  const summary = page.locator('#pane-summary')
  await expect(summary.locator('.el-table__row').filter({ hasText: 'ma5_bias' })).toContainText('正向')
  await expect(summary.locator('.el-table__row').filter({ hasText: 'rsi_14' })).toContainText('反向')
  await page.getByRole('tab', { name: '因子评估', exact: true }).click()
  const pane = page.locator('#pane-evaluate')
  await expect(pane.locator('.el-table__row').filter({ hasText: 'ma5_bias' })).toContainText('0.0%')
  const legacy = pane.locator('.el-table__row').filter({ hasText: 'rsi_14' })
  await expect(legacy).toContainText('旧排名自相关·非IC')
  await expect(legacy).toContainText('待评估')
  await page.getByRole('button', { name: '刷新报告' }).click()
  await expect(page.getByRole('button', { name: '刷新报告' })).not.toHaveClass(/is-loading/)
  expect(requests.filter(row => row.method !== 'GET')).toEqual([])
  const clipped = await pane.locator('.el-table__row .cell').evaluateAll(cells =>
    cells.filter(cell => /^(0\\.0000|0\\.000|正常|待评估)$/.test(cell.textContent.trim()))
      .filter(cell => cell.scrollWidth > cell.clientWidth).map(cell => cell.textContent))
  expect(clipped).toEqual([])
  await page.screenshot({ path: 'test-results/factors-contract-evaluation.png', fullPage: true, animations: 'disabled' })
  expect(errors).toEqual([])
})

test('explicit research action is the only evaluation POST', async ({ page }) => {
  const { requests } = await isolate(page)
  await page.goto(ORIGIN + '/factors')
  await page.getByRole('tab', { name: '因子评估', exact: true }).click()
  await page.getByRole('button', { name: '运行研究评估' }).click()
  await expect(page.getByRole('button', { name: '运行研究评估' })).not.toHaveClass(/is-loading/)
  expect(requests.filter(row => row.method === 'POST')).toEqual([{ method: 'POST', path: '/api/v1/eval/evaluate/daily' }])
})

test('computation distinguishes zero, missing confidence and fractional percentile', async ({ page }) => {
  const { errors } = await isolate(page)
  await page.goto(ORIGIN + '/factors')
  await page.getByRole('tab', { name: '因子计算', exact: true }).click()
  await page.getByPlaceholder('输入股票代码').fill('600000')
  await page.getByRole('button', { name: '计算', exact: true }).click()
  const pane = page.locator('#pane-compute')
  const zero = pane.locator('.el-table__row').filter({ hasText: 'ma5_bias' })
  await expect(zero).toContainText('0.0000')
  await expect(zero).toContainText('75.0%')
  const missing = pane.locator('.el-table__row').filter({ hasText: 'news_heat' })
  await expect(missing).toContainText('0%')
  await expect(missing).toContainText('news_count_1h: missing_context')
  await expect(missing).toContainText('--')
  expect(errors).toEqual([])
})

test('empty reports and compute failures remain explicit on mobile', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await isolate(page, { empty: true, failCompute: true })
  await page.goto(ORIGIN + '/factors')
  await page.getByRole('tab', { name: '因子评估', exact: true }).click()
  await expect(page.locator('#pane-evaluate')).toContainText('暂无数据')
  await page.getByRole('tab', { name: '因子计算', exact: true }).click()
  await page.getByPlaceholder('输入股票代码').fill('600000')
  await page.getByRole('button', { name: '计算', exact: true }).click()
  await expect(page.locator('.el-message').filter({ hasText: '因子计算失败' })).toBeVisible()
})

test('performance cannot present legacy autocorrelation as healthy IC', async ({ page }) => {
  const { errors } = await isolate(page)
  await page.goto(ORIGIN + '/performance')
  await page.getByRole('tab', { name: '因子评估', exact: true }).click()
  const row = page.locator('#pane-factor-eval .el-table__row').filter({ hasText: 'rsi_14' })
  await expect(row).toContainText('旧排名自相关·非IC')
  await expect(row).toContainText('待评估')
  expect(errors).toEqual([])
})
