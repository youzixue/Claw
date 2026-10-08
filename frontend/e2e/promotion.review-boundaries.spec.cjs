const { test, expect } = require('@playwright/test')

// Every API request is fulfilled locally. This suite never writes production
// data, starts a server, or claims that the running backend is upgraded.
const WEB_URL = process.env.CLAW_WEB_URL || 'http://127.0.0.1:5173'
const researchRow = {
  code: '600001', name: '隔离研究样本', direction_probability: 0,
  limit_up_probability: 0.02,
  probability_factors: { direction_research: {
    rank_position: 1, probability_method: 'neutral_prior_insufficient_sample',
  } },
}
const research = overrides => ({
  version: 'direction_rank_research_v1', label_version: 'next_day_close_up_v1',
  scope: 'research_only', status: 'insufficient_candidates',
  production_unchanged: true, manual_review_eligible: false,
  candidate_count: 1, eligible_count: 1, missing_probability_count: 0,
  selected_count: 1, rank_limit: 12, target_precision: 0.8,
  frozen: false, persistence_status: 'not_verified', candidates: [researchRow],
  ...overrides,
})
async function open(page, metrics = {}, payload = {}, reviewOverrides = {}) {
  const errors = []
  page.on('pageerror', error => errors.push(error.message))
  const latest = { prediction_trade_date: '2026-09-28', actual_trade_date: '2026-09-29', ...metrics }
  const review = {
    lookback_days: 10, latest, daily: [latest],
    aggregate: { ...latest, launch_precursor_metrics: {
      fixture: { label: '边界证据组', sample_count: 1, ...metrics },
    } },
    ...reviewOverrides,
  }
  await page.route('**/api/v1/**', route => {
    const url = route.request().url()
    return route.fulfill({ json: url.includes('/learning-review') ? review
      : url.includes('/candidates') ? payload : {} })
  })
  await page.goto(WEB_URL + '/promotion')
  return errors
}
async function tab(page, name) {
  await page.getByRole('tab', { name, exact: true }).click()
}
const firstRow = table => table.locator('.el-table__body-wrapper tbody tr').first()

test('risk-annotated market scope never claims restricted names were filtered out', async ({ page }) => {
  await page.route('**/api/v1/**', route => route.fulfill({ json:
    route.request().url().includes('/board-height') ? {
      trade_date: '2026-09-29', status: 'ok', scope: 'non_st_market_with_risk_annotations',
      height: 6, limit_up_count: 56,
      ladder_summary: [{ days: 1, count: 46 }, { days: 2, count: 7 }, { days: 6, count: 3 }],
    } : {},
  }))
  await page.goto(WEB_URL + '/promotion')
  const note = page.getByTestId('market-scope-note')
  await expect(note).toContainText('保留风险标签')
  await expect(note).toContainText('仅观察或受限标的，展示不代表允许交易')
  await expect(note).toContainText('下方复盘按当前风险标签过滤主板首/二板')
  await expect(note).toContainText('不认证历史时点交易资格')
  await expect(note).not.toContainText('该口径过滤 ST/停牌/退市')
})

test('known outcome evidence reasons explain gaps in Chinese and retain audit codes', async ({ page }) => {
  const reasons = [
    'candidate_outcome_bar_missing', 'candidate_outcome_price_chain_discontinuity',
    'outcome_calendar_gap', 'candidate_outcome_source_unverified',
    'candidate_outcome_suspended_or_invalid', 'candidate_outcome_change_invalid',
    'prediction_limit_up_pool_incomplete', 'outcome_limit_up_pool_incomplete',
  ]
  await open(page, { predicted_count: 168, evaluation_status: 'partial', evaluation_reasons: reasons })
  await tab(page, '预测复盘')
  const summary = page.getByTestId('review-directional-latest')
  for (const reason of reasons) await expect(summary).toContainText(reason)
  for (const text of ['行情缺失', '价格链不连续', '日历证据缺失', '来源未验证',
    '停牌或结局行情无效', '涨跌幅无效', '预测日涨停池证据不完整', '结果日涨停池证据不完整']) {
    await expect(summary).toContainText(text)
  }
  await expect(summary).not.toContainText('未识别原因')
})

