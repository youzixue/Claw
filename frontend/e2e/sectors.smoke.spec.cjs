const { test, expect } = require('@playwright/test')

test('sectors smoke', async ({ page }) => {
  const errors = []

  page.on('pageerror', (e) => errors.push(`pageerror:${e.message}`))
  page.on('console', (msg) => {
    if (msg.type() === 'error') errors.push(`console:${msg.text()}`)
  })
  page.on('response', (res) => {
    if (res.url().includes('/api/v1/sectors') && res.status() >= 400) {
      errors.push(`api:${res.status()} ${res.url()}`)
    }
  })

  await page.goto('http://localhost:5177/sectors', { waitUntil: 'networkidle' })
  await expect(page.getByRole('heading', { name: /板块营地/ })).toBeVisible()

  const tabs = ['生命周期', '轮动日历', '主线追踪', '板块强弱', '板块轮动', '板块持续性', '板块K线']
  for (const name of tabs) {
    const el = page.getByRole('tab', { name })
    if (await el.count()) {
      await el.first().click()
      await page.waitForTimeout(1000)
    }
  }

  const industry = page.getByText('🏭 行业板块')
  if (await industry.count()) {
    await industry.first().click()
    await page.waitForTimeout(1500)
  }

  const concept = page.getByText('🔥 概念板块')
  if (await concept.count()) {
    await concept.first().click()
    await page.waitForTimeout(1500)
  }

  expect(errors, errors.join('\n')).toEqual([])
})

test('sectors kline flow', async ({ page }) => {
  const errors = []
  page.on('pageerror', (e) => errors.push(`pageerror:${e.message}`))
  page.on('console', (msg) => {
    if (msg.type() === 'error') errors.push(`console:${msg.text()}`)
  })
  page.on('response', (res) => {
    if (res.url().includes('/api/v1/sectors') && res.status() >= 400) {
      errors.push(`api:${res.status()} ${res.url()}`)
    }
  })

  await page.goto('http://localhost:5177/sectors', { waitUntil: 'networkidle' })
  await page.getByRole('tab', { name: '板块K线' }).click()
  await page.waitForTimeout(1500)

  const quickCards = page.locator('.quick-card')
  if (await quickCards.count()) {
    await quickCards.first().click()
    await page.waitForTimeout(2500)
  }

  // 切周期触发重新加载
  const day120 = page.getByText('120天')
  if (await day120.count()) {
    await day120.first().click()
    await page.waitForTimeout(1500)
  }

  // 如果有K线图，确保图容器存在；如果无数据，确保空态文案存在
  const chart = page.locator('.echarts')
  const empty = page.getByText('暂无K线数据')
  const hasChart = (await chart.count()) > 0
  const hasEmpty = (await empty.count()) > 0
  expect(hasChart || hasEmpty).toBeTruthy()

  expect(errors, errors.join('\n')).toEqual([])
})
