<template>
  <div class="page-container">
    <div class="page-shell backtest-page">
      <div class="page-hero">
      <div>
        <h2 class="page-title"><el-icon class="title-icon"><Timer /></el-icon>回测引擎</h2>
        <div class="page-subtitle">统一管理信号回测、策略回测与历史绩效查询</div>
      </div>
      <div class="hero-chip">
        <el-icon><Timer /></el-icon>
        <span>策略验证面板</span>
      </div>
    </div>

    <div class="overview-grid">
      <div class="panel-card data-status-panel">
        <div class="panel-title"><el-icon><DataAnalysis /></el-icon>选股数据状态</div>
        <div class="status-grid">
          <div class="status-item">
            <span class="status-label">研究样本</span>
            <strong>{{ backtestStatus?.signal_count ?? '--' }}</strong>
            <small>{{ compactRange(backtestStatus?.signal_min_time, backtestStatus?.signal_max_time) }}</small>
          </div>
          <div class="status-item">
            <span class="status-label">行情样本</span>
            <strong>{{ backtestStatus?.price_trade_days ?? '--' }}</strong>
            <small>{{ priceSourceText }} · {{ compactRange(backtestStatus?.price_min_date, backtestStatus?.price_max_date) }}</small>
          </div>
          <div class="status-item">
            <span class="status-label">因子快照</span>
            <strong>{{ backtestStatus?.factor_value_count ?? '--' }}</strong>
            <small>{{ backtestStatus?.factor_name_count ? `${backtestStatus.factor_name_count} 个因子` : '暂无因子' }}</small>
          </div>
          <div class="status-item">
            <span class="status-label">最近回测</span>
            <strong>{{ backtestStatus?.run_count ?? '--' }}</strong>
            <small>{{ formatDateTime(backtestStatus?.latest_run_at) || '暂无记录' }}</small>
          </div>
        </div>
        <el-alert
          v-for="item in backtestWarnings"
          :key="item"
          :title="item"
          type="warning"
          :closable="false"
          class="mt-12"
        />
        <div class="prepare-actions">
          <el-button type="primary" size="small" :loading="backfillLoading" :disabled="!canBackfillSignals" @click="runSignalBackfill">
            生成选股研究样本
          </el-button>
          <span class="prepare-hint">{{ prepareHint }}</span>
        </div>
      </div>

      <div class="panel-card recent-runs-panel">
        <div class="panel-title"><el-icon><TrendCharts /></el-icon>最近策略验证</div>
        <el-empty v-if="!recentRuns.length && !metaLoading" description="暂无回测记录" :image-size="72" />
        <el-table v-else :data="recentRuns" stripe size="small" height="214" @row-click="selectRecentRun" @selection-change="handleCompareSelection" class="clickable-table">
          <el-table-column type="selection" width="48" />
          <el-table-column prop="created_at" label="时间" width="132">
            <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
          </el-table-column>
          <el-table-column prop="run_type" label="类型" width="82">
            <template #default="{ row }">{{ runTypeText(row.run_type) }}</template>
          </el-table-column>
          <el-table-column prop="status" label="状态" width="86">
            <template #default="{ row }"><el-tag size="small" :type="row.status === 'completed' ? 'success' : 'info'">{{ statusText(row.status) }}</el-tag></template>
          </el-table-column>
          <el-table-column prop="total_return_pct" label="收益" min-width="76" align="right">
            <template #default="{ row }"><span :class="changeColorClass(row.total_return_pct)">{{ row.total_return_pct == null ? '--' : formatChange(row.total_return_pct) }}</span></template>
          </el-table-column>
        </el-table>
        <div v-if="compareRuns.length" class="compare-panel">
          <div class="compare-head">
            <span>横向对比</span>
            <el-button size="small" text :loading="compareLoading" @click.stop="loadCompareRuns">刷新</el-button>
          </div>
          <el-table :data="compareRows" size="small" stripe class="compare-table">
            <el-table-column prop="label" label="指标" width="86" />
            <el-table-column v-for="row in compareRuns" :key="row.run_id" :label="shortRunId(row.run_id)" min-width="86" align="right">
              <template #default="{ row: metric }">{{ formatCompareValue(metric.key, row[metric.key]) }}</template>
            </el-table-column>
          </el-table>
        </div>
      </div>
    </div>

    <el-tabs v-model="activeTab">
      <el-tab-pane label="信号回测" name="signal">
        <div class="panel-card form-panel">
          <div class="panel-title"><el-icon><DataAnalysis /></el-icon>信号回测参数</div>
          <el-form :model="signalForm" label-width="100px" size="small" class="config-form">
            <el-form-item label="开始日期"><el-input v-model="signalForm.start_date" placeholder="YYYY-MM-DD" /></el-form-item>
            <el-form-item label="结束日期"><el-input v-model="signalForm.end_date" placeholder="YYYY-MM-DD" /></el-form-item>
            <el-form-item label="最低评分"><el-input-number v-model="signalForm.min_score" :min="0" :max="100" /></el-form-item>
            <el-form-item label="持有天数">
              <el-checkbox-group v-model="signalForm.holding_days">
                <el-checkbox :value="1">1天</el-checkbox><el-checkbox :value="3">3天</el-checkbox>
                <el-checkbox :value="5">5天</el-checkbox><el-checkbox :value="10">10天</el-checkbox>
              </el-checkbox-group>
            </el-form-item>
            <el-form-item>
              <el-button type="primary" @click="runSignalBacktest" :loading="running" :disabled="!canSignalBacktest">验证信号收益</el-button>
            </el-form-item>
          </el-form>

          <div v-if="signalResult" class="result-section metrics-panel compact-metrics-panel">
            <div class="stat-row two-col">
              <div class="stat-card bt-stat-card"><div class="metric-head"><el-icon><Histogram /></el-icon><span>信号数</span></div><div class="stat-value">{{ signalResult.total || 0 }}</div></div>
              <div class="stat-card bt-stat-card"><div class="metric-head"><el-icon><DataLine /></el-icon><span>胜率</span></div><div class="stat-value">{{ signalWinRate == null ? '--' : signalWinRate.toFixed(1) + '%' }}</div></div>
            </div>
            <div v-if="signalResult.run_id" class="run-id-line">回测ID：{{ signalResult.run_id }}</div>
            <el-alert v-if="signalResult.status === 'no_signals'" title="当前条件下没有可回测的信号" type="info" :closable="false" class="mt-16" />
          </div>
        </div>
      </el-tab-pane>

      <el-tab-pane label="策略回测" name="strategy">
        <div class="strategy-workbench">
          <div class="panel-card form-panel">
            <div class="panel-title"><el-icon><Cpu /></el-icon>策略回测参数</div>
            <el-form :model="strategyForm" label-width="100px" size="small" class="config-form">
              <el-form-item label="策略">
                <el-select v-model="strategyForm.strategy_id" placeholder="选择策略" class="form-control">
                  <el-option
                    v-for="item in strategies"
                    :key="item.id"
                    :label="item.name"
                    :value="item.id"
                    :disabled="!item.enabled"
                  >
                    <span>{{ item.name }}</span>
                    <span class="option-hint">{{ item.enabled ? item.description : item.disabled_reason }}</span>
                  </el-option>
                </el-select>
              </el-form-item>
              <el-form-item label="开始日期"><el-input v-model="strategyForm.start_date" placeholder="YYYY-MM-DD" @change="loadCandidates" /></el-form-item>
              <el-form-item label="结束日期"><el-input v-model="strategyForm.end_date" placeholder="YYYY-MM-DD" @change="loadCandidates" /></el-form-item>
              <el-form-item label="初始资金"><el-input-number v-model="strategyForm.initial_capital" :min="100000" :step="100000" /></el-form-item>
              <el-form-item label="手续费%"><el-input-number v-model="strategyForm.commission_rate" :min="0" :max="0.01" :step="0.0001" :precision="4" /></el-form-item>
              <el-form-item label="滑点%"><el-input-number v-model="strategyForm.slippage_pct" :min="0" :max="5" :step="0.05" :precision="2" /></el-form-item>
              <el-form-item label="成交量上限%"><el-input-number v-model="strategyForm.volume_limit_pct" :min="0.1" :max="100" :step="1" :precision="1" /></el-form-item>
              <el-form-item label="涨跌停过滤"><el-switch v-model="strategyForm.avoid_limit_up_down" /></el-form-item>
              <el-form-item label="基准代码"><el-input v-model="strategyForm.benchmark_code" placeholder="000001" /></el-form-item>
              <el-form-item label="验证方式">
                <el-select v-model="strategyForm.validation_mode" class="form-control">
                  <el-option label="样本内/样本外" value="split" />
                  <el-option label="Walk-forward" value="walk_forward" />
                  <el-option label="不验证" value="none" />
                </el-select>
              </el-form-item>
              <el-form-item label="止损%"><el-input-number v-model="strategyForm.stop_loss_pct" :min="1" :max="20" /></el-form-item>
              <el-form-item label="止盈%"><el-input-number v-model="strategyForm.take_profit_pct" :min="5" :max="100" /></el-form-item>
              <el-form-item label="最大持仓"><el-input-number v-model="strategyForm.max_positions" :min="1" :max="20" @change="loadCandidates" /></el-form-item>
              <el-form-item label="最低评分"><el-input-number v-model="strategyForm.min_score_to_buy" :min="0" :max="100" @change="loadCandidates" /></el-form-item>
              <el-form-item>
                <el-button type="primary" @click="runStrategyBacktest" :loading="running" :disabled="!canStrategyBacktest">验证这组选股</el-button>
                <el-button @click="loadCandidates" :loading="candidateLoading">刷新选股清单</el-button>
              </el-form-item>
            </el-form>

            <div v-if="strategyResult" class="result-section metrics-panel compact-metrics-panel">
              <div class="stat-row two-col">
                <div class="stat-card bt-stat-card"><div class="metric-head"><el-icon><Flag /></el-icon><span>状态</span></div><div class="stat-value">{{ strategyStatusText }}</div></div>
                <div class="stat-card bt-stat-card"><div class="metric-head"><el-icon><Coin /></el-icon><span>最终资产</span></div><div class="stat-value">{{ formatAmount(strategyMetrics.final_capital || strategyResult.config?.initial_capital) }}</div></div>
              </div>
              <div v-if="strategyResult.run_id" class="run-id-line">回测ID：{{ strategyResult.run_id }}</div>
              <el-alert v-if="strategyResult.message" :title="strategyResult.message" :type="strategyResult.status === 'completed' ? 'success' : 'info'" :closable="false" class="mt-16" />
            </div>
          </div>

          <div class="panel-card candidate-panel">
            <div class="panel-title"><el-icon><DataAnalysis /></el-icon>选股决策</div>
            <div class="rule-grid">
              <div class="rule-item"><span>候选来源</span><strong>研究样本评分</strong></div>
              <div class="rule-item"><span>交易风格</span><strong>短线 1-3 日</strong></div>
              <div class="rule-item"><span>排序方式</span><strong>净收益期望 / 回撤</strong></div>
              <div class="rule-item"><span>入选门槛</span><strong>{{ strategyForm.min_score_to_buy }} 分 + 形态达标</strong></div>
              <div class="rule-item"><span>持仓约束</span><strong>最多 {{ strategyForm.max_positions }} 只</strong></div>
              <div class="rule-item"><span>成交过滤</span><strong>{{ executionRuleText }}</strong></div>
            </div>
            <div class="decision-strip">
              <div><span>入选</span><strong>{{ candidateSummary.selected }}</strong></div>
              <div><span>候补</span><strong>{{ candidateSummary.reserve }}</strong></div>
              <div><span>观察</span><strong>{{ candidateSummary.watch }}</strong></div>
            </div>

            <div class="candidate-head">
              <div>
                <div class="mini-title">选股清单</div>
                <small>{{ candidateDateText }}</small>
              </div>
              <el-button size="small" :loading="candidateLoading" @click="loadCandidates">刷新选股</el-button>
            </div>
            <el-empty v-if="!candidateRows.length && !candidateLoading" description="当前条件没有候选股" :image-size="72" />
            <el-table v-else :data="candidateRows" size="small" stripe height="336" class="candidate-table">
              <el-table-column prop="decision" label="结论" width="60">
                <template #default="{ row }"><el-tag size="small" :type="decisionTagType(row.decision)">{{ row.decision }}</el-tag></template>
              </el-table-column>
              <el-table-column prop="code" label="代码" width="76" />
              <el-table-column prop="name" label="名称" min-width="84" show-overflow-tooltip>
                <template #default="{ row }">{{ row.name || '--' }}</template>
              </el-table-column>
              <el-table-column prop="score" label="评分" width="52" align="right" />
              <el-table-column prop="evidence.short_score" label="短分" width="54" align="right">
                <template #default="{ row }">{{ row.evidence?.short_score == null ? '--' : row.evidence.short_score.toFixed(1) }}</template>
              </el-table-column>
              <el-table-column prop="evidence.sample_count" label="样本" width="52" align="right">
                <template #default="{ row }">{{ row.evidence?.sample_count ?? 0 }}</template>
              </el-table-column>
              <el-table-column prop="evidence.win_rate_1d" label="1日胜" width="66" align="right">
                <template #default="{ row }">{{ row.evidence?.win_rate_1d == null ? '--' : `${row.evidence.win_rate_1d.toFixed(1)}%` }}</template>
              </el-table-column>
              <el-table-column prop="evidence.expected_net_return" label="期望" width="62" align="right">
                <template #default="{ row }"><span :class="changeColorClass(row.evidence?.expected_net_return)">{{ row.evidence?.expected_net_return == null ? '--' : formatChange(row.evidence.expected_net_return) }}</span></template>
              </el-table-column>
              <el-table-column prop="evidence.avg_max_drawdown" label="回撤" width="62" align="right">
                <template #default="{ row }"><span :class="changeColorClass(row.evidence?.avg_max_drawdown)">{{ row.evidence?.avg_max_drawdown == null ? '--' : formatChange(row.evidence.avg_max_drawdown) }}</span></template>
              </el-table-column>
              <el-table-column prop="reason" label="依据" min-width="96" show-overflow-tooltip />
            </el-table>
          </div>
        </div>
      </el-tab-pane>

      <el-tab-pane label="回测结果" name="results">
        <div class="panel-card">
          <div class="panel-title"><el-icon><TrendCharts /></el-icon>结果查询</div>
          <div class="query-row">
            <el-input v-model="runId" placeholder="回测ID" style="width: 360px" @keyup.enter="loadResults" />
            <el-button type="primary" @click="loadResults" :loading="loading">查询回测结果</el-button>
          </div>

          <div v-if="perfData && perfData.run_type === 'strategy'" class="metrics-panel backtest-metrics-panel">
            <div class="stat-row">
              <div class="stat-card bt-stat-card"><div class="metric-head"><el-icon><Wallet /></el-icon><span>初始资金</span></div><div class="stat-value">{{ formatAmount(perfData.initial_capital) }}</div></div>
              <div class="stat-card bt-stat-card"><div class="metric-head"><el-icon><Money /></el-icon><span>当前市值</span></div><div class="stat-value">{{ formatAmount(perfData.current_value) }}</div></div>
              <div class="stat-card bt-stat-card"><div class="metric-head"><el-icon><Top /></el-icon><span>累计收益</span></div><div class="stat-value" :class="changeColorClass(perfData.total_return_pct)">{{ formatChange(perfData.total_return_pct) }}</div></div>
              <div class="stat-card bt-stat-card"><div class="metric-head"><el-icon><Bottom /></el-icon><span>最大回撤</span></div><div class="stat-value text-green">{{ perfData.max_drawdown_pct?.toFixed(2) || '--' }}%</div></div>
              <div class="stat-card bt-stat-card"><div class="metric-head"><el-icon><DataLine /></el-icon><span>超额收益</span></div><div class="stat-value" :class="changeColorClass(perfData.benchmark?.excess_return_pct)">{{ perfData.benchmark?.excess_return_pct == null ? '--' : formatChange(perfData.benchmark.excess_return_pct) }}</div></div>
            </div>
          </div>

          <div v-if="perfData && perfData.run_type === 'signal'" class="metrics-panel backtest-metrics-panel">
            <div class="stat-row">
              <div class="stat-card bt-stat-card"><div class="metric-head"><el-icon><Histogram /></el-icon><span>信号数</span></div><div class="stat-value">{{ perfData.total_count || 0 }}</div></div>
              <div class="stat-card bt-stat-card"><div class="metric-head"><el-icon><DataLine /></el-icon><span>1日胜率</span></div><div class="stat-value">{{ resultSignalStats?.win_rate == null ? '--' : resultSignalStats.win_rate.toFixed(1) + '%' }}</div></div>
              <div class="stat-card bt-stat-card"><div class="metric-head"><el-icon><Top /></el-icon><span>1日均收益</span></div><div class="stat-value" :class="changeColorClass(resultSignalStats?.avg_return)">{{ resultSignalStats?.avg_return == null ? '--' : formatChange(resultSignalStats.avg_return) }}</div></div>
              <div class="stat-card bt-stat-card"><div class="metric-head"><el-icon><Flag /></el-icon><span>状态</span></div><div class="stat-value">{{ resultStatusText }}</div></div>
            </div>
          </div>

          <el-empty v-if="hasQueriedResults && !loading && !perfData" description="未查询到回测结果" />

          <el-alert v-if="perfData?.message" :title="perfData.message" :type="perfData.status === 'completed' ? 'success' : 'info'" :closable="false" class="mt-16" />

          <div class="chart-panel" v-if="perfData?.run_type === 'strategy' && perfNavCurve.length">
            <div class="mini-title">策略净值 vs 基准</div>
            <v-chart :option="perfChartOption" style="height: 300px" autoresize />
          </div>

          <div class="analysis-grid" v-if="perfData?.run_type === 'strategy'">
            <div class="mini-panel" v-if="perfDrawdownCurve.length">
              <div class="mini-title">回撤曲线</div>
              <v-chart :option="drawdownChartOption" style="height: 220px" autoresize />
            </div>
            <div class="mini-panel" v-if="perfMonthlyReturns.length">
              <div class="mini-title">月度收益</div>
              <el-table :data="perfMonthlyReturns" size="small" stripe>
                <el-table-column prop="month" label="月份" />
                <el-table-column prop="return_pct" label="收益" align="right">
                  <template #default="{ row }"><span :class="changeColorClass(row.return_pct)">{{ formatChange(row.return_pct) }}</span></template>
                </el-table-column>
              </el-table>
            </div>
            <div class="mini-panel" v-if="tradeDistributionRows.length">
              <div class="mini-title">交易分布</div>
              <el-table :data="tradeDistributionRows" size="small" stripe>
                <el-table-column prop="label" label="区间" />
                <el-table-column prop="count" label="次数" align="right" />
              </el-table>
            </div>
            <div class="mini-panel" v-if="validationRows.length">
              <div class="mini-title">样本验证</div>
              <el-table :data="validationRows" size="small" stripe>
                <el-table-column prop="labelText" label="窗口" width="112" />
                <el-table-column prop="total_return_pct" label="收益" align="right">
                  <template #default="{ row }"><span :class="changeColorClass(row.total_return_pct)">{{ formatChange(row.total_return_pct) }}</span></template>
                </el-table-column>
                <el-table-column prop="excess_return_pct" label="超额" align="right">
                  <template #default="{ row }"><span :class="changeColorClass(row.excess_return_pct)">{{ row.excess_return_pct == null ? '--' : formatChange(row.excess_return_pct) }}</span></template>
                </el-table-column>
              </el-table>
            </div>
          </div>

          <div v-if="perfData?.run_type === 'strategy' && perfTrades.length" class="mini-title mt-16">交易明细</div>
          <el-table v-if="perfData?.run_type === 'strategy' && perfTrades.length" :data="perfTrades" stripe size="small" height="420" class="result-trade-table">
            <el-table-column prop="time" label="时间" width="116" />
            <el-table-column prop="code" label="代码" width="88" />
            <el-table-column prop="type" label="方向" width="70" align="center">
              <template #default="{ row }"><el-tag :type="row.type === 'buy' ? 'danger' : 'success'" size="small">{{ row.type === 'buy' ? '买' : '卖' }}</el-tag></template>
            </el-table-column>
            <el-table-column prop="price" label="价格" width="96" align="right">
              <template #default="{ row }">{{ formatNumber(row.price, 2) }}</template>
            </el-table-column>
            <el-table-column prop="shares" label="股数" width="96" align="right" />
            <el-table-column prop="amount" label="成交额" width="118" align="right">
              <template #default="{ row }">{{ formatNumber(row.amount, 2) }}</template>
            </el-table-column>
            <el-table-column prop="pnl" label="盈亏" min-width="120" align="right">
              <template #default="{ row }"><span v-if="row.pnl" :class="changeColorClass(row.pnl)">{{ formatNumber(row.pnl, 2) }}</span></template>
            </el-table-column>
          </el-table>

          <el-table v-if="perfData?.run_type === 'signal' && perfSignals.length" :data="perfSignals" stripe size="small" class="mt-16">
            <el-table-column prop="signal_date" label="信号日期" width="110" />
            <el-table-column prop="code" label="代码" width="80" />
            <el-table-column prop="type" label="类型" width="100" />
            <el-table-column prop="score" label="评分" width="80" align="right" />
            <el-table-column prop="buy_price" label="买入价" width="90" align="right" />
            <el-table-column prop="return_1d" label="1日收益" width="100" align="right">
              <template #default="{ row }"><span :class="changeColorClass(row.return_1d)">{{ row.return_1d == null ? '--' : formatChange(row.return_1d) }}</span></template>
            </el-table-column>
            <el-table-column prop="return_3d" label="3日收益" width="100" align="right">
              <template #default="{ row }"><span :class="changeColorClass(row.return_3d)">{{ row.return_3d == null ? '--' : formatChange(row.return_3d) }}</span></template>
            </el-table-column>
            <el-table-column prop="return_5d" label="5日收益" width="100" align="right">
              <template #default="{ row }"><span :class="changeColorClass(row.return_5d)">{{ row.return_5d == null ? '--' : formatChange(row.return_5d) }}</span></template>
            </el-table-column>
            <el-table-column prop="return_10d" label="10日收益" width="100" align="right">
              <template #default="{ row }"><span :class="changeColorClass(row.return_10d)">{{ row.return_10d == null ? '--' : formatChange(row.return_10d) }}</span></template>
            </el-table-column>
          </el-table>
        </div>
      </el-tab-pane>
    </el-tabs>
    </div>
  </div>
