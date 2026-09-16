const { test, expect } = require('@playwright/test')
const WEB_URL = process.env.CLAW_WEB_URL || 'http://127.0.0.1:5173'

// 隔离页面契约测试：所有 API 均拦截，禁止触发真实新闻采集、模型调用或授权。
async function mockNews(page, { missing = false, failure = false, empty = false, pending = false, authorized = false, saved = {}, catalog = null } = {}) {
  const config = {
    enabled: true, auth_mode: 'api_key', api_format: 'anthropic',
    base_url: 'https://api.example.com/anthropic', model: 'test-model', oauth_model: '',
    max_tokens: 2048, temperature: 0.3, has_key: true, ready: true,
    active_model: 'test-model', oauth_reasoning_effort: '',
    ...saved,
    oauth: { available: !missing, connected: authorized, chat_available: false, login_status: missing ? 'unavailable' : authorized ? 'connected' : 'disconnected', message: missing ? '请安装 Codex CLI' : authorized ? '已授权' : '等待授权' },
  }
  let loginStarted = false
  let reads = 0
  const requests = []
  const analysis = { fetched_count: 20, analyzed_count: 5, ai_analyzed_count: 3, fallback_count: 2, legacy_analyzed_count: 0, analyzing_count: 1, pending_count: 14, failed_count: 2, analysis_rate: 0.25 }
  const news = ['analyzed', 'fallback', 'failed'].map((status, index) => ({
    id: index + 1, title: ['航运主题新闻样本', '规则降级新闻样本', '失败待重试新闻样本'][index],
    content: '这里是正文摘录，不包含任何真实新闻或交易数据。', summary: '这是隔离测试中的消息摘要。',
    source: 'cls', source_layer: 'domestic_news', source_layer_label: '国内市场新闻',
    publish_time: '2026-09-14T12:00:00', nlp_status: status,
    is_analyzed: status !== 'failed', sentiment: 'bullish', bull_bear_confidence: 0.6,
    direction_score: 0.47, related_codes: [], related_sectors: [],
    url: index ? 'javascript:alert(1)' : 'https://example.com/news',
  }))
  await page.route('**/api/v1/**', async route => {
    const req = route.request()
    const path = new URL(req.url()).pathname
    requests.push({ path, method: req.method(), body: req.postDataJSON() })
    let json = {}
    if (path === '/api/v1/ai/status') {
      if (loginStarted) {
        reads++
        const connected = reads > 1 && !pending
        config.oauth = { available: true, connected, login_status: connected ? 'connected' : 'pending', message: connected ? '授权完成' : '等待用户授权' }
      }
      json = config
    } else if (path === '/api/v1/ai/config') {
      Object.assign(config, req.postDataJSON())
      delete config.api_key
      config.ready = config.enabled && (config.auth_mode === 'openai_oauth' ? !!config.oauth.chat_available : config.has_key)
      config.active_model = config.auth_mode === 'openai_oauth' ? config.oauth_model || '账号默认模型' : config.model
      json = config
    } else if (path === '/api/v1/ai/oauth/models') {
      json = catalog || { ok: true, models: [
        { id: 'catalog-model-a', label: '目录模型 A', is_default: true, reasoning_efforts: ['low', 'high'], default_reasoning_effort: 'high' },
        { id: 'catalog-model-b', label: '目录模型 B', is_default: false, reasoning_efforts: ['minimal', 'medium'], default_reasoning_effort: 'medium' },
      ] }
    } else if (path === '/api/v1/ai/test') {
      json = { ok: true, message: '连接成功' }
    } else if (path === '/api/v1/ai/oauth/start') {
      loginStarted = true
      json = { auth_url: 'https://auth.openai.com/oauth/authorize?state=test', login_id: 'test-login', login_status: 'pending' }
    } else if (path === '/api/v1/news/list') {
      if (failure) return route.fulfill({ status: 503, json: { detail: 'test unavailable' } })
      json = {
        news: empty ? [] : news, analysis, window: { label: '最新新闻流', since: null },
        summary: empty ? {} : { headline: '当前样本偏利好，净分 +0.47。', direction: 'bullish', direction_label: '偏利好', net_score: 0.47, bullish_count: 2, bearish_count: 0, major_count: 0, interpretation: ['仅使用已处理样本，不代表完整市场。'], key_points: ['规则降级沿用原有口径'] },
      }
    } else if (path === '/api/v1/news/bull-bear') json = { bull: [], bear: [] }
    else if (path === '/api/v1/news/events') json = { events: [] }
    else if (path === '/api/v1/news/lockup-calendar') json = { lockup: [] }
    else if (path === '/api/v1/news/impact-map') json = { holdings: [], stocks: [], sectors: [] }
    else if (path === '/api/v1/news/analyze-window') json = { job_id: 'job-1', status: 'queued', requested: 100, processed: 0 }
    else if (path === '/api/v1/news/analysis-jobs/job-1') json = { job_id: 'job-1', status: 'cancelled', requested: 100, processed: 2, failed: 1, analysis }
    await route.fulfill({ json })
  })
  return { requests, config }
}