test('missing formal snapshots never become a zero-score review', async ({ page }) => {
  const missing = {
    predicted_count: 0, snapshot_complete: false,
    evaluation_status: 'unavailable', evaluation_reasons: ['formal_ranked_predictions_unavailable'],
    hit_count: 0, precision: 0, recall: 0, actual_count: 8,
    directional_evaluable_count: 0, directional_unknown_count: 0, directional_coverage: 0,
    predicted_limit_up_hit_count: 0, limit_up_precision: 0, limit_up_recall: 0,
    brier_score: 0,
  }
  const errors = await open(page, { ...missing, lane_metrics: { target_1: { ...missing, target_label: '首板' } } })
  await tab(page, '预测复盘')
  await expect(page.getByTestId('review-first-predicted')).toContainText('暂无记录')
  await expect(page.getByTestId('review-first-actual').locator('strong')).toHaveText('8')
  for (const id of ['review-first-hit', 'review-first-precision', 'review-first-recall']) {
    await expect(page.getByTestId(id).locator('strong')).toHaveText('--')
  }
  for (const id of ['review-directional-latest', 'review-directional-aggregate']) {
    await expect(page.getByTestId(id)).toContainText('-- / 暂无记录')
    await expect(page.getByTestId(id)).toContainText('覆盖率 --')
    await expect(page.getByTestId(id)).not.toContainText('0.0%')
  }
  await expect(page.getByTestId('review-directional-daily')).toContainText('暂无记录')
  await expect(page.getByTestId('review-directional-daily')).not.toContainText('0.0%')
  await expect(page.getByTestId('review-lanes')).toContainText('暂无记录')
  expect(errors).toEqual([])
})

test('partial aggregate keeps 122 of 168 with 46 unknown despite one missing formal day', async ({ page }) => {
  const aggregate = {
    predicted_count: 168, directional_evaluable_count: 122, directional_unknown_count: 46,
    directional_coverage: 122 / 168, directional_observed_precision: 54 / 122,
    directional_precision: null, directional_precision_lower_bound: 54 / 168,
    directional_precision_upper_bound: 100 / 168, directional_target_met: null,
    evaluation_status: 'partial', evaluation_reasons: ['formal_ranked_predictions_unavailable'],
    launch_precursor_metrics: {
      partial: { label: '部分窗口分组', sample_count: 168,
        directional_evaluable_count: 122, directional_unknown_count: 46,
        directional_coverage: 122 / 168, directional_observed_precision: 54 / 122,
        evaluation_reasons: ['formal_ranked_predictions_unavailable'] },
    },
  }
  await open(page, {
    predicted_count: 0, snapshot_complete: false,
    evaluation_reasons: ['formal_ranked_predictions_unavailable'],
  }, {}, { aggregate })
  await tab(page, '预测复盘')
  await expect(page.getByTestId('review-directional-latest')).toContainText('暂无记录')
  const summary = page.getByTestId('review-directional-aggregate')
  await expect(summary).toContainText('122 / 168')
  await expect(summary).toContainText('未知 46')
  await expect(summary).toContainText('覆盖率 72.6%')
  await expect(summary).toContainText('已评价子集上涨率 44.3%')
  await expect(summary).toContainText('完整上涨实际率 --')
  await expect(summary).toContainText('达标未知')
  await expect(summary).not.toContainText('暂无记录')
  const cohort = page.getByTestId('review-directional-cohorts')
  await expect(cohort).toContainText('122 / 168')
  await expect(cohort).toContainText('44.3%')
  await expect(cohort).not.toContainText('暂无记录')
})