</template>

<script setup>
import { defineAsyncComponent, ref, computed, onMounted } from 'vue'
const VChart = defineAsyncComponent(() => import('vue-echarts'))
import { ensureLineChartsRegistered } from '@/composables/echarts/line'
import {
  backtestSignals,
  backtestStrategy,
  getBacktestPerformance,
  getBacktestTrades,
  getBacktestSignalResults,
  getBacktestStatus,
  getBacktestStrategies,
  getBacktestRuns,
  getBacktestCandidates,
  backfillBacktestSignals,
  compareBacktestRuns,
} from '@/api'
import { formatChange, changeColorClass, formatAmount } from '@/composables/useUtils'
import { notifySuccess, notifyWarning } from '@/utils/message'

ensureLineChartsRegistered()

const activeTab = ref('signal')
const running = ref(false)
const loading = ref(false)
const metaLoading = ref(false)
const backfillLoading = ref(false)
const compareLoading = ref(false)
const candidateLoading = ref(false)

const formatDateInput = (date) => {
  const year = date.getFullYear()
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}
const daysAgo = (days) => {
  const date = new Date()
  date.setDate(date.getDate() - days)
  return formatDateInput(date)
}

const defaultStartDate = daysAgo(90)
const defaultEndDate = formatDateInput(new Date())

