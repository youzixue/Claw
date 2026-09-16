<template>
  <div class="plan-container">
    <div class="plan-view-toolbar">
      <el-radio-group v-model="displayMode" size="small">
        <el-radio-button value="plans">预案候选 {{ rows.length }}</el-radio-button>
        <el-radio-button value="patterns">形态检测池 {{ patternRows.length }}</el-radio-button>
      </el-radio-group>
      <div class="snapshot-status">
        <span v-if="snapshotTime" class="snapshot-time">快照 {{ formatSnapshotTime(snapshotTime) }}</span>
        <el-tag v-if="snapshotStale" type="warning" size="small" effect="plain">快照较旧</el-tag>
        <el-button size="small" :loading="loading" @click="emit('refresh')">重新扫描</el-button>
      </div>
    </div>

    <el-alert
      v-if="error"
      class="plan-alert"
      type="error"
      :title="error"
      :closable="false"
      show-icon
    />
    <el-alert
      v-else-if="displayMode === 'plans'"
      class="plan-alert"
      type="info"
      title="本页按事件锁价、题材换手、情绪记忆、业绩预期差、断板二波五类高标归因；亏损不作硬过滤，纯容量趋势已排除。所有高标和ST在次日确认前仓位为0，ST始终仅研究。"
      :closable="false"
      show-icon
    />
    <el-alert
      v-else-if="displayMode === 'patterns'"
      class="plan-alert"
      type="warning"
      title="形态命中不等于买点：牛股潜力评分只负责入池；主升/潜力回踩须触及盘前支撑，再经滚动60秒价升量增、VWAP修复及盘口/资金/主驱动确认后才推送A2。"
      :closable="false"
      show-icon
    />

    <div v-if="latestReplay.status === 'ok'" class="plan-replay-panel">
      <div class="plan-replay-head">
        <strong>上一版预案复盘</strong>
        <span>{{ latestReplay.prediction_trade_date }} → {{ latestReplay.actual_trade_date }}</span>
        <span class="muted">覆盖命中与可执行命中分开统计</span>
      </div>
      <div class="plan-replay-grid">
        <div class="plan-replay-metric">
          <span>原始覆盖</span>
          <strong>{{ latestReplay.predicted_count ?? 0 }} / 涨停{{ latestReplay.limit_up_hit_count ?? 0 }}</strong>
        </div>
        <div class="plan-replay-metric">
          <span>可执行预案</span>
          <strong>{{ latestReplay.actionable_predicted_count ?? 0 }} / 涨停{{ latestReplay.actionable_limit_up_hit_count ?? 0 }}</strong>
        </div>
        <div class="plan-replay-metric">
          <span>覆盖上涨命中率</span>
          <strong>{{ formatRate(latestReplay.directional_precision) }}</strong>
        </div>
        <div class="plan-replay-metric">
          <span>可执行上涨命中率</span>
          <strong>{{ formatRate(latestReplay.actionable_directional_precision) }}</strong>
        </div>
      </div>
    </div>

    <!-- 筛选栏 -->
    <div v-if="displayMode === 'plans'" class="plan-filter">
      <div v-for="group in planFilterGroups" :key="group.label" class="plan-filter-row">
        <span class="plan-filter-label">{{ group.label }}</span>
        <el-radio-group v-model="strategyFilter" size="small">
          <el-radio-button
            v-for="option in group.options"
            :key="option.value"
            :value="option.value"
            :disabled="option.value !== 'all' && filterCount(option.value) === 0"
          >
            {{ option.label }} <span class="filter-count">{{ filterCount(option.value) }}</span>
          </el-radio-button>
        </el-radio-group>
      </div>
      <span class="buy-filter-note">“条件买点”表示盘后条件已成立，但仍须次日触发；表中仓位是触发后的计划上限，触发前实际仓位始终为0。0条标签会自动禁用。</span>
      <div class="direct-buy-gate" :class="{ 'is-open': marketBreadth.direct_buy_ok }">
        <strong>市场仓位：</strong>{{ directBuyGateText }}
      </div>
    </div>

    <!-- 表格 -->
    <div v-if="displayMode === 'plans'" v-loading="loading" class="table-container anomaly-table-wrap">
      <div v-if="strategyFilter === 'second_wave'" class="second-wave-guide">
        <span>本页同时展示普通二波与高标下杀准备态；高标下杀准备态仓位为0，仅在止跌急拉、放量和盘口/资金/板块共振后形成A2提醒。</span>
      </div>
      <div v-if="['event_relay', 'relay_watch'].includes(strategyFilter)" class="second-wave-guide event-relay-guide">
        <span>事件涨停仅表示进入次日接力检测池；竞价、开盘换手、VWAP、盘口和板块前排未二次确认前仓位为0，不排一字板。</span>
      </div>
      <div v-if="['theme_turnover', 'emotion_memory', 'earnings_surprise'].includes(strategyFilter)" class="second-wave-guide event-relay-guide">
        <span>该分类按驱动与筹码结构归因；同股可以同时命中多类。ST只展示研究逻辑，不生成仓位、模拟盘订单或飞书买点。</span>
      </div>
      <div v-if="strategyFilter === 'leader_linkage'" class="second-wave-guide leader-linkage-guide">
        <span>看A做B必须同属核心主营产业链或明确跨行业因果主题，且B自身均线、量能和资金形态达标；默认仓位为0，盘中二次确认后才形成A2提醒。</span>
      </div>
      <el-table :data="filteredRows" :fit="false" stripe size="small" :empty-text="emptyText" row-key="code" :row-class-name="planRowClassName" @row-click="goStock" class="anomaly-table plan-table">
        <el-table-column prop="code" label="代码" width="96">
          <template #default="{ row }"><strong class="code-text">{{ row.code }}</strong></template>
        </el-table-column>
        <el-table-column prop="name" label="名称" width="104" show-overflow-tooltip>
          <template #default="{ row }">
            <span>{{ row.name }}</span>
            <el-tag v-if="row.research_only" class="st-research-tag" size="small" type="info" effect="plain">研究</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="price" label="现价" width="96" align="right">
          <template #default="{ row }"><span class="num-text">{{ row.price?.toFixed(2) || '--' }}</span></template>
        </el-table-column>
        <el-table-column prop="change_pct" label="涨幅" width="88" align="right">
          <template #default="{ row }">
            <span class="num-text" :class="row.change_pct > 0 ? 'text-red' : row.change_pct < 0 ? 'text-green' : ''">
              {{ row.change_pct > 0 ? '+' : '' }}{{ row.change_pct?.toFixed(2) }}
            </span>
          </template>
        </el-table-column>
        <el-table-column prop="bull_level" label="评级" width="76" align="center">
          <template #default="{ row }"><span class="level-badge" :style="{ backgroundColor: levelColor(row.bull_level) }">{{ row.bull_level }}</span></template>
        </el-table-column>
        <el-table-column label="关注分" width="88" align="center">
          <template #default="{ row }">
            <el-tag
              size="small"
              :type="row.plan_priority_tier === 'A' ? 'danger' : row.plan_priority_tier === 'B' ? 'warning' : 'info'"
              effect="plain"
            >{{ Number(row.plan_priority_score || 0).toFixed(0) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="策略" width="250" show-overflow-tooltip>
          <template #default="{ row }">
            <div v-for="s in row.strategies || []" :key="s.strategy_type" class="strategy-tag-row">
              <el-tag size="small" :type="strategyType(s.strategy_type)" effect="dark">{{ s.strategy_label }}</el-tag>
              <span class="strategy-detail">
                <strong v-if="s.entry_price_hint && s.entry_price_hint !== '--'">{{ s.entry_price_hint }}；</strong>{{ s.entry_condition }}
              </span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="形态" width="232" show-overflow-tooltip>
          <template #default="{ row }">
            <div v-if="row.candidate_source_label" class="pattern-cell">
              <el-tag size="small" :type="patternType(row.candidate_source)" effect="plain">
                {{ row.candidate_source_label }}
              </el-tag>
              <div v-if="displayHighBoardTypeLabels(row).length" class="high-board-types">
                <el-tag
                  v-for="label in displayHighBoardTypeLabels(row)"
                  :key="label"
                  size="small"
                  :type="highBoardTypeTag(label)"
                  effect="plain"
                >{{ label }}</el-tag>
              </div>
              <span v-if="row.high_board_reasons?.length" class="pattern-meta">
                {{ row.high_board_reasons.join('；') }}
              </span>
              <span v-if="isSecondWaveResetRow(row)" class="pattern-meta">
                {{ secondWaveResetSummary(row) }}
              </span>
              <span v-if="isEventRelayRow(row)" class="pattern-meta">
                {{ eventRelaySummary(row) }}
              </span>
              <span v-if="isLeaderLinkageRow(row)" class="pattern-meta">
                {{ leaderLinkageSummary(row) }}
              </span>
              <span v-if="longCycleSummary(row)" class="pattern-meta">
                {{ longCycleSummary(row) }}
              </span>
            </div>
            <span v-else class="muted">--</span>
          </template>
        </el-table-column>
        <el-table-column label="触发后仓位" width="94" align="center">
          <template #default="{ row }">
            <span v-for="s in plannedPositionStrategies(row)" :key="s.strategy_type" class="position-text">{{ s.position_after_trigger || s.position_ratio }}</span>
            <span v-if="plannedPositionStrategies(row).length === 0" class="watch-position">0</span>
          </template>
        </el-table-column>
        <el-table-column label="止损→目标" width="172" align="right">
          <template #default="{ row }">
            <div v-for="s in (row.strategies||[]).filter(s=>s.stop_loss>0)" :key="s.strategy_type" class="price-range">
              <span class="text-green">{{ s.stop_loss?.toFixed(2) }}</span>
              <span class="arrow">→</span>
              <span class="text-red">{{ s.target_price?.toFixed(2) }}</span>
              <span class="rr-tag" v-if="s.risk_reward_ratio">R{{ s.risk_reward_ratio }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="支撑/压力" width="174" align="right">
          <template #default="{ row }">
            <div v-if="row.support_detail || row.resistance_detail" class="sr-row">
              <span class="sr-label">{{ isEventRelayRow(row) ? '前板价' : '低吸' }}</span>{{ row.support_detail?.primary?.toFixed(2) || '--' }}
              <span class="sr-sep">/</span>
              <span class="sr-label">{{ isEventRelayRow(row) ? '确认限' : '减仓' }}</span>{{ row.resistance_detail?.primary?.toFixed(2) || '--' }}
            </div>
          </template>
        </el-table-column>
        <el-table-column label="资金" width="74" align="right">
          <template #default="{ row }">
            <span class="num-text" :class="row.fund_5d_direction === '净流入' ? 'text-red' : row.fund_5d_direction === '净流出' ? 'text-green' : ''">
              {{ row.fund_5d_billion?.toFixed(1) || 0 }}亿
            </span>
          </template>
        </el-table-column>
        <el-table-column label="技术" width="132" show-overflow-tooltip>
          <template #default="{ row }">
            <span class="tech-summary">{{ row.tech_summary }}</span>
          </template>
        </el-table-column>
        <el-table-column label="板块" width="136" show-overflow-tooltip>
          <template #default="{ row }">
            <div class="sector-emotion">
              <div v-if="row.sector_resonance" class="resonance-row">
                <el-tag size="small" :type="resonanceType(row.sector_resonance)" effect="plain">{{ row.sector_resonance }}</el-tag>
                <span v-if="row.sector_resonance_name" class="sector-name">{{ row.sector_resonance_name }}</span>
              </div>
              <div v-if="row.sector_resonance && !row.sector_driver_causal" class="sentiment-row">
                <span class="muted">无可验证产业主驱动</span>
              </div>
              <div v-if="row.sentiment_cycle" class="sentiment-row">
                <span class="sentiment-label">{{ row.sentiment_cycle }}</span>
              </div>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="失效条件" width="220" show-overflow-tooltip>
          <template #default="{ row }">
            <span class="invalidation-text">{{ row.strategies?.[0]?.invalidation || '--' }}</span>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <div v-else v-loading="loading" class="pattern-pool-section">
      <div class="pattern-pool-filter">
        <el-radio-group v-model="patternFilter" size="small">
          <el-radio-button value="all">全部</el-radio-button>
          <el-radio-button value="二波跟踪">二波</el-radio-button>
          <el-radio-button value="高标重置">高标重置</el-radio-button>
          <el-radio-button value="低位启动">低位启动</el-radio-button>
        <el-radio-button value="趋势驱动">趋势</el-radio-button>
        <el-radio-button value="潜力回踩">潜力回踩</el-radio-button>
          <el-radio-button value="超跌修复">修复</el-radio-button>
          <el-radio-button value="事件接力">事件</el-radio-button>
        </el-radio-group>
      </div>
      <div class="table-container pattern-table-wrap">
        <el-table
          :data="filteredPatternRows"
          :fit="false"
          stripe
          size="small"
          :empty-text="patternEmptyText"
          row-key="code"
          @row-click="goStock"
          class="anomaly-table pattern-pool-table"
        >
          <el-table-column prop="code" label="代码" width="96">
            <template #default="{ row }"><strong class="code-text">{{ row.code }}</strong></template>
          </el-table-column>
          <el-table-column prop="name" label="名称" width="96" show-overflow-tooltip />
          <el-table-column prop="price" label="现价" width="88" align="right">
            <template #default="{ row }"><span class="num-text">{{ formatPrice(row.price) }}</span></template>
          </el-table-column>
          <el-table-column prop="change_pct" label="涨幅" width="84" align="right">
            <template #default="{ row }">
              <span class="num-text" :class="row.change_pct > 0 ? 'text-red' : row.change_pct < 0 ? 'text-green' : ''">
                {{ formatPct(row.change_pct) }}
              </span>
            </template>
          </el-table-column>
          <el-table-column label="形态" width="210" show-overflow-tooltip>
            <template #default="{ row }">
              <div class="pattern-cell">
                <el-tag size="small" :type="patternType(row.candidate_source)" effect="plain">{{ row.pattern_label }}</el-tag>
                <span class="pattern-meta">{{ row.source_group }} · {{ row.pattern_score?.toFixed(0) || 0 }}分</span>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="质量" width="164" show-overflow-tooltip>
            <template #default="{ row }">
              <div class="pattern-status-cell">
                <el-tag
                  size="small"
                  :type="row.strict_setup_ready ? 'danger' : row.pattern_quality_tier === 'strict' ? 'success' : 'info'"
                  effect="plain"
                >{{ row.pattern_quality_label || '标准形态观察' }}</el-tag>
                <span v-if="row.candidate_source === 'main_wave_pullback_pattern'">
                  缩量 {{ Number(row.pullback_volume_contraction || 0).toFixed(2) }} · 日涨 {{ formatPct(row.pullback_day_change_pct) }}%
                </span>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="250日画像" width="218" show-overflow-tooltip>
            <template #default="{ row }">
              <div v-if="row.long_cycle_regime_label" class="pattern-status-cell">
                <span>{{ row.long_cycle_regime_label }} · {{ Number(row.long_cycle_quality_score || 0).toFixed(0) }}分</span>
                <span>120位 {{ formatPosition(row.position_120) }} · 250位 {{ formatPosition(row.position_250) }} · 20日试盘 {{ row.probe_count_20 || 0 }}</span>
              </div>
              <span v-else class="muted">历史画像待补齐</span>
            </template>
          </el-table-column>
          <el-table-column label="阶段/状态" width="190" show-overflow-tooltip>
            <template #default="{ row }">
              <div class="pattern-status-cell">
                <span>{{ row.stage_label }}</span>
                <el-tag size="small" :type="row.is_limit_up ? 'danger' : 'warning'" effect="plain">{{ row.monitor_status }}</el-tag>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="量能" width="126" align="right">
            <template #default="{ row }">
              <span class="num-text">量比 {{ Number(row.volume_ratio || 0).toFixed(2) }}</span>
              <span class="pattern-volume">换 {{ Number(row.turnover || 0).toFixed(1) }}%</span>
            </template>
          </el-table-column>
          <el-table-column label="板块/大单" width="198" show-overflow-tooltip>
            <template #default="{ row }">
              <div class="pattern-status-cell">
                <span>
                  {{ row.core_sector_confirmed
                    ? `${row.core_sector_name}${row.core_identity_confirmed ? ' · 核心' : ' · 非核心'}`
                    : '有效核心板块待确认' }}
                </span>
                <span :class="row.large_order_inflow_confirmed ? 'text-red' : ''">
                  大单 {{ formatFundAmount(row.large_order_net_inflow) }}
                </span>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="支撑/压力" width="166" align="right">
            <template #default="{ row }">
              <span class="sr-row"><span class="sr-label">支</span>{{ formatPrice(row.support) }}<span class="sr-sep">/</span><span class="sr-label">压</span>{{ formatPrice(row.resistance) }}</span>
            </template>
          </el-table-column>
          <el-table-column prop="trigger_condition" label="盘中触发条件" width="320" show-overflow-tooltip />
          <el-table-column label="飞书" width="170">
            <template #default="{ row }">
              <el-tag :type="row.feishu_monitoring ? 'success' : 'info'" size="small" effect="plain">{{ row.push_policy || '未入监控' }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="仓位" width="68" align="center">
            <template #default="{ row }"><span class="watch-position">{{ row.position_ratio || '0' }}</span></template>
          </el-table-column>
          <el-table-column prop="invalidation" label="失效条件" width="260" show-overflow-tooltip>
            <template #default="{ row }"><span class="invalidation-text">{{ row.invalidation }}</span></template>
          </el-table-column>
        </el-table>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { levelColor } from '@/composables/useUtils'

const props = defineProps({
  rows: { type: Array, default: () => [] },
  patternRows: { type: Array, default: () => [] },
  loading: { type: Boolean, default: false },
  error: { type: String, default: '' },
  snapshotTime: { type: String, default: '' },
  snapshotStale: { type: Boolean, default: false },
  latestReplay: { type: Object, default: () => ({}) },
  marketBreadth: { type: Object, default: () => ({}) },
})
const emit = defineEmits(['refresh'])

const router = useRouter()
const displayMode = ref('plans')
const strategyFilter = ref('all')
const patternFilter = ref('all')
const planFilterGroups = [
  {
    label: '执行状态',
    options: [
      { value: 'all', label: '全部' },
      { value: 'observation', label: '待确认' },
      { value: 'high_board_watch', label: '高标观察' },
      { value: 'relay_watch', label: '首板接力' },
      { value: 'buy_signal', label: '条件买点' },
      { value: 'st_research', label: 'ST研究' },
      { value: 'avoid', label: '不建议' },
    ],
  },
  {
    label: '高标归因',
    options: [
      { value: 'event_relay', label: '事件锁价' },
      { value: 'theme_turnover', label: '题材换手' },
      { value: 'emotion_memory', label: '情绪记忆' },
      { value: 'earnings_surprise', label: '业绩预期差' },
      { value: 'second_wave', label: '断板/二波' },
    ],
  },
  {
    label: '形态策略',
    options: [
      { value: 'strong_get_stronger', label: '强势回踩执行' },
      { value: 'main_wave_confirm', label: '主升执行' },
      { value: 'trend', label: '趋势形态' },
      { value: 'leader_linkage', label: 'A→B联动' },
      { value: 'aggressive', label: '激进执行' },
    ],
  },
]
const directBuyGateText = computed(() => {
  if (props.marketBreadth?.direct_buy_ok) {
    return '极强窗口；通过个股触发条件后可采用标准计划仓位。'
  }
  const blockers = Array.isArray(props.marketBreadth?.direct_buy_blockers)
    ? props.marketBreadth.direct_buy_blockers.filter(Boolean)
    : []
  if (props.marketBreadth?.market_regime === 'bull') {
    return `强势但非极强；不否决条件买点，触发后自动降为轻仓。标准仓位不足项：${blockers.join('；') || '市场宽度未达极强阈值'}`
  }
  if (blockers.length) return `中性/弱势，仅保留观察：${blockers.join('；')}`
  return '尚无可用的市场宽度判定，预案维持观察。'
})
const emptyText = computed(() => {
  if (strategyFilter.value === 'second_wave') return '暂无二波预案候选'
  if (strategyFilter.value === 'event_relay') return '暂无直接事件涨停候选'
  if (strategyFilter.value === 'relay_watch') return '暂无首板接力确认候选'
  if (strategyFilter.value === 'theme_turnover') return '暂无题材换手高标候选'
  if (strategyFilter.value === 'emotion_memory') return '暂无情绪记忆高标候选'
  if (strategyFilter.value === 'earnings_surprise') return '暂无业绩预期差高标候选'
  if (strategyFilter.value === 'leader_linkage') return '暂无看A做B联动候选'
  return '暂无预案数据'
})

const filteredRows = computed(() => {
  if (strategyFilter.value === 'all') return props.rows
  return props.rows.filter(row => matchesStrategyFilter(row, strategyFilter.value))
})

watch(() => props.rows, () => {
  if (strategyFilter.value !== 'all' && filterCount(strategyFilter.value) === 0) {
    strategyFilter.value = 'all'
  }
})

const filteredPatternRows = computed(() => {
  if (patternFilter.value === 'all') return props.patternRows
  return props.patternRows.filter(row => row.source_group === patternFilter.value)
})

const patternEmptyText = computed(() => (
  patternFilter.value === 'all' ? '暂无形态检测池数据' : `暂无${patternFilter.value}候选`
))

function formatPrice(value) {
  const number = Number(value)
  return Number.isFinite(number) && number > 0 ? number.toFixed(2) : '--'
}

function formatPct(value) {
  const number = Number(value)
  if (!Number.isFinite(number)) return '--'
  return `${number > 0 ? '+' : ''}${number.toFixed(2)}`
}

function formatRate(value) {
  const number = Number(value)
  return Number.isFinite(number) ? `${(number * 100).toFixed(1)}%` : '--'
}

function formatPosition(value) {
  const number = Number(value)
  return Number.isFinite(number) ? `${(number * 100).toFixed(0)}%` : '--'
}

function formatFundAmount(value) {
  const amount = Number(value)
  if (!Number.isFinite(amount)) return '--'
  if (Math.abs(amount) >= 1e8) return `${(amount / 1e8).toFixed(2)}亿`
  return `${(amount / 1e4).toFixed(0)}万`
}

function formatSnapshotTime(value) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString('zh-CN', { hour12: false })
}

function isTrendPoolRow(row, types) {
  return types.some(type => ['trend', 'trend_pullback_buy', 'trend_breakout_buy'].includes(type)) || [
    'low_expectation_trend_watch',
    'tenbagger_pullback_watch',
    'pre_board_probe_wash',
    'trend_driver_setup',
    'trend_main_wave_pattern',
  ].includes(row.candidate_source)
}

function matchesStrategyFilter(row, filter) {
  const types = (row.strategies || []).map(strategy => strategy.strategy_type)
  if (filter === 'all') return true
  if (filter === 'observation') return types.some(type => [
    'watch',
    'event_relay_watch',
    'high_board_watch',
    'leader_linkage_watch',
    'sector_core_laggard_watch',
  ].includes(type))
  if (filter === 'high_board_watch') return isHighBoardRow(row)
  if (filter === 'relay_watch') return types.includes('event_relay_watch')
  if (filter === 'st_research') return row.research_only === true
  if (filter === 'main_wave_confirm') return isMainWaveBuyRow(row, types)
  if (filter === 'buy_signal') return isBuySignalRow(row, types)
  if (filter === 'event_relay') return hasHighBoardType(row, 'event_lock')
  if (filter === 'theme_turnover') return hasHighBoardType(row, 'theme_turnover')
  if (filter === 'emotion_memory') return hasHighBoardType(row, 'emotion_memory')
  if (filter === 'earnings_surprise') return hasHighBoardType(row, 'earnings_surprise')
  if (filter === 'leader_linkage') return isLeaderLinkageRow(row)
  if (filter === 'second_wave') return isSecondWaveRow(row)
  if (filter === 'trend') return isTrendPoolRow(row, types)
  return types.includes(filter)
}

function filterCount(filter) {
  return props.rows.reduce(
    (count, row) => count + (matchesStrategyFilter(row, filter) ? 1 : 0),
    0,
  )
}

function isSecondWaveRow(row) {
  return hasHighBoardType(row, 'second_wave') || [
    'second_wave_reset_watch',
    'second_wave_pattern',
    'high_board_turnover_second_wave',
  ].includes(row.candidate_source)
}

function isSecondWaveResetRow(row) {
  return row.candidate_source === 'second_wave_reset_watch'
}

function isEventRelayRow(row) {
  return hasHighBoardType(row, 'event_lock') || ['event_first_board', 'event_high_board', 'second_board_relay'].includes(row.candidate_source)
}

function hasHighBoardType(row, type) {
  return Array.isArray(row.high_board_types) && row.high_board_types.includes(type)
}

function isHighBoardRow(row) {
  return Boolean(row.is_high_board_candidate) || (row.high_board_types || []).length > 0
}

function displayHighBoardTypeLabels(row) {
  const sourceLabel = String(row.candidate_source_label || '')
  return (row.high_board_type_labels || []).filter(label => !sourceLabel.includes(label))
}

function highBoardTypeTag(label) {
  if (label.includes('事件') || label.includes('业绩')) return 'danger'
  if (label.includes('换手') || label.includes('二波')) return 'warning'
  return 'primary'
}

function isLeaderLinkageRow(row) {
  return row.candidate_source === 'leader_linkage_follow'
}

function leaderLinkageSummary(row) {
  const link = row.leader_linkage || row.main_wave_stats || {}
  const leader = link.leader_name || link.leader_code || '龙头A'
  const score = Number(link.linkage_score || 0).toFixed(0)
  const business = Number(link.business_relevance_score || 0).toFixed(0)
  const theme = Number(link.theme_alignment_score || 0).toFixed(0)
  const shape = Number(link.follower_shape_score || 0).toFixed(0)
  const driver = link.leader_driver_reason ? `驱动:${link.leader_driver_reason} · ` : ''
  return `${leader} → ${row.name} · ${driver}联动${score} / 业务${business} / 归因${theme} / 形态${shape} · ${link.relay_ready === false ? '仅观察' : '待盘中确认'}`
}

function eventRelaySummary(row) {
  const event = row.event_catalyst || row.main_wave_stats || {}
  if (row.candidate_source === 'second_board_relay') {
    const quality = Number(event.score ?? event.relay_quality_score ?? 0).toFixed(0)
    return `冲二板质量${quality}分 · ${event.relay_ready ? '待竞价/换手确认' : '仅观察'}`
  }
  const grade = event.grade || event.news_event_grade
  const score = Number(event.score ?? event.news_catalyst_score ?? 0).toFixed(0)
  const prefix = grade === 'hard' ? '硬利好' : '事件催化'
  return `${prefix}${score}分 · ${event.relay_ready ? '待竞价确认' : '仅观察'}`
}

function secondWaveResetSummary(row) {
  const stats = row.main_wave_stats || {}
  const firstWave = stats.reset_type === 'high_board_reset'
    ? `首波${stats.max_board_streak || '--'}板`
    : `首波主升${Number(stats.first_wave_gain_pct || 0).toFixed(1)}%`
  const drawdown = Math.abs(Number(stats.drawdown_pct || 0)).toFixed(1)
  return `${firstWave} · 调整${stats.days_since_peak || '--'}日 · 回撤${drawdown}%`
}

function longCycleSummary(row) {
  const stats = row.main_wave_stats || {}
  const label = stats.long_cycle_regime_label
  if (!label) return ''
  const score = Number(stats.quality_setup_score || 0).toFixed(0)
  const position = formatPosition(stats.position_250)
  return `${label} · 长周期${score}分 · 250日位置${position} · 仅入池待盘中量价确认`
}

function planRowClassName({ row }) {
  if (row.research_only) return 'st-research-row'
  if (isHighBoardRow(row)) return 'high-board-watch-row'
  if (isEventRelayRow(row)) return 'event-relay-watch-row'
  if (isLeaderLinkageRow(row)) return 'leader-linkage-watch-row'
  if (row.candidate_source === 'sector_core_laggard') return 'sector-core-watch-row'
  return isSecondWaveResetRow(row) ? 'second-wave-watch-row' : ''
}

function isBuySignalRow(row, types) {
  if (row.research_only || row.is_tradeable === false) return false
  const hasPlannedPosition = plannedPositionStrategies(row).length > 0
  return Boolean(row.buy_signal) && hasPlannedPosition
}

function plannedPositionStrategies(row) {
  if (!row.buy_signal) return []
  return (row.strategies || []).filter(strategy => {
    const value = String(strategy.position_after_trigger || strategy.position_ratio || '').trim()
    return value !== '' && value !== '0' && value !== '0仓'
  })
}

function isMainWaveBuyRow(row, types) {
  if (types.includes('main_wave_confirm')) return true
  const actionable = types.some(type => !['avoid', 'watch'].includes(type))
  return actionable && row.candidate_source === 'trend_main_wave_pattern'
}

function goStock(row) {
  router.push(`/stocks/${row.code}`)
}

function strategyType(type) {
  return ({
    strong_get_stronger: 'warning',
    main_wave_confirm: 'success',
    trend: 'primary',
    trend_pullback_buy: 'success',
    trend_breakout_buy: 'primary',
    aggressive: 'danger',
    avoid: 'info',
    watch: 'warning',
    event_relay_watch: 'warning',
    high_board_watch: 'warning',
    leader_linkage_watch: 'primary',
    sector_core_laggard_watch: 'info',
  })[type] || 'info'
}

function resonanceType(r) {
  return ({
    '强共振': 'danger',
    '弱共振': 'warning',
    '独立行情': 'info',
    '逆势': 'info',
  })[r] || 'info'
}

function patternType(source) {
  return ({
    low_expectation_trend_watch: 'success',
    tenbagger_pullback_watch: 'danger',
    pre_board_probe_wash: 'success',
    pre_board_compression: 'success',
    pre_board_long_base: 'success',
    pre_board_probe_breakout: 'danger',
    high_board_platform_breakout: 'danger',
    high_board_lockup_acceleration: 'warning',
    high_board_turnover_second_wave: 'success',
    second_wave_reset_watch: 'warning',
    trend_main_wave_pattern: 'success',
    trend_driver_setup: 'primary',
    platform_breakout_pattern: 'danger',
    second_wave_pattern: 'warning',
    main_wave_pullback_pattern: 'success',
    main_wave_pattern: 'primary',
    event_first_board: 'danger',
    event_high_board: 'warning',
    second_board_relay: 'warning',
    high_board_event_lock: 'danger',
    high_board_theme_turnover: 'warning',
    high_board_emotion_memory: 'primary',
    high_board_earnings_surprise: 'danger',
    high_board_second_wave: 'success',
    leader_linkage_follow: 'primary',
  })[source] || 'info'
}
</script>

<style scoped lang="scss">
.plan-container {
  margin-top: var(--spacing-4);
}

.plan-view-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}

.snapshot-status {
  display: flex;
  align-items: center;
  gap: 8px;
}

.snapshot-time {
  color: var(--claw-text-tertiary);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}

.plan-alert {
  margin-bottom: 12px;
}

.plan-replay-panel {
  margin-bottom: 12px;
  padding: 12px;
  border: 1px solid var(--claw-border);
  border-radius: var(--radius-lg);
  background: var(--neutral-50);
}

.plan-replay-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
  color: var(--claw-text-secondary);
  font-size: 12px;
}

.plan-replay-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 8px;
}

