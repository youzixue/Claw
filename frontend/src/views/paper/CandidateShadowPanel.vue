<template>
  <section v-if="active" class="candidate-shadow panel-card" data-testid="candidate-shadow-panel">
    <header>
      <div>
        <h3>同批候选影子实验 · 只读研究</h3>
        <p>{{ routes.join(' + ') || '路由未知' }} · 不交易、不推送，不计入持仓与账户绩效。</p>
      </div>
      <div class="shadow-controls">
        <el-date-picker v-model="tradeDate" type="date" value-format="YYYY-MM-DD" placeholder="请选择研究交易日" clearable size="small" aria-label="影子实验交易日" />
        <el-button size="small" :loading="loading" @click="refresh">手动刷新实验</el-button>
      </div>
    </header>
    <p class="shadow-note">仅比较同批原版、实验v1与v2观察结果；原版 true 仅是历史生产者确认，时效按该记录当时计算，刷新不重新判断；时效通过也不代表通过当前预算、风控或可以买入。v2缺证与主动过滤分开，旧记录不补造v2结果。仅提供 5/15/30 分钟参考涨跌，本期未提供收盘或 T+ 标签。后续价格标记不是成交收益、净收益或 T+1 可实现收益。每路最多 200 条，无数据不等于 0 收益或 0 命中。无自动轮询；同一范围刷新间隔至少 15 秒。</p>
    <el-alert v-if="notice" :title="notice" type="warning" :closable="false" show-icon />
    <p v-if="!reports.length && !loading" class="shadow-empty">尚未接收研究报告，请手动刷新；未开盘、无候选与覆盖缺失不能凭空推断。</p>
    <div v-for="report in reports" :key="report.route" class="shadow-route">
      <h4>{{ report.route }} · {{ report.trade_date || '交易日未知' }}</h4>
      <el-alert v-if="report.error" :title="report.error" type="error" :closable="false" show-icon />
      <template v-else>
        <div class="shadow-status">
          <span>实验配置：{{ flag(report.status.enabled, '开启', '关闭') }}</span>
          <span>运行：{{ flag(report.status.running, '运行中', '未运行') }}（仅配置开启不代表已采样）</span>
          <span>启动：{{ text(report.status.started_at) }}</span>
          <span>版本：{{ text(report.status.version) }}</span>
        </div>
        <p>覆盖：{{ coverageText(report.coverage) }}</p>
        <details class="shadow-receipts" :open="!report.rows.length">
          <summary>最近扫描回执 · 非候选命中统计</summary>
          <p v-if="!report.receipts.length">来源未提供可展示的扫描回执；不能据此推断候选为 0 或无阻断。</p>
          <el-table v-else :data="report.receipts" size="small" stripe>
            <el-table-column label="路线 / 阶段" min-width="150">
              <template #default="{ row }">{{ text(row.route, '全局 / 未注明') }} · {{ stateText(row.stage) }}</template>
            </el-table-column>
            <el-table-column label="原因" min-width="220">
              <template #default="{ row }">{{ text(row.reason, '来源未提供原因') }}</template>
            </el-table-column>
            <el-table-column label="原扫描 / 谓词时点" min-width="215">
              <template #default="{ row }">{{ text(row.scan_id) }}<small>{{ text(row.predicate_asof) }}</small></template>
            </el-table-column>
            <el-table-column label="来源报告计数（非命中率）" min-width="200">
              <template #default="{ row }">{{ receiptCounts(row.counts) }}</template>
            </el-table-column>
          </el-table>
          <small>最多展示 20 条原回执，可能包含较早时点；不是当前路线完整覆盖或无阻断的证明。</small>
        </details>
        <p v-for="(warning, index) in report.warnings" :key="index" class="shadow-warning">{{ warning }}</p>
        <p v-if="report.truncated" class="shadow-warning">结果已截断，仅展示部分记录，不代表完整候选分母。</p>
        <el-table :data="report.rows" size="small" stripe empty-text="未接收候选记录；不推断零命中或零收益">
          <el-table-column label="候选 / 阶段" min-width="150">
            <template #default="{ row }">
              <strong>{{ text(row.code) }} {{ text(row.name, '') }}</strong>
              <small>{{ text(row.account_name, '无账户 / 研究') }} · {{ stateText(row.stage) }}</small>
              <small>{{ text(row.reason, '原因未提供') }}</small>
              <small class="shadow-frame-id">原帧 ID：{{ text(row.frame_id) }}</small>
              <small>标签锚帧：{{ text(row.label_sampling?.anchor_frame_id) }}</small>
              <small>{{ row.label_sampling?.selected === true ? '本帧为首状态标签锚点' : row.label_sampling?.selected === false ? '复用首状态锚点；涨跌不以本帧参考价重算' : '标签采样身份未提供' }}</small>
            </template>
          </el-table-column>
          <el-table-column v-for="column in resultColumns" :key="column.key" :label="column.label" min-width="185">
            <template #default="{ row }">
              <strong>{{ resultText(row[column.key], column.key) }}</strong>
              <small>{{ text(row[column.key]?.reason, '原因未提供') }}</small>
            </template>
          </el-table-column>
          <el-table-column label="源报价 / 原谓词观察 / 研究记录生成" min-width="235">
            <template #default="{ row }">
              <div>源报价：{{ text(row.source_quote_at) }}</div>
              <small>原谓词观察：{{ text(row.observed_at) }}</small>
              <small>研究记录生成：{{ text(row.recorded_at) }}</small>
              <small>研究处理延迟：{{ delayText(row) }}（记录生成减观察，非持久化完成、发送或成交延迟）</small>
            </template>
          </el-table-column>
          <el-table-column label="本帧原参考价（非成交价）" min-width="110">
            <template #default="{ row }">{{ priceText(row.reference_price) }}</template>
          </el-table-column>
          <el-table-column v-for="mark in markColumns" :key="mark.key" :label="mark.label" min-width="160">
            <template #default="{ row }">
              <span :class="markClass(labelFor(row, mark.key))">{{ markText(labelFor(row, mark.key)) }}</span>
              <small>{{ text(labelFor(row, mark.key)?.reason, '') }}</small>
              <small>锚点：{{ text(labelFor(row, mark.key)?.anchor_at) }}</small>
              <small>目标：{{ text(labelFor(row, mark.key)?.target_at) }}</small>
            </template>
          </el-table-column>
        </el-table>
      </template>
    </div>
  </section>