const signalForm = ref({ start_date: defaultStartDate, end_date: defaultEndDate, min_score: 50, holding_days: [1, 3, 5, 10] })
const strategyForm = ref({
  strategy_id: 'signal_score_rank',
  start_date: defaultStartDate,
  end_date: defaultEndDate,
  initial_capital: 1000000,
  commission_rate: 0.0003,
  slippage_pct: 0.1,
  volume_limit_pct: 10,
  avoid_limit_up_down: true,
  benchmark_code: '000001',
  validation_mode: 'split',
  stop_loss_pct: 7,
  take_profit_pct: 30,
  max_positions: 5,
  min_score_to_buy: 70,
})
const signalResult = ref(null)
const strategyResult = ref(null)
const backtestStatus = ref(null)
const strategies = ref([])
const recentRuns = ref([])
const compareRuns = ref([])
const candidateRows = ref([])
const candidateDate = ref('')
const candidateRequestSeq = ref(0)

const runId = ref('')
const perfData = ref(null)
const perfNavCurve = ref([])
const perfBenchmarkCurve = ref([])
const perfDrawdownCurve = ref([])
const perfMonthlyReturns = ref([])
const perfTrades = ref([])
const perfSignals = ref([])
const hasQueriedResults = ref(false)

const backtestWarnings = computed(() => backtestStatus.value?.warnings || [])
const canSignalBacktest = computed(() => Boolean(backtestStatus.value?.can_signal_backtest))
const canBackfillSignals = computed(() => Boolean(backtestStatus.value?.price_count))
const selectedStrategy = computed(() => strategies.value.find(item => item.id === strategyForm.value.strategy_id))
const canStrategyBacktest = computed(() => Boolean(backtestStatus.value?.can_strategy_backtest && selectedStrategy.value?.enabled))
const executionRuleText = computed(() => {
  const rules = []
  if (strategyForm.value.avoid_limit_up_down) rules.push('涨跌停')
  rules.push(`量限${strategyForm.value.volume_limit_pct}%`)
  return rules.join(' / ')
})
const candidateDateText = computed(() => candidateDate.value ? `信号日 ${candidateDate.value}` : '等待候选数据')
const candidateSummary = computed(() => ({
  selected: candidateRows.value.filter(row => row.decision === '入选').length,
  reserve: candidateRows.value.filter(row => row.decision === '候补').length,
  watch: candidateRows.value.filter(row => row.decision === '观察').length,
}))
const prepareHint = computed(() => {
  if (!backtestStatus.value) return '读取数据状态中'
  if (!backtestStatus.value.price_count) return '需要先采集日 K 行情'
  if (!backtestStatus.value.signal_count) return '用历史 K 线生成研究样本'
  return `已有 ${backtestStatus.value.signal_count} 条研究样本，可按需补充`
})
const priceSourceText = computed(() => {
  const map = {
    stock_kline: '日K',
    stock_daily: '日线',
  }
  return map[backtestStatus.value?.price_source] || '无行情'
})
const compareRows = [
  { key: 'total_return_pct', label: '收益' },
  { key: 'benchmark_return_pct', label: '基准' },
  { key: 'excess_return_pct', label: '超额' },
  { key: 'max_drawdown_pct', label: '回撤' },
  { key: 'sharpe_ratio', label: '夏普' },
  { key: 'win_rate_pct', label: '胜率' },
  { key: 'total_trades', label: '交易' },
]

