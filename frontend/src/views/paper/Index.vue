<template>
  <div class="page-container">
    <div class="page-shell paper-page">
      <div class="page-hero">
      <div>
        <h2 class="page-title"><el-icon class="title-icon"><Wallet /></el-icon>模拟盘</h2>
        <div class="page-subtitle">统一查看账户表现、持仓、交易执行与净值曲线</div>
      </div>
      <div class="hero-chip">
        <el-icon><Wallet /></el-icon>
        <span>模拟自动执行 + 手动校验</span>
      </div>
      <el-button v-if="accountName === 'mainline'" data-testid="open-c3-records" @click="activeTab = 'c3-records'">C3 检测记录 · 只读研究</el-button>
      <div class="hero-chip account-switch">
        <el-radio-group v-model="accountName" size="small" @change="onAccountSwitch">
          <el-radio-button value="default">A·高胜率预案</el-radio-button>
          <el-radio-button value="promotion">B·晋级二板</el-radio-button>
          <el-radio-button value="mainline">C·主线扩散</el-radio-button>
          <el-radio-button value="auction">D·竞价强攻</el-radio-button>
          <el-radio-button value="tenbagger">E·高标接力</el-radio-button>
          <el-radio-button value="reversal">F·断板反包</el-radio-button>
        </el-radio-group>
      </div>
    </div>

    <!-- 当前策略辨识度横幅: 切换后一眼确认真的切到了哪个策略 (2026-08-31) -->
    <div class="strategy-banner" :style="{ background: currentStrategyTheme.bg, borderColor: currentStrategyTheme.color }">
      <span class="strategy-badge" :style="{ background: currentStrategyTheme.color }">{{ currentStrategyMeta.short }}</span>
      <div class="strategy-banner-text">
        <strong :style="{ color: currentStrategyTheme.color }">{{ currentStrategyMeta.label }}</strong>
        <span>{{ currentStrategyMeta.desc }}</span>
        <small>当前策略版本：{{ account.strategy_version || '--' }}</small>
      </div>
    </div>

    <el-alert
      class="simulation-guard-alert"
      type="warning"
      :closable="false"
      show-icon
      title="本页全部成交均为本地模拟成交，不连接真实券商，不代表实盘成交。"
    />
    <div class="protocol-start-hint">
      <strong>持续实验协议</strong>
      <span v-if="experimentReport.start_date">授权边界 {{ experimentReport.activation_at || experimentReport.start_date }}（不等于实际扫描开始） · {{ experimentReport.protocol_version || '版本未知' }}<template v-if="experimentReport.first_session_scope === 'afternoon_only'"> · 首日午后样本</template></span>
      <span v-else>启动日期未知，等待实验报告</span>
      <small>收益与胜率只统计当前协议下已平完整轮次；未平仓和控制样本不计入绩效。</small>
    </div>

    <el-tabs v-model="activeTab" @tab-change="onTabChange">
      <el-tab-pane v-if="accountName === 'mainline'" label="C3 检测记录" name="c3-records">
        <C3Records v-if="accountName === 'mainline' && activeTab === 'c3-records'" />
      </el-tab-pane>
      <el-tab-pane label="账户概览" name="account">
        <div class="metrics-panel paper-metrics-panel">
          <div class="stat-row">
            <div class="stat-card paper-stat-card"><div class="metric-head"><el-icon><Money /></el-icon><span>总资产</span></div><div class="stat-value">{{ formatAmount(account.total_assets) }}</div></div>
            <div class="stat-card paper-stat-card"><div class="metric-head"><el-icon><TrendCharts /></el-icon><span>累计收益</span></div><div class="stat-value" :class="changeColorClass(account.total_return)">{{ formatChange(account.total_return) }}</div></div>
            <div class="stat-card paper-stat-card"><div class="metric-head"><el-icon><Bottom /></el-icon><span>最大回撤</span></div><div class="stat-value text-green">{{ account.max_drawdown?.toFixed(2) || '--' }}%</div></div>
            <div class="stat-card paper-stat-card"><div class="metric-head"><el-icon><Bottom /></el-icon><span>当前回撤</span></div><div class="stat-value text-green">{{ account.current_drawdown?.toFixed(2) || '--' }}%</div></div>
            <div class="stat-card paper-stat-card"><div class="metric-head"><el-icon><Trophy /></el-icon><span>历史账本成交胜率</span></div><div class="stat-value">{{ formatRate(account.trade_win_rate ?? account.win_rate) }}</div></div>
            <div class="stat-card paper-stat-card"><div class="metric-head"><el-icon><DataAnalysis /></el-icon><span>历史账本轮次胜率</span></div><div class="stat-value">{{ formatRate(account.round_trip_win_rate) }}</div></div>
            <div class="stat-card paper-stat-card"><div class="metric-head"><el-icon><Money /></el-icon><span>费用拖累</span></div><div class="stat-value">{{ formatMoney(account.fee_drag) }}</div></div>
          </div>
        </div>
        <div class="panel-card accounting-panel" data-testid="account-accounting">
          <div class="panel-title">本账户经济损益 · 不与挑战者合账</div>
          <div class="position-summary">
            <span>今日净值变动 <strong>{{ formatSignedMoney(account.accounting?.today_mtm?.daily_pnl) }} 元</strong></span>
            <span>今日卖出净实现 <strong>{{ formatSignedMoney(account.accounting?.today_realized_net_pnl) }} 元</strong>（持有期）</span>
            <span>累计净已实现 <strong>{{ formatSignedMoney(account.accounting?.realized_net_pnl) }} 元</strong></span>
            <span>持仓净浮盈 <strong>{{ formatSignedMoney(account.accounting?.net_unrealized_pnl) }} 元</strong></span>
            <span>余仓已付买费 <strong>{{ formatMoney(account.accounting?.remaining_entry_fees) }} 元</strong></span>
          </div>
          <p class="chart-caption">{{ account.accounting?.today_mtm?.daily_note || '缺少今日净值变动证据，不以卖出已实现或累计收益代替' }}</p>
          <p class="chart-caption">净浮盈 = gross价差 − 余仓已付买费（未估未来卖费）。累计净已实现包含部分减仓，按完整买卖现金流分摊；不是当前协议绩效。</p>
          <p class="chart-caption">历史账本已实现 {{ formatSignedMoney(account.accounting?.realized_ledger_pnl) }} 元；核验旧买费展示调整 {{ formatSignedMoney(account.accounting?.legacy_entry_fee_adjustment) }} 元；其他待核差额 {{ formatSignedMoney(account.accounting?.unexplained_realized_adjustment) }} 元。仅展示桥接，不改历史金额。</p>
          <p class="chart-caption">本账户完整轮次经济净胜率 {{ formatRate(account.accounting?.closed_cycle_performance?.win_rate) }}，有效 {{ account.accounting?.closed_cycle_performance?.sample_count ?? '--' }} 轮，排除实验标记 {{ account.accounting?.closed_cycle_performance?.excluded_cycle_count ?? '--' }} 轮；成立以来跨版本，仅历史背景，不替代当前协议样本。</p>
          <p class="chart-caption" data-testid="account-reconciliation">现金对账差额 {{ formatSignedMoney(account.accounting?.cash_reconciliation_residual) }} 元；资产对账差额 {{ formatSignedMoney(account.accounting?.asset_reconciliation_residual) }} 元。仅展示不平衡，不重复扣净值。</p>
          <p v-if="account.accounting?.status !== 'ok'" class="chart-caption">经济口径未完整对账：{{ account.accounting?.issues?.join('；') || (account.accounting?.status === 'unreconciled' ? '现金或资产对账不平衡，请核对上列差额；不推定为缺少数据或手续费' : '缺少数据') }}</p>
        </div>
        <div class="panel-card">
          <div class="panel-title"><el-icon><Odometer /></el-icon>净值曲线</div>
          <v-chart :option="navChartOption" style="height: 300px" autoresize />
        </div>
      </el-tab-pane>

      <el-tab-pane label="持续实验" name="experiment">
        <div class="experiment-toolbar">
          <div>
            <div class="panel-title"><el-icon><DataAnalysis /></el-icon>12 账户持续模拟交易实验</div>
            <p>当前协议已平完整轮次是唯一绩效口径；账户成立以来数据仅作历史背景，不与本协议样本混算。</p>
          </div>
          <el-button :loading="experimentLoading" @click="loadExperimentReport">刷新报告</el-button>
        </div>
        <el-alert
          class="experiment-method-alert"
          type="info"
          :closable="false"
          show-icon
          title="市场风格、情绪和牛熊趋势代理均在首次入场时冻结；牛熊代理只使用上日收盘后完整指数日线，是规则研究口径，不是长期行情定论。"
        />
        <div v-loading="experimentLoading" class="experiment-content">
          <div v-if="experimentError" class="panel-card experiment-error-state">
            <el-alert :title="experimentError" type="error" :closable="false" show-icon />
            <el-button @click="loadExperimentReport">重新加载</el-button>
          </div>
          <template v-else>
            <div class="experiment-meta panel-card">
              <div><span>协议版本</span><strong>{{ experimentReport.protocol_version || '未知' }}</strong></div>
              <div><span>协议授权日期</span><strong>{{ experimentReport.start_date || '未知' }}</strong></div>
              <div><span>运行模式</span><strong>{{ experimentReport.mode || '未知' }}</strong></div>
              <div><span>绩效范围</span><strong>当前协议已平完整轮次</strong></div>
              <div><span>生成时间</span><strong>{{ formatComparisonTime(experimentReport.generated_at) }}</strong></div>
            </div>
            <el-empty v-if="!experimentAccounts.length" description="报告暂无账户；不会用 0 收益或 0 胜率填充" />
            <div v-else class="experiment-account-grid">
              <div v-for="item in experimentAccounts" :key="item.account_name" class="panel-card experiment-account-card">
                <div class="experiment-account-head">
                  <div>
                    <strong>{{ item.strategy_label || item.account_name }}</strong>
                    <small>{{ item.account_name }} · {{ item.strategy_version || '版本未知' }}</small>
                  </div>
                  <div class="experiment-tags">
                    <el-tag :type="item.configured === true ? 'success' : item.configured === false ? 'info' : 'warning'" size="small">{{ triStateLabel(item.configured, '策略已配置', '策略未配置') }}</el-tag>
                    <el-tag :type="item.account_created === true ? 'success' : item.account_created === false ? 'info' : 'warning'" size="small">{{ triStateLabel(item.account_created, '账户已创建', '账户未创建') }}</el-tag>
                    <el-tag :type="item.active === true ? 'danger' : item.active === false ? 'info' : 'warning'" size="small">{{ triStateLabel(item.active, '协议已激活', '协议未激活') }}</el-tag>
                    <el-tag :type="item.auto_buy_enabled === true ? 'warning' : item.auto_buy_enabled === false ? 'info' : 'warning'" size="small">{{ triStateLabel(item.auto_buy_enabled, '自动买入开', '自动买入关') }}</el-tag>
                  </div>
                </div>
                <div class="experiment-metrics">
                  <div><span>已平完整轮次</span><strong>{{ countOrUnknown(item.closed_round_trips) }}</strong></div>
                  <div><span>协议净收益</span><strong :class="hasClosedSamples(item) ? changeColorClass(item.net_pnl) : ''">{{ protocolMoney(item) }}</strong></div>
                  <div><span>协议胜率</span><strong>{{ protocolRate(item) }}</strong></div>
                  <div><span>收益因子</span><strong>{{ hasClosedSamples(item) ? formatRatio(item.profit_factor) : sampleState(item) }}</strong></div>
                  <div><span>未平仓轮次</span><strong>{{ countOrUnknown(item.open_round_trips) }}</strong></div>
                  <div><span>审计排除轮次</span><strong>{{ countOrUnknown(item.excluded_round_trips) }}</strong></div>
                </div>
                <div class="lifetime-note">
                  成立以来跨协议指标：本报告不返回、不展示，避免与当前协议混算 ·
                  {{ item.experiment?.sentiment_required === true ? '入场要求有效情绪证据' : item.experiment?.sentiment_required === false ? '入场不强制情绪证据' : '情绪证据要求未知' }} ·
                  {{ item.experiment?.real_order_connected === false ? '未连接实盘' : '连接状态未知' }}
                </div>
                <div class="activity-box">
                  <div class="activity-summary">
                    <span>最近活动 {{ item.latest_activity?.trade_date || '未知日期' }}</span>
                    <strong>扫描 {{ countOrUnknown(item.latest_activity?.scan_count) }} · 决策 {{ countOrUnknown(item.latest_activity?.decision_count) }} · 成交 {{ countOrUnknown(item.latest_activity?.fill_count) }} · 拦截 {{ countOrUnknown(item.latest_activity?.blocked_count) }}</strong>
                  </div>
                  <small v-if="item.latest_activity?.runtime?.last_scan_at">
                    当前版本实际扫描至 {{ formatComparisonTime(item.latest_activity.runtime.last_scan_at) }}；
                    首次允许买入尝试 {{ item.latest_activity.runtime.first_buy_attempt_allowed_scan_at ? formatComparisonTime(item.latest_activity.runtime.first_buy_attempt_allowed_scan_at) : '尚无证据' }}（不代表满足买点或成交）
                  </small>
                  <small v-else>当前版本尚无明确运行心跳；配置开启不等于已实时扫描。</small>
                  <div v-if="item.latest_activity?.top_reasons?.length" class="reason-list">
                    <span v-for="reason in item.latest_activity.top_reasons" :key="`${reason.reason_code}-${reason.reason}`">{{ reason.reason || reason.reason_code || '原因未知' }} × {{ reason.count ?? '未知' }}</span>
                  </div>
                  <small v-else>暂无扫描/未买原因记录</small>
                </div>
                <div class="regime-grid">
                  <div>
                    <strong>市场状态分层</strong>
                    <p v-if="!item.by_entry_regime?.length">无已平样本或入场状态未知</p>
                    <p v-for="row in item.by_entry_regime || []" :key="row.label">{{ row.label || '未知状态' }}：{{ row.sample_count ?? '未知' }} 样本 / {{ row.sample_count ? formatRate(row.win_rate) : '无样本' }} / {{ row.sample_count ? formatMoney(row.net_pnl) : '无样本' }}</p>
                  </div>
                  <div>
                    <strong>入场情绪分层</strong>
                    <p v-if="!item.by_entry_sentiment?.length">无已平样本或入场情绪未知</p>
                    <p v-for="row in item.by_entry_sentiment || []" :key="row.label">{{ row.label || '未知状态' }}：{{ row.sample_count ?? '未知' }} 样本 / {{ row.sample_count ? formatRate(row.win_rate) : '无样本' }} / {{ row.sample_count ? formatMoney(row.net_pnl) : '无样本' }}</p>
                  </div>
                  <div>
                    <strong>牛熊趋势代理分层</strong>
                    <p class="proxy-note">双指数 MA20/MA60 及五日斜率规则；仅研究口径，非长期行情定论。</p>
                    <p v-if="!item.by_entry_bull_bear?.length">无已平样本或入场代理状态未知</p>
                    <p v-for="row in item.by_entry_bull_bear || []" :key="row.label" :title="bullBearDetail(row)">
                      {{ bullBearLabel(row.label) }}：{{ row.sample_count ?? '未知' }} 样本 / {{ row.sample_count ? formatRate(row.win_rate) : '无样本' }} / {{ row.sample_count ? formatMoney(row.net_pnl) : '无样本' }}
                      <small v-if="row.sample_count">95%区间 {{ formatRateInterval(row.win_rate_interval_95) }} · 入场日 {{ entrySessionsText(row.entry_sessions) }}</small>
                    </p>
                  </div>
                </div>
              </div>
            </div>
          </template>
        </div>
      </el-tab-pane>

      <el-tab-pane label="当前持仓" name="positions">
        <div class="panel-card">
          <div class="position-summary">
            <span>持仓 <strong>{{ countOrUnknown(currentPositionSummary.count) }} 只</strong></span>
            <span>数量 <strong>{{ formatTradeShares(currentPositionSummary.shares) }} 股</strong></span>
            <span>总市值 <strong>{{ formatMoney(currentPositionSummary.market_value) }} 元</strong></span>
            <span>浮盈亏 <strong :class="changeColorClass(currentPositionSummary.profit_loss)">{{ formatSignedMoney(currentPositionSummary.profit_loss) }} 元</strong>（未扣费用）</span>
          </div>
          <el-table :data="positions" stripe size="small" empty-text="暂无持仓">
            <el-table-column prop="code" label="代码" width="80">
              <template #default="{ row }"><router-link :to="`/stocks/${row.code}`" class="link">{{ row.code }}</router-link></template>
            </el-table-column>
            <el-table-column prop="name" label="名称" width="80" />
            <el-table-column prop="buy_amount" label="持股数量（股）" min-width="140" align="right">
              <template #default="{ row }">{{ formatTradeShares(row.buy_amount) }}</template>
            </el-table-column>
            <el-table-column label="成交性质" width="92" align="center">
              <template #default><el-tag type="warning" size="small">本地模拟</el-tag></template>
            </el-table-column>
            <el-table-column prop="buy_price" label="模拟买入价" width="104" align="right" />
            <el-table-column prop="current_price" label="模拟现价" width="90" align="right" />
            <el-table-column label="持仓市值（元）" min-width="130" align="right">
              <template #default="{ row }">{{ formatMoney(positionMarketValue(row)) }}</template>
            </el-table-column>
            <el-table-column prop="profit_loss" label="浮盈亏（元）" min-width="120" align="right">
              <template #default="{ row }"><span :class="changeColorClass(row.profit_loss)">{{ formatSignedMoney(row.profit_loss) }}</span></template>
            </el-table-column>
            <el-table-column prop="profit_pct" label="浮盈亏（%）" min-width="130" align="right">
              <template #default="{ row }"><span :class="changeColorClass(row.profit_pct)">{{ row.profit_pct?.toFixed(2) || '--' }}%</span></template>
            </el-table-column>
            <el-table-column label="净浮盈（元）" min-width="135" align="right">
              <template #default="{ row }">{{ formatSignedMoney(row.accounting?.net_unrealized_pnl) }}</template>
            </el-table-column>
            <el-table-column label="余仓买费（元）" min-width="135" align="right">
              <template #default="{ row }">{{ formatMoney(row.accounting?.remaining_entry_fees) }}</template>
            </el-table-column>
            <el-table-column label="持仓生命周期净损益" min-width="180" align="right">
              <template #default="{ row }">{{ formatSignedMoney(row.accounting?.cycle_total_net_pnl) }}</template>
            </el-table-column>
            <el-table-column prop="hold_days" label="持天数" width="70" align="center" />
            <el-table-column prop="stop_loss_price" label="止损价" width="80" align="right" />
            <el-table-column prop="strategy_version" label="建仓策略版本" min-width="210" show-overflow-tooltip />
            <el-table-column prop="buy_reason" label="原因" min-width="150" show-overflow-tooltip />
          </el-table>
        </div>
      </el-tab-pane>

      <el-tab-pane label="交易操作" name="trade">
        <div class="trade-forms">
          <div class="panel-card trade-card buy-card">
            <div class="panel-title text-red"><el-icon><Top /></el-icon>模拟买入</div>
            <el-form :model="buyForm" label-width="90px" size="small">
              <el-form-item label="代码"><el-input v-model="buyForm.code" /></el-form-item>
              <el-form-item label="模拟价格"><el-input-number v-model="buyForm.price" :min="0" :precision="2" /></el-form-item>
              <el-form-item label="数量"><el-input-number v-model="buyForm.amount" :min="100" :step="100" /></el-form-item>
              <el-form-item label="信号ID"><el-input v-model="buyForm.signal_id" /></el-form-item>
              <el-form-item><el-button type="danger" @click="doBuy">确认模拟买入</el-button></el-form-item>
            </el-form>
          </div>
          <div class="panel-card trade-card sell-card">
            <div class="panel-title text-green"><el-icon><Bottom /></el-icon>模拟卖出</div>
            <el-form :model="sellForm" label-width="90px" size="small">
              <el-form-item label="代码"><el-input v-model="sellForm.code" /></el-form-item>
              <el-form-item label="模拟价格"><el-input-number v-model="sellForm.price" :min="0" :precision="2" /></el-form-item>
              <el-form-item label="数量"><el-input-number v-model="sellForm.amount" :min="100" :step="100" /></el-form-item>
              <el-form-item label="原因"><el-input v-model="sellForm.reason" /></el-form-item>
              <el-form-item><el-button type="success" @click="doSell">确认模拟卖出</el-button></el-form-item>
            </el-form>
          </div>
        </div>
      </el-tab-pane>

      <el-tab-pane label="自动执行" name="auto">
        <div class="auto-toolbar">
          <div class="auto-summary">
            <div class="auto-item"><span>执行状态</span><strong :class="autoModeClass">{{ autoModeLabel }}</strong></div>
            <div class="auto-item"><span>当前 / 上限</span><strong>{{ autoStatus.current_positions?.count ?? 0 }} / {{ autoStatus.max_positions || '--' }} 只</strong></div>
            <div class="auto-item"><span>分层仓位</span><strong>{{ positionPolicyText }}</strong></div>
            <div class="auto-item"><span>买点来源</span><strong>{{ autoStatus.signal_policy?.buy_source || '--' }}</strong></div>
            <div class="auto-item auto-item-wide"><span>执行定位</span><strong>{{ autoStatus.signal_policy?.value_entry_guard || '--' }}</strong></div>
            <div class="auto-item auto-item-wide"><span>盘面观察</span><strong>{{ autoStatus.signal_policy?.empty_position_policy || '--' }}</strong></div>
            <div class="auto-item"><span>买入窗口</span><strong>{{ buyWindowText }}</strong></div>
            <div class="auto-item"><span>成交模型</span><strong>{{ executionModelText }}</strong></div>
            <div class="auto-item"><span>最近日终闭环</span><strong :title="autoStatus.daily_outcome?.reason || ''">{{ dailyOutcomeText }}</strong></div>
            <div class="auto-item"><span>今日模拟成交</span><strong>买 {{ autoStatus.today?.buy_executed || 0 }} / 卖 {{ autoStatus.today?.sell_executed || 0 }}</strong></div>
            <div class="auto-item"><span>今日演练候选</span><strong>{{ autoStatus.today?.dry_run_buy_candidates || 0 }} 只 / {{ autoStatus.today?.dry_run_buys || 0 }} 次</strong></div>
            <div class="auto-item"><span>跳过 / 拦截</span><strong>{{ (autoStatus.today?.skipped || 0) + (autoStatus.today?.blocked || 0) }} 条</strong></div>
          </div>
          <div class="auto-actions">
            <el-button :loading="autoRunning" @click="runAuto(false)">演练一次</el-button>
            <el-button type="danger" :loading="autoRunning" @click="runAuto(true)">提交决策（下一轮撮合）</el-button>
          </div>
        </div>
        <div class="panel-card">
          <div class="panel-title"><el-icon><Odometer /></el-icon>买/卖/空仓原因</div>
          <div class="table-scroll-hint">可左右滑动查看完整字段</div>
          <div class="table-scroll">
          <el-table class="auto-log-table" :data="autoLogs" stripe size="small" empty-text="暂无自动执行日志">
            <el-table-column prop="created_at" label="时间" width="156">
              <template #default="{ row }">
                <div class="trade-time">
                  <span>{{ formatTradeDate(row.created_at) }}</span>
                  <strong>{{ formatTradeClock(row.created_at) }}</strong>
                </div>
              </template>
            </el-table-column>
            <el-table-column prop="code" label="代码" width="96" />
            <el-table-column prop="name" label="名称" width="112" />
            <el-table-column prop="signal_source" label="来源" width="132" />
            <el-table-column label="成交性质" width="92" align="center">
              <template #default><el-tag type="warning" size="small">本地模拟</el-tag></template>
            </el-table-column>
            <el-table-column prop="action" label="动作" width="112">
              <template #default="{ row }"><el-tag :type="autoActionType(row.action, row.decision)" size="small">{{ autoActionLabel(row.action, row.decision) }}</el-tag></template>
            </el-table-column>
            <el-table-column prop="price" label="模拟价格" width="96" align="right">
              <template #default="{ row }">{{ row.price ? row.price.toFixed(2) : '--' }}</template>
            </el-table-column>
            <el-table-column prop="amount" label="数量" width="80" align="right" />
            <el-table-column prop="strategy_version" label="策略版本" min-width="210" show-overflow-tooltip />
            <el-table-column prop="quote_round_id" label="行情轮次" min-width="220" show-overflow-tooltip />
            <el-table-column prop="as_of_at" label="共同水位" width="166">
              <template #default="{ row }">{{ row.as_of_at ? String(row.as_of_at).replace('T', ' ') : '--' }}</template>
            </el-table-column>
            <el-table-column v-if="accountName === 'default'" label="确认分层（本条日志时点）" min-width="230">
              <template #default="{ row }">
                <div data-testid="confirmation-evidence">
                  <div>历史路径：{{ confirmationState(row, 'historical_quote_path_confirmed') }}</div>
                  <div>本轮形态：{{ confirmationState(row, 'current_setup_valid') }}</div>
                  <div>执行许可：{{ confirmationState(row, 'execution_permitted') }}</div>
                  <div>订单结果：{{ confirmationOrderResult(row) }}</div>
                </div>
              </template>
            </el-table-column>
            <el-table-column prop="stage_code" label="阶段码" width="104" />
            <el-table-column prop="reason_code" label="原因码" min-width="150" show-overflow-tooltip />
            <el-table-column prop="candidate_score" label="评分" width="80" align="right">
              <template #default="{ row }">{{ row.candidate_score ? row.candidate_score.toFixed(1) : '--' }}</template>
            </el-table-column>
            <el-table-column prop="stop_loss_price" label="止损" width="80" align="right">
              <template #default="{ row }">{{ row.stop_loss_price ? Number(row.stop_loss_price).toFixed(2) : '--' }}</template>
            </el-table-column>
            <el-table-column prop="reason" label="原因" min-width="360" show-overflow-tooltip />
          </el-table>
          </div>
        </div>
        <div class="panel-card">
          <div class="panel-title"><el-icon><DataAnalysis /></el-icon>自动交易评估</div>
          <div class="eval-summary">
            <div class="auto-item"><span>样本数</span><strong>{{ autoEvaluation.overall?.count || 0 }}</strong></div>
            <div class="auto-item"><span>胜率</span><strong>{{ formatRate(autoEvaluation.overall?.win_rate) }}</strong></div>
            <div class="auto-item"><span>平均收益</span><strong :class="changeColorClass(autoEvaluation.overall?.avg_pnl)">{{ formatMoney(autoEvaluation.overall?.avg_pnl) }}</strong></div>
            <div class="auto-item"><span>累计收益</span><strong :class="changeColorClass(autoEvaluation.overall?.total_pnl)">{{ formatMoney(autoEvaluation.overall?.total_pnl) }}</strong></div>
            <div class="auto-item"><span>评估回撤</span><strong class="text-green">{{ formatPct(autoEvaluation.overall?.max_drawdown) }}</strong></div>
            <div class="auto-item"><span>盈亏比</span><strong>{{ formatRatio(autoEvaluation.overall?.payoff_ratio) }}</strong></div>
            <div class="auto-item"><span>期望值</span><strong :class="changeColorClass(autoEvaluation.overall?.expectancy)">{{ formatMoney(autoEvaluation.overall?.expectancy) }}</strong></div>
          </div>
          <el-table :data="evaluationRows" stripe size="small" empty-text="暂无自动交易评估样本">
            <el-table-column prop="label" label="类型" min-width="90" />
            <el-table-column prop="count" label="样本" width="70" align="right" />
            <el-table-column prop="win_rate" label="胜率" width="80" align="right">
              <template #default="{ row }">{{ formatRate(row.win_rate) }}</template>
            </el-table-column>
            <el-table-column prop="avg_pnl" label="均盈亏" width="90" align="right">
              <template #default="{ row }"><span :class="changeColorClass(row.avg_pnl)">{{ formatMoney(row.avg_pnl) }}</span></template>
            </el-table-column>
            <el-table-column prop="total_pnl" label="累计盈亏" width="100" align="right">
              <template #default="{ row }"><span :class="changeColorClass(row.total_pnl)">{{ formatMoney(row.total_pnl) }}</span></template>
            </el-table-column>
            <el-table-column prop="avg_return_pct" label="均收益率" width="90" align="right">
              <template #default="{ row }">{{ formatPct(row.avg_return_pct) }}</template>
            </el-table-column>
            <el-table-column prop="avg_hold_days" label="均持天" width="80" align="right">
              <template #default="{ row }">{{ row.avg_hold_days ?? '--' }}</template>
            </el-table-column>
            <el-table-column prop="payoff_ratio" label="盈亏比" width="80" align="right">
              <template #default="{ row }">{{ formatRatio(row.payoff_ratio) }}</template>
            </el-table-column>
            <el-table-column prop="expectancy" label="期望" width="80" align="right">
              <template #default="{ row }"><span :class="changeColorClass(row.expectancy)">{{ formatMoney(row.expectancy) }}</span></template>
            </el-table-column>
          </el-table>
          <el-table :data="sourceEvaluationRows" stripe size="small" empty-text="暂无来源评估样本" class="source-eval-table">
            <el-table-column prop="label" label="来源" min-width="100" />
            <el-table-column prop="count" label="样本" width="70" align="right" />
            <el-table-column prop="win_rate" label="胜率" width="80" align="right">
              <template #default="{ row }">{{ formatRate(row.win_rate) }}</template>
            </el-table-column>
            <el-table-column prop="total_pnl" label="累计盈亏" width="100" align="right">
              <template #default="{ row }"><span :class="changeColorClass(row.total_pnl)">{{ formatMoney(row.total_pnl) }}</span></template>
            </el-table-column>
            <el-table-column prop="profit_factor" label="收益因子" width="90" align="right">
              <template #default="{ row }">{{ formatRatio(row.profit_factor) }}</template>
            </el-table-column>
            <el-table-column prop="expectancy" label="期望" width="80" align="right">
              <template #default="{ row }"><span :class="changeColorClass(row.expectancy)">{{ formatMoney(row.expectancy) }}</span></template>
            </el-table-column>
          </el-table>
          <div class="mistake-grid">
            <div>
              <div class="mini-title">疑似误买</div>
              <el-table :data="autoEvaluation.mistakes?.wrong_buys || []" stripe size="small" empty-text="暂无样本">
                <el-table-column prop="code" label="代码" width="80" />
                <el-table-column prop="return_pct" label="收益%" width="80" align="right">
                  <template #default="{ row }"><span class="text-green">{{ formatPct(row.return_pct) }}</span></template>
                </el-table-column>
                <el-table-column prop="reason" label="原因" min-width="160" show-overflow-tooltip />
              </el-table>
            </div>
            <div>
              <div class="mini-title">疑似误卖</div>
              <el-table :data="autoEvaluation.mistakes?.wrong_sells || []" stripe size="small" empty-text="暂无样本">
                <el-table-column prop="code" label="代码" width="80" />
                <el-table-column prop="current_profit_pct" label="现浮盈" width="80" align="right">
                  <template #default="{ row }"><span class="text-red">{{ formatPct(row.current_profit_pct) }}</span></template>
                </el-table-column>
                <el-table-column prop="reason" label="原因" min-width="160" show-overflow-tooltip />
              </el-table>
            </div>
          </div>
        </div>
      </el-tab-pane>

      <el-tab-pane label="策略对比" name="challengers">
        <div class="challenger-toolbar">
          <div>
            <div class="panel-title"><el-icon><DataAnalysis /></el-icon>当前策略的隔离验证</div>
            <div class="challenger-subtitle">
              基准策略使用当前 A–F 模拟账户；有撮合账户的候选路线使用独立资金与持仓，仅采证路线则只记录完整分母和前向结果。
            </div>
          </div>
          <div class="challenger-actions">
            <el-radio-group v-model="challengerHorizon" size="small" @change="loadChallengerComparison">
              <el-radio-button :value="1">观察1日</el-radio-button>
              <el-radio-button :value="3">观察3日</el-radio-button>
              <el-radio-button :value="5">观察5日</el-radio-button>
            </el-radio-group>
            <el-button :loading="challengerLoading" @click="loadChallengerComparison">刷新数据</el-button>
          </div>
        </div>
        <el-alert
          class="challenger-alert"
          type="warning"
          :closable="false"
          show-icon
          title="仅做隔离验证：所有路线都不连接真实券商；标注“只采证”的路线不生成任何模拟买单，有撮合账户的路线仍受卖盘、时效、漂移与风控门禁。"
        />

        <CandidateShadowPanel :account-name="accountName" :active="activeTab === 'challengers'" />

        <div v-loading="challengerLoading" class="challenger-content">
          <div v-if="challengerError" class="panel-card challenger-error-state">
            <el-alert :title="challengerError" type="error" :closable="false" show-icon />
            <el-button @click="loadChallengerComparison">重新加载</el-button>
          </div>

          <div v-else class="strategy-coverage-strip" aria-label="六个策略的隔离候选覆盖情况">
            <div
              v-for="item in strategyCoverage"
              :key="item.account_name"
              class="coverage-chip"
              :class="{
                active: item.account_name === accountName,
                configured: Number(item.execution_enabled_route_count || 0) > 0,
                paused: Number(item.execution_paused_route_count || 0) > 0 && Number(item.execution_enabled_route_count || 0) === 0,
                evidence: Number(item.account_backed_route_count || 0) === 0 && Number(item.evidence_only_route_count || 0) > 0,
              }"
              :title="item.reason"
            >
              <strong>{{ item.strategy_code }}</strong>
              <span>{{ coverageExecutionLabel(item) }}</span>
            </div>
            <div class="coverage-total">
              <span>已配置 {{ challengerCoverageSummary.configured || 0 }}/{{ challengerCoverageSummary.total || 6 }}</span>
              <span>· 共 {{ challengerCoverageSummary.total_route_count ?? ((challengerCoverageSummary.route_count || challengerPairs.length || 0) + (challengerCoverageSummary.control_sample_route_count || 0)) }} 条路线</span>
              <span>· 模拟账户 {{ challengerCoverageSummary.account_backed_route_count || 0 }}（开启 {{ challengerCoverageSummary.execution_enabled_route_count || 0 }} / 暂停 {{ challengerCoverageSummary.execution_paused_route_count || 0 }}）</span>
              <span>· 控制样本账户 {{ challengerCoverageSummary.control_sample_account_count || 0 }}</span>
              <span>· 只采证 {{ challengerCoverageSummary.evidence_only_route_count || 0 }}</span>
            </div>
          </div>

          <div v-if="challengerPairs.length > 1" class="panel-card challenger-route-selector">
            <div>
              <strong>当前策略有 {{ challengerPairs.length }} 条独立候选路线</strong>
              <small>每条路线按自己的不可变版本单独累计，不会把 C2 与 C3 样本混在一起。</small>
            </div>
            <el-radio-group v-model="selectedChallengerRouteId" size="small">
              <el-radio-button v-for="pair in challengerPairs" :key="pair.route_id" :value="pair.route_id">
                {{ pair.label }}
              </el-radio-button>
            </el-radio-group>
          </div>

          <div
            v-if="!challengerError && selectedStrategyCoverage && !selectedStrategyCoverage.challenger_configured"
            class="panel-card no-challenger-state"
          >
            <div class="no-challenger-badge">{{ selectedStrategyCoverage.strategy_code }}</div>
            <div>
              <el-tag type="info" size="small">未配置隔离候选策略</el-tag>
              <h3>{{ currentStrategyMeta.label }} 当前没有可对照的候选子账户</h3>
              <p>{{ selectedStrategyCoverage.reason }}</p>
              <small>这里显示“未配置”，不会用空账户的 0% 冒充算法验证结果。</small>
              <div class="no-challenger-metrics">
                <div><span>基准总资产</span><strong>{{ formatAmount(selectedStrategyCoverage.champion?.total_assets) }}</strong></div>
                <div><span>基准成立以来收益</span><strong :class="changeColorClass(selectedStrategyCoverage.champion?.total_return)">{{ formatPct(selectedStrategyCoverage.champion?.total_return) }}</strong></div>
                <div><span>基准最大回撤</span><strong>{{ formatPct(selectedStrategyCoverage.champion?.max_drawdown) }}</strong></div>
                <div><span>基准成交 / 持仓</span><strong>{{ selectedStrategyCoverage.champion?.trade_count || 0 }} / {{ selectedStrategyCoverage.champion?.open_position_count || 0 }}</strong></div>
                <div><span>基准版本</span><strong class="version-text">{{ selectedStrategyCoverage.champion?.strategy_version || '--' }}</strong></div>
              </div>
            </div>
          </div>

          <div
            v-else-if="!challengerError && selectedStrategyCoverage?.control_sample_only"
            class="panel-card no-challenger-state"
          >
            <div class="no-challenger-badge">{{ selectedStrategyCoverage.strategy_code }}2</div>
            <div>
              <el-tag type="success" size="small">控制样本审计已配置</el-tag>
              <h3>{{ selectedStrategyCoverage.challenger?.strategy_label || `${currentStrategyMeta.label} 控制样本` }}</h3>
              <p>{{ selectedStrategyCoverage.reason }}</p>
              <small>控制样本用于可成交性与收盘表现审计，并排除在 Champion/Challenger 绩效外；账户是否实际启动与交易请以“持续实验”报告为准。</small>
              <div class="no-challenger-metrics">
                <div><span>控制样本</span><strong>{{ selectedStrategyCoverage.control_sample_count || 0 }}</strong></div>
                <div><span>已完成结算</span><strong>{{ selectedStrategyCoverage.control_evaluated_count || 0 }}</strong></div>
                <div><span>绩效计入</span><strong>排除</strong></div>
                <div><span>账户运行状态</span><strong>见持续实验</strong></div>
                <div><span>控制版本</span><strong class="version-text">{{ selectedStrategyCoverage.challenger?.strategy_version || '--' }}</strong></div>
              </div>
            </div>
          </div>

          <div
            v-else-if="!challengerError && selectedStrategyCoverage?.challenger?.execution_mode === 'independent_paper_loop'"
            class="panel-card no-challenger-state independent-challenger-state"
          >
            <div class="no-challenger-badge">E2</div>
            <div>
              <el-tag type="warning" size="small">独立模拟买卖链路</el-tag>
              <h3>{{ selectedStrategyCoverage.challenger.strategy_label }}</h3>
              <p>{{ selectedStrategyCoverage.reason }}</p>
              <small>不是只采证账户；入口以当前后端执行版本为准，持仓退出优先使用建仓时冻结参数。触板排队不等于回封路径认证；两者均保留真实盘口、资金仓位及T+1约束。</small>
              <div class="no-challenger-metrics">
                <div><span>模拟账户</span><strong>{{ selectedStrategyCoverage.challenger.account_name }}</strong></div>
                <div><span>自动买入状态</span><strong>{{ selectedStrategyCoverage.challenger_execution_enabled ? '开启' : '未到启动日或维护中' }}</strong></div>
                <div><span>持仓数</span><strong>{{ countOrUnknown(selectedStrategyCoverage.challenger.open_position_count) }}</strong></div>
                <div><span>执行版本</span><strong class="version-text">{{ selectedStrategyCoverage.challenger.strategy_version || '未知' }}</strong></div>
              </div>
              <el-button size="small" @click="activeTab = 'experiment'">查看十二账户协议绩效与未买原因</el-button>
            </div>
          </div>

          <template v-else-if="!challengerError && selectedChallengerPair">
            <div class="panel-card challenger-route-overview">
              <div class="challenger-route-copy">
                <div class="route-kicker">当前候选路线</div>
                <h3>{{ selectedChallengerPair.label }}</h3>
                <p>{{ selectedChallengerPair.hypothesis }}</p>
              </div>
              <div class="route-status">
                <el-tag :type="challengerEvidenceTagType" size="small">{{ challengerEvidenceStatus }}</el-tag>
                <el-tag :type="selectedChallengerPair.challenger?.execution_enabled ? 'warning' : 'info'" size="small">
                  {{ selectedChallengerPair.execution_mode === 'evidence_only' ? '只采证 · 不撮合' : `隔离自动撮合${selectedChallengerPair.challenger?.execution_enabled ? '开启' : '暂停'}` }}
                </el-tag>
                <span>数据生成于 {{ formatComparisonTime(challengerComparison.generated_at) }}</span>
                <span v-if="selectedChallengerPair.challenger?.account_configured">账户编号：基准 #{{ selectedChallengerPair.champion?.id }} / 候选 #{{ selectedChallengerPair.challenger?.id }}</span>
                <span v-else>账户：仅证据台账，未创建候选撮合账户</span>
                <span>路由版本：{{ selectedChallengerPair.evidence?.route_version || '--' }}</span>
                <span>{{ selectedChallengerPair.challenger?.account_configured ? '候选账户版本' : '证据版本' }}：{{ selectedChallengerPair.challenger?.strategy_version || '--' }}</span>
                <span v-if="selectedChallengerPair.activation_date">完整采集起始：{{ selectedChallengerPair.activation_date }}</span>
              </div>
            </div>

            <div class="comparison-metric-grid">
              <div class="comparison-metric">
                <span>基准账户成立以来收益</span>
                <strong :class="changeColorClass(selectedChallengerPair.champion?.total_return)">{{ formatPct(selectedChallengerPair.champion?.total_return) }}</strong>
                <small>{{ selectedChallengerPair.champion?.trade_count || 0 }} 笔成交</small>
              </div>
              <div class="comparison-metric">
                <span>{{ selectedChallengerPair.challenger?.account_configured ? '候选账户成立以来收益' : '候选撮合账户' }}</span>
                <strong v-if="selectedChallengerPair.challenger?.account_configured" :class="changeColorClass(selectedChallengerPair.challenger?.total_return)">{{ formatPct(selectedChallengerPair.challenger?.total_return) }}</strong>
                <strong v-else>未创建</strong>
                <small v-if="selectedChallengerPair.challenger?.account_configured">累计盈亏 {{ formatSignedMoney(challengerTotalPnl) }} 元 · 非今日收益</small>
                <small>{{ selectedChallengerPair.challenger?.account_configured ? `账户跨版本共 ${selectedChallengerPair.challenger?.trade_count || 0} 笔成交` : '只采证，不用 0% 冒充收益' }}</small>
              </div>
              <div class="comparison-metric" :title="selectedChallengerPair.common_period?.note">
                <span>{{ selectedChallengerPair.challenger?.account_configured ? '共同观察期收益差' : '确认相对未确认对照' }}</span>
                <strong :class="changeColorClass(selectedChallengerPair.challenger?.account_configured ? selectedChallengerPair.common_period?.return_delta_pct : selectedChallengerPair.evidence?.confirmed_minus_control_pct)">{{ formatSignedPct(selectedChallengerPair.challenger?.account_configured ? selectedChallengerPair.common_period?.return_delta_pct : selectedChallengerPair.evidence?.confirmed_minus_control_pct) }}</strong>
                <small>{{ selectedChallengerPair.challenger?.account_configured ? commonPeriodText : '同一资格分母，按固定观察期比较' }}</small>
              </div>
              <div class="comparison-metric">
                <span>候选当前持仓</span>
                <strong>{{ selectedChallengerPair.challenger?.account_configured ? `${countOrUnknown(challengerPositionSummary.count)} 只` : '不适用' }}</strong>
                <small>{{ selectedChallengerPair.challenger?.account_configured ? `候选共 ${formatTradeShares(challengerPositionSummary.shares)} 股 · 基准 ${countOrUnknown(selectedChallengerPair.champion?.open_position_count)} 只` : '该路线不生成模拟买单' }}</small>
              </div>
              <div v-if="selectedChallengerPair.challenger?.account_configured" class="comparison-metric daily-pnl-metric">
                <span>候选今日盈亏（元）</span>
                <strong :class="changeColorClass(challengerReturnBreakdown.daily_pnl)">{{ formatSignedMoney(challengerReturnBreakdown.daily_pnl) }}</strong>
                <small>今日收益率 {{ formatSignedPct(challengerReturnBreakdown.daily_return_pct) }} · {{ challengerReturnBreakdown.daily_trade_date || '日期待确认' }}</small>
                <small>{{ challengerReturnBreakdown.daily_note || '缺少日收益口径数据，不以累计收益代替' }}</small>
              </div>
              <div v-if="selectedChallengerPair.challenger?.account_configured" class="comparison-metric position-pnl-metric">
                <span>候选持仓浮盈亏（元）</span>
                <strong :class="changeColorClass(challengerPositionSummary.profit_loss)">{{ formatSignedMoney(challengerPositionSummary.profit_loss) }}</strong>
                <small>相对买入成本的价差 · 未扣费用 · 非今日盈亏</small>
              </div>
              <div class="comparison-metric">
                <span>前向确认信号</span>
                <strong>{{ selectedChallengerPair.event_counts?.confirmed || 0 }}</strong>
                <small>只统计盘中事先确认</small>
              </div>
              <div class="comparison-metric">
                <span>已完成结算样本</span>
                <strong>{{ selectedChallengerPair.evidence?.sample_count || 0 }}</strong>
                <small>{{ challengerHorizon }} 个交易日口径</small>
              </div>
              <div class="comparison-metric" :title="selectedChallengerPair.evidence?.return_basis">
                <span>候选平均净收益</span>
                <strong :class="changeColorClass(selectedChallengerPair.evidence?.avg_net_return_pct)">{{ formatPct(selectedChallengerPair.evidence?.avg_net_return_pct) }}</strong>
                <small>已扣模拟成本</small>
              </div>
              <div class="comparison-metric" :title="selectedChallengerPair.evidence?.benchmark_label">
                <span>相对市场平均超额</span>
                <strong :class="changeColorClass(selectedChallengerPair.evidence?.avg_excess_return_pct)">{{ formatPct(selectedChallengerPair.evidence?.avg_excess_return_pct) }}</strong>
                <small>相对全市场等权平均</small>
              </div>
              <div v-if="selectedChallengerPair.evidence?.control_basis" class="comparison-metric" :title="selectedChallengerPair.evidence.control_basis">
                <span>已结算未确认对照</span>
                <strong>{{ selectedChallengerPair.evidence?.control_sample_count || 0 }}</strong>
                <small>不按最终是否涨停筛选</small>
              </div>
            </div>

            <div class="panel-card evidence-card">
              <div class="evidence-head">
                <div>
                  <div class="panel-title">前向验证流水线</div>
                  <p>{{ challengerComparison.evidence_purpose?.does || '记录盘中事先确认的信号，并在固定观察期后结算收益。' }}</p>
                </div>
                <strong>已结算 {{ settledEvidenceProgress(selectedChallengerPair) }}%</strong>
              </div>
              <div class="evidence-purpose-box">
                <div><strong>它回答什么</strong><span>{{ challengerComparison.evidence_purpose?.question }}</span></div>
                <div><strong>它不会做什么</strong><span>{{ challengerComparison.evidence_purpose?.does_not }}</span></div>
              </div>
              <div class="evidence-progress-stack">
                <div class="evidence-progress-row">
                  <div><span>① 前向信号采集</span><small>确认信号与确认交易日同时达标</small></div>
                  <strong>{{ collectionEvidenceProgress(selectedChallengerPair) }}%</strong>
                </div>
                <el-progress :percentage="collectionEvidenceProgress(selectedChallengerPair)" :stroke-width="9" :show-text="false" color="#7c3aed" />
                <div class="evidence-progress-row settled-row">
                  <div><span>② 到期结算验收</span><small>只有观察期结束的样本才进入收益与回撤门槛</small></div>
                  <strong>{{ settledEvidenceProgress(selectedChallengerPair) }}%</strong>
                </div>
                <el-progress :percentage="settledEvidenceProgress(selectedChallengerPair)" :stroke-width="9" :show-text="false" />
              </div>
              <el-alert
                class="settlement-hint"
                :title="evidenceSettlementHint"
                type="info"
                :closable="false"
                show-icon
              />
              <div v-if="selectedCurrentPool" class="same-day-funnel-box">
                <div><span>当日累计确认（不可变证据）</span><strong>{{ selectedCurrentPool.cumulative_confirmed_today }}</strong></div>
                <div><span>当前有效结构 / 资格 / 确认</span><strong>{{ selectedCurrentPool.structural_count }} / {{ selectedCurrentPool.eligible_count }} / {{ selectedCurrentPool.confirmed_count }}</strong></div>
                <p>全市场 C3 · 仅影子观察 · {{ selectedCurrentPool.read_model_version }}</p>
                <p>as_of {{ selectedCurrentPool.as_of || '今日尚无扫描' }} · 过期时间 {{ selectedCurrentPool.expires_at || '—' }}</p>
                <p>行情覆盖 {{ selectedCurrentPool.coverage?.valid_quote_count ?? '—' }}/{{ selectedCurrentPool.coverage?.allowed_count ?? '—' }} · {{ selectedCurrentPool.expired ? '已过期' : selectedCurrentPool.valid ? '有效' : '失效' }}：{{ selectedCurrentPool.reason }}</p>
                <p>板块映射覆盖 {{ selectedCurrentPool.coverage?.context_audit?.mapping_coverage ?? '—' }} · 当日板块截面覆盖 {{ selectedCurrentPool.coverage?.context_audit?.persistence_context_coverage ?? '—' }}（比例0–1）</p>
                <p>{{ selectedCurrentPool.note }}</p>
                <p class="table-scroll-hint">可左右滑动查看完整字段</p>
                <div class="table-scroll current-pool-table-wrap">
                  <el-table class="current-pool-table" :data="selectedCurrentPool.members || []" size="small" max-height="240" empty-text="当前无有效候选；不以昨日榜单或当日累计补齐">
                    <el-table-column prop="code" label="代码" width="110" />
                    <el-table-column prop="name" label="名称" width="110" />
                    <el-table-column label="当前层级" width="100"><template #default="{ row }">{{ row.currently_confirmed ? '连续确认' : row.eligible ? '资格' : '结构' }}</template></el-table-column>
                    <el-table-column prop="reason" label="当前原因" min-width="200" show-overflow-tooltip />
                  </el-table>
                </div>
                <p v-for="item in (selectedCurrentPool.invalidated || []).slice(0, 20)" :key="item.code">{{ item.code }} 已退出：{{ item.reason }}</p>
              </div>
              <div class="evidence-gate-title">当前路线版本历史累计（非当前有效池）</div>
              <div class="evidence-stage-grid">
                <div><span>结构候选</span><strong>{{ selectedChallengerPair.event_counts?.structural_pool || 0 }}</strong></div>
                <div><span>形态满足</span><strong>{{ selectedChallengerPair.event_counts?.eligible || 0 }}</strong></div>
                <div><span>盘中确认</span><strong>{{ selectedChallengerPair.event_counts?.confirmed || 0 }}</strong></div>
                <div v-if="selectedChallengerPair.evidence?.control_basis"><span>未确认对照</span><strong>{{ selectedChallengerPair.event_counts?.control || 0 }}</strong></div>
                <div><span>确认信号交易日</span><strong>{{ selectedChallengerPair.evidence?.confirmed_signal_sessions || 0 }}/{{ selectedChallengerPair.evidence?.minimum_required_sessions || 20 }}</strong></div>
                <div><span>等待到期结算</span><strong>{{ selectedChallengerPair.evidence?.pending_settlement_count || 0 }}</strong></div>
                <div><span>已结算独立交易日</span><strong>{{ selectedChallengerPair.evidence?.independent_sessions || 0 }}/{{ selectedChallengerPair.evidence?.minimum_required_sessions || 20 }}</strong></div>
                <div><span>已结算样本</span><strong>{{ selectedChallengerPair.evidence?.sample_count || 0 }}/{{ selectedChallengerPair.evidence?.minimum_required_samples || 100 }}</strong></div>
              </div>
              <div v-if="selectedChallengerPair.evidence?.control_basis" class="control-evidence-box">
                <div>
                  <span>未确认对照平均净收益</span>
                  <strong :class="changeColorClass(selectedChallengerPair.evidence?.control_avg_net_return_pct)">{{ formatPct(selectedChallengerPair.evidence?.control_avg_net_return_pct) }}</strong>
                </div>
                <div>
                  <span>确认信号相对对照提升</span>
                  <strong :class="changeColorClass(selectedChallengerPair.evidence?.confirmed_minus_control_pct)">{{ formatSignedPct(selectedChallengerPair.evidence?.confirmed_minus_control_pct) }}</strong>
                </div>
                <p>{{ selectedChallengerPair.evidence.control_basis }}</p>
              </div>
              <div v-if="selectedChallengerPair.execution_mode === 'evidence_only'" class="same-day-funnel-box">
                <div>
                  <span>已归档收盘结果</span>
                  <strong>{{ selectedChallengerPair.same_day_funnel?.outcome_count || 0 }}</strong>
                </div>
                <div>
                  <span>确认后封住首板</span>
                  <strong>{{ selectedChallengerPair.same_day_funnel?.confirmed_first_board_count || 0 }}</strong>
                </div>
                <div>
                  <span>未确认但最终首板</span>
                  <strong>{{ selectedChallengerPair.same_day_funnel?.unconfirmed_first_board_count || 0 }}</strong>
                </div>
                <p>{{ selectedChallengerPair.same_day_funnel?.note }}</p>
              </div>
              <div class="evidence-gate-title">统计证据验收清单（仅使用当前路由版本的到期结算数据）</div>
              <div class="evidence-gate-grid">
                <div
                  v-for="item in evidenceGateRows"
                  :key="item.key"
                  class="evidence-gate-item"
                  :class="item.notApplicable ? 'not-applicable' : item.passed ? 'passed' : 'pending'"
                >
                  <el-tag :type="item.passed ? 'success' : 'info'" size="small">
                    {{ item.notApplicable ? '不适用' : item.passed ? '通过' : '未通过' }}
                  </el-tag>
                  <div><span>{{ item.label }}</span><strong>{{ item.value }}</strong></div>
                  <small>{{ item.threshold }}</small>
                </div>
              </div>
              <div class="execution-guardrail-box">
                <el-tag
                  :type="selectedChallengerPair.evidence?.execution_guardrails?.account_drawdown_applicable === false ? 'info' : selectedChallengerPair.evidence?.execution_guardrails?.account_drawdown_acceptable ? 'success' : 'danger'"
                  size="small"
                >
                  {{ selectedChallengerPair.evidence?.execution_guardrails?.tag_only ? '历史回撤仅作实验标签' : selectedChallengerPair.evidence?.execution_guardrails?.account_drawdown_applicable === false ? '执行护栏不适用' : selectedChallengerPair.evidence?.execution_guardrails?.account_drawdown_acceptable ? '执行护栏通过' : '执行护栏阻断' }}
                </el-tag>
                <div>
                  <span>候选账户成立以来跨版本最大回撤</span>
                  <strong>{{ selectedChallengerPair.evidence?.execution_guardrails?.account_max_drawdown_pct == null ? '不适用' : formatPct(selectedChallengerPair.evidence.execution_guardrails.account_max_drawdown_pct) }}</strong>
                </div>
                <small>参考阈值 {{ formatPct(selectedChallengerPair.evidence?.execution_guardrails?.maximum_account_drawdown_pct) }}；这是跨版本账户风控事实，不计入当前路由版本的统计证据门槛。{{ selectedChallengerPair.evidence?.execution_guardrails?.tag_only ? "持续实验不因此关停买入，资金与交易硬约束仍保留。" : "" }}</small>
              </div>
            </div>

            <div class="challenger-account-grid">
              <div class="panel-card account-compare-card champion-card">
                <div class="compare-card-head">
                  <div><span>基准模拟账户</span><strong>{{ selectedChallengerPair.champion?.strategy_label }}</strong></div>
                  <el-tag :type="selectedChallengerPair.champion?.auto_buy_enabled ? 'danger' : 'info'" size="small">{{ selectedChallengerPair.champion?.auto_buy_enabled ? '自动买入开启' : '自动买入暂停' }}</el-tag>
                </div>
                <div class="compare-metrics">
                  <div><span>总资产</span><strong>{{ formatAmount(selectedChallengerPair.champion?.total_assets) }}</strong></div>
                  <div><span>成立以来收益</span><strong :class="changeColorClass(selectedChallengerPair.champion?.total_return)">{{ formatPct(selectedChallengerPair.champion?.total_return) }}</strong></div>
                  <div><span>最大回撤</span><strong class="text-green">{{ formatPct(selectedChallengerPair.champion?.max_drawdown) }}</strong></div>
                  <div><span>成交 / 持仓</span><strong>{{ selectedChallengerPair.champion?.trade_count || 0 }} / {{ selectedChallengerPair.champion?.open_position_count || 0 }}</strong></div>
                  <div><span>策略版本</span><strong class="version-text">{{ selectedChallengerPair.champion?.strategy_version || '--' }}</strong></div>
                </div>
              </div>
              <div v-if="selectedChallengerPair.challenger?.account_configured" class="panel-card account-compare-card challenger-card">
                <div class="compare-card-head">
                  <div><span>隔离候选账户</span><strong>{{ selectedChallengerPair.challenger?.strategy_label }}</strong></div>
                  <el-tag :type="selectedChallengerPair.challenger?.execution_enabled ? 'warning' : 'info'" size="small">
                    {{ selectedChallengerPair.challenger?.execution_enabled ? '本地模拟撮合开启' : '仅采集证据 / 撮合暂停' }}
                  </el-tag>
                </div>
                <div class="compare-metrics">
                  <div><span>总资产</span><strong>{{ formatAmount(selectedChallengerPair.challenger?.total_assets) }}</strong></div>
                  <div><span>成立以来跨版本收益</span><strong :class="changeColorClass(selectedChallengerPair.challenger?.total_return)">{{ formatPct(selectedChallengerPair.challenger?.total_return) }}</strong></div>
                  <div><span>账户累计盈亏（元）</span><strong :class="changeColorClass(challengerTotalPnl)">{{ formatSignedMoney(challengerTotalPnl) }}</strong></div>
                  <div><span>累计已实现账本值（元，旧值可能漏买费）</span><strong :class="changeColorClass(challengerRealizedPnl)">{{ formatSignedMoney(challengerRealizedPnl) }}</strong></div>
                  <div data-testid="challenger-net-realized"><span>累计净已实现（含部分减仓）</span><strong>{{ formatSignedMoney(selectedChallengerPair.challenger?.accounting?.realized_net_pnl) }}</strong></div>
                  <div data-testid="challenger-net-unrealized"><span>持仓净浮盈（扣已付买费）</span><strong>{{ formatSignedMoney(selectedChallengerPair.challenger?.accounting?.net_unrealized_pnl) }}</strong></div>
                  <div><span>今日卖出净实现（非今日净值变动）</span><strong>{{ formatSignedMoney(selectedChallengerPair.challenger?.accounting?.today_realized_net_pnl) }}</strong></div>
                  <div data-testid="challenger-reconciliation"><span>现金 / 资产对账差额（仅展示，不重复扣净值）</span><strong>{{ formatSignedMoney(selectedChallengerPair.challenger?.accounting?.cash_reconciliation_residual) }} / {{ formatSignedMoney(selectedChallengerPair.challenger?.accounting?.asset_reconciliation_residual) }}</strong></div>
                  <div v-if="selectedChallengerPair.challenger?.accounting?.status !== 'ok'"><span>经济口径未完整对账</span><strong>{{ selectedChallengerPair.challenger?.accounting?.issues?.join('；') || (selectedChallengerPair.challenger?.accounting?.status === 'unreconciled' ? '现金或资产对账不平衡，请核对差额；不推定为缺少数据或手续费' : '缺少数据') }}</strong></div>
                  <div><span>旧买费只读调整 / 其他待核差额</span><strong>{{ formatSignedMoney(selectedChallengerPair.challenger?.accounting?.legacy_entry_fee_adjustment) }} / {{ formatSignedMoney(selectedChallengerPair.challenger?.accounting?.unexplained_realized_adjustment) }}</strong></div>
                  <div><span>累计已付费用（元，已计入收益）</span><strong>{{ formatMoney(challengerFeesPaid) }}</strong></div>
                  <div><span>成立以来跨版本最大回撤</span><strong class="text-green">{{ formatPct(selectedChallengerPair.challenger?.max_drawdown) }}</strong></div>
                  <div><span>当前版本成交 / 持仓</span><strong>{{ selectedChallengerPair.challenger?.current_version_execution?.trade_count || 0 }} / {{ selectedChallengerPair.challenger?.current_version_execution?.open_position_count || 0 }}</strong></div>
                  <div><span>当前版本已实现盈亏</span><strong :class="changeColorClass(selectedChallengerPair.challenger?.current_version_execution?.realized_pnl)">{{ formatAmount(selectedChallengerPair.challenger?.current_version_execution?.realized_pnl) }}</strong></div>
                  <div><span>策略版本</span><strong class="version-text">{{ selectedChallengerPair.challenger?.strategy_version || '--' }}</strong></div>
                </div>
              </div>
              <div v-else class="panel-card account-compare-card evidence-only-card">
                <div class="compare-card-head">
                  <div><span>前向证据台账</span><strong>{{ selectedChallengerPair.challenger?.strategy_label }}</strong></div>
                  <el-tag type="info" size="small">永不撮合</el-tag>
                </div>
                <div class="compare-metrics">
                  <div><span>结构候选</span><strong>{{ selectedChallengerPair.event_counts?.structural_pool || 0 }}</strong></div>
                  <div><span>资格候选</span><strong>{{ selectedChallengerPair.event_counts?.eligible || 0 }}</strong></div>
                  <div><span>未确认对照</span><strong>{{ selectedChallengerPair.event_counts?.control || 0 }}</strong></div>
                  <div><span>模拟成交 / 持仓</span><strong>不适用</strong></div>
                  <div><span>证据版本</span><strong class="version-text">{{ selectedChallengerPair.challenger?.strategy_version || '--' }}</strong></div>
                </div>
              </div>
            </div>

            <div v-if="selectedChallengerPair.challenger?.account_configured" class="panel-card">
              <div class="panel-title"><el-icon><Odometer /></el-icon>双方净值曲线</div>
              <div class="chart-caption">曲线来自各自模拟账户净值；公平收益差另按共同起始日归一化计算。</div>
              <v-chart :option="challengerNavChartOption" style="height: 300px" autoresize />
            </div>

            <div v-if="selectedChallengerPair.challenger?.account_configured" class="panel-card">
              <div class="panel-title">隔离候选账户当前持仓</div>
              <div class="position-summary challenger-position-summary">
                <span>持仓 <strong>{{ countOrUnknown(challengerPositionSummary.count) }} 只</strong></span>
                <span>数量 <strong>{{ formatTradeShares(challengerPositionSummary.shares) }} 股</strong></span>
                <span>总市值 <strong>{{ formatMoney(challengerPositionSummary.market_value) }} 元</strong></span>
                <span>浮盈亏 <strong :class="changeColorClass(challengerPositionSummary.profit_loss)">{{ formatSignedMoney(challengerPositionSummary.profit_loss) }} 元</strong></span>
                <span>盈利 {{ countOrUnknown(challengerPositionSummary.winners) }} 只 · 亏损 {{ countOrUnknown(challengerPositionSummary.losers) }} 只 · 持平 {{ countOrUnknown(challengerPositionSummary.flat) }} 只</span>
              </div>
              <div class="chart-caption">浮盈亏为gross价差；净浮盈再扣余仓已付买费，不预扣未来卖费。生命周期净损益另含该仓此前减仓净实现。缺少买入链显示 --；三者均非今日净值变动。</div>
              <div class="table-scroll-hint">可左右滑动查看完整字段</div>
              <div class="table-scroll">
                <el-table class="challenger-position-table" :data="selectedChallengerPair.challenger?.positions || []" stripe size="small" empty-text="暂无隔离持仓">
                  <el-table-column prop="code" label="股票代码" min-width="96" />
                  <el-table-column prop="name" label="股票名称" min-width="108" />
                  <el-table-column prop="buy_amount" label="持股数量（股）" min-width="140" align="right">
                    <template #default="{ row }">{{ formatTradeShares(row.buy_amount) }}</template>
                  </el-table-column>
                  <el-table-column label="成交性质" min-width="92" align="center">
                    <template #default><el-tag type="warning" size="small">本地模拟</el-tag></template>
                  </el-table-column>
                  <el-table-column prop="buy_price" label="模拟买入价" min-width="104" align="right" />
                  <el-table-column prop="current_price" label="模拟现价" min-width="92" align="right" />
                  <el-table-column label="持仓市值（元）" min-width="130" align="right">
                    <template #default="{ row }">{{ formatMoney(positionMarketValue(row)) }}</template>
                  </el-table-column>
                  <el-table-column prop="profit_loss" label="浮盈亏（元）" min-width="120" align="right">
                    <template #default="{ row }"><span :class="changeColorClass(row.profit_loss)">{{ formatSignedMoney(row.profit_loss) }}</span></template>
                  </el-table-column>
                  <el-table-column prop="profit_pct" label="浮盈亏（%）" min-width="130" align="right">
                    <template #default="{ row }"><span :class="changeColorClass(row.profit_pct)">{{ formatPct(row.profit_pct) }}</span></template>
                  </el-table-column>
                  <el-table-column label="净浮盈（元）" min-width="135" align="right">
                    <template #default="{ row }">{{ formatSignedMoney(row.accounting?.net_unrealized_pnl) }}</template>
                  </el-table-column>
                  <el-table-column label="余仓买费（元）" min-width="135" align="right">
                    <template #default="{ row }">{{ formatMoney(row.accounting?.remaining_entry_fees) }}</template>
                  </el-table-column>
                  <el-table-column label="持仓生命周期净损益" min-width="180" align="right">
                    <template #default="{ row }">{{ formatSignedMoney(row.accounting?.cycle_total_net_pnl) }}</template>
                  </el-table-column>
                  <el-table-column prop="hold_days" label="持仓交易日" min-width="104" align="center" />
                  <el-table-column prop="stop_loss_price" label="止损参考价" min-width="104" align="right" />
                  <el-table-column prop="strategy_version" label="建仓策略版本" min-width="240" show-overflow-tooltip />
                  <el-table-column prop="buy_reason" label="入场依据" min-width="300" show-overflow-tooltip />
                </el-table>
              </div>
            </div>
          </template>

          <div v-if="!challengerError && selectedChallengerPair" class="challenger-detail-stack">
            <div class="panel-card">
              <div class="panel-title">最近的前向确认与收益结算</div>
              <div class="table-scroll-hint">可左右滑动查看完整字段</div>
              <div class="table-scroll">
                <el-table class="challenger-signal-table" :data="filteredChallengerSignals" stripe size="small" empty-text="当前策略尚无前向确认事件">
                  <el-table-column prop="observed_at" label="盘中确认时间" min-width="168">
                    <template #default="{ row }">{{ formatComparisonTime(row.observed_at) }}</template>
                  </el-table-column>
                  <el-table-column label="股票" min-width="132">
                    <template #default="{ row }">
                      <div class="challenger-stock-cell"><strong>{{ row.code || '--' }}</strong><span>{{ row.name || '--' }}</span></div>
                    </template>
                  </el-table-column>
                  <el-table-column prop="event_type" label="事件状态" min-width="106">
                    <template #default="{ row }"><el-tag :type="challengerEventType(row.event_type)" size="small">{{ challengerEventLabel(row.event_type) }}</el-tag></template>
                  </el-table-column>
                  <el-table-column prop="route_version" label="路由版本" min-width="230" show-overflow-tooltip />
                  <el-table-column prop="assumed_fill_price" label="确认参考价" min-width="106" align="right">
                    <template #default="{ row }">{{ formatTradePrice(row.assumed_fill_price) }}</template>
                  </el-table-column>
                  <el-table-column label="持有期净收益" min-width="122" align="right">
                    <template #default="{ row }">
                      <div class="return-cell">
                        <span :class="changeColorClass(signalEvaluation(row)?.net_return_pct)">{{ formatPct(signalEvaluation(row)?.net_return_pct) }}</span>
                        <small>{{ challengerHorizon }} 个交易日</small>
                      </div>
                    </template>
                  </el-table-column>
                  <el-table-column label="最大不利波动" min-width="118" align="right">
                    <template #default="{ row }"><span class="text-green">{{ formatPct(signalEvaluation(row)?.max_adverse_pct) }}</span></template>
                  </el-table-column>
                </el-table>
              </div>
            </div>
            <div v-if="selectedChallengerPair.challenger?.account_configured" class="panel-card">
              <div class="panel-title">隔离候选账户执行日志</div>
              <div class="table-scroll-hint">可左右滑动查看完整字段</div>
              <div class="table-scroll">
                <el-table class="challenger-action-table" :data="filteredChallengerActions" stripe size="small" empty-text="当前策略尚无模拟执行日志">
                  <el-table-column prop="created_at" label="执行时间" min-width="168">
                    <template #default="{ row }">{{ formatComparisonTime(row.created_at) }}</template>
                  </el-table-column>
                  <el-table-column label="股票" min-width="132">
                    <template #default="{ row }">
                      <div class="challenger-stock-cell"><strong>{{ row.code || '--' }}</strong><span>{{ row.name || '--' }}</span></div>
                    </template>
                  </el-table-column>
                  <el-table-column label="成交性质" min-width="92" align="center">
                    <template #default><el-tag type="warning" size="small">本地模拟</el-tag></template>
                  </el-table-column>
                  <el-table-column prop="action" label="处理结果" min-width="104">
                    <template #default="{ row }"><el-tag :type="autoActionType(row.action, row.decision)" size="small">{{ autoActionLabel(row.action, row.decision) }}</el-tag></template>
                  </el-table-column>
                  <el-table-column prop="strategy_version" label="策略版本" min-width="240" show-overflow-tooltip />
                  <el-table-column prop="price" label="模拟价格" min-width="100" align="right">
                    <template #default="{ row }">{{ formatTradePrice(row.price) }}</template>
                  </el-table-column>
                  <el-table-column prop="amount" label="数量" min-width="90" align="right">
                    <template #default="{ row }">{{ formatTradeShares(row.amount) }}</template>
                  </el-table-column>
                  <el-table-column prop="reason" label="处理原因" min-width="340" show-overflow-tooltip>
                    <template #default="{ row }">{{ humanizeChallengerText(row.reason) }}</template>
                  </el-table-column>
                </el-table>
              </div>
            </div>
          </div>
        </div>
      </el-tab-pane>

      <el-tab-pane label="交易记录" name="trades">
        <div class="panel-card trade-record-card">
          <p class="chart-caption">本笔费用 = 佣金 + 印花税；卖出净实现另含分摊的已付买费，属于持有期而非当日涨跌。原买入行的毛浮盈是当前同股余仓口径，不能逐行相加；原账本字段不改写。</p>
          <div class="table-scroll-hint">可左右滑动查看完整字段</div>
          <div class="table-scroll">
          <el-table class="trade-record-table" :data="trades" stripe size="small" empty-text="暂无记录" style="width: 100%">
            <el-table-column prop="trade_time" label="时间" width="156">
              <template #default="{ row }">
                <div class="trade-time">
                  <span>{{ formatTradeDate(row.trade_time) }}</span>
                  <strong>{{ formatTradeClock(row.trade_time) }}</strong>
                </div>
              </template>
            </el-table-column>
            <el-table-column prop="code" label="代码" width="96">
              <template #default="{ row }"><span class="trade-code">{{ row.code || '--' }}</span></template>
            </el-table-column>
            <el-table-column prop="name" label="股票名称" width="112">
              <template #default="{ row }"><span class="trade-name">{{ row.name || '--' }}</span></template>
            </el-table-column>
            <el-table-column label="成交性质" width="92" align="center">
              <template #default><el-tag type="warning" size="small">本地模拟</el-tag></template>
            </el-table-column>
            <el-table-column prop="type" label="方向" width="76" align="center">
              <template #default="{ row }"><el-tag :type="row.type === 'buy' ? 'danger' : 'success'" size="small">{{ row.type === 'buy' ? '模拟买' : '模拟卖' }}</el-tag></template>
            </el-table-column>
            <el-table-column prop="price" label="模拟成交价" width="112" align="right">
              <template #default="{ row }"><span class="trade-number">{{ formatTradePrice(row.price) }}</span></template>
            </el-table-column>
            <el-table-column prop="shares" label="数量" width="96" align="right">
              <template #default="{ row }"><span class="trade-number">{{ formatTradeShares(row.shares ?? row.amount) }}</span></template>
            </el-table-column>
            <el-table-column label="成交额" width="116" align="right">
              <template #default="{ row }"><span class="trade-number">{{ formatTradeTurnover(row) }}</span></template>
            </el-table-column>
            <el-table-column prop="commission" label="佣金" width="96" align="right">
              <template #default="{ row }"><span class="trade-number">{{ formatTradeMoney(row.commission) }}</span></template>
            </el-table-column>
            <el-table-column label="印花税" width="96" align="right">
              <template #default="{ row }">{{ formatTradeMoney(row.tax) }}</template>
            </el-table-column>
            <el-table-column label="本笔已付总费用" min-width="140" align="right">
              <template #default="{ row }">{{ formatTradeMoney(row.total_fee) }}</template>
            </el-table-column>
            <el-table-column label="卖出净实现（持有期）" min-width="180" align="right">
              <template #default="{ row }">{{ formatSignedMoney(row.accounting?.realized_net_pnl) }}</template>
            </el-table-column>
            <el-table-column prop="pnl" label="原账本/毛浮盈(元)" min-width="160" align="right">
              <template #default="{ row }"><span v-if="row.pnl != null" class="trade-number" :class="changeColorClass(row.pnl)" :title="row.pnl_type_zh || ''">{{ formatTradeMoney(row.pnl) }}</span><span v-else>--</span></template>
            </el-table-column>
            <el-table-column prop="pnl_pct" label="盈亏%" width="112" align="right">
              <template #default="{ row }">
                <span v-if="row.pnl_pct != null" class="trade-number" :class="changeColorClass(row.pnl_pct)" :title="row.pnl_type_zh || ''">
                  <small class="trade-pnl-kind">{{ row.pnl_kind === 'floating' ? '浮' : '实' }}</small>{{ formatTradePct(row.pnl_pct) }}
                </span>
                <span v-else>--</span>
              </template>
            </el-table-column>
            <el-table-column prop="strategy_version" label="策略版本" min-width="220" show-overflow-tooltip />
            <el-table-column prop="reason_zh" label="原因/信号" min-width="300" show-overflow-tooltip>
              <template #default="{ row }"><span class="trade-reason">{{ row.reason_zh || row.display_reason || '暂无中文说明' }}</span></template>
            </el-table-column>
          </el-table>
          </div>
        </div>
      </el-tab-pane>
    </el-tabs>
    </div>
  </div>