test('reasoning defaults match catalog default and persist with visible feedback', async ({ page }) => {
  const { requests } = await mockNews(page, { authorized: true })
  await page.goto(`${WEB_URL}/news`)
  await page.getByRole('button', { name: 'AI 接入配置' }).click()
  await page.locator('.el-radio-button').filter({ hasText: 'OpenAI 账号授权' }).click()
  const effort = page.getByTestId('reasoning-effort')
  await expect(effort).toContainText('模型默认（high）')
  await effort.locator('.el-select').click()
  await expect(page.getByRole('option', { name: 'high', exact: true })).toBeVisible()
  await expect(page.getByRole('option', { name: 'xhigh', exact: true })).toHaveCount(0)
  await page.getByRole('option', { name: 'high', exact: true }).click()
  await page.getByRole('button', { name: '保存配置', exact: true }).click()
  await expect(page.locator('.el-drawer__footer')).toContainText('思考强度：high')
  expect(requests.find(req => req.path === '/api/v1/ai/config').body.oauth_reasoning_effort).toBe('high')
  await page.locator('.el-drawer__close-btn').click()
  await page.getByRole('button', { name: 'AI 接入配置' }).click()
  await expect(effort).toContainText('已保存思考强度：high')
  await page.locator('.el-drawer .el-select').first().click()
  await page.getByRole('option', { name: '目录模型 B · catalog-model-b', exact: true }).click()
  await expect(effort).toContainText('已重置为模型默认；尚未保存')
  // Hidden OAuth draft must not overwrite the saved high/default-model settings.
  await page.locator('.el-radio-button').filter({ hasText: 'API Key' }).click()
  await page.getByRole('button', { name: '保存配置', exact: true }).click()
  await expect(page.locator('.el-drawer__footer')).toContainText('配置已保存。')
  const last = requests.filter(req => req.path === '/api/v1/ai/config').at(-1).body
  expect(last.oauth_reasoning_effort).toBe('high')
  expect(last.oauth_model).toBe('')
})

test('saved incompatible effort is retained until an explicit supported choice', async ({ page }) => {
  const { requests } = await mockNews(page, { authorized: true, saved: { auth_mode: 'openai_oauth', oauth_reasoning_effort: 'xhigh' } })
  await page.goto(`${WEB_URL}/news`)
  await page.getByRole('button', { name: 'AI 接入配置' }).click()
  await expect(page.getByTestId('reasoning-effort')).toContainText('已保留原值')
  await page.getByRole('button', { name: '保存配置', exact: true }).click()
  await expect(page.locator('.el-drawer__footer')).toContainText('配置未保存：请选择')
  expect(requests.some(req => req.path === '/api/v1/ai/config')).toBe(false)
  await page.getByTestId('reasoning-effort').locator('.el-select').click()
  await page.getByRole('option', { name: '模型默认（high）', exact: true }).click()
  await page.getByRole('button', { name: '保存配置', exact: true }).click()
  await expect(page.locator('.el-drawer__footer')).toContainText('思考强度：模型默认')
})