const signalPrimaryStat = computed(() => {
  const stats = signalResult.value?.stats || {}
  for (const days of signalForm.value.holding_days || []) {
    const item = stats[`return_${days}d`]
    if (item && item.count > 0) return item
  }
  return Object.values(stats)[0] || null
})
const signalWinRate = computed(() => signalPrimaryStat.value?.win_rate ?? null)
const strategyMetrics = computed(() => strategyResult.value?.metrics || {})
const strategyStatusText = computed(() => {
  const map = {
    completed: '已完成',
    no_scores: '无信号',
    ready: '已配置',
  }
  return map[strategyResult.value?.status] || strategyResult.value?.status || '--'
})
const resultSignalStats = computed(() => {
  const stats = perfData.value?.metrics || {}
  return stats.return_1d || stats.return_3d || stats.return_5d || stats.return_10d || null
})
const resultStatusText = computed(() => {
  const map = {
    completed: '已完成',
    no_signals: '无信号',
    no_scores: '无信号',
  }
  return map[perfData.value?.status] || perfData.value?.status || '--'
})

function compactRange(start, end) {
  if (!start && !end) return '暂无数据'
  const left = String(start || '').slice(0, 10)
  const right = String(end || '').slice(0, 10)
  return left === right ? left : `${left} 至 ${right}`
}