</template>

<script setup>
import { defineAsyncComponent, ref, computed, onMounted, onUnmounted } from 'vue'
const VChart = defineAsyncComponent(() => import('vue-echarts'))
const C3Records = defineAsyncComponent(() => import('./C3Records.vue'))
const CandidateShadowPanel = defineAsyncComponent(() => import('./CandidateShadowPanel.vue'))
import { ensureLineChartsRegistered } from '@/composables/echarts/line'
import { getPaperAccount, getPaperPositions, paperBuy, paperSell, getPaperNav, getPaperTrades, getPaperAutoStatus, getPaperAutoLogs, getPaperAutoEvaluation, getPaperChallengerComparison, getPaperExperimentReport, runPaperAutoTrade } from '@/api'
import { formatChange, changeColorClass, formatAmount } from '@/composables/useUtils'
import { notifySuccess } from '@/utils/message'

ensureLineChartsRegistered()

const activeTab = ref('account')
const accountName = ref('default')
const account = ref({})
const positions = ref([])
const navList = ref([])
const trades = ref([])
const autoStatus = ref({})
const autoLogs = ref([])
const autoEvaluation = ref({})
const autoRunning = ref(false)
const challengerComparison = ref({})
const challengerLoading = ref(false)
const challengerError = ref('')
const challengerHorizon = ref(3)
const selectedChallengerRouteId = ref('')
const experimentReport = ref({})
const experimentLoading = ref(false)
const experimentError = ref('')
let dataRequestId = 0
let challengerRequestId = 0