.plan-replay-metric {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 9px 10px;
  border: 1px solid var(--claw-border);
  border-radius: var(--radius-md);
  background: var(--claw-bg-card);
  font-size: 11px;

  span { color: var(--claw-text-tertiary); }
  strong { color: var(--claw-text-primary); font-size: 14px; }
}

.plan-filter {
  margin-bottom: 12px;

  :deep(.el-radio-group) {
    flex-wrap: wrap;
    row-gap: 6px;
  }
}

.plan-filter-row {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  margin-bottom: 6px;
}

.plan-filter-label {
  flex: 0 0 52px;
  padding-top: 6px;
  color: var(--claw-text-secondary);
  font-size: 11px;
  font-weight: 600;
}

.filter-count {
  margin-left: 2px;
  font-size: 10px;
  opacity: 0.75;
}

.buy-filter-note {
  display: block;
  margin-top: 6px;
  color: var(--claw-text-tertiary);
  font-size: 11px;
  line-height: 1.4;
}

.direct-buy-gate {
  margin-top: 6px;
  padding: 7px 10px;
  border: 1px solid rgba(var(--el-color-warning-rgb), 0.25);
  border-radius: 6px;
  background: rgba(var(--el-color-warning-rgb), 0.08);
  color: var(--claw-text-secondary);
  font-size: 11px;
  line-height: 1.45;

  strong {
    color: var(--el-color-warning);
  }

  &.is-open {
    border-color: rgba(var(--el-color-success-rgb), 0.25);
    background: rgba(var(--el-color-success-rgb), 0.08);

    strong {
      color: var(--el-color-success);
    }
  }
}