function formatDateTime(value) {
  if (!value) return ''
  return String(value).replace('T', ' ').slice(0, 16)
}

function statusText(status) {
  const map = {
    completed: '已完成',
    no_signals: '无信号',
    no_scores: '无信号',
    failed: '失败',
  }
  return map[status] || status || '--'
}

function runTypeText(type) {
  const map = {
    signal: '信号',
    strategy: '策略',
  }
  return map[type] || type || '--'
}

function shortRunId(id) {
  return id ? id.slice(0, 8) : '--'
}

function formatCompareValue(key, value) {
  if (value == null) return '--'
  if (['total_return_pct', 'benchmark_return_pct', 'excess_return_pct', 'max_drawdown_pct', 'win_rate_pct'].includes(key)) {
    return `${Number(value).toFixed(2)}%`
  }
  if (key === 'sharpe_ratio') return Number(value).toFixed(2)
  return value
}

function formatNumber(value, digits = 2) {
  if (value == null || Number.isNaN(Number(value))) return '--'
  return Number(value).toFixed(digits)
}

function decisionTagType(decision) {
  if (decision === '入选') return 'success'
  if (decision === '候补') return 'warning'
  return 'info'
}

const perfChartOption = computed(() => {
  const items = perfNavCurve.value
  if (!items.length) return {}
  const benchmark = perfBenchmarkCurve.value || []
  return {
    backgroundColor: 'transparent',
    tooltip: { trigger: 'axis' },
    grid: { left: 60, right: 20, top: 10, bottom: 30 },
    xAxis: { type: 'category', data: items.map(i => i.date?.slice(5) || ''), axisLabel: { color: '#7a8aa0' } },
    yAxis: { type: 'value', axisLabel: { color: '#7a8aa0' }, splitLine: { lineStyle: { color: '#edf2fb' } } },
    legend: { top: 0, right: 12, data: ['策略', '基准'] },
    series: [
      { name: '策略', type: 'line', data: items.map(i => i.nav), smooth: true, lineStyle: { color: '#007aff', width: 2 }, areaStyle: { color: 'rgba(0,122,255,0.10)' } },
      { name: '基准', type: 'line', data: benchmark.map(i => i.nav), smooth: true, lineStyle: { color: '#8b98a8', width: 2, type: 'dashed' } },
    ],
  }
})