// 六策略主题色（用于账户切换辨识度）
const STRATEGY_THEMES = {
  default:   { color: '#007aff', bg: 'rgba(0,122,255,0.12)' },   // 蓝 策略A
  promotion: { color: '#a855f7', bg: 'rgba(168,85,247,0.12)' }, // 紫 策略B
  mainline:  { color: '#10b981', bg: 'rgba(16,185,129,0.12)' }, // 绿 策略C
  auction:   { color: '#f59e0b', bg: 'rgba(245,158,11,0.12)' }, // 橙 策略D
  tenbagger: { color: '#ef4444', bg: 'rgba(239,68,68,0.12)' },  // 红 策略E
  reversal:  { color: '#06b6d4', bg: 'rgba(6,182,212,0.12)' },  // 青 策略F
}
const STRATEGY_FALLBACK_META = {
  default: { label: '策略A · 高胜率预案', short: 'A' },
  promotion: { label: '策略B · 晋级二板', short: 'B' },
  mainline: { label: '策略C · 主线扩散首板', short: 'C' },
  auction: { label: '策略D · 竞价高开强攻', short: 'D' },
  tenbagger: { label: '策略E · 十倍潜力中线', short: 'E' },
  reversal: { label: '策略F · 断板反包', short: 'F' },
}
const currentStrategyTheme = computed(() => STRATEGY_THEMES[accountName.value] || STRATEGY_THEMES.default)
const currentStrategyMeta = computed(() => {
  const fallback = STRATEGY_FALLBACK_META[accountName.value] || {
    label: `策略${accountName.value}`,
    short: accountName.value.slice(0, 1).toUpperCase(),
  }
  return {
    label: account.value.strategy_label || fallback.label,
    desc: account.value.strategy_desc || '',
    short: account.value.strategy_short || fallback.short,
  }
})

