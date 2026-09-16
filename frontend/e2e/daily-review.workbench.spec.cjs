const { test, expect } = require('@playwright/test')

const WEB_URL = process.env.CLAW_WEB_URL || 'http://127.0.0.1:5173'

test('daily review explains dates, decisions, AI capability and alerts', async ({ page }) => {
  test.setTimeout(45_000)
  const browserIssues = []

  page.on('pageerror', error => browserIssues.push(`pageerror: ${error.message}`))
  page.on('console', message => {
    if (message.type() === 'error') browserIssues.push(`console: ${message.text()}`)
  })

  const capabilityResponse = await page.request.get(
    `${WEB_URL}/api/v1/daily-review/gpt-reports/capabilities`,
  )
  expect(capabilityResponse.ok()).toBeTruthy()
  const capabilities = await capabilityResponse.json()

  await page.setViewportSize({ width: 1440, height: 1000 })
  await page.goto(`${WEB_URL}/daily-review`, { waitUntil: 'networkidle' })
  await expect(page.getByRole('heading', { name: '每日复盘 · 盘后决策台' })).toBeVisible()
  await expect(page.locator('a[href="/daily-review"] svg')).toHaveCount(1)
  await expect(page.locator('.date-facts')).toContainText('复盘交易日')
  await expect(page.locator('.date-facts')).toContainText('完整日线基准日')
  await expect(page.locator('.date-facts')).toContainText('市场状态交易日')
  await expect(page.locator('.decision-headline')).not.toBeEmpty()
  await expect(page.getByText('下一交易日条件卡', { exact: true })).toBeVisible()
  await expect(page.locator('body')).toContainText(/冻结模型观察池（非买入指令）|下一交易日观察池尚未冻结/)
  await expect(page.getByText('沪深成交额', { exact: true })).toBeVisible()

  const panelByTitle = title => page.locator('section.panel-card').filter({
    has: page.locator('.panel-title').filter({ hasText: new RegExp(`^${title}$`) }),
  }).first()
  const newsPanel = panelByTitle('消息影响筛选')
  await expect(newsPanel).toContainText('影响对象')
  await newsPanel.locator('.el-radio-button').filter({ hasText: '利好' }).click()
  await expect(newsPanel.locator('.news-item').first().locator('.el-tag').first()).toHaveText('利好')
  expect(await newsPanel.locator('.news-item').count()).toBeLessThanOrEqual(8)
  await newsPanel.locator('.el-radio-button').filter({ hasText: '全部' }).click()

  const fundamentalPanel = panelByTitle('观察池基本面体检')
  await expect(fundamentalPanel).toContainText('不负责预测大盘涨跌')
  if (await fundamentalPanel.locator('.fundamental-empty-state').count()) {
    await expect(fundamentalPanel).toContainText('没有统计对象')
    await expect(fundamentalPanel).toContainText('不再用 0 填充缺失数据')
  } else {
    await expect(fundamentalPanel).toContainText('观察池覆盖')
  }

  const predictionPanel = panelByTitle('预测可信度与失败位置')
  await expect(predictionPanel).toContainText(/候选池覆盖|正在读取最近已结算日|历史快照不叠加/)
  await expect(predictionPanel).toContainText('实际样本与晋级预测正式口径一致')

  const highBoardPanel = page.locator('section.panel-card').filter({ hasText: '涨停池与高标反馈' }).first()
  const highBoardRows = highBoardPanel.locator('tbody tr')
  if (await highBoardRows.count()) {
    const firstRowCells = highBoardRows.first().locator('td .cell')
    await expect(firstRowCells.nth(1)).toHaveText(/^\d{6}$/)
    await expect(firstRowCells.nth(4)).toHaveText(/^\d{2}:\d{2}:\d{2}$/)
    const clippedCells = await highBoardPanel.locator('tbody td .cell').evaluateAll(cells => (
      cells
        .filter(cell => cell.scrollWidth > cell.clientWidth + 1)
        .map(cell => cell.innerText.trim())
    ))
    expect(clippedCells, `涨停列表仍有截断字段：${clippedCells.join('、')}`).toEqual([])
  }

  const aiButton = page.getByRole('button', {
    name: /生成当前快照解读|重新生成当前快照解读/,
  })
  if (capabilities.enabled) {
    await expect(aiButton).toBeEnabled()
  } else {
    await expect(aiButton).toBeDisabled()
    await expect(page.getByText('按钮已安全禁用，不会再发送必然失败的请求；历史已生成报告仍可查看。')).toBeVisible()
  }

  await page.getByRole('tab', { name: /告警记录/ }).click()
  await expect(page.getByRole('tab', { name: /告警记录/ })).toHaveAttribute(
    'aria-selected',
    'true',
  )

  const selectedDate = await page.locator('.review-controls .el-date-editor input').inputValue()
  await page.locator('.review-controls .el-radio-button').filter({ hasText: '盘前' }).click()
  await expect(page.locator('.review-controls .el-date-editor input')).toHaveValue(selectedDate)
  await expect(page.locator('.decision-summary')).toContainText('盘前证据')

  expect(browserIssues, browserIssues.join('\n')).toEqual([])
})