const drawdownChartOption = computed(() => {
  const items = perfDrawdownCurve.value
  if (!items.length) return {}
  return {
    backgroundColor: 'transparent',
    tooltip: { trigger: 'axis' },
    grid: { left: 58, right: 16, top: 10, bottom: 28 },
    xAxis: { type: 'category', data: items.map(i => i.date?.slice(5) || ''), axisLabel: { color: '#7a8aa0' } },
    yAxis: { type: 'value', axisLabel: { color: '#7a8aa0', formatter: '{value}%' }, splitLine: { lineStyle: { color: '#edf2fb' } } },
    series: [{ type: 'line', data: items.map(i => i.drawdown_pct), smooth: true, lineStyle: { color: '#ef4444', width: 2 }, areaStyle: { color: 'rgba(239,68,68,0.10)' } }],
  }
})

const tradeDistributionRows = computed(() => {
  const buckets = perfData.value?.trade_distribution?.buckets || {}
  const labels = {
    large_loss: '大亏',
    loss: '亏损',
    small_profit: '小赚',
    profit: '盈利',
    large_profit: '大赚',
  }
  return Object.keys(labels).map(key => ({ key, label: labels[key], count: buckets[key] || 0 }))
})

const validationRows = computed(() => {
  const labelMap = {
    in_sample: '样本内',
    out_of_sample: '样本外',
  }
  return (perfData.value?.validation?.windows || []).map(row => ({
    ...row,
    labelText: labelMap[row.label] || row.label,
  }))
})