.pattern-pool-section {
  min-height: 180px;
}

.pattern-pool-filter {
  margin-bottom: 12px;

  :deep(.el-radio-group) {
    flex-wrap: wrap;
    row-gap: 6px;
  }
}

.pattern-pool-table {
  min-width: 2180px;
}

:global(.pattern-pool-table .cell) {
  padding: 0 7px;
  white-space: nowrap !important;
  overflow: hidden;
  text-overflow: ellipsis;
  word-break: keep-all !important;
  line-height: 1.25;
}

.pattern-status-cell {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 3px;
  color: var(--claw-text-secondary);
  font-size: 11px;
}

.pattern-volume {
  margin-left: 5px;
  color: var(--claw-text-tertiary);
  font-size: 10px;
}

@media (max-width: 768px) {
  .plan-view-toolbar {
    align-items: flex-start;
    flex-direction: column;
  }

  .snapshot-status {
    width: 100%;
    justify-content: space-between;
  }

  .plan-replay-head { align-items: flex-start; flex-direction: column; gap: 3px; }
  .plan-replay-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}

.second-wave-guide {
  padding: 8px 12px;
  border-bottom: 1px solid rgba(var(--el-color-warning-rgb), 0.22);
  background: rgba(var(--el-color-warning-rgb), 0.08);
  color: var(--claw-text-secondary);
  font-size: 12px;
  line-height: 1.45;
}

