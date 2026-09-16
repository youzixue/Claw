<template>
  <div class="rank-tab">
    <div class="rank-toolbar">
      <el-radio-group :model-value="mode" size="small" @update:model-value="handleModeChange">
        <el-radio-button value="bull">短线强势</el-radio-button>
        <el-radio-button value="tenbagger">十倍潜力</el-radio-button>
      </el-radio-group>
      <el-segmented
        v-if="mode === 'bull'"
        :model-value="profile"
        :options="profileOptions"
        size="small"
        class="profile-switch"
        @update:model-value="handleProfileChange"
      />
      <el-dropdown trigger="click" @command="handleExport" class="export-dropdown">
        <el-button size="small" type="primary" plain :disabled="!rows.length">
          <el-icon><Download /></el-icon> 导出
        </el-button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item command="xlsx">导出 Excel</el-dropdown-item>
            <el-dropdown-item command="csv">导出 CSV</el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
    </div>

    <div class="table-container anomaly-table-wrap">
      <el-table :data="rows" stripe size="small" empty-text="暂无排行数据" row-key="code" v-loading="loading" @row-click="goStock" class="rank-table">
        <el-table-column label="#" width="50" align="center" fixed>
          <template #default="{ $index }">
            <span :class="rankClass((currentPage - 1) * pageSize + $index + 1)">
              {{ (currentPage - 1) * pageSize + $index + 1 }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="股票" min-width="150" fixed>
          <template #default="{ row }">
            <div class="stock-cell">
              <div class="stock-name">{{ row.name }}</div>
              <div class="stock-meta">{{ row.code }}</div>
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="total_score" label="总分" width="70" align="center" sortable>
          <template #default="{ row }">
            <strong :class="scoreClass(row.total_score)">{{ Math.round(row.total_score || 0) }}</strong>
          </template>
        </el-table-column>
        <el-table-column prop="level" label="评级" width="70" align="center">
          <template #default="{ row }">
            <el-tag size="small" :type="levelTagType(row.level)" effect="light">{{ row.level || '--' }}</el-tag>
            <span v-if="isLimitDownRisk(row)" class="limit-down-badge" title="当日跌停/暴跌，评分已降级">⚠️</span>
          </template>
        </el-table-column>
        <el-table-column label="行情/资金" min-width="200">
          <template #default="{ row }">
            <div class="metric-cell">
              <div class="metric-line">
                <span :class="changeColorClass(row.change_pct)">{{ formatChange(row.change_pct) }}</span>
                <span>换手 {{ formatPct(row.turnover) }}</span>
              </div>
              <div class="metric-line">
                <span>量比 {{ formatNumber(row.volume_ratio) }}</span>
                <span :title="rankFundHint(row)">资金 {{ formatBillions(row.main_net_inflow_billion) }}</span>
              </div>
              <div class="stock-meta rank-fund-context" :title="rankFundHint(row)">{{ rankFundLabel(row) }}</div>
              <div class="stock-meta rank-fund-window" :title="fundWindowHint(row)">
                近5会话 {{ row.fund_5d_complete === true ? formatBillions(row.fund_5d_billion) : '未知' }}
              </div>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="短线形态" min-width="210" v-if="mode === 'bull'">
          <template #default="{ row }">
            <div class="metric-cell">
              <div class="metric-line">
                <el-tag v-if="row.short_trend_label" size="small" :type="row.is_volume_price_uptrend ? 'danger' : 'warning'" effect="light">
                  {{ row.short_trend_label }}
                </el-tag>
                <span v-else>--</span>
                <span v-if="row.short_trend_score">短线分 {{ Math.round(row.short_trend_score) }}</span>
              </div>
              <div class="stock-meta">{{ shortTrendHint(row) }}</div>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="维度得分" min-width="280">
          <template #default="{ row }">
            <div class="dim-cell">
              <div class="dim-bars">
                <template v-for="(val, key) in (row.dimensions || {})" :key="key">
                  <div class="dim-bar-item" v-if="dimLabel(key)">
                    <span class="dim-label">{{ dimLabel(key) }}</span>
                    <div class="dim-bar-track">
                      <div class="dim-bar-fill" :style="{ width: (val || 0) + '%', background: dimColor(val) }"></div>
                    </div>
                    <span class="dim-value">{{ typeof val === 'number' ? val.toFixed(0) : val }}</span>
                  </div>
                </template>
              </div>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="信号摘要" min-width="220" show-overflow-tooltip>
          <template #default="{ row }">
            <div class="metric-cell">
              <div class="stock-name">{{ topSignal(row) }}</div>
              <div class="stock-meta">{{ row.suggestion || '等待更多信号确认' }}</div>
            </div>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <div class="pagination-wrap" v-if="total > pageSize">
      <el-pagination
        :current-page="currentPage"
        :page-size="pageSize"
        :total="total"
        layout="prev, pager, next"
        size="small"
        background
        @current-change="handlePageChange"
      />
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { Download } from '@element-plus/icons-vue'
import { formatChange, changeColorClass } from '@/composables/useUtils'
import { notifySuccess } from '@/utils/message'

const props = defineProps({
  rows: {
    type: Array,
    default: () => [],
  },
  mode: {
    type: String,
    default: 'bull',
  },
  profile: {
    type: String,
    default: '',
  },
  loading: {
    type: Boolean,
    default: false,
  },
  currentPage: {
    type: Number,
    default: 1,
  },
  pageSize: {
    type: Number,
    default: 20,
  },
  total: {
    type: Number,
    default: 0,
  },
})

const emit = defineEmits(['update:mode', 'update:profile', 'update:page'])

const router = useRouter()
const currentPage = computed(() => props.currentPage)
const pageSize = computed(() => props.pageSize)
const total = computed(() => props.total)
const profileOptions = [
  { label: '全部', value: '' },
  { label: '量价齐升', value: 'volume_price_uptrend' },
]

const handleModeChange = (val) => {
  emit('update:mode', val)
}

const handleProfileChange = (val) => {
  emit('update:profile', val)
}

const handlePageChange = (page) => {
  emit('update:page', page)
}

const goStock = (row) => router.push(`/stocks/${row.code}`)

// 排名序号样式
const rankClass = (rank) => {
  if (rank <= 3) return 'rank-top3'
  if (rank <= 10) return 'rank-top10'
  return ''
}

// 总分颜色
const scoreClass = (score) => {
  if (score >= 75) return 'score-a'
  if (score >= 60) return 'score-b'
  return ''
}

const levelTagType = (level) => ({
  T: 'danger',    // [v3.2] 十倍潜力最高级
  S: 'danger',
  A: 'success',
  B: 'warning',
  C: 'info',
}[level || ''] || 'info')

// 维度标签映射 — 7维 BullScore + 5维 Tenbagger
const DIM_LABELS = {
  momentum: '动量',
  capital: '资金',
  technical: '技术',
  valuation: '估值',
  fundamental: '基本面',
  scale: '规模',
  activity: '活跃度',
  market_cap: '市值',
  growth: '增速',
  sector: '赛道',
}

const dimLabel = (key) => DIM_LABELS[key] || ''

// 维度条颜色
const dimColor = (val) => {
  if (val >= 75) return '#ef4444'
  if (val >= 60) return '#f97316'
  if (val >= 40) return '#3b82f6'
  return '#6b7280'
}

const formatPct = (value, digits = 1) => {
  const num = Number(value || 0)
  if (!num) return '--'
  return `${num > 0 ? '+' : ''}${num.toFixed(digits)}%`
}

const formatNumber = (value, digits = 1) => {
  const num = Number(value || 0)
  if (!num) return '--'
  return num.toFixed(digits)
}

const formatBillions = (value) => {
  if (!['number', 'string'].includes(typeof value) || String(value).trim() === '') return '--'
  const num = Number(value)
  if (!Number.isFinite(num)) return '--'
  return `${num > 0 ? '+' : ''}${num.toFixed(2)}亿`
}

const rankFundLabel = (row) => {
  if (row.main_fund_status === 'unknown' || row.main_net_inflow_billion == null) return '资金未知'
  if (row.main_fund_status !== 'snapshot_known') return '旧排行快照 · 资金时点未标记'
  const sourceAt = row.main_fund_source_quote_at
  return `排行快照 · ${sourceAt ? String(sourceAt).replace('T', ' ') : '源时点未知'}`
}
const rankFundHint = (row) => `${rankFundLabel(row)}；排行生成时的资金证据，不是当前交易资金确认。`
const fundWindowHint = (row) => {
  const window = row.fund_5d_window || {}
  const dates = Array.isArray(window.session_dates) ? window.session_dates : []
  const boundary = dates.length ? `${dates[0]} 至 ${dates[dates.length - 1]}` : '日历未确认'
  return `5个已确认收盘会话 · ${boundary}；有效 ${row.fund_5d_count ?? 0}/5；判定时点 ${window.decision_at || '未知'}；${window.version || '旧版窗口未知'}；仅有每股每日最新记录，不是历史PIT还原。`
}

const topSignal = (row) => {
  const signals = Array.isArray(row?.top_signals) ? row.top_signals : []
  if (!signals.length) return '暂无明显强信号'
  return signals[0]
}

const shortTrendHint = (row) => {
  const tags = Array.isArray(row?.short_trend_tags) ? row.short_trend_tags : []
  if (tags.length) return tags.join(' · ')
  const blockers = Array.isArray(row?.short_trend_blockers) ? row.short_trend_blockers : []
  return blockers[0] || '未形成短线量价齐升'
}

// [v3.1] 跌停/暴跌风险判断
const isLimitDownRisk = (row) => {
  const pct = Number(row?.change_pct || 0)
  return pct <= -7 // 跌停或暴跌(>7%)都显示警告
}

const loadExportTool = async () => import('@/utils/export')

// 导出功能
const BULL_COLUMNS = [
  { key: 'code', label: '代码' },
  { key: 'name', label: '名称' },
  { key: 'total_score', label: '总分', format: v => v?.toFixed(1) },
  { key: 'level', label: '评级' },
  { key: 'change_pct', label: '涨跌幅%', format: v => v?.toFixed(2) },
  { key: 'turnover', label: '换手率%', format: v => v?.toFixed(2) },
  { key: 'volume_ratio', label: '量比', format: v => v?.toFixed(2) },
  { key: 'short_trend_label', label: '短线形态' },
  { key: 'short_trend_score', label: '短线分', format: v => v?.toFixed(1) },
  { key: 'main_net_inflow_billion', label: '主力资金净额(亿)', format: v => v?.toFixed(2) },
  { key: 'main_fund_status', label: '资金快照状态' },
  { key: 'main_fund_source_quote_at', label: '资金源时点' },
  { key: 'main_fund_purpose', label: '资金用途(非当前交易确认)' },
  { key: 'fund_5d_billion', label: '近5个确认收盘会话净额(亿)', format: v => v == null ? '未知' : formatBillions(v) },
  { key: 'fund_5d_complete', label: '5会话完整', format: v => v === true ? '是' : '否' },
  { key: 'fund_5d_status', label: '5会话资金状态' },
  { key: 'fund_5d_window', label: '5会话窗口证据', format: v => v ? JSON.stringify(v) : '未知' },
  { key: 'circ_market_cap_billion', label: '流通市值(亿)', format: v => v?.toFixed(1) },
  { key: 'pe_ttm', label: 'PE(TTM)', format: v => v?.toFixed(1) },
  { key: 'volume_ratio_level', label: '量比等级' },
  { key: 'turnover_level', label: '换手率等级' },
  { key: 'price_volume_relation', label: '量价关系' },
  { key: 'suggestion', label: '操作建议' },
]

const TENBAGGER_COLUMNS = [
  { key: 'code', label: '代码' },
  { key: 'name', label: '名称' },
  { key: 'total_score', label: '总分', format: v => v?.toFixed(1) },
  { key: 'level', label: '评级' },
  { key: 'change_pct', label: '涨跌幅%', format: v => v?.toFixed(2) },
  { key: 'circ_market_cap_billion', label: '流通市值(亿)', format: v => v?.toFixed(1) },
  { key: 'net_profit_growth', label: '净利润增速%', format: v => v?.toFixed(1) },
  { key: 'pe_ttm', label: 'PE(TTM)', format: v => v?.toFixed(1) },
  { key: 'pb', label: 'PB', format: v => v?.toFixed(2) },
  { key: 'main_net_inflow_billion', label: '主力资金净额(亿)', format: v => v?.toFixed(2) },
  { key: 'main_fund_status', label: '资金快照状态' },
  { key: 'main_fund_source_quote_at', label: '资金源时点' },
  { key: 'main_fund_purpose', label: '资金用途(非当前交易确认)' },
  { key: 'fund_5d_billion', label: '近5个确认收盘会话净额(亿)', format: v => v == null ? '未知' : formatBillions(v) },
  { key: 'fund_5d_complete', label: '5会话完整', format: v => v === true ? '是' : '否' },
  { key: 'fund_5d_status', label: '5会话资金状态' },
  { key: 'fund_5d_window', label: '5会话窗口证据', format: v => v ? JSON.stringify(v) : '未知' },
  { key: 'turnover', label: '换手率%', format: v => v?.toFixed(2) },
  { key: 'dividend_yield', label: '股息率%', format: v => v?.toFixed(2) },
  { key: 'suggestion', label: '建议' },
]

const handleExport = async (command) => {
  const columns = props.mode === 'tenbagger' ? TENBAGGER_COLUMNS : BULL_COLUMNS
  const prefix = props.mode === 'tenbagger' ? '十倍潜力排行' : '短线强势排行'
  const dateStr = new Date().toISOString().slice(0, 10)
  const filename = `${prefix}_${dateStr}`
  const { exportToExcel, exportToCSV } = await loadExportTool()
  const exportFn = command === 'csv' ? exportToCSV : exportToExcel

  // 添加维度得分子列
  const enrichedRows = props.rows.map(row => {
    const dims = row.dimensions || {}
    const base = { ...row }
    Object.entries(dims).forEach(([k, v]) => {
      const label = dimLabel(k)
      if (label) base[`维度-${label}`] = typeof v === 'number' ? v.toFixed(0) : v
    })
    return base
  })

  // 添加维度列
  const dimColumns = Object.keys((props.rows[0]?.dimensions) || {}).map(k => ({
    key: `维度-${dimLabel(k)}`,
    label: dimLabel(k),
  })).filter(c => c.label)

  const allColumns = [...columns, ...dimColumns]
  await exportFn(enrichedRows, allColumns, filename, prefix)
  notifySuccess(`已导出 ${enrichedRows.length} 条数据`)
}
</script>

<style scoped lang="scss">
.rank-tab {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-3);
}