for (const scenario of ['legacy', 'empty', 'failure', 'manual']) {
  test('unknown reasoning capability preserves saved effort: ' + scenario, async ({ page }) => {
    const catalog = scenario === 'legacy' ? { ok: true, models: [{ id: 'legacy', label: 'Legacy', is_default: true }] }
      : scenario === 'failure' ? { ok: false, message: 'isolated catalog failure' } : { ok: true, models: [] }
    const { requests } = await mockNews(page, { authorized: true, catalog,
      saved: { auth_mode: 'openai_oauth', oauth_model: scenario === 'manual' ? 'manual-model' : '', oauth_reasoning_effort: 'high' } })
    await page.goto(`${WEB_URL}/news`)
    await page.getByRole('button', { name: 'AI 接入配置' }).click()
    const effort = page.getByTestId('reasoning-effort')
    await expect(effort).toContainText('思考强度能力未知')
    await expect(effort).toContainText('已保存思考强度：high')
    await page.getByRole('button', { name: '保存配置', exact: true }).click()
    await expect(page.locator('.el-drawer__footer')).toContainText('思考强度：high')
    expect(requests.find(req => req.path === '/api/v1/ai/config').body.oauth_reasoning_effort).toBe('high')
  })
}

test('news distinguishes AI, fallback and partial coverage while preserving tabs', async ({ page }, testInfo) => {
  await mockNews(page)
  const errors = []
  page.on('pageerror', error => errors.push(error.message))
  await page.goto(`${WEB_URL}/news`)
  await expect(page.getByRole('button', { name: 'AI 接入配置' })).toBeVisible()
  await expect(page.getByRole('tab')).toHaveCount(5)
  await expect(page.locator('.analysis-strip')).toContainText('AI 参与分析')
  await expect(page.locator('.analysis-strip')).toContainText('规则降级')
  await expect(page.locator('.cleaning-progress')).toContainText('25%')
  await expect(page.locator('.summary-details')).not.toHaveAttribute('open')
  await page.locator('.summary-details summary').click()
  await expect(page.getByText('仅使用已处理样本，不代表完整市场。')).toBeVisible()
  await page.locator('.news-details summary').first().click()
  await expect(page.getByRole('link', { name: '查看原文' })).toHaveAttribute('rel', 'noopener noreferrer')
  expect(await page.locator('a[href^="javascript:"]').count()).toBe(0)
  await page.screenshot({ path: testInfo.outputPath('news-desktop.png'), fullPage: true })
  expect(errors).toEqual([])
})

test('configuration saves without returning or resubmitting saved secrets', async ({ page }) => {
  const { requests } = await mockNews(page)
  await page.goto(`${WEB_URL}/news`)
  await page.getByRole('button', { name: 'AI 接入配置' }).click()
  await expect(page.getByPlaceholder('已保存密钥 · 留空保持不变')).toHaveValue('')
  await page.getByRole('button', { name: '保存配置', exact: true }).click()
  await expect(page.getByText('配置已保存。新请求使用新配置')).toBeVisible()
  const saved = requests.find(req => req.path === '/api/v1/ai/config')
  expect(saved.body.api_key).toBeUndefined()
  await page.getByRole('button', { name: '测试已保存配置' }).click()
  await expect(page.getByText('连接成功', { exact: true })).toBeVisible()
})

test('official OAuth entry shows link and updates authorization without auto-switching config', async ({ page }) => {
  const { requests } = await mockNews(page)
  await page.goto(`${WEB_URL}/news`)
  await page.getByRole('button', { name: 'AI 接入配置' }).click()
  await page.locator('.el-radio-button').filter({ hasText: 'OpenAI 账号授权' }).click()
  await page.getByRole('button', { name: '连接 OpenAI 账号', exact: true }).click()
  await expect(page.getByRole('link', { name: '打开 OpenAI 授权页面' })).toBeVisible()
  await expect(page.getByText('已授权', { exact: true })).toBeVisible({ timeout: 10000 })
  expect(requests.some(req => req.path === '/api/v1/ai/config')).toBe(false)
})