const buyForm = ref({ code: '', price: 0, amount: 100, signal_id: '' })
const sellForm = ref({ code: '', price: 0, amount: 100, reason: '' })

const navChartOption = computed(() => {
  const items = navList.value
  if (!items.length) return { backgroundColor: 'transparent' }
  return {
    backgroundColor: 'transparent',
    tooltip: { trigger: 'axis' },
    grid: { left: 60, right: 20, top: 10, bottom: 30 },
    xAxis: { type: 'category', data: items.map(i => i.date?.slice(5) || ''), axisLabel: { color: '#7a8aa0' } },
    yAxis: { type: 'value', axisLabel: { color: '#7a8aa0' }, splitLine: { lineStyle: { color: '#edf2fb' } } },
    series: [{ type: 'line', data: items.map(i => i.nav), smooth: true, lineStyle: { color: '#007aff', width: 2 }, areaStyle: { color: 'rgba(0,122,255,0.12)' } }],
  }
})

const positionPolicyText = computed(() => {
  const policy = autoStatus.value.position_policy
  if (!policy) return '--'
  const minPct = Number(policy.trial_pct || 0) * 100
  const maxPct = Number(policy.core_pct || 0) * 100
  return `${minPct.toFixed(0)}%-${maxPct.toFixed(0)}%`
})

