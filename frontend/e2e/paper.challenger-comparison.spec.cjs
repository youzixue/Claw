const { test, expect } = require('@playwright/test')

const WEB_URL = process.env.CLAW_WEB_URL || 'http://127.0.0.1:5173'

// Optional real responses generated from the migrated rehearsal database. Without
// this environment variable the same assertions verify the deployed API.
test.beforeEach(async ({ page }) => {
  if (!process.env.CLAW_COMPARISON_FIXTURE) return
  const fixture = JSON.parse(require('fs').readFileSync(process.env.CLAW_COMPARISON_FIXTURE, 'utf8'))
  await page.route('**/api/v1/paper/challengers/comparison**', route => {
    const account = new URL(route.request().url()).searchParams.get('account_name') || 'default'
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(fixture[account]) })
  })
  await page.route('**/api/v1/paper/experiment/report**', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify(fixture.experiment_report),
  }))
})

async function openComparison(page) {
  await page.getByRole('tab', { name: '策略对比' }).click()
  await expect(page.getByText('当前策略的隔离验证')).toBeVisible()
}

async function switchStrategy(page, label) {
  await page.locator('.el-radio-button').filter({ hasText: label }).click()
  await expect(page.getByRole('tab', { name: '账户概览' })).toHaveAttribute('aria-selected', 'true')
  await openComparison(page)
}

test('strategy comparison covers all six secondary accounts and keeps C3 evidence separate', async ({ page }) => {
  test.setTimeout(40_000)
  const browserIssues = []
  page.on('pageerror', error => browserIssues.push(`pageerror: ${error.message}`))
  page.on('console', message => {
    if (message.type() === 'error') browserIssues.push(`console: ${message.text()}`)
  })

  await page.goto(`${WEB_URL}/paper`, { waitUntil: 'domcontentloaded' })
  await expect(page.getByRole('heading', { name: '模拟盘' })).toBeVisible()
  await expect(page.getByText('本页全部成交均为本地模拟成交')).toBeVisible()

  await openComparison(page)
  await expect(page.getByText('未配置隔离候选策略')).toHaveCount(0)
  await expect(page.getByRole('heading', { name: /策略A2/ })).toBeVisible()
  await expect(page.getByText('基准账户成立以来收益')).toBeVisible()
  await expect(page.getByText('已配置 6/6')).toBeVisible()
  await expect(page.getByText('共 7 条路线', { exact: false })).toBeVisible()
  await expect(page.getByText('模拟账户 6（开启', { exact: false })).toBeVisible()
  await expect(page.getByText('只采证 1', { exact: false })).toBeVisible()
  await expect(page.locator('.coverage-chip')).toHaveCount(6)

  await switchStrategy(page, 'B·晋级二板')
  await expect(page.getByRole('heading', { name: '策略B2 · 负开弱转强', exact: true })).toBeVisible()
  await expect(page.locator('.challenger-content').getByText('策略C2 · 涨停记忆再启动')).toHaveCount(0)
  await expect(page.getByText('共同观察期收益差')).toBeVisible()
  await expect(page.getByText('代码自测通过不等于策略有效')).toBeVisible()
  await expect(page.getByText('前向验证流水线')).toBeVisible()
  await expect(page.getByText('① 前向信号采集')).toBeVisible()
  await expect(page.getByText('② 到期结算验收')).toBeVisible()
  await expect(page.getByText('等待到期结算')).toBeVisible()
  await expect(page.getByText('统计证据验收清单（仅使用当前路由版本的到期结算数据）')).toBeVisible()
  await expect(page.getByText('候选账户成立以来跨版本最大回撤')).toBeVisible()
  await expect(page.getByText('当前版本成交 / 持仓')).toBeVisible()
  await expect(page.getByText('这是跨版本账户风控事实，不计入当前路由版本的统计证据门槛。', { exact: false })).toBeVisible()
  await expect(page.getByText(/隔离自动撮合(开启|暂停)/, { exact: true })).toBeVisible()
  await expect(page.getByText('候选账户版本：')).toBeVisible()

  await switchStrategy(page, 'C·主线扩散')
  await expect(page.getByText('当前策略有 2 条独立候选路线')).toBeVisible()
  await expect(page.getByRole('heading', { name: '策略C2 · 涨停记忆再启动', exact: true })).toBeVisible()
  await expect(page.locator('.challenger-route-selector').getByText('策略C3 · 主线首板盘中确认', { exact: true })).toBeVisible()
  await page.locator('.challenger-route-selector .el-radio-button').filter({ hasText: '策略C3 · 主线首板盘中确认' }).click()
  await expect(page.getByRole('heading', { name: '策略C3 · 主线首板盘中确认', exact: true })).toBeVisible()
  await expect(page.getByText('只采证 · 不撮合', { exact: true })).toBeVisible()
  await expect(page.getByText('账户：仅证据台账，未创建候选撮合账户')).toBeVisible()
  await expect(page.getByText('只采证，不用 0% 冒充收益')).toBeVisible()
  await expect(page.getByText('确认信号相对对照提升')).toBeVisible()
  await expect(page.getByText('未确认但最终首板')).toBeVisible()
  await expect(page.getByText('执行护栏不适用')).toBeVisible()
  await expect(page.getByText('这是跨版本账户风控事实，不计入当前路由版本的统计证据门槛。', { exact: false })).toBeVisible()
  await expect(page.locator('.evidence-only-card')).toBeVisible()
  await expect(page.locator('.challenger-card')).toHaveCount(0)
  await expect(page.locator('.challenger-position-table')).toHaveCount(0)
  await expect(page.locator('.challenger-action-table')).toHaveCount(0)

  let comparisonText = await page.locator('.challenger-content').innerText()
  expect(comparisonText).not.toContain('Champion')
  expect(comparisonText).not.toContain('Challenger')
  expect(comparisonText).not.toContain('paper broker')
  expect(comparisonText).not.toContain('warn/block')

  await switchStrategy(page, 'D·竞价强攻')
  await expect(page.getByRole('heading', { name: '策略D2 · 竞价恢复', exact: true })).toBeVisible()
  await switchStrategy(page, 'E·高标接力')
  await expect(page.getByText('独立模拟买卖链路', { exact: true })).toBeVisible()
  await expect(page.getByRole('heading', { name: /策略E2/ })).toBeVisible()
  await expect(page.locator('.independent-challenger-state')).toContainText('challenger_e')
  await expect(page.locator('.independent-challenger-state')).toContainText('不是只采证账户')
  await page.getByRole('button', { name: '查看十二账户协议绩效与未买原因' }).click()
  await expect(page.getByRole('tab', { name: '持续实验' })).toHaveAttribute('aria-selected', 'true')
  await expect(page.locator('.experiment-account-card')).toHaveCount(12)
  await switchStrategy(page, 'F·断板反包')
  await expect(page.getByRole('heading', { name: '策略F2 · 高标断板收复', exact: true })).toBeVisible()

  comparisonText = await page.locator('.challenger-content').innerText()
  expect(comparisonText).not.toContain('策略B2 · 负开弱转强')
  expect(browserIssues, browserIssues.join('\n')).toEqual([])
})