test('authorized model picker saves wire model and distinguishes saved from ready', async ({ page }, testInfo) => {
  const { requests } = await mockNews(page, { authorized: true })
  await page.goto(`${WEB_URL}/news`)
  await page.getByRole('button', { name: 'AI 接入配置' }).click()
  await page.locator('.el-radio-button').filter({ hasText: 'OpenAI 账号授权' }).click()
  await page.locator('.el-drawer .el-select').first().click()
  await page.getByRole('option', { name: '目录模型 B · catalog-model-b', exact: true }).click()
  await expect(page.getByText('当前选择尚未保存。', { exact: false })).toBeVisible()
  await page.getByRole('button', { name: '保存配置', exact: true }).click()
  await expect(page.locator('.el-drawer__footer')).toContainText('配置已保存：OpenAI 账号 · catalog-model-b')
  await expect(page.locator('.el-drawer__footer')).toContainText('这不代表保存失败')
  const saved = requests.find(req => req.path === '/api/v1/ai/config')
  expect(saved.body.auth_mode).toBe('openai_oauth')
  expect(saved.body.oauth_model).toBe('catalog-model-b')
  expect(saved.body.model).toBe('test-model')
  expect(saved.body.api_key).toBeUndefined()
  expect(requests.some(req => req.path === '/api/v1/ai/test')).toBe(false)
  await page.locator('.el-drawer .el-select').first().scrollIntoViewIfNeeded()
  await page.screenshot({ path: testInfo.outputPath('oauth-model-saved-not-ready.png'), animations: 'disabled' })
  await page.locator('.el-drawer__close-btn').click()
  await page.getByRole('button', { name: 'AI 接入配置' }).click()
  await expect(page.getByText('已保存模型：catalog-model-b', { exact: false })).toBeVisible()
  await expect.poll(() => requests.filter(req => req.path === '/api/v1/ai/oauth/models').length).toBe(2)
})

test('missing model route still allows custom model and save', async ({ page }) => {
  const { requests } = await mockNews(page, { authorized: true })
  await page.route('**/api/v1/ai/oauth/models', route => route.fulfill({ status: 404, json: { detail: 'Not Found' } }))
  await page.goto(`${WEB_URL}/news`)
  await page.getByRole('button', { name: 'AI 接入配置' }).click()
  await page.locator('.el-radio-button').filter({ hasText: 'OpenAI 账号授权' }).click()
  await expect(page.getByText('模型目录接口尚未加载', { exact: false })).toBeVisible()
  const input = page.locator('.el-drawer .el-select input').first()
  await input.fill('manual-account-model')
  await page.getByRole('option', { name: 'manual-account-model', exact: true }).click()
  await page.getByRole('button', { name: '保存配置', exact: true }).click()
  await expect(page.locator('.el-drawer__footer')).toContainText('配置已保存：OpenAI 账号 · manual-account-model')
  expect(requests.find(req => req.path === '/api/v1/ai/config').body.oauth_model).toBe('manual-account-model')
})

test('OAuth save preserves hidden API settings instead of validating unsaved API draft', async ({ page }) => {
  const { requests } = await mockNews(page, { authorized: true })
  await page.goto(`${WEB_URL}/news`)
  await page.getByRole('button', { name: 'AI 接入配置' }).click()
  await page.getByPlaceholder('https://api.openai.com/v1').fill('')
  await page.getByPlaceholder('填写服务商支持的模型 ID').fill('')
  await page.locator('.el-checkbox').filter({ hasText: '清除已保存密钥' }).click()
  await expect(page.getByRole('checkbox', { name: '清除已保存密钥' })).toBeChecked()
  await page.locator('.el-radio-button').filter({ hasText: 'OpenAI 账号授权' }).click()
  await page.getByRole('button', { name: '保存配置', exact: true }).click()
  await expect(page.locator('.el-drawer__footer')).toContainText('配置已保存：OpenAI 账号 · 账号默认模型')
  const saved = requests.find(req => req.path === '/api/v1/ai/config')
  expect(saved.body.base_url).toBe('https://api.example.com/anthropic')
  expect(saved.body.model).toBe('test-model')
  expect(saved.body.clear_api_key).toBe(false)
  expect(saved.body.api_key).toBeUndefined()
})

test('save error remains visible next to footer action', async ({ page }) => {
  await mockNews(page, { authorized: true })
  await page.route('**/api/v1/ai/config', route => route.fulfill({ status: 503, json: { detail: 'test write failed' } }))
  await page.goto(`${WEB_URL}/news`)
  await page.getByRole('button', { name: 'AI 接入配置' }).click()
  await page.locator('.el-radio-button').filter({ hasText: 'OpenAI 账号授权' }).click()
  await page.getByRole('button', { name: '保存配置', exact: true }).click()
  await expect(page.locator('.el-drawer__footer')).toContainText('配置未保存：服务端配置目录无法写入')
  await expect(page.getByRole('button', { name: '保存配置', exact: true })).toBeEnabled()
})