const autoModeLabel = computed(() => {
  if (!autoStatus.value.enabled) return '全局停用'
  if (autoStatus.value.auto_order_enabled === false) return '买入演练 / 卖出风控'
  return '自动模拟买卖已启用'
})
const autoModeClass = computed(() => (
  autoStatus.value.enabled && autoStatus.value.auto_order_enabled !== false
    ? 'text-red'
    : 'text-gray'
))

const buyWindowText = computed(() => {
  const win = autoStatus.value.intraday_buy_window || {}
  const suffix = autoStatus.value.order_window?.buy_window_open ? '可买' : '等待'
  return `${win.start || '--'}-${win.end || '--'} ${suffix}`
})

const executionModelText = computed(() => {
  const model = autoStatus.value.execution_model || {}
  if (!model.fill_timing) return '--'
  return model.fill_timing === 'next_healthy_quote_round'
    ? '下一健康轮次 · 五档部分成交'
    : '同请求撮合'
})

const DAILY_OUTCOME_LABELS = {
  filled: '有成交',
  partial_fill: '部分成交·余量撤单',
  order_unfilled: '委托未成交',
  blocked: '风控/执行拦截',
  data_limited: '必要数据等待·无成交',
  control_only: '仅控制样本',
  candidate_observed: '有候选未下单',
  no_candidate: '已扫描无候选',
  not_run: '未找到运行证据',
}
const dailyOutcomeText = computed(() => {
  const outcome = autoStatus.value.daily_outcome
  if (!outcome) return '尚未固化'
  const label = DAILY_OUTCOME_LABELS[outcome.terminal_status] || outcome.terminal_status || '--'
  return `${outcome.trade_date || '--'} · ${label}`
})