test('comparison layout contains wide tables without body overflow on mobile', async ({ page }) => {
  test.setTimeout(40_000)
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto(`${WEB_URL}/paper`, { waitUntil: 'domcontentloaded' })
  await expect(page.getByRole('heading', { name: '模拟盘' })).toBeVisible()

  await switchStrategy(page, 'C·主线扩散')
  await expect(page.getByRole('heading', { name: '策略C2 · 涨停记忆再启动', exact: true })).toBeVisible()
  await expect(page.locator('.challenger-content').getByText('策略B2 · 负开弱转强')).toHaveCount(0)
  await expect(page.getByText(/隔离自动撮合(开启|暂停)/, { exact: true })).toBeVisible()
  await expect(page.locator('.challenger-route-selector')).toBeVisible()
  await page.locator('.challenger-route-selector .el-radio-button').filter({ hasText: '策略C3 · 主线首板盘中确认' }).click()
  await expect(page.getByText('只采证 · 不撮合', { exact: true })).toBeVisible()

  const layout = await page.evaluate(() => {
    const root = document.documentElement
    const wrappers = [...document.querySelectorAll('.table-scroll')]
    const routeSelector = document.querySelector('.challenger-route-selector')
    return {
      bodyOverflow: root.scrollWidth > root.clientWidth + 1,
      routeSelectorInsideViewport: routeSelector
        ? routeSelector.getBoundingClientRect().left >= -1 && routeSelector.getBoundingClientRect().right <= root.clientWidth + 1
        : false,
      wrappersInsideViewport: wrappers.every(element => {
        const rect = element.getBoundingClientRect()
        return rect.left >= -1 && rect.right <= root.clientWidth + 1
      }),
      wrappersScrollable: wrappers.every(element => element.scrollWidth >= element.clientWidth),
    }
  })
  expect(layout.bodyOverflow).toBe(false)
  expect(layout.routeSelectorInsideViewport).toBe(true)
  expect(layout.wrappersInsideViewport).toBe(true)
  expect(layout.wrappersScrollable).toBe(true)
})