test('pending OAuth primary action opens official page without restarting authorization', async ({ page, context }, testInfo) => {
  const { requests } = await mockNews(page, { pending: true })
  // Never visit the real identity provider or submit a real OAuth request.
  await context.route('https://auth.openai.com/**', route => route.fulfill({
    contentType: 'text/html', body: '<title>Isolated OAuth navigation test</title>',
  }))
  await page.goto(`${WEB_URL}/news`)
  await page.getByRole('button', { name: 'AI 接入配置' }).click()
  await page.locator('.el-radio-button').filter({ hasText: 'OpenAI 账号授权' }).click()
  await page.getByRole('button', { name: '连接 OpenAI 账号', exact: true }).click()
  const link = page.getByRole('link', { name: '打开 OpenAI 授权页面' })
  await expect(link).toBeVisible()
  await expect(link).toHaveClass(/el-button--primary/)
  await expect(page.getByRole('button', { name: '连接 OpenAI 账号', exact: true })).toHaveCount(0)
  await expect(page.getByText('下一步：打开官方页面完成登录', { exact: true })).toBeVisible()
  const popupPromise = page.waitForEvent('popup')
  await link.click()
  const popup = await popupPromise
  await expect(popup).toHaveURL('https://auth.openai.com/oauth/authorize?state=test')
  expect(await popup.evaluate(() => window.opener === null)).toBe(true)
  await popup.close()
  await page.screenshot({ path: testInfo.outputPath('oauth-pending-primary-action.png'), animations: 'disabled' })
  expect(requests.filter(req => req.path === '/api/v1/ai/oauth/start')).toHaveLength(1)
  expect(requests.some(req => ['/api/v1/ai/config', '/api/v1/ai/oauth/cancel', '/api/v1/ai/test'].includes(req.path))).toBe(false)
})

test('reloaded pending OAuth explains missing link without cancelling user login', async ({ page }) => {
  const { requests } = await mockNews(page, { pending: true })
  await page.goto(`${WEB_URL}/news`)
  await page.getByRole('button', { name: 'AI 接入配置' }).click()
  await page.locator('.el-radio-button').filter({ hasText: 'OpenAI 账号授权' }).click()
  await page.getByRole('button', { name: '连接 OpenAI 账号', exact: true }).click()
  await expect(page.getByRole('link', { name: '打开 OpenAI 授权页面' })).toBeVisible()
  await page.reload()
  await page.getByRole('button', { name: 'AI 接入配置' }).click()
  await page.locator('.el-radio-button').filter({ hasText: 'OpenAI 账号授权' }).click()
  await expect(page.getByText('服务端有一笔未完成的授权', { exact: true })).toBeVisible()
  await expect(page.getByText('当前页面没有授权链接，请回到发起授权的标签页继续；', { exact: false })).toBeVisible()
  await expect(page.getByRole('button', { name: '取消授权', exact: true })).toBeVisible()
  expect(requests.filter(req => req.path === '/api/v1/ai/oauth/start')).toHaveLength(1)
  expect(requests.some(req => req.path === '/api/v1/ai/oauth/cancel')).toBe(false)
})

test('missing OAuth dependency is visible and does not pretend login succeeded', async ({ page }) => {
  await mockNews(page, { missing: true })
  await page.goto(`${WEB_URL}/news`)
  await page.getByRole('button', { name: 'AI 接入配置' }).click()
  await page.locator('.el-radio-button').filter({ hasText: 'OpenAI 账号授权' }).click()
  await expect(page.getByText('前置依赖：服务端安装官方 Codex CLI')).toBeVisible()
  await expect(page.getByRole('button', { name: '连接 OpenAI 账号', exact: true })).toBeDisabled()
})