const evaluationRows = computed(() => Object.values(autoEvaluation.value.stats || {}))
const sourceEvaluationRows = computed(() => Object.values(autoEvaluation.value.source_stats || {}))
const challengerPairs = computed(() => challengerComparison.value.pairs || [])
const selectedChallengerPair = computed(() => (
  challengerPairs.value.find(item => item.route_id === selectedChallengerRouteId.value)
  || challengerPairs.value[0]
  || null
))
const challengerPositionSummary = computed(() => summarizePaperPositions(selectedChallengerPair.value?.challenger?.positions))
const currentPositionSummary = computed(() => summarizePaperPositions(positions.value))
const challengerReturnBreakdown = computed(() => selectedChallengerPair.value?.challenger?.return_breakdown || {})
const challengerRealizedPnl = computed(() => Object.hasOwn(challengerReturnBreakdown.value, 'realized_pnl')
  ? challengerReturnBreakdown.value.realized_pnl
  : selectedChallengerPair.value?.challenger?.trade_stats?.total_pnl)
const challengerFeesPaid = computed(() => Object.hasOwn(challengerReturnBreakdown.value, 'fees_paid')
  ? challengerReturnBreakdown.value.fees_paid
  : selectedChallengerPair.value?.challenger?.fee_drag)
const challengerTotalPnl = computed(() => {
  const challenger = selectedChallengerPair.value?.challenger
  if (Object.hasOwn(challengerReturnBreakdown.value, 'total_pnl')) return finitePaperNumber(challengerReturnBreakdown.value.total_pnl)
  const assets = finitePaperNumber(challenger?.total_assets)
  const initial = finitePaperNumber(challenger?.initial_capital)
  return assets != null && initial != null ? assets - initial : null
})
const poolClock = ref(Date.now())
const selectedCurrentPool = computed(() => {
  const pool = selectedChallengerPair.value?.current_pool
  if (!pool) return null
  // API timestamps are exchange-local (Asia/Shanghai), not browser-local.
  const expiry = pool.expires_at ? Date.parse(pool.expires_at + '+08:00') : NaN
  if (!pool.valid || (Number.isFinite(expiry) && poolClock.value <= expiry)) return pool
  return { ...pool, valid: false, expired: true, structural_count: 0, eligible_count: 0,
    confirmed_count: 0, members: [], reason: '扫描帧已过期，等待刷新；累计证据不变' }
})
const strategyCoverage = computed(() => challengerComparison.value.strategy_coverage || [])
const challengerCoverageSummary = computed(() => challengerComparison.value.coverage_summary || {})
const selectedStrategyCoverage = computed(() => (
  challengerComparison.value.selected_strategy
  || strategyCoverage.value.find(item => item.account_name === accountName.value)
  || null
))
function coverageExecutionLabel(item) {
  if (!item?.challenger_configured) return '未配置'
  if (item.control_sample_only) return '控制样本'
  const enabled = Number(item.execution_enabled_route_count || 0)
  const paused = Number(item.execution_paused_route_count || 0)
  const evidenceOnly = Number(item.evidence_only_route_count || 0)
  if (enabled && paused) return '部分撮合'
  if (enabled) return evidenceOnly ? '撮合+采证' : '撮合开启'
  if (paused) return evidenceOnly ? '暂停+采证' : '撮合暂停'
  return evidenceOnly ? '只采证' : '已配置'
}
const filteredChallengerSignals = computed(() => (
  (challengerComparison.value.recent_signals || []).filter(
    item => item.route_id === selectedChallengerPair.value?.route_id,
  )
))
const filteredChallengerActions = computed(() => (
  (challengerComparison.value.recent_actions || []).filter(
    item => item.source === selectedChallengerPair.value?.route_id,
  )
))
const commonPeriodText = computed(() => {
  const period = selectedChallengerPair.value?.common_period
  if (!period?.comparable) return period?.note || '共同净值日期不足'
  return `${period.start_date} 至 ${period.end_date} · ${period.session_count} 个净值日`
})
const challengerEvidenceStatus = computed(() => {
  const status = selectedChallengerPair.value?.evidence_status
  if (status === 'manual_review_eligible') return '统计证据与执行护栏已满足，待人工评审'
  if (status === 'execution_guardrail_blocked') return '统计证据已满足，跨版本账户护栏阻断'
  if (status === 'collecting') return '正在积累前向证据'
  if (status === 'scheduled') return '等待完整交易日起采集'
  if (status === 'configuration_blocked') return '激活日期配置异常，采集已阻断'
  return '等待首个前向确认信号'
})
const challengerEvidenceTagType = computed(() => (
  selectedChallengerPair.value?.evidence_status === 'manual_review_eligible'
    ? 'success'
    : ['configuration_blocked', 'execution_guardrail_blocked'].includes(selectedChallengerPair.value?.evidence_status)
      ? 'danger'
    : selectedChallengerPair.value?.evidence_status === 'collecting'
      ? 'warning'
      : 'info'
))
const evidenceSettlementHint = computed(() => {
  const evidence = selectedChallengerPair.value?.evidence || {}
  const pending = Number(evidence.pending_settlement_count || 0)
  const pendingSessions = Number(evidence.pending_settlement_sessions || 0)
  const horizon = Number(evidence.horizon_days || challengerHorizon.value || 3)
  if (selectedChallengerPair.value?.evidence_status === 'configuration_blocked') {
    return '首板路线激活日期缺失或格式非法，后端已失败关闭；修复配置前不会产生任何 C3 事件。'
  }
  if (selectedChallengerPair.value?.evidence_status === 'scheduled') {
    return `为避免把今天中途上线后的残缺时段当作完整分母，本路线从 ${selectedChallengerPair.value?.activation_date || '下一完整交易日'} 开始前向采集，历史上午数据不会事后回填。`
  }
  if (pending > 0) {
    return `已采集 ${pending} 个待结算确认信号（${pendingSessions} 个交易日）。需再观察 ${horizon} 个后续交易日并取得完整正式日K后，才会从“盘中确认”转入“已结算样本”；当前为 0 不是代码自测失败。`
  }
  if (Number(evidence.sample_count || 0) > 0) {
    return `当前 ${evidence.sample_count} 个信号已按 ${horizon} 个交易日口径结算，收益、超额收益和不利波动均来自这些到期样本。`
  }
  return '尚无通过盘中持续确认的信号，因此还没有可等待或可结算的样本。'
})
const evidenceGateRows = computed(() => {
  const evidence = selectedChallengerPair.value?.evidence || {}
  const gates = evidence.gates || {}
  const thresholds = evidence.thresholds || {}
  const shareText = value => (
    value == null || !Number.isFinite(Number(value))
      ? '--'
      : `${(Number(value) * 100).toFixed(2)}%`
  )
  const rows = [
    {
      key: 'enough_independent_sessions',
      label: '独立交易日',
      value: `${evidence.independent_sessions || 0}/${evidence.minimum_required_sessions || 20}`,
      threshold: `至少 ${evidence.minimum_required_sessions || 20} 个交易日`,
    },
    {
      key: 'enough_confirmed_samples',
      label: '已结算确认样本',
      value: `${evidence.sample_count || 0}/${evidence.minimum_required_samples || 100}`,
      threshold: `至少 ${evidence.minimum_required_samples || 100} 个样本`,
    },
    { key: 'avg_net_return_positive', label: '平均净收益', value: formatPct(evidence.avg_net_return_pct), threshold: '扣除模拟成本后 > 0%' },
    { key: 'avg_excess_return_positive', label: '平均超额收益', value: formatPct(evidence.avg_excess_return_pct), threshold: '相对全市场等权 > 0%' },
    { key: 'first_half_positive', label: '前半样本净收益', value: formatPct(evidence.first_half_avg_net_return_pct), threshold: '前半样本均值 > 0%' },
    { key: 'second_half_positive', label: '后半样本净收益', value: formatPct(evidence.second_half_avg_net_return_pct), threshold: '后半样本均值 > 0%' },
    { key: 'top5_removed_still_positive', label: '剔除最佳5笔', value: formatPct(evidence.top5_removed_avg_net_return_pct), threshold: '剔除后均值仍 > 0%' },
    { key: 'avg_mae_acceptable', label: '平均最大不利波动', value: formatPct(evidence.avg_max_adverse_pct), threshold: `不低于 ${formatPct(thresholds.minimum_avg_mae_pct)}` },
    { key: 'worst_mae_acceptable', label: '最差最大不利波动', value: formatPct(evidence.worst_max_adverse_pct), threshold: `不低于 ${formatPct(thresholds.minimum_worst_mae_pct)}` },
    { key: 'session_concentration_acceptable', label: '单日样本集中度', value: shareText(evidence.max_session_share), threshold: `不高于 ${shareText(thresholds.maximum_session_share)}` },
    { key: 'code_concentration_acceptable', label: '单股样本集中度', value: shareText(evidence.max_code_share), threshold: `不高于 ${shareText(thresholds.maximum_code_share)}` },
  ]
  if (evidence.control_basis) {
    rows.push({ key: 'beats_unconfirmed_control', label: '优于未确认对照', value: formatSignedPct(evidence.confirmed_minus_control_pct), threshold: '确认组固定观察期净收益 > 同资格未确认组' })
  }
  return rows.map(item => ({ ...item, passed: Boolean(gates[item.key]) }))
})
const challengerNavChartOption = computed(() => {
  const pair = selectedChallengerPair.value
  if (!pair) return { backgroundColor: 'transparent' }
  const championNav = pair.champion?.nav || []
  const challengerNav = pair.challenger?.nav || []
  const dates = [...new Set([...championNav, ...challengerNav].map(item => item.date))].sort()
  const championByDate = Object.fromEntries(championNav.map(item => [item.date, item.nav]))
  const challengerByDate = Object.fromEntries(challengerNav.map(item => [item.date, item.nav]))
  const routeColor = {
    b_weak_open_second_board: '#a855f7',
    c_recent_limit_relaunch: '#10b981',
    c3_mainline_first_board: '#14b8a6',
    d_auction_recovery: '#f59e0b',
    f2_highboard_break_reclaim: '#06b6d4',
  }[pair.route_id] || '#007aff'
  return {
    backgroundColor: 'transparent',
    tooltip: { trigger: 'axis' },
    legend: { data: ['基准账户', '隔离候选账户'], textStyle: { color: '#7a8aa0' } },
    grid: { left: 58, right: 20, top: 42, bottom: 30 },
    xAxis: { type: 'category', data: dates.map(item => item.slice(5)), axisLabel: { color: '#7a8aa0' } },
    yAxis: { type: 'value', scale: true, axisLabel: { color: '#7a8aa0' }, splitLine: { lineStyle: { color: '#edf2fb' } } },
    series: [
      { name: '基准账户', type: 'line', data: dates.map(item => championByDate[item] ?? null), connectNulls: false, smooth: false, itemStyle: { color: '#64748b' }, lineStyle: { color: '#64748b', width: 2 } },
      { name: '隔离候选账户', type: 'line', data: dates.map(item => challengerByDate[item] ?? null), connectNulls: false, smooth: false, itemStyle: { color: routeColor }, lineStyle: { color: routeColor, width: 2 }, areaStyle: { color: `${routeColor}22` } },
    ],
  }
})

function settledEvidenceProgress(row) {
  const evidence = row?.evidence || {}
  if (Number.isFinite(Number(evidence.settled_progress_pct))) {
    return Math.min(100, Math.max(0, Math.round(Number(evidence.settled_progress_pct))))
  }
  const sessionProgress = Number(evidence.independent_sessions || 0) / Math.max(Number(evidence.minimum_required_sessions || 20), 1)
  const sampleProgress = Number(evidence.sample_count || 0) / Math.max(Number(evidence.minimum_required_samples || 100), 1)
  return Math.min(100, Math.round(Math.min(sessionProgress, sampleProgress) * 100))
}

function collectionEvidenceProgress(row) {
  const evidence = row?.evidence || {}
  if (Number.isFinite(Number(evidence.collection_progress_pct))) {
    return Math.min(100, Math.max(0, Math.round(Number(evidence.collection_progress_pct))))
  }
  const sessionProgress = Number(evidence.confirmed_signal_sessions || 0) / Math.max(Number(evidence.minimum_required_sessions || 20), 1)
  const sampleProgress = Number(evidence.confirmed_signal_count || row?.event_counts?.confirmed || 0) / Math.max(Number(evidence.minimum_required_samples || 100), 1)
  return Math.min(100, Math.round(Math.min(sessionProgress, sampleProgress) * 100))
}

function formatComparisonTime(value) {
  const { date, clock } = splitTradeTime(value)
  return date === '--' ? '--' : `${date} ${clock}`
}

function challengerEventLabel(eventType) {
  return {
    confirmed: '盘中已确认',
    coverage_blocked: '盘中覆盖不足',
    evidence_blocked: '证据质量阻断',
    session_blocked: '全时段观测不足',
    outcome_blocked: '收盘结果待补全',
  }[eventType] || '审计事件'
}

function challengerEventType(eventType) {
  return eventType === 'confirmed' ? 'danger' : eventType === 'outcome_blocked' ? 'info' : 'warning'
}

function humanizeChallengerText(value) {
  return String(value || '--')
    .replaceAll('Challenger', '隔离候选策略')
    .replaceAll('Champion', '基准策略')
    .replaceAll('paper broker', '本地模拟撮合')
    .replaceAll('warn/block', '风控警告/阻断')
    .replaceAll('confirmed', '盘中确认')
    .replaceAll('VWAP', '日内成交均价线')
}

function signalEvaluation(row) {
  return row?.evaluations?.[String(challengerHorizon.value)] || null
}

function formatSignedPct(value) {
  if (value == null || !Number.isFinite(Number(value))) return '--'
  const number = Number(value)
  return `${number > 0 ? '+' : ''}${number.toFixed(2)}%`
}

const experimentAccounts = computed(() => experimentReport.value.accounts || [])

function triStateLabel(value, yesLabel, noLabel) {
  if (value === true) return yesLabel
  if (value === false) return noLabel
  return '状态未知'
}

function countOrUnknown(value) {
  return value == null || !Number.isFinite(Number(value)) ? '未知' : Number(value)
}

function hasClosedSamples(item) {
  return Number.isFinite(Number(item?.closed_round_trips)) && Number(item.closed_round_trips) > 0
}

function sampleState(item) {
  if (item?.closed_round_trips == null) return '未知'
  return Number(item.closed_round_trips) > 0 ? '--' : '无样本'
}

function protocolMoney(item) {
  if (!hasClosedSamples(item)) return sampleState(item)
  return formatMoney(item.net_pnl)
}

function protocolRate(item) {
  if (!hasClosedSamples(item)) return sampleState(item)
  return formatRate(item.win_rate)
}

function bullBearLabel(value) {
  return { bull: '偏多', bear: '偏空', sideways: '震荡', unknown: '未知' }[value] || value || '未知'
}

function formatRateInterval(value) {
  const bounds = Array.isArray(value) ? value : [value?.lower ?? value?.low, value?.upper ?? value?.high]
  if (bounds.length !== 2 || bounds.some(item => item == null)) return '未知'
  return `${formatRate(bounds[0])}–${formatRate(bounds[1])}`
}

function entrySessionsText(value) {
  if (Array.isArray(value)) return `${value.length} 个（${value.join('、')}）`
  return value == null ? '未知' : `${value} 个`
}

function bullBearDetail(row) {
  return `首次入场冻结 · 入场日 ${entrySessionsText(row.entry_sessions)} · 胜率95%区间 ${formatRateInterval(row.win_rate_interval_95)}`
}

async function loadExperimentReport() {
  experimentLoading.value = true
  experimentError.value = ''
  try {
    experimentReport.value = await getPaperExperimentReport() || {}
  } catch (error) {
    experimentReport.value = {}
    experimentError.value = error?.response?.status === 404
      ? '持续实验报告接口尚未部署（HTTP 404）。本页不会用旧账户收益、当日 markout 或 0 胜率代替。'
      : '持续实验报告加载失败。页面不会用账户总收益、当日 markout 或 0 胜率替代。'
  } finally {
    experimentLoading.value = false
  }
}

