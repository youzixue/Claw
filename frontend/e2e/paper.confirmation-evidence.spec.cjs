// Fully isolated: serves the isolated build via routes; no server or business API.
const { test, expect } = require('@playwright/test')
const fs = require('node:fs')
const path = require('node:path')
const build = process.env.CLAW_E2E_BUILD_DIR
  ? path.resolve(process.env.CLAW_E2E_BUILD_DIR)
  : path.resolve(__dirname, '../../.tmp/a-confirmation-build')
test('A evidence layers retain history while current setup fails and old logs remain unknown', async ({ page }) => {
  const errors = []
  page.on('pageerror', error => errors.push(error.message))
  await page.route('**/*', async route => {
    const url = new URL(route.request().url())
    if (url.pathname.startsWith('/api/')) {
      expect(route.request().method()).toBe('GET')
      const rows = [
        { id: 1, code: '600001', name: '历史确认形态失效', action: 'skip_buy', decision: 'skipped',
          confirmation_evidence: { historical_quote_path_confirmed: 'true', current_setup_valid: 'false',
            execution_permitted: 'false', order_result: 'not_submitted' } },
        { id: 2, code: '600002', name: '旧日志缺证据', action: 'skip_buy', decision: 'blocked' },
        { id: 3, code: '600003', name: '许可但待成交', action: 'deferred_buy', decision: 'wait',
          confirmation_evidence: { historical_quote_path_confirmed: 'true', current_setup_valid: 'true',
            execution_permitted: 'true', order_result: 'submitted' } },
      ]
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify(
        url.pathname.endsWith('/auto/logs') ? { logs: rows } :
        url.pathname.endsWith('/account') ? { account: { account_name: 'default' } } : {}
      ) })
    }
    if (url.origin !== 'http://confirmation.test') return route.abort()
    const file = url.pathname.startsWith('/assets/') ? path.join(build, url.pathname) : path.join(build, 'index.html')
    const ext = path.extname(file)
    return route.fulfill({ body: fs.readFileSync(file), contentType: ({ '.js': 'text/javascript', '.css': 'text/css' })[ext] || 'text/html' })
  })
  await page.goto('http://confirmation.test/paper')
  await page.getByRole('tab', { name: '自动执行', exact: true }).click()
  const cells = page.getByTestId('confirmation-evidence')
  await expect(cells).toHaveCount(3)
  await expect(cells.nth(0)).toContainText('历史路径：是')
  await expect(cells.nth(0)).toContainText('本轮形态：否')
  await expect(cells.nth(0)).toContainText('执行许可：否')
  await expect(cells.nth(0)).toContainText('订单结果：未提交')
  await expect(cells.nth(1)).toContainText('历史路径：未知')
  await expect(cells.nth(1)).toContainText('执行许可：未知')
  await expect(cells.nth(2)).toContainText('执行许可：是')
  await expect(cells.nth(2)).toContainText('订单结果：已提交待成交')
  expect(errors).toEqual([])
})