test('legacy live-shaped status explains backend upgrade instead of missing CLI', async ({ page }) => {
  const { requests } = await mockNews(page)
  await page.route('**/api/v1/ai/status', route => route.fulfill({
    json: { enabled: true, model: 'legacy-model', base_url: 'https://api.example.com', has_key: true },
  }))
  await page.goto(`${WEB_URL}/news`)
  await page.getByRole('button', { name: 'AI 接入配置' }).click()
  await expect(page.getByText('后端尚未加载 AI 配置与 OAuth 接口。', { exact: false })).toBeVisible()
  await page.locator('.el-radio-button').filter({ hasText: 'OpenAI 账号授权' }).click()
  await expect(page.getByText('状态未确认', { exact: true })).toBeVisible()
  await expect(page.getByText('前置依赖：服务端安装官方 Codex CLI')).toHaveCount(0)
  await expect(page.getByRole('button', { name: '连接 OpenAI 账号', exact: true })).toBeDisabled()
  await expect(page.getByRole('button', { name: '保存配置', exact: true })).toBeDisabled()
  await expect(page.getByRole('button', { name: '测试已保存配置' })).toBeDisabled()
  expect(requests.filter(req => req.path.startsWith('/api/v1/ai/') && req.method !== 'GET')).toEqual([])
})

test('status 404 or outage blocks actions without pretending dependency is missing', async ({ page }) => {
  await mockNews(page)
  let status = 404
  await page.route('**/api/v1/ai/status', route => route.fulfill({ status, json: { detail: 'Not Found' } }))
  await page.goto(`${WEB_URL}/news`)
  await page.getByRole('button', { name: 'AI 接入配置' }).click()
  await page.locator('.el-radio-button').filter({ hasText: 'OpenAI 账号授权' }).click()
  await expect(page.getByText('后端尚未加载 AI 配置与 OAuth 接口。', { exact: false })).toBeVisible()
  await expect(page.getByText('前置依赖：服务端安装官方 Codex CLI')).toHaveCount(0)
  await expect(page.getByRole('button', { name: '连接 OpenAI 账号', exact: true })).toBeDisabled()
  status = 503
  await page.locator('.el-drawer').getByRole('button', { name: '刷新状态', exact: true }).click()
  await expect(page.getByText('无法读取 AI 配置，请检查后端连接后重试；', { exact: false })).toBeVisible()
  await expect(page.getByRole('button', { name: '保存配置', exact: true })).toBeDisabled()
  await page.unroute('**/api/v1/ai/status')
  await page.getByRole('button', { name: '重新读取配置', exact: true }).click()
  await expect(page.getByRole('button', { name: '保存配置', exact: true })).toBeEnabled()
  // Reload restores the saved API-key mode; select OAuth again after recovery.
  await page.locator('.el-radio-button').filter({ hasText: 'OpenAI 账号授权' }).click()
  await expect(page.getByRole('button', { name: '连接 OpenAI 账号', exact: true })).toBeEnabled()
})

test('CLI startup failure is not shown as missing installation or authorization success', async ({ page }) => {
  await mockNews(page)
  await page.route('**/api/v1/ai/oauth/start', route => route.fulfill({
    json: { available: true, connected: false, login_status: 'error', message: 'Codex 服务不可用、协议异常或请求超时。' },
  }))
  await page.goto(`${WEB_URL}/news`)
  await page.getByRole('button', { name: 'AI 接入配置' }).click()
  await page.locator('.el-radio-button').filter({ hasText: 'OpenAI 账号授权' }).click()
  await page.getByRole('button', { name: '连接 OpenAI 账号', exact: true }).click()
  await expect(page.getByText('Codex 服务不可用、协议异常或请求超时。', { exact: true })).toBeVisible()
  await expect(page.getByRole('link', { name: '打开 OpenAI 授权页面' })).toHaveCount(0)
  await expect(page.getByText('前置依赖：服务端安装官方 Codex CLI')).toHaveCount(0)
  await expect(page.getByRole('button', { name: '连接 OpenAI 账号', exact: true })).toBeEnabled()
})