async function runSignalBacktest() {
  if (running.value) return
  if (!canSignalBacktest.value) {
    notifyWarning('缺少研究样本或日线行情，暂时不能执行信号回测')
    return
  }
  running.value = true
  try {
    signalResult.value = await backtestSignals(signalForm.value)
    if (signalResult.value?.run_id) runId.value = signalResult.value.run_id
    if (signalResult.value?.total) {
      notifySuccess(`完成 ${signalResult.value.total} 条信号回测`)
    } else {
      notifyWarning('当前条件下没有可回测的信号')
    }
    await loadMeta()
  } catch {
    signalResult.value = null
  } finally {
    running.value = false
  }
}
async function runStrategyBacktest() {
  if (running.value) return
  if (!canStrategyBacktest.value) {
    notifyWarning(selectedStrategy.value?.disabled_reason || '当前策略暂不可执行')
    return
  }
  if (!strategyForm.value.start_date || !strategyForm.value.end_date) {
    notifyWarning('请先填写策略回测日期范围')
    return
  }
  running.value = true
  try {
    strategyResult.value = await backtestStrategy(strategyForm.value)
    if (strategyResult.value?.run_id) runId.value = strategyResult.value.run_id
    if (strategyResult.value?.status === 'completed') {
      notifySuccess('策略回测完成')
    } else {
      notifyWarning(strategyResult.value?.message || '策略回测没有生成交易')
    }
    await loadMeta()
  } catch {
    strategyResult.value = null
  } finally {
    running.value = false
  }
}

async function runSignalBackfill() {
  if (backfillLoading.value) return
  if (!canBackfillSignals.value) {
    notifyWarning('暂无可用行情，不能生成研究样本')
    return
  }
  backfillLoading.value = true
  try {
    const result = await backfillBacktestSignals({
      max_codes: 300,
      min_score: 70,
      max_signals: 1000,
    })
    notifySuccess(`研究样本生成完成：新增 ${result.created || 0} 条，更新 ${result.updated || 0} 条`)
    await loadMeta()
  } finally {
    backfillLoading.value = false
  }
}
async function loadResults() {
  if (!runId.value) {
    notifyWarning('请输入回测ID')
    return
  }
  loading.value = true
  hasQueriedResults.value = true
  perfData.value = null
  perfNavCurve.value = []
  perfBenchmarkCurve.value = []
  perfDrawdownCurve.value = []
  perfMonthlyReturns.value = []
  perfTrades.value = []
  perfSignals.value = []
  try {
    const p = await getBacktestPerformance(runId.value)
    perfData.value = p
    perfNavCurve.value = p.nav_curve || []
    perfBenchmarkCurve.value = p.benchmark_curve || []
    perfDrawdownCurve.value = p.drawdown_curve || []
    perfMonthlyReturns.value = p.monthly_returns || []
    if (p.run_type === 'signal') {
      const signalResult = await getBacktestSignalResults(runId.value)
      perfSignals.value = signalResult.signals || []
    } else {
      const tradeResult = await getBacktestTrades(runId.value)
      perfTrades.value = tradeResult.trades || []
    }
    if (!perfData.value) notifyWarning('未查询到回测结果')
  } catch {
    perfData.value = null
  } finally {
    loading.value = false
  }
}

async function loadMeta() {
  metaLoading.value = true
  try {
    const [status, strategyResult, runsResult] = await Promise.all([
      getBacktestStatus(),
      getBacktestStrategies(),
      getBacktestRuns({ limit: 10 }),
    ])
    backtestStatus.value = status
    strategies.value = strategyResult.strategies || []
    recentRuns.value = runsResult.runs || []
    if (!selectedStrategy.value || !selectedStrategy.value.enabled) {
      const defaultStrategy = strategies.value.find(item => item.default && item.enabled) || strategies.value.find(item => item.enabled)
      if (defaultStrategy) strategyForm.value.strategy_id = defaultStrategy.id
    }
    if (canStrategyBacktest.value) loadCandidates()
  } finally {
    metaLoading.value = false
  }
}

async function loadCandidates() {
  const requestId = candidateRequestSeq.value + 1
  candidateRequestSeq.value = requestId
  candidateLoading.value = true
  try {
    const result = await getBacktestCandidates({
      start_date: strategyForm.value.start_date,
      end_date: strategyForm.value.end_date,
      min_score: strategyForm.value.min_score_to_buy,
      max_positions: strategyForm.value.max_positions,
      limit: 20,
    })
    if (requestId !== candidateRequestSeq.value) return
    candidateRows.value = result.candidates || []
    candidateDate.value = result.signal_date || ''
  } finally {
    if (requestId === candidateRequestSeq.value) candidateLoading.value = false
  }
}