test('current-risk limit-up scope never becomes a historical or directional universe claim', async ({ page }) => {
  await open(page, { outcome_universe_scope: 'current_risk_filtered_main_board',
    lane_metrics: { target_1: { actual_count: 41 }, target_2: { actual_count: 6 } } })
  await tab(page, '预测复盘')
  const scope = page.getByTestId('review-limit-up-universe-scope')
  await expect(scope).toContainText('涨停实际首/二板统计按当前风险标签过滤主板')
  await expect(scope).toContainText('当前标签不是历史时点证据')
  await expect(scope).toContainText('上涨方向统计另按主板代码范围与有效结局行情评价')
  await expect(page.getByTestId('review-first-actual').locator('strong')).toHaveText('41')
  await expect(page.getByTestId('review-second-actual').locator('strong')).toHaveText('6')
  await expect(page.getByTestId('review-scope-note')).toContainText('不是不可变证据认证')
})

test('real zero outcomes and zero lift survive while absent snapshot status stays unknown', async ({ page }) => {
  const metrics = {
    predicted_count: 1, directional_evaluable_count: 1, directional_unknown_count: 0,
    directional_coverage: 1, directional_precision: 0, directional_target_met: false,
    directional_lift: 0, brier_score: 0, evaluation_status: 'complete',
    lane_metrics: { target_1: { predicted_count: 1, hit_count: 0, precision: 0, recall: 0 } },
  }
  await open(page, metrics)
  await tab(page, '预测复盘')
  await expect(page.getByText('预测记录状态未知', { exact: true })).toBeVisible()
  await expect(page.getByTestId('review-first-hit').locator('strong')).toHaveText('0')
  await expect(page.getByTestId('review-first-precision').locator('strong')).toHaveText('0.0%')
  await expect(page.getByTestId('review-directional-latest')).toContainText('本样本未达目标')
  await expect(page.getByTestId('review-directional-cohorts')).toContainText('0.00×')
  await expect(page.getByTestId('review-directional-daily')).toContainText('0.000')
})

for (const invalid of [null, '', ' ', false, true, [], {}, 'NaN']) {
  test('strict scalar boundaries: ' + JSON.stringify(invalid), async ({ page }) => {
    await open(page, {
      predicted_count: invalid, directional_evaluable_count: invalid,
      directional_unknown_count: invalid, directional_coverage: invalid,
      directional_precision: 0.6, directional_lift: invalid, brier_score: invalid,
      directional_target_met: 'false', evaluation_status: 'new_unknown_status',
    }, { direction_research: research({
      candidate_count: invalid, eligible_count: invalid, selected_count: invalid,
      missing_probability_count: invalid, missing_recordable_count: invalid, rank_limit: invalid,
    }) })
    await tab(page, '预测复盘')
    const summary = page.getByTestId('review-directional-latest')
    await expect(summary).toContainText('完整上涨实际率 60.0%')
    await expect(summary).toContainText('可评价 / 原预测 -- / --')
    await expect(summary).toContainText('覆盖率 --')
    await expect(summary).toContainText('评价状态未知')
    await expect(summary).toContainText('达标未知')
    await expect(page.getByTestId('review-directional-daily')).not.toContainText('0.000')
    await expect(page.getByTestId('review-directional-cohorts')).not.toContainText('1.00×')
    await tab(page, '研究观察')
    const panel = page.getByTestId('direction-research-panel')
    await expect(panel).toContainText('候选 --')
    await expect(panel).toContainText('已选 -- / --')
    await expect(page.getByTestId('direction-research-recordability-gap')).toHaveText('无法完整记录 --')
  })
}