.event-relay-guide {
  border-bottom-color: rgba(var(--el-color-danger-rgb), 0.22);
  background: rgba(var(--el-color-danger-rgb), 0.06);
}

.leader-linkage-guide {
  border-bottom-color: rgba(var(--el-color-primary-rgb), 0.22);
  background: rgba(var(--el-color-primary-rgb), 0.06);
}

.table-container,
.anomaly-table-wrap {
  border: 1px solid var(--claw-border);
  border-radius: var(--radius-lg);
  overflow-x: auto;
  overflow-y: hidden;
  box-shadow: var(--shadow-sm);
}

.plan-table {
  min-width: 1766px;
}

::deep(.el-table) {
  border: none;
  cursor: pointer;

  th.el-table__cell {
    background: var(--neutral-50);
    font-weight: 600;
    color: var(--claw-text-secondary);
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.025em;
    padding: 8px 0;
  }

  td.el-table__cell {
    padding: 7px 0;
    font-size: 0.8rem;
  }

  .el-table__row:hover > td {
    background: var(--primary-50);
  }
}

:global(.plan-table .cell) {
  padding: 0 7px;
  white-space: nowrap !important;
  overflow: hidden;
  text-overflow: ellipsis;
  word-break: keep-all !important;
  line-height: 1.25;
}