function selectRecentRun(row) {
  runId.value = row.run_id
  activeTab.value = 'results'
  loadResults()
}

function handleCompareSelection(rows) {
  compareRuns.value = rows.slice(0, 4)
  if (compareRuns.value.length >= 2) loadCompareRuns()
}

async function loadCompareRuns() {
  if (compareRuns.value.length < 2) return
  compareLoading.value = true
  try {
    const result = await compareBacktestRuns({
      run_ids: compareRuns.value.map(row => row.run_id).join(','),
    })
    compareRuns.value = result.runs || compareRuns.value
  } finally {
    compareLoading.value = false
  }
}

onMounted(() => {
  loadMeta()
})
</script>

<style scoped lang="scss">
.backtest-page { display: flex; flex-direction: column; gap: 18px; }
.bt-stat-card { padding: 16px; background: var(--claw-bg-card); border: 1px solid var(--claw-border); border-radius: 14px; box-shadow: var(--claw-shadow-sm); }
.form-panel { max-width: 760px; }
.config-form { max-width: 600px; }
.form-control { width: 100%; }
.strategy-workbench { display: grid; grid-template-columns: minmax(520px, 0.95fr) minmax(420px, 1.05fr); gap: 16px; align-items: start; }
.candidate-panel { min-width: 0; }
.candidate-table { width: 100%; }
.rule-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; margin-bottom: 16px; }
.rule-item { min-width: 0; padding: 12px; border: 1px solid var(--claw-border-light); border-radius: 8px; background: #f7faff; }
.rule-item span { display: block; color: var(--claw-text-muted); font-size: 12px; margin-bottom: 6px; }
.rule-item strong { display: block; color: var(--claw-text-primary); font-size: 14px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.decision-strip { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin-bottom: 16px; }
.decision-strip div { padding: 12px; border: 1px solid var(--claw-border); border-radius: 8px; background: #fff; }
.decision-strip span { display: block; color: var(--claw-text-muted); font-size: 12px; margin-bottom: 4px; }
.decision-strip strong { color: var(--claw-text-primary); font-size: 22px; line-height: 1; }
.candidate-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 10px; }
.candidate-head small { color: var(--claw-text-muted); font-size: 12px; }
.overview-grid { display: grid; grid-template-columns: minmax(0, 1.25fr) minmax(360px, 0.75fr); gap: 16px; }
.data-status-panel, .recent-runs-panel { min-width: 0; }
.status-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }
.status-item { min-width: 0; padding: 14px; background: #f7faff; border: 1px solid var(--claw-border-light); border-radius: 10px; }
.status-label { display: block; color: var(--claw-text-muted); font-size: 12px; margin-bottom: 8px; }
.status-item strong { display: block; color: var(--claw-text-primary); font-size: 24px; line-height: 1.1; }
.status-item small { display: block; margin-top: 6px; color: var(--claw-text-muted); font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.option-hint { float: right; max-width: 260px; color: var(--claw-text-muted); font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.prepare-actions { display: flex; align-items: center; gap: 12px; margin-top: 14px; flex-wrap: wrap; }
.prepare-hint { color: var(--claw-text-muted); font-size: 12px; }
.compare-panel { margin-top: 12px; }
.compare-head { display: flex; justify-content: space-between; align-items: center; color: var(--claw-text-secondary); font-size: 13px; margin-bottom: 8px; }
.compare-table { box-shadow: none; }
.result-section { margin-top: 12px; }
.backtest-metrics-panel { padding: 4px; margin-bottom: 16px; }
.compact-metrics-panel { padding: 4px; }
.stat-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 0; }
.two-col { grid-template-columns: repeat(2, 1fr); }
.query-row { display: flex; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }
.chart-panel { margin-bottom: 16px; }
.analysis-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; margin-bottom: 16px; }
.mini-panel { min-width: 0; padding: 14px; border: 1px solid var(--claw-border); border-radius: 10px; background: #fff; }
.mini-title { color: var(--claw-text-secondary); font-size: 13px; font-weight: 600; margin-bottom: 10px; }
.run-id-line { margin-top: 12px; color: var(--claw-text-muted); font-size: 12px; word-break: break-all; }
.mt-16 { margin-top: 16px; }
.mt-12 { margin-top: 12px; }
:deep(.el-tabs__header) { margin-bottom: 18px; }
:deep(.el-tabs__nav-wrap::after) { background: var(--claw-border-light); }
:deep(.el-tabs__item) { height: 40px; font-weight: 500; }
:deep(.el-table) { border: 1px solid var(--claw-border); border-radius: 14px; overflow: hidden; box-shadow: var(--claw-shadow-sm); }
:deep(.el-table th.el-table__cell) { background: #f7faff; }
:deep(.clickable-table .el-table__row) { cursor: pointer; }
:deep(.candidate-table .cell) { white-space: nowrap; line-height: 22px; }
:deep(.candidate-table .el-table__cell) { padding: 8px 0; }
:deep(.result-trade-table) { width: 100%; }
:deep(.result-trade-table .cell) { white-space: nowrap; line-height: 22px; }
:deep(.result-trade-table .el-table__cell) { padding: 8px 0; }
@media (max-width: 768px) {
  .overview-grid, .strategy-workbench, .stat-row, .two-col, .status-grid, .analysis-grid, .rule-grid, .decision-strip { grid-template-columns: 1fr; }
  .option-hint { display: none; }
}
</style>