async function loadChallengerComparison() {
  const targetAccount = accountName.value
  const requestId = ++challengerRequestId
  challengerLoading.value = true
  challengerError.value = ''
  try {
    const result = await getPaperChallengerComparison({
      horizon_days: challengerHorizon.value,
      recent_limit: 100,
      account_name: targetAccount,
    })
    if (requestId !== challengerRequestId || targetAccount !== accountName.value) return
    challengerComparison.value = result || {}
    const routes = challengerComparison.value.pairs || []
    if (!routes.some(item => item.route_id === selectedChallengerRouteId.value)) {
      selectedChallengerRouteId.value = routes[0]?.route_id || ''
    }
  } catch {
    if (requestId !== challengerRequestId || targetAccount !== accountName.value) return
    challengerComparison.value = {}
    challengerError.value = '策略对比数据加载失败。页面不会用默认值或演示数据替代真实结果。'
  } finally {
    if (requestId === challengerRequestId) challengerLoading.value = false
  }
}

function onTabChange(name) {
  if (name === 'c3-records' && accountName.value !== 'mainline') {
    activeTab.value = 'account'
    return
  }
  if (name === 'experiment' && !experimentLoading.value) loadExperimentReport()
  if (name === 'challengers' && !challengerLoading.value) loadChallengerComparison()
}

async function doBuy() {
  const targetAccount = accountName.value
  const payload = { ...buyForm.value }
  try {
    await paperBuy(payload, targetAccount)
    notifySuccess(`${strategyDisplayName(targetAccount)} 模拟买入成功`)
    if (accountName.value === targetAccount) await loadData(targetAccount)
  } catch { /* ignore */ }
}
async function doSell() {
  const targetAccount = accountName.value
  const payload = { ...sellForm.value }
  try {
    await paperSell(payload, targetAccount)
    notifySuccess(`${strategyDisplayName(targetAccount)} 模拟卖出成功`)
    if (accountName.value === targetAccount) await loadData(targetAccount)
  } catch { /* ignore */ }
}

const strategyDisplayName = (name) => STRATEGY_FALLBACK_META[name]?.label || name

function clearAccountData() {
  account.value = {}
  positions.value = []
  navList.value = []
  trades.value = []
  autoStatus.value = {}
  autoLogs.value = []
  autoEvaluation.value = {}
}

function clearChallengerData() {
  challengerRequestId += 1
  challengerComparison.value = {}
  selectedChallengerRouteId.value = ''
  challengerError.value = ''
  challengerLoading.value = false
}

async function onAccountSwitch(nextAccount) {
  // 每次切换策略都从账户概览开始，不能沿用上一个策略的 Tab。
  activeTab.value = 'account'
  buyForm.value = { code: '', price: 0, amount: 100, signal_id: '' }
  sellForm.value = { code: '', price: 0, amount: 100, reason: '' }
  // 先清空旧账户数据和上一个策略的对比结果，避免跨策略串页。
  clearAccountData()
  clearChallengerData()
  const loaded = await loadData(nextAccount)
  if (loaded && accountName.value === nextAccount) {
    notifySuccess(`已切换到 ${strategyDisplayName(nextAccount)}`)
  }
}

function confirmationState(row, key) {
  const value = row.confirmation_evidence?.[key]
  return value === 'true' ? '是' : value === 'false' ? '否' : '未知'
}
function confirmationOrderResult(row) {
  const value = row.confirmation_evidence?.order_result
  return ({
    not_submitted: '未提交', dry_run: '仅演练', filled: '已成交',
    submitted: '已提交待成交', partial: '部分成交', rejected: '已拒绝',
    risk_blocked: '风控拦截', canceled: '已撤单',
  })[value] || (value && value !== 'unknown' ? value : '未知')
}

function autoActionLabel(action, decision) {
  if (decision === 'dry_run') return '演练'
  if (decision === 'quote_confirmed') return '报价路径确认（非下单）'
  if (action === 'queue_buy') return '模拟涨停排队'
  if (action === 'deferred_buy') return '下一轮待撮合买入'
  if (action === 'deferred_sell') return '下一轮待撮合卖出'
  if (action === 'quote_confirmed') return '报价路径确认（非下单）'
  if (action === 'buy') return '模拟买入'
  if (action === 'sell') return '模拟卖出'
  if (action === 'hold') return '持有'
  if (action === 'empty') return '空仓等待'
  if (action === 'skip_buy') return '跳过买入'
  if (action === 'skip_sell') return '跳过卖出'
  return humanizeChallengerText(action)
}

function autoActionType(action, decision) {
  if (decision === 'blocked') return 'warning'
  if (decision === 'dry_run') return 'info'
  if (action === 'queue_buy' || action === 'deferred_buy' || action === 'deferred_sell') return 'warning'
  if (action === 'buy') return 'danger'
  if (action === 'sell') return 'success'
  return 'info'
}

function formatRate(value) {
  return value == null ? '--' : `${(Number(value) * 100).toFixed(1)}%`
}

function formatPct(value) {
  const number = Number(value)
  return value == null || !Number.isFinite(number) ? '--' : `${number.toFixed(2)}%`
}

function finitePaperNumber(value) {
  if (value == null || typeof value === 'boolean' || (typeof value === 'string' && !value.trim())) return null
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}

function positionMarketValue(row) {
  const price = finitePaperNumber(row?.current_price)
  const shares = finitePaperNumber(row?.buy_amount ?? row?.amount)
  return price != null && price > 0 && shares != null && Number.isSafeInteger(shares) && shares >= 0
    ? price * shares
    : null
}

function summarizePaperPositions(rows) {
  const unknown = { count: null, shares: null, market_value: null, profit_loss: null, winners: null, losers: null, flat: null }
  if (!Array.isArray(rows)) return unknown
  const sumKnown = values => {
    if (values.some(value => value == null)) return null
    const total = values.reduce((sum, value) => sum + value, 0)
    return Number.isFinite(total) ? total : null
  }
  const sumMoney = values => {
    const total = sumKnown(values)
    return total == null ? null : Number(total.toFixed(2))
  }
  const shares = rows.map(row => {
    const value = finitePaperNumber(row.buy_amount ?? row.amount)
    return value != null && Number.isSafeInteger(value) && value >= 0 ? value : null
  })
  const pnls = rows.map(row => finitePaperNumber(row.profit_loss))
  const pnlKnown = pnls.every(value => value != null)
  return {
    count: rows.length,
    shares: sumKnown(shares),
    market_value: sumMoney(rows.map(positionMarketValue)),
    profit_loss: sumMoney(pnls),
    winners: pnlKnown ? pnls.filter(value => value > 0).length : null,
    losers: pnlKnown ? pnls.filter(value => value < 0).length : null,
    flat: pnlKnown ? pnls.filter(value => value === 0).length : null,
  }
}

function formatMoney(value) {
  const number = finitePaperNumber(value)
  return number == null ? '--' : number.toFixed(2)
}

function formatSignedMoney(value) {
  const number = finitePaperNumber(value)
  return number == null ? '--' : `${number > 0 ? '+' : ''}${number.toFixed(2)}`
}

function formatRatio(value) {
  return value == null ? '--' : Number(value).toFixed(2)
}

function splitTradeTime(value) {
  if (!value) return { date: '--', clock: '--' }
  const normalized = String(value).replace('T', ' ')
  const [datePart = '--', rawClock = ''] = normalized.split(' ')
  const clock = rawClock.split('.')[0]
  return { date: datePart || '--', clock: clock || '--' }
}

function formatTradeDate(value) {
  return splitTradeTime(value).date
}

function formatTradeClock(value) {
  return splitTradeTime(value).clock
}

function formatTradePrice(value) {
  const number = Number(value)
  return Number.isFinite(number) ? number.toFixed(2) : '--'
}

function formatTradeShares(value) {
  const number = finitePaperNumber(value)
  return number == null ? '--' : number.toLocaleString('zh-CN')
}

function formatTradeMoney(value) {
  const number = Number(value)
  return Number.isFinite(number) ? number.toFixed(2) : '--'
}

function formatTradePct(value) {
  const number = Number(value)
  if (!Number.isFinite(number)) return '--'
  return `${number > 0 ? '+' : ''}${number.toFixed(2)}%`
}

function formatTradeTurnover(row) {
  const price = Number(row.price)
  const shares = Number(row.shares ?? row.amount)
  if (!Number.isFinite(price) || !Number.isFinite(shares)) return '--'
  return (price * shares).toFixed(2)
}

async function runAuto(execute) {
  const targetAccount = accountName.value
  autoRunning.value = true
  try {
    await runPaperAutoTrade({ execute, trigger: execute ? 'manual-execute' : 'manual-dry-run', max_candidates: 20, execution_mode: 'intraday' }, targetAccount)
    notifySuccess(`${strategyDisplayName(targetAccount)} ${execute ? '决策已提交，成交需等待下一健康行情轮次' : '演练完成'}`)
    if (accountName.value === targetAccount) await loadData(targetAccount)
  } catch { /* ignore */ }
  finally { autoRunning.value = false }
}

async function loadData(selectedAccount = accountName.value) {
  const targetAccount = selectedAccount || accountName.value
  const requestId = ++dataRequestId
  try {
    // 先完成账户初始化，再并发读取各 Tab，避免新策略首次打开时多个 GET 同时创建重复账户。
    const accountResult = await getPaperAccount(targetAccount)
    if (requestId !== dataRequestId || targetAccount !== accountName.value) return false
    account.value = accountResult.account || {}

    const [p, n, t, s, l, e] = await Promise.allSettled([
      getPaperPositions(targetAccount),
      getPaperNav(targetAccount),
      getPaperTrades({ limit: 100 }, targetAccount),
      getPaperAutoStatus(targetAccount),
      getPaperAutoLogs({ limit: 100, today_only: true }, targetAccount),
      getPaperAutoEvaluation(targetAccount),
    ])
    // 快速连续切换时，过期账户的慢响应不得覆盖当前账户。
    if (requestId !== dataRequestId || targetAccount !== accountName.value) return false
    positions.value = p.status === 'fulfilled' ? (p.value.positions || []) : []
    navList.value = n.status === 'fulfilled' ? (n.value.nav || []) : []
    trades.value = t.status === 'fulfilled' ? (t.value.trades || []) : []
    autoStatus.value = s.status === 'fulfilled' ? (s.value || {}) : {}
    autoLogs.value = l.status === 'fulfilled' ? (l.value.logs || []) : []
    autoEvaluation.value = e.status === 'fulfilled' ? (e.value || {}) : {}
    return true
  } catch {
    return false
  }
}

let poolClockTimer
let poolRefreshTimer
onMounted(() => {
  loadData()
  loadExperimentReport()
  poolClockTimer = setInterval(() => { poolClock.value = Date.now() }, 1000)
  poolRefreshTimer = setInterval(() => {
    if (activeTab.value === 'challengers' && selectedCurrentPool.value && !challengerLoading.value) {
      loadChallengerComparison()
    }
  }, 30000)
})
onUnmounted(() => {
  clearInterval(poolClockTimer)
  clearInterval(poolRefreshTimer)
})
</script>

