const { test, expect } = require('@playwright/test')

const WEB_URL = process.env.CLAW_WEB_URL || 'http://127.0.0.1:5173'

const report = {
  generated_at: '2026-09-07T15:31:00',
  protocol_version: 'continuous-v1',
  start_date: '2026-09-07',
  activation_at: '2026-09-07T14:33:17',
  first_session_scope: 'afternoon_only',
  mode: 'continuous_paper',
  scope: 'current_protocol_closed_round_trips',
  methodology: {
    bull_bear: '入场前上一完整交易日沪深双指数MA20/60及5日斜率趋势代理',
    real_order_connected: false,
  },
  accounts: [
    {
      account_name: 'default', strategy_label: '策略A', strategy_version: 'a:v1',
      configured: true, account_created: true, active: true, auto_buy_enabled: true,
      experiment: { sentiment_required: true, real_order_connected: false },
      closed_round_trips: 0, open_round_trips: 1, excluded_round_trips: 2,
      net_pnl: 0, win_rate: null, profit_factor: null,
      by_entry_regime: [], by_entry_sentiment: [],
      by_entry_bull_bear: [{ label: 'unknown', sample_count: 0, win_rate: null, net_pnl: 0, win_rate_interval_95: null, entry_sessions: 0 }],
      latest_activity: { trade_date: null, scan_count: 0, decision_count: 0, fill_count: 0, blocked_count: 0, top_reasons: [] },
    },
    {
      account_name: 'challenger_a', strategy_label: '策略A2', strategy_version: 'a2:v1',
      configured: true, account_created: false, active: true, auto_buy_enabled: false,
      experiment: { sentiment_required: false, real_order_connected: false },
      closed_round_trips: 3, open_round_trips: 0, excluded_round_trips: 1,
      net_pnl: 128.5, win_rate: 2 / 3, profit_factor: 1.8,
      by_entry_regime: [{ label: 'risk_on', sample_count: 3, win_rate: 2 / 3, net_pnl: 128.5 }],
      by_entry_sentiment: [{ label: 'repair', sample_count: 3, win_rate: 2 / 3, net_pnl: 128.5 }],
      by_entry_bull_bear: [{ label: 'bull', sample_count: 3, win_rate: 2 / 3, net_pnl: 128.5, win_rate_interval_95: [0.2077, 0.9385], entry_sessions: 2 }],
      latest_activity: { trade_date: '2026-09-07', scan_count: 4, decision_count: 9, fill_count: 1, blocked_count: 2, top_reasons: [{ reason_code: 'risk_block', reason: '风险门禁', count: 2 }], runtime: { first_buy_attempt_allowed_scan_at: '2026-09-07T14:41:16', last_scan_at: '2026-09-07T14:49:44' } },
    },
  ],
}

async function openExperiment(page) {
  await page.goto(`${WEB_URL}/paper`, { waitUntil: 'domcontentloaded' })
  await page.getByRole('tab', { name: '持续实验' }).click()
}

test('renders current-protocol contract without falsifying null or unknown states', async ({ page }) => {
  await page.route('**/api/v1/paper/experiment/report**', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(report) }))
  await openExperiment(page)

  await expect(page.getByText('12 账户持续模拟交易实验')).toBeVisible()
  await expect(page.getByText('首日午后样本', { exact: false })).toBeVisible()
  await expect(page.getByText('协议已激活').first()).toBeVisible()
  await expect(page.getByText('账户未创建')).toBeVisible()
  await expect(page.getByText('牛熊趋势代理分层').first()).toBeVisible()
  await expect(page.getByText('偏多：3 样本 / 66.7% / 128.50')).toBeVisible()
  await expect(page.getByText('95%区间 20.8%–93.8% · 入场日 2 个')).toBeVisible()
  await expect(page.getByText('未知：0 样本 / 无样本 / 无样本')).toBeVisible()
  await expect(page.getByText('震荡：0 样本', { exact: false })).toHaveCount(0)

  const firstCard = page.locator('.experiment-account-card').first()
  await expect(firstCard.getByText('协议净收益').locator('..')).toContainText('无样本')
  await expect(firstCard.getByText('协议胜率').locator('..')).toContainText('无样本')
  await expect(firstCard.getByText('最近活动 未知日期')).toBeVisible()
  await expect(page.locator('.protocol-start-hint')).toContainText('授权边界')
  await expect(firstCard).toContainText('配置开启不等于已实时扫描')
  await expect(page.locator('.experiment-account-card').nth(1)).toContainText('不代表满足买点或成交')
})

test('reports an undeployed backend HTTP 404 explicitly', async ({ page }) => {
  await page.route('**/api/v1/paper/experiment/report**', route => route.fulfill({ status: 404, contentType: 'application/json', body: JSON.stringify({ detail: 'Not Found' }) }))
  await openExperiment(page)
  await expect(page.getByText('持续实验报告接口尚未部署（HTTP 404）', { exact: false })).toBeVisible()
})