test('live backend read-only AI status matches drawer capability', async ({ page }, testInfo) => {
  test.skip(process.env.CLAW_TEST_LIVE_AI !== '1', 'Explicit opt-in: actual AI status, no mutation/model calls')
  await mockNews(page)
  let observed = null
  await page.route('**/api/v1/ai/status', async route => {
    const response = await route.fetch()
    const json = await response.json()
    observed = {
      http: response.status(), auth_mode: json.auth_mode || null,
      api_format: json.api_format || null,
      available: json.oauth?.available ?? null, connected: json.oauth?.connected ?? null,
      login_status: json.oauth?.login_status ?? null,
    }
    await route.fulfill({ response })
  })
  await page.goto(`${WEB_URL}/news`)
  await page.getByRole('button', { name: 'AI 接入配置' }).click()
  await page.locator('.el-radio-button').filter({ hasText: 'OpenAI 账号授权' }).click()
  await expect.poll(() => observed).not.toBeNull()
  await expect(page.locator('.el-drawer .el-loading-mask')).toHaveCount(0)
  const compatible = observed.auth_mode && observed.api_format && typeof observed.available === 'boolean'
  if (!compatible) {
    await expect(page.getByText('后端尚未加载 AI 配置与 OAuth 接口。', { exact: false })).toBeVisible()
    await expect(page.getByRole('button', { name: '连接 OpenAI 账号', exact: true })).toBeDisabled()
    await expect(page.getByText('前置依赖：服务端安装官方 Codex CLI')).toHaveCount(0)
  } else if (['pending', 'starting', 'waiting', 'awaiting_authorization', 'completed'].includes(observed.login_status)) {
    await expect(page.getByText('服务端有一笔未完成的授权', { exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: '连接 OpenAI 账号', exact: true })).toBeDisabled()
  } else if (observed.available && !observed.connected) {
    await expect(page.getByRole('button', { name: '连接 OpenAI 账号', exact: true })).toBeEnabled()
  }
  await testInfo.attach('live-ai-capability', { body: JSON.stringify(observed), contentType: 'application/json' })
  await page.screenshot({ path: testInfo.outputPath('live-ai-config.png'), animations: 'disabled' })
})

test('cancelled cleaning task stops polling and restores action', async ({ page }) => {
  const { requests } = await mockNews(page)
  await page.goto(`${WEB_URL}/news`)
  await page.getByRole('button', { name: '分析待处理新闻（最多 100 条）', exact: true }).click()
  await expect(page.getByText('任务已取消', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '分析待处理新闻（最多 100 条）', exact: true })).toBeEnabled()
  expect(requests.filter(req => req.path.includes('/analysis-jobs/')).length).toBe(1)
})

test('lost progress prevents duplicate submissions and can resume', async ({ page }) => {
  const { requests } = await mockNews(page)
  await page.route('**/api/v1/news/analysis-jobs/job-1', route => route.fulfill({ status: 503, json: {} }))
  await page.goto(`${WEB_URL}/news`)
  await page.getByRole('button', { name: '分析待处理新闻（最多 100 条）', exact: true }).click()
  await expect(page.getByText('进度连接中断，已锁定重复提交')).toBeVisible({ timeout: 12000 })
  await expect(page.getByRole('button', { name: '分析待处理新闻（最多 100 条）', exact: true })).toBeDisabled()
  await page.unroute('**/api/v1/news/analysis-jobs/job-1')
  await page.getByRole('button', { name: '恢复任务进度', exact: true }).click()
  await expect(page.getByText('任务已取消', { exact: true })).toBeVisible()
  expect(requests.filter(req => req.path === '/api/v1/news/analyze-window')).toHaveLength(1)
})

test('empty and failed news states are distinct', async ({ page }) => {
  await mockNews(page, { empty: true })
  await page.goto(`${WEB_URL}/news`)
  await expect(page.getByText('暂无新闻，可手动抓取并清洗')).toBeVisible()
  await page.unroute('**/api/v1/**')
  await mockNews(page, { failure: true })
  await page.reload()
  await expect(page.getByText('加载失败，请刷新重试')).toBeVisible()
})

test('news and configuration stay readable on mobile', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await mockNews(page)
  await page.goto(`${WEB_URL}/news`)
  await expect(page.getByRole('button', { name: 'AI 接入配置' })).toBeVisible()
  const widths = await page.evaluate(() => ({ content: document.documentElement.scrollWidth, viewport: innerWidth }))
  expect(widths.content).toBeLessThanOrEqual(widths.viewport)
  await page.getByRole('button', { name: 'AI 接入配置' }).click()
  await expect(page.getByRole('button', { name: '保存配置', exact: true })).toBeVisible()
  const drawer = page.locator('.el-drawer')
  await expect.poll(async () => Math.round((await drawer.boundingBox()).x)).toBe(0)
  const bounds = await drawer.boundingBox()
  expect(Math.round(bounds.width)).toBe(390)
  await page.screenshot({ path: testInfo.outputPath('news-mobile-config.png'), animations: 'disabled' })
})