test('live-shaped evidence blockage remains blocked with Chinese cause and no stale rows', async ({ page }) => {
  const errors = await open(page, {}, { direction_research: research({
    status: 'blocked', reason: 'eligible_candidates_not_recordable',
    candidate_count: 1428, eligible_count: 970, missing_probability_count: 0,
    selected_count: 0, missing_recordable_count: 1, candidates: [researchRow],
  }) })
  await tab(page, '研究观察')
  const panel = page.getByTestId('direction-research-panel')
  await expect(panel).toContainText('研究榜阻塞')
  await expect(panel).toContainText('合资格候选无法完整记录')
  await expect(panel).toContainText('eligible_candidates_not_recordable')
  await expect(panel).toContainText('候选 1428')
  await expect(panel).toContainText('合资格 970')
  await expect(panel).toContainText('缺方向概率 0')
  await expect(panel).toContainText('已选 0 / 12')
  await expect(page.getByTestId('direction-research-recordability-gap')).toHaveText('无法完整记录 1')
  await expect(panel).toContainText('未验证冻结持久化')
  await expect(page.getByTestId('direction-research-table')).not.toContainText('隔离研究样本')
  expect(errors).toEqual([])
})

for (const mismatch of [
  { scope: 'production' }, { version: 'unknown_v2' }, { label_version: 'limit_up' },
  { production_unchanged: false }, { manual_review_eligible: true },
  { production_unchanged: 'true' }, { manual_review_eligible: 'false' },
]) {
  test('incompatible contract cannot claim available: ' + JSON.stringify(mismatch), async ({ page }) => {
    await open(page, {}, { direction_research: research({ status: 'available', ...mismatch }) })
    await tab(page, '研究观察')
    const panel = page.getByTestId('direction-research-panel')
    await expect(panel).toContainText('研究合同缺失或不支持')
    await expect(panel).not.toContainText('研究榜可用')
    await expect(panel).toContainText('候选 --')
    await expect(page.getByTestId('direction-research-table')).not.toContainText('隔离研究样本')
  })
}

for (const field of ['version', 'label_version', 'scope', 'production_unchanged', 'manual_review_eligible']) {
  for (const value of [undefined, null]) {
    test('missing research contract fails closed: ' + field + ':' + String(value), async ({ page }) => {
      await open(page, {}, { direction_research: research({ [field]: value }) })
      await tab(page, '研究观察')
      await expect(page.getByTestId('direction-research-panel')).toContainText('研究合同缺失或不支持')
      await expect(page.getByTestId('direction-research-table')).not.toContainText('隔离研究样本')
    })
  }
}

for (const [label, overrides] of [
  ['null row', { candidates: [researchRow, null], selected_count: 2 }],
  ['empty row', { candidates: [researchRow, {}], selected_count: 2 }],
  ['not an array', { candidates: {}, selected_count: 2 }],
  ['duplicate code', { candidates: [researchRow, researchRow], selected_count: 2 }],
  ['count mismatch', { candidates: [researchRow], selected_count: 2 }],
  ['missing selected count', { selected_count: undefined }],
  ['boolean selected count', { selected_count: true }],
]) {
  test('malformed research blocks entire list without shrinking counts: ' + label, async ({ page }) => {
    await open(page, {}, { direction_research: research({ candidate_count: 1428, eligible_count: 970, ...overrides }) })
    await tab(page, '研究观察')
    const panel = page.getByTestId('direction-research-panel')
    await expect(panel).toContainText('研究榜阻塞 · 响应证据不一致')
    await expect(panel).toContainText('候选 1428')
    await expect(panel).toContainText('合资格 970')
    await expect(page.getByTestId('direction-research-payload-error')).toBeVisible()
    await expect(page.getByTestId('direction-research-table')).not.toContainText('隔离研究样本')
  })
}

for (const [status, reason, label] of [
  ['unavailable', 'direction_research_contract_unsupported', '研究合同版本、标签或范围不受支持'],
  ['blocked', 'direction_research_contract_mismatch', '研究排名、概率或名单分母与原证据不一致'],
]) {
  test('backend proof rejection stays unavailable: ' + reason, async ({ page }) => {
    await open(page, {}, { direction_research: research({ status, reason }) })
    await tab(page, '研究观察')
    const panel = page.getByTestId('direction-research-panel')
    await expect(panel).toContainText(label)
    await expect(panel).toContainText(reason)
    await expect(page.getByTestId('direction-research-table')).not.toContainText('隔离研究样本')
    await expect(page.getByTestId('direction-research-recordability-gap')).toHaveText('无法完整记录 --')
  })
}