.code-text {
  display: inline-block;
  white-space: nowrap;
  letter-spacing: 0;
  min-width: 6ch;
  font-variant-numeric: tabular-nums;
}

.num-text {
  display: inline-block;
  white-space: nowrap;
  min-width: max-content;
  font-variant-numeric: tabular-nums;
}

.level-badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  border-radius: 6px;
  color: #fff;
  font-size: 13px;
  font-weight: 700;
  line-height: 1;
}

.strategy-tag-row {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-bottom: 2px;

  .strategy-detail {
    font-size: 11px;
    color: var(--claw-text-secondary);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    max-width: 150px;
  }
}

.pattern-cell {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 3px;
  min-width: 0;
  max-width: 100%;
  overflow: hidden;

  .el-tag {
    flex: 0 0 auto;
    max-width: 152px;
    overflow: hidden;
    text-overflow: ellipsis;
  }
}

.pattern-meta {
  max-width: 100%;
  color: var(--claw-text-secondary);
  font-size: 10px;
  line-height: 1.2;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.high-board-types {
  display: flex;
  gap: 3px;
  max-width: 100%;
  overflow: hidden;

  .el-tag {
    max-width: 92px;
  }
}

.st-research-tag {
  margin-left: 3px;
  transform: scale(0.88);
  transform-origin: left center;
}

.pattern-score {
  color: var(--claw-text-secondary);
  font-size: 11px;
  font-weight: 600;
}

.muted {
  color: var(--claw-text-tertiary);
  font-size: 12px;
}

.position-text {
  display: inline-block;
  white-space: nowrap;
  font-weight: 600;
  font-size: 12px;
  color: var(--el-color-primary);
}

.watch-position {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 22px;
  height: 20px;
  border-radius: 4px;
  background: rgba(var(--el-color-warning-rgb), 0.12);
  color: var(--el-color-warning);
  font-size: 11px;
  font-weight: 700;
}

:global(.plan-table .second-wave-watch-row td.el-table__cell:first-child) {
  box-shadow: inset 3px 0 0 var(--el-color-warning);
}

:global(.plan-table .event-relay-watch-row td.el-table__cell:first-child) {
  box-shadow: inset 3px 0 0 var(--el-color-danger);
}

:global(.plan-table .high-board-watch-row td.el-table__cell:first-child) {
  box-shadow: inset 3px 0 0 var(--el-color-warning);
}

:global(.plan-table .st-research-row td.el-table__cell:first-child) {
  box-shadow: inset 3px 0 0 var(--el-color-info);
}

:global(.plan-table .leader-linkage-watch-row td.el-table__cell:first-child) {
  box-shadow: inset 3px 0 0 var(--el-color-primary);
}

:global(.plan-table .sector-core-watch-row td.el-table__cell:first-child) {
  box-shadow: inset 3px 0 0 var(--el-color-info);
}

.price-range {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 2px;
  font-size: 12px;
  white-space: nowrap;

  .arrow {
    color: var(--claw-text-secondary);
  }

  .rr-tag {
    margin-left: 4px;
    padding: 0 4px;
    border-radius: 4px;
    font-size: 10px;
    font-weight: 600;
    background: rgba(var(--el-color-primary-rgb), 0.15);
    color: var(--el-color-primary);
  }
}

.sr-row {
  display: inline-flex;
  align-items: center;
  justify-content: flex-end;
  gap: 4px;
  font-size: 12px;
  line-height: 1.2;
  white-space: nowrap;

  .sr-label {
    display: inline-block;
    width: auto;
    text-align: center;
    font-size: 10px;
    color: var(--claw-text-secondary);
  }

  .sr-sep {
    color: var(--claw-text-tertiary);
  }
}

.tech-summary {
  display: inline-block;
  max-width: 100%;
  font-size: 11px;
  color: var(--claw-text-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.sector-emotion {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  line-height: 1.2;
  white-space: nowrap;
}
.resonance-row {
  display: flex;
  align-items: center;
  gap: 4px;
  min-width: 0;
}
.sector-name {
  color: var(--claw-text-secondary);
  font-size: 10px;
  max-width: 76px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sentiment-row {
  .sentiment-label {
    font-size: 10px;
    padding: 1px 4px;
    border-radius: 3px;
    background: rgba(var(--el-color-warning-rgb), 0.1);
    color: var(--el-color-warning);
  }
}

.invalidation-text {
  display: inline-block;
  max-width: 100%;
  color: #ef4444;
  font-size: 12px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.text-red { color: #ef4444; }
.text-green { color: #22c55e; }
</style>