</template>

<script setup>
import { computed, onUnmounted, ref, watch } from 'vue'
import { getPaperCandidateShadow } from '@/api'
import { familyRoutes, normalizeReport, text, flag, stateText, resultText, priceText, delayText, coverageText, receiptCounts, labelFor, markText, markClass, markColumns } from './candidateShadowModel'

const props = defineProps({ accountName: { type: String, required: true }, active: Boolean })
const routes = computed(() => familyRoutes(props.accountName))
const tradeDate = ref('')
const reports = ref([])
const loading = ref(false)
const notice = ref('')
const resultColumns = [
  { key: 'baseline', label: '原版结果' },
  { key: 'candidate', label: '实验v1（保留对照）' },
  { key: 'candidate_v2', label: '实验v2（不下单）' },
  { key: 'confirmation_freshness', label: '原确认时效（非交易许可）' },
  { key: 'early_observation', label: '提前观察（非买点）' },
]
const attempts = new Map()
let controller
let generation = 0

function cancel() {
  generation += 1
  controller?.abort()
  controller = undefined
  loading.value = false
}

watch(() => [props.accountName, tradeDate.value], () => {
  cancel()
  reports.value = []
  notice.value = ''
})
watch(() => props.active, active => { if (!active) cancel() })
onUnmounted(cancel)

async function refresh() {
  if (!props.active || document.visibilityState === 'hidden' || loading.value) return
  if (!tradeDate.value) {
    notice.value = '请先选择研究交易日；页面不使用本机日期猜测交易日。'
    return
  }
  if (!routes.value.length) {
    notice.value = '当前账户路由未知，未发出请求。'
    return
  }
  const key = routes.value.join(',') + ':' + tradeDate.value
  const previous = attempts.get(key)
  if (previous !== undefined && Date.now() - previous < 15000) {
    notice.value = '刷新限频：请在上次请求 15 秒后再试；当前展示未自动更新。'
    return
  }
  attempts.set(key, Date.now())
  cancel()
  const current = generation
  controller = new AbortController()
  const signal = controller.signal
  loading.value = true
  notice.value = ''
  const requestedDate = tradeDate.value
  const next = await Promise.all(routes.value.map(async route => {
    try {
      const payload = await getPaperCandidateShadow({
        route, limit: 200, ...(requestedDate ? { trade_date: requestedDate } : {}),
      }, signal)
      return normalizeReport(payload, route, requestedDate)
    } catch {
      return { route, error: '研究接口未接收或请求失败；不以空记录替代真实结果。' }
    }
  }))
  if (current !== generation || !props.active) return
  reports.value = next
  loading.value = false
}
</script>

<style scoped>
.candidate-shadow { margin: 16px 0; border: 1px solid var(--claw-border); min-width: 0; color: var(--claw-text); }
header { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 12px; }
h3, h4 { margin: 0 0 8px; }
p { font-size: 12px; line-height: 1.7; overflow-wrap: anywhere; }
.shadow-controls { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.shadow-note, .shadow-status, small, .shadow-empty { color: var(--claw-text-muted); }
.shadow-status { display: flex; flex-wrap: wrap; gap: 8px 20px; font-size: 12px; overflow-wrap: anywhere; }
.shadow-route { margin-top: 18px; min-width: 0; }
small { display: block; margin-top: 5px; font-size: 11px; line-height: 1.5; overflow-wrap: anywhere; }
.shadow-warning { color: var(--el-color-warning); }
.shadow-up { color: var(--claw-red, #ef4444); }
.shadow-down { color: var(--claw-green, #10b981); }
:deep(.el-table) { max-width: 100%; }
</style>