test('final-frame health never overrides independent multi-frame and route evidence', async ({ page }) => {
  await open(page, {}, {
    prediction_health: { auction_health: {
      trade_date: '2026-09-28', status: 'ok',
      timely_snapshot_count: 10, latest_code_count: 10,
      feed_complete_count: 10, executable_strong_open_count: 0, strong_open_count: 0,
    } },
    quality_gate: {
      trade_date: '2026-09-29', snapshot_context: 'promotion_0925',
      watermarks: [{ dataset: 'auction_data', trade_date: '2026-09-29', status: 'missing',
        details: { auction_health: { multi_frame_complete_count: 0, latest_code_count: 10 } } }],
      route_gates: { auction_route: { status: 'blocked', gate_passed: false, required_datasets: ['auction_data'] } },
    },
  })
  await tab(page, '研究观察')
  const frame = page.getByTestId('auction-health-strip')
  await expect(frame).toContainText('最终帧检查 通过（仅本项）')
  await expect(frame).toContainText('竞价四字段完整 10/10')
  await expect(frame).toContainText('最终帧统计日 2026-09-28')
  await expect(frame).toContainText('非执行授权')
  const path = page.getByTestId('auction-path-evidence')
  await expect(path).toContainText('2026-09-29 / promotion_0925')
  await expect(path).toContainText('竞价多帧证据 缺失')
  await expect(path).toContainText('多帧完整 0 /')
  await expect(path).toContainText('auction_route：阻断')
  await expect(path).toContainText('未知或异日上下文不合并')
})

test('old unknown auction status and missing counts never imply available or zero', async ({ page }) => {
  await open(page, {}, {
    prediction_health: { auction_health: { trade_date: '2026-09-29', status: 'legacy_unknown',
      timely_snapshot_count: false, feed_complete_count: '', strong_open_count: null } },
    quality_gate: { watermarks: [{ dataset: 'auction_data', status: 'legacy_unknown' }],
      route_gates: { auction_route: { status: 'ok', gate_passed: 'true', required_datasets: ['auction_data'] } } },
  })
  await tab(page, '研究观察')
  const frame = page.getByTestId('auction-health-strip')
  await expect(frame).toContainText('最终帧检查 状态未知')
  await expect(frame).toContainText('及时快照（仅时点） --/--')
  await expect(frame).toContainText('竞价四字段完整 --/--')
  await expect(frame).not.toContainText('0/0')
  const path = page.getByTestId('auction-path-evidence')
  await expect(path).toContainText('竞价多帧证据 状态未知')
  await expect(path).toContainText('多帧完整 -- /')
  await expect(path).toContainText('auction_route：状态未知')
})

test('explicit zero recordability gap differs from old missing field', async ({ page }) => {
  await open(page, {}, { direction_research: research({ missing_recordable_count: 0 }) })
  await tab(page, '研究观察')
  await expect(page.getByTestId('direction-research-recordability-gap')).toHaveText('无法完整记录 0')
})

test('unfrozen insufficient research rows remain observations, tabs remain independent', async ({ page }) => {
  await open(page, {}, { direction_research: research({}) })
  await tab(page, '研究观察')
  const table = page.getByTestId('direction-research-table')
  await expect(firstRow(table)).toContainText('隔离研究样本')
  await expect(firstRow(table).locator('td').nth(2)).toHaveText('0.0%')
  await expect(page.getByTestId('direction-research-persistence')).toContainText('不作合格训练材料')
  await page.locator('summary.research-summary').click()
  await expect(page.getByText('未入池样本 -- 只', { exact: true })).toBeVisible()
  await expect(page.getByText('尚未提供首板未入池诊断', { exact: true })).toBeVisible()
  for (const name of ['市场梯队', '晋级候选', '预测复盘']) {
    await tab(page, name)
    await expect(page.getByTestId('direction-research-panel')).toHaveCount(0)
  }
})