<style scoped lang="scss">
.paper-page { display: flex; flex-direction: column; min-width: 0; gap: 18px; }
.paper-page :deep(.panel-card) { min-width: 0; }
.account-switch { padding: 4px; }
.account-switch :deep(.el-radio-button__inner) { font-size: 12px; }
/* 策略辨识度横幅: 切换账户后明确显示当前策略 (2026-08-31) */
.strategy-banner {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 14px 18px;
  border-radius: 14px;
  border: 1px solid;
}
.strategy-badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 44px;
  height: 44px;
  border-radius: 12px;
  color: #fff;
  font-size: 20px;
  font-weight: 800;
  flex-shrink: 0;
}
.strategy-banner-text { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.strategy-banner-text strong { font-size: 17px; font-weight: 700; }
.strategy-banner-text span { font-size: 13px; color: var(--claw-text-muted); }
.strategy-banner-text small { color: var(--claw-text-muted); font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size: 11px; overflow-wrap: anywhere; }
.simulation-guard-alert { margin-top: -4px; }
.protocol-start-hint { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 14px; margin-top: -8px; padding: 9px 12px; border: 1px solid var(--claw-border-light); border-radius: 10px; background: var(--claw-bg-card); font-size: 12px; }
.protocol-start-hint strong { color: var(--claw-primary); }
.protocol-start-hint span { color: var(--claw-text); }
.protocol-start-hint small { color: var(--claw-text-muted); }
.experiment-toolbar { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 12px; }
.experiment-toolbar p { margin: 5px 0 0; color: var(--claw-text-muted); font-size: 12px; line-height: 1.6; }
.experiment-method-alert { margin-bottom: 16px; }
.experiment-content { min-height: 180px; }
.experiment-error-state { display: flex; align-items: center; gap: 12px; }
.experiment-error-state :deep(.el-alert) { flex: 1; }
.experiment-meta { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px; margin-bottom: 16px; }
.experiment-meta > div { min-width: 0; padding: 10px; border: 1px solid var(--claw-border-light); border-radius: 9px; background: var(--claw-bg-page); }
.experiment-meta span, .experiment-account-head small { display: block; color: var(--claw-text-muted); font-size: 11px; }
.experiment-meta strong { display: block; margin-top: 5px; color: var(--claw-text); font-size: 13px; overflow-wrap: anywhere; }
.experiment-account-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
.experiment-account-card { min-width: 0; }
.experiment-account-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.experiment-account-head > div:first-child { min-width: 0; }
.experiment-account-head strong { display: block; color: var(--claw-text); font-size: 16px; }
.experiment-account-head small { margin-top: 4px; overflow-wrap: anywhere; }
.experiment-tags { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 5px; }
.experiment-metrics { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; margin-top: 14px; }
.experiment-metrics > div { padding: 9px; border-radius: 8px; background: var(--claw-bg-page); }
.experiment-metrics span { display: block; margin-bottom: 4px; color: var(--claw-text-muted); font-size: 10px; }
.experiment-metrics strong { color: var(--claw-text); font-size: 14px; font-variant-numeric: tabular-nums; }
.lifetime-note { margin-top: 10px; padding: 8px 10px; border: 1px dashed var(--claw-border); border-radius: 8px; color: var(--claw-text-muted); font-size: 11px; }
.activity-box { margin-top: 10px; padding: 10px; border: 1px solid var(--claw-border-light); border-radius: 9px; }
.activity-summary { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 6px; font-size: 11px; }
.activity-summary span, .activity-box small { color: var(--claw-text-muted); }
.reason-list { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 8px; }
.reason-list span { padding: 3px 7px; border-radius: 999px; color: #b26a00; background: rgba(245, 158, 11, .1); font-size: 10px; }
.regime-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; margin-top: 10px; }
.regime-grid > div { min-width: 0; padding: 9px; border-radius: 8px; background: var(--claw-bg-page); }
.regime-grid strong { color: var(--claw-text); font-size: 11px; }
.regime-grid p { margin: 5px 0 0; color: var(--claw-text-muted); font-size: 10px; line-height: 1.45; overflow-wrap: anywhere; }
.regime-grid p small { display: block; margin-top: 2px; color: var(--claw-text-muted); }
.regime-grid .proxy-note { color: #b26a00; }
@media (max-width: 768px) {
  .strategy-banner-text span { display: none; }
  .experiment-toolbar { flex-direction: column; }
  .experiment-meta, .experiment-account-grid { grid-template-columns: 1fr; }
  .experiment-metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .regime-grid { grid-template-columns: 1fr; }
}
.paper-metrics-panel { padding: 4px; }
.stat-row { display: grid; grid-template-columns: repeat(7, 1fr); gap: 12px; margin-bottom: 0; }
.paper-stat-card { padding: 16px; background: var(--claw-bg-card); border: 1px solid var(--claw-border); border-radius: 14px; box-shadow: var(--claw-shadow-sm); }
.metric-head { color: var(--claw-text-muted); font-size: 13px; margin-bottom: 10px; }
.trade-forms { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.trade-card { min-height: 100%; }
.auto-toolbar { display: flex; justify-content: space-between; gap: 16px; align-items: center; margin-bottom: 16px; }
.auto-summary { display: grid; grid-template-columns: repeat(5, minmax(110px, 1fr)); gap: 10px; flex: 1; }
.auto-item { padding: 12px; border: 1px solid var(--claw-border); border-radius: 8px; background: var(--claw-bg-card); }
.auto-item span { display: block; color: var(--claw-text-muted); font-size: 12px; margin-bottom: 6px; }
.auto-item strong { font-size: 15px; }
.auto-item-wide { grid-column: span 2; }
.auto-item-wide strong { display: block; color: var(--claw-text); font-size: 13px; line-height: 1.55; }
.auto-actions { display: flex; gap: 8px; }
.eval-summary { display: grid; grid-template-columns: repeat(5, minmax(110px, 1fr)); gap: 10px; margin-bottom: 14px; }
.source-eval-table { margin-top: 14px; }
.mistake-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-top: 16px; }
.mini-title { color: var(--claw-text-muted); font-size: 13px; margin-bottom: 8px; }
.link { color: var(--claw-primary); text-decoration: none; }
.trade-record-card { padding: 16px; }
.auto-log-table { min-width: 1570px; }
.trade-record-table { width: 100%; min-width: 1710px; }
.trade-time { display: flex; flex-direction: column; gap: 2px; line-height: 1.25; white-space: nowrap; }
.trade-time span { color: var(--claw-text-muted); font-size: 12px; }
.trade-time strong { color: var(--claw-text); font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size: 13px; font-weight: 700; }
.trade-code,
.trade-number { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-variant-numeric: tabular-nums; white-space: nowrap; }
.trade-code { font-weight: 700; letter-spacing: 0; }
.trade-name { color: var(--claw-text); font-weight: 600; white-space: nowrap; }
.trade-pnl-kind { margin-right: 3px; color: var(--claw-text-muted); font-family: inherit; font-size: 10px; font-weight: 600; }
.trade-reason { display: inline-block; max-width: 100%; overflow: hidden; text-overflow: ellipsis; vertical-align: middle; white-space: nowrap; }
.challenger-toolbar { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; min-width: 0; }
.challenger-subtitle { max-width: 820px; margin-top: 6px; color: var(--claw-text-muted); font-size: 13px; line-height: 1.65; }
.challenger-actions { display: flex; align-items: center; gap: 10px; flex: 0 0 auto; }
.challenger-alert { margin: 12px 0 16px; }
.challenger-content { display: flex; flex-direction: column; gap: 16px; min-width: 0; min-height: 180px; }
.challenger-error-state { display: flex; align-items: center; gap: 12px; }
.challenger-error-state :deep(.el-alert) { flex: 1; }
.strategy-coverage-strip { display: flex; flex-wrap: wrap; align-items: stretch; gap: 8px; padding: 10px; overflow-x: auto; border: 1px solid var(--claw-border-light); border-radius: 12px; background: var(--claw-bg-card); }
.coverage-chip { display: flex; align-items: center; gap: 7px; min-width: 88px; padding: 8px 10px; border: 1px solid var(--claw-border); border-radius: 9px; color: var(--claw-text-muted); background: var(--claw-bg-page); white-space: nowrap; }
.coverage-chip strong { display: inline-flex; align-items: center; justify-content: center; width: 24px; height: 24px; border-radius: 7px; color: var(--claw-text); background: var(--claw-border-light); }
.coverage-chip span { font-size: 12px; }
.coverage-chip.configured span { color: #0f9f6e; }
.coverage-chip.paused span { color: #d97706; }
.coverage-chip.evidence span { color: var(--claw-primary); }
.coverage-chip.active { border-color: var(--claw-primary); box-shadow: 0 0 0 2px rgba(0, 122, 255, .08); }
.coverage-chip.active strong { color: #fff; background: var(--claw-primary); }
.coverage-total { display: flex; flex-wrap: wrap; align-items: center; gap: 4px; margin-left: auto; padding: 0 8px; color: var(--claw-text-muted); font-size: 12px; }
.challenger-route-selector { display: flex; align-items: center; justify-content: space-between; gap: 16px; min-width: 0; }
.challenger-route-selector > div { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.challenger-route-selector strong { color: var(--claw-text); font-size: 13px; }
.challenger-route-selector small { color: var(--claw-text-muted); font-size: 11px; line-height: 1.5; }
.challenger-route-selector :deep(.el-radio-group) { flex: 0 1 auto; max-width: 100%; overflow-x: auto; }
.no-challenger-state { display: grid; grid-template-columns: auto minmax(0, 1fr); align-items: center; gap: 18px; min-height: 170px; }
.no-challenger-badge { display: flex; align-items: center; justify-content: center; width: 64px; height: 64px; border-radius: 18px; color: #fff; background: #94a3b8; font-size: 26px; font-weight: 800; }
.no-challenger-state h3 { margin: 10px 0 8px; color: var(--claw-text); font-size: 18px; }
.no-challenger-state p { max-width: 850px; margin: 0 0 8px; color: var(--claw-text-muted); line-height: 1.65; }
.no-challenger-state small { color: #b26a00; }
.no-challenger-metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(145px, 1fr)); gap: 9px; margin-top: 14px; }
.no-challenger-metrics > div { min-width: 0; padding: 10px; border: 1px solid var(--claw-border-light); border-radius: 9px; background: var(--claw-bg-page); }
.no-challenger-metrics span { display: block; margin-bottom: 4px; color: var(--claw-text-muted); font-size: 10px; }
.no-challenger-metrics strong { color: var(--claw-text); font-size: 13px; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
.challenger-route-overview { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 20px; align-items: center; }
.route-kicker { margin-bottom: 6px; color: var(--claw-primary); font-size: 12px; font-weight: 700; letter-spacing: .04em; }
.challenger-route-copy h3 { margin: 0 0 7px; color: var(--claw-text); font-size: 19px; }
.challenger-route-copy p { margin: 0; color: var(--claw-text-muted); line-height: 1.6; overflow-wrap: anywhere; }
.route-status { display: flex; flex-direction: column; align-items: flex-end; gap: 7px; color: var(--claw-text-muted); font-size: 12px; white-space: nowrap; }
.comparison-metric-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
.comparison-metric { display: flex; flex-direction: column; min-width: 0; min-height: 104px; padding: 14px; border: 1px solid var(--claw-border); border-radius: 12px; background: var(--claw-bg-card); box-shadow: var(--claw-shadow-sm); }
.comparison-metric > span { color: var(--claw-text-muted); font-size: 12px; line-height: 1.4; }
.comparison-metric > strong { margin: 8px 0 5px; color: var(--claw-text); font-size: 20px; font-variant-numeric: tabular-nums; }
.comparison-metric > small { margin-top: auto; color: var(--claw-text-muted); font-size: 11px; line-height: 1.4; overflow-wrap: anywhere; }
.evidence-card { min-width: 0; }
.evidence-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 18px; margin-bottom: 12px; }
.evidence-head p { margin: 5px 0 0; color: var(--claw-text-muted); font-size: 12px; line-height: 1.6; }
.evidence-head > strong { color: var(--claw-primary); font-size: 20px; font-variant-numeric: tabular-nums; white-space: nowrap; }
.evidence-purpose-box { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; margin-bottom: 16px; }
.evidence-purpose-box > div { display: flex; flex-direction: column; gap: 5px; min-width: 0; padding: 11px 13px; border: 1px solid var(--claw-border-light); border-radius: 10px; background: var(--claw-bg-page); }
.evidence-purpose-box strong { color: var(--claw-text); font-size: 12px; }
.evidence-purpose-box span { color: var(--claw-text-muted); font-size: 11px; line-height: 1.55; overflow-wrap: anywhere; }
.evidence-progress-stack { display: flex; flex-direction: column; gap: 8px; }
.evidence-progress-row { display: flex; align-items: flex-end; justify-content: space-between; gap: 12px; }
.evidence-progress-row > div { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.evidence-progress-row span { color: #6d28d9; font-size: 12px; font-weight: 700; }
.evidence-progress-row.settled-row { margin-top: 7px; }
.evidence-progress-row.settled-row span { color: var(--claw-primary); }
.evidence-progress-row small { color: var(--claw-text-muted); font-size: 10px; line-height: 1.4; }
.evidence-progress-row > strong { color: var(--claw-text); font-size: 14px; font-variant-numeric: tabular-nums; }
.settlement-hint { margin-top: 14px; }
.settlement-hint :deep(.el-alert__title) { line-height: 1.55; }
.evidence-stage-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 10px; margin-top: 14px; }
.evidence-stage-grid > div { padding: 10px 12px; border-radius: 9px; background: var(--claw-bg-page); }
.evidence-stage-grid span { display: block; margin-bottom: 5px; color: var(--claw-text-muted); font-size: 11px; }
.evidence-stage-grid strong { color: var(--claw-text); font-size: 15px; font-variant-numeric: tabular-nums; }
.control-evidence-box,
.same-day-funnel-box { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; margin-top: 14px; padding: 12px; border: 1px solid rgba(20, 184, 166, .24); border-radius: 10px; background: rgba(20, 184, 166, .05); }
.same-day-funnel-box { grid-template-columns: repeat(3, minmax(0, 1fr)); border-color: var(--claw-border-light); background: var(--claw-bg-page); }
.control-evidence-box > div,
.same-day-funnel-box > div { display: flex; flex-direction: column; gap: 5px; min-width: 0; }
.control-evidence-box span,
.same-day-funnel-box span { color: var(--claw-text-muted); font-size: 11px; }
.control-evidence-box strong,
.same-day-funnel-box strong { color: var(--claw-text); font-size: 16px; font-variant-numeric: tabular-nums; }
.control-evidence-box p,
.same-day-funnel-box p { grid-column: 1 / -1; margin: 0; color: var(--claw-text-muted); font-size: 10px; line-height: 1.55; }
.current-pool-table-wrap { grid-column: 1 / -1; }
.current-pool-table { min-width: 520px; }
.current-pool-table :deep(.cell) { white-space: nowrap; word-break: normal; }
.evidence-gate-title { margin-top: 18px; color: var(--claw-text); font-size: 13px; font-weight: 700; }
.evidence-gate-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 10px; margin-top: 10px; }
.evidence-gate-item { display: grid; grid-template-columns: auto minmax(0, 1fr); align-items: center; gap: 9px; min-width: 0; padding: 11px; border: 1px solid var(--claw-border); border-radius: 10px; background: var(--claw-bg-page); }
.evidence-gate-item.passed { border-color: rgba(16, 185, 129, .35); background: rgba(16, 185, 129, .06); }
.evidence-gate-item > div { display: flex; flex-direction: column; min-width: 0; gap: 3px; }
.evidence-gate-item span { color: var(--claw-text-muted); font-size: 11px; }
.evidence-gate-item strong { color: var(--claw-text); font-size: 14px; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
.evidence-gate-item small { grid-column: 2; color: var(--claw-text-muted); font-size: 10px; line-height: 1.45; }
.execution-guardrail-box { display: grid; grid-template-columns: auto minmax(0, 1fr); align-items: center; gap: 10px; margin-top: 12px; padding: 12px; border: 1px dashed var(--claw-border); border-radius: 10px; background: var(--claw-bg-card); }
.execution-guardrail-box > div { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.execution-guardrail-box span { color: var(--claw-text-muted); font-size: 11px; }
.execution-guardrail-box strong { color: var(--claw-text); font-size: 14px; font-variant-numeric: tabular-nums; }
.execution-guardrail-box small { grid-column: 2; color: var(--claw-text-muted); font-size: 10px; line-height: 1.5; }
.challenger-account-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); gap: 16px; }
.account-compare-card { position: relative; min-width: 0; overflow: hidden; }
.account-compare-card::before { position: absolute; top: 0; right: 0; left: 0; height: 3px; content: ''; }
.champion-card::before { background: #64748b; }
.challenger-card::before { background: linear-gradient(90deg, #a855f7, #06b6d4); }
.evidence-only-card::before { background: linear-gradient(90deg, #10b981, #14b8a6); }
.compare-card-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin-bottom: 16px; }
.compare-card-head > div { display: flex; flex-direction: column; min-width: 0; gap: 4px; }
.compare-card-head span { color: var(--claw-text-muted); font-size: 12px; }
.compare-card-head strong { color: var(--claw-text); font-size: 16px; overflow-wrap: anywhere; }
.compare-metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(112px, 1fr)); gap: 10px; }
.compare-metrics > div { min-width: 0; padding: 10px; border: 1px solid var(--claw-border-light); border-radius: 9px; background: var(--claw-bg-card); }
.compare-metrics span { display: block; margin-bottom: 5px; color: var(--claw-text-muted); font-size: 11px; overflow-wrap: anywhere; }
.compare-metrics strong { font-size: 14px; font-variant-numeric: tabular-nums; }
.version-text { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size: 11px !important; overflow-wrap: anywhere; }
.chart-caption { margin: 6px 0 4px; color: var(--claw-text-muted); font-size: 12px; line-height: 1.5; }
.challenger-detail-stack { display: flex; flex-direction: column; gap: 16px; min-width: 0; }
.table-scroll-hint { display: none; margin: 6px 0; color: var(--claw-text-muted); font-size: 11px; text-align: right; }
.table-scroll { width: 100%; max-width: 100%; min-width: 0; overflow-x: auto; overscroll-behavior-inline: contain; }
.challenger-position-table { min-width: 1730px; }
.position-summary { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 24px; margin: 8px 0 12px; padding: 12px; border: 1px solid var(--claw-border-light); border-radius: 9px; background: var(--claw-bg-page); color: var(--claw-text-muted); font-size: 12px; }
.position-summary strong { color: var(--claw-text); font-variant-numeric: tabular-nums; }
.challenger-position-table :deep(.cell) { white-space: nowrap; word-break: normal; }
.challenger-signal-table { min-width: 1040px; }
.challenger-action-table { min-width: 1320px; }
.challenger-stock-cell,
.return-cell { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.challenger-stock-cell strong { color: var(--claw-text); font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }
.challenger-stock-cell span,
.return-cell small { color: var(--claw-text-muted); font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
:deep(.el-tabs__header) { margin-bottom: 18px; }
:deep(.el-tabs__nav-wrap::after) { background: var(--claw-border-light); }
:deep(.el-tabs__item) { height: 40px; font-weight: 500; }
:deep(.el-table) { border: 1px solid var(--claw-border); border-radius: 14px; overflow: hidden; box-shadow: var(--claw-shadow-sm); }
:deep(.el-table th.el-table__cell) { background: #f7faff; }
:deep(.auto-log-table .cell),
:deep(.trade-record-table .cell) { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; word-break: normal; }
:deep(.auto-log-table th .cell),
:deep(.trade-record-table th .cell) { font-weight: 700; white-space: nowrap; }
:deep(.trade-record-table .el-table__cell) { padding: 10px 0; }
@media (max-width: 1180px) {
  .challenger-route-overview { grid-template-columns: 1fr; }
  .route-status { align-items: flex-start; white-space: normal; }
}
@media (max-width: 768px) {
  .stat-row { grid-template-columns: repeat(2, 1fr); }
  .trade-forms { grid-template-columns: 1fr; }
  .auto-toolbar { flex-direction: column; align-items: stretch; }
  .auto-summary { grid-template-columns: repeat(2, 1fr); }
  .eval-summary { grid-template-columns: repeat(2, 1fr); }
  .mistake-grid { grid-template-columns: 1fr; }
  .auto-actions { justify-content: flex-end; }
  .challenger-toolbar { flex-direction: column; }
  .challenger-actions { width: 100%; flex-wrap: wrap; justify-content: space-between; }
  .challenger-actions :deep(.el-radio-group) { max-width: 100%; overflow-x: auto; }
  .strategy-coverage-strip { padding: 8px; }
  .coverage-total { order: -1; width: 100%; margin-left: 0; padding: 0 2px 4px; }
  .challenger-route-selector { flex-direction: column; align-items: stretch; }
  .challenger-route-selector :deep(.el-radio-group) { width: 100%; }
  .same-day-funnel-box { grid-template-columns: 1fr; }
  .no-challenger-state { grid-template-columns: 1fr; align-items: flex-start; }
  .no-challenger-badge { width: 52px; height: 52px; border-radius: 14px; font-size: 22px; }
  .comparison-metric-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .challenger-account-grid { grid-template-columns: 1fr; }
  .compare-card-head { flex-direction: column; }
  .compare-metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .evidence-head { flex-direction: column; gap: 8px; }
  .evidence-purpose-box { grid-template-columns: 1fr; }
  .evidence-gate-grid { grid-template-columns: 1fr; }
  .table-scroll-hint { display: block; }
}
@media (max-width: 460px) {
  .comparison-metric { min-height: 94px; }
  .challenger-actions { align-items: stretch; }
  .challenger-actions > :last-child { width: 100%; }
  .evidence-stage-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .control-evidence-box { grid-template-columns: 1fr; }
}
@media (max-width: 340px) {
  .comparison-metric-grid { grid-template-columns: 1fr; }
}
</style>
