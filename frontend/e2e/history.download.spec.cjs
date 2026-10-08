// Fully intercepted built-app test: no requests to a live Claw backend.
const { test, expect } = require('@playwright/test')
const fs = require('node:fs')
const path = require('node:path')

const DIST = path.resolve(__dirname, '../dist')
const ORIGIN = 'http://history-download.test'
const bars = Array.from({ length: 30 }, (_, index) => ({
  trade_date: `2026-09-${String(index + 1).padStart(2, '0')}`,
  open: 10, close: 10.5, high: 11, low: 9.5, volume: 1000,
  amount: 10000, turnover: null, change_pct: null, prev_close: null,
}))

async function isolate(page, mode = 'normal') {
  const errors = []
  const requests = []
  page.on('pageerror', error => errors.push(error.message))
  // Prevent even the layout's WebSocket from contacting a real service.
  await page.routeWebSocket('**/*', socket => socket.close())
  await page.route('**/*', async route => {
    const url = new URL(route.request().url())
    if (url.origin !== ORIGIN) return route.abort()
    if (url.pathname.startsWith('/api/')) {
      expect(route.request().method()).toBe('GET')
      requests.push(url.pathname + url.search)
      let body = {}
      if (url.pathname.includes('/stocks/kline/')) {
        const downloaded = url.searchParams.get('view') === 'downloaded'
        if (downloaded && mode === 'error') {
          return route.fulfill({ status: 503, json: { detail: '历史下载不可用' } })
        }
        if (!downloaded && mode === 'race' && url.searchParams.get('limit') === '800') {
          await new Promise(resolve => setTimeout(resolve, 650))
        }
        body = {
          code: '000001', klines: downloaded && mode !== 'empty' ? bars : [],
          count: downloaded && mode !== 'empty' ? bars.length : 0,
          historical_pit_eligible: false,
          downloaded_at: downloaded && mode !== 'empty' ? '2026-10-08T19:00:00+08:00' : null,
          coverage: downloaded && mode !== 'empty' ? { certified: false, unavailable_years: [2020] } : null,
        }
      } else if (url.pathname.endsWith('/profile')) {
        body = { profile: { name: '隔离样本', code: '000001', sectors: [] } }
      } else if (url.pathname.includes('/stocks/spot/')) {
        body = { error: 'missing' }
      }
      return route.fulfill({ json: body })
    }
    const asset = url.pathname.startsWith('/assets/')
      ? path.join(DIST, url.pathname) : path.join(DIST, 'index.html')
    if (!asset.startsWith(DIST + path.sep)) return route.abort()
    const extension = path.extname(asset)
    return route.fulfill({
      body: fs.readFileSync(asset),
      contentType: extension === '.js' ? 'text/javascript'
        : extension === '.css' ? 'text/css' : 'text/html',
    })
  })
  await page.goto(ORIGIN + '/stocks/000001')
  await page.getByRole('tab', { name: 'K线', exact: true }).click()
  return { errors, requests }
}

test('explicit historical view shows provenance without replacing the daily default', async ({ page }, testInfo) => {
  const { errors, requests } = await isolate(page)
  await expect(page.getByRole('radio', { name: '日常采集', exact: true })).toBeChecked()
  await page.locator('.el-radio-button').filter({ hasText: '下载历史' }).click()
  const note = page.getByTestId('downloaded-history-note')
  await expect(note).toContainText('2026-10-08T19:00:00+08:00')
  await expect(note).toContainText('不用于历史交易证明')
  await expect(note).toContainText('2020')
  await expect(page.locator('.kline-panel canvas')).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('history-download.png'), fullPage: true })
  await page.locator('.el-radio-button').filter({ hasText: '日常采集' }).click()
  await expect(note).toHaveCount(0)
  expect(requests.some(url => url.includes('view=downloaded'))).toBeTruthy()
  expect(errors).toEqual([])
})

test('missing downloaded history shows an honest empty state', async ({ page }) => {
  const { errors } = await isolate(page, 'empty')
  await page.locator('.el-radio-button').filter({ hasText: '下载历史' }).click()
  await expect(page.getByTestId('downloaded-history-note')).toContainText('尚无有效历史下载')
  await expect(page.locator('.kline-loading')).toContainText('暂无K线数据')
  expect(errors).toEqual([])
})

test('invalid archive shows failure rather than daily-data fallback', async ({ page }) => {
  await isolate(page, 'error')
  await page.locator('.el-radio-button').filter({ hasText: '下载历史' }).click()
  await expect(page.locator('.kline-loading')).toContainText('K线数据加载失败')
  await expect(page.locator('.kline-panel canvas')).toHaveCount(0)
})

test('late daily response cannot overwrite the selected downloaded view', async ({ page }) => {
  const { errors } = await isolate(page, 'race')
  await page.locator('.el-radio-button').filter({ hasText: '下载历史' }).click()
  await expect(page.locator('.kline-panel canvas')).toBeVisible()
  await page.waitForTimeout(850)
  await expect(page.getByTestId('downloaded-history-note')).toContainText('2026-10-08')
  await expect(page.locator('.kline-panel canvas')).toBeVisible()
  expect(errors).toEqual([])
})