.rank-toolbar {
  display: flex;
  justify-content: flex-start;
  align-items: center;
  gap: 12px;
}

.export-dropdown {
  margin-left: auto;
}

.stock-cell,
.metric-cell {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.stock-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--claw-text-primary);
}

.stock-meta,
.metric-line {
  font-size: 12px;
  color: var(--claw-text-muted);
}

.metric-line {
  display: flex;
  gap: 12px;
}

// 排名序号
.rank-top3 {
  font-weight: 700;
  color: #ef4444;
  font-size: 14px;
}
.rank-top10 {
  font-weight: 600;
  color: #f97316;
}

// 跌停/暴跌警告
.limit-down-badge {
  margin-left: 2px;
  font-size: 12px;
  cursor: help;
}

// 总分
.score-a {
  color: #ef4444;
}
.score-b {
  color: #f97316;
}

// 维度条
.dim-cell {
  width: 100%;
}
.dim-bars {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.dim-bar-item {
  display: flex;
  align-items: center;
  gap: 6px;
  height: 18px;
}
.dim-label {
  font-size: 11px;
  color: var(--claw-text-muted);
  width: 32px;
  text-align: right;
  flex-shrink: 0;
}
.dim-bar-track {
  flex: 1;
  height: 6px;
  background: rgba(255,255,255,0.06);
  border-radius: 3px;
  overflow: hidden;
}
.dim-bar-fill {
  height: 100%;
  border-radius: 3px;
  transition: width 0.3s ease;
}
.dim-value {
  font-size: 11px;
  color: var(--claw-text-muted);
  width: 24px;
  text-align: right;
  flex-shrink: 0;
}

// 分页
.pagination-wrap {
  display: flex;
  justify-content: center;
  padding: 8px 0 4px;
}
</style>
