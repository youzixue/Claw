<template>
  <section class="c3-records" data-testid="c3-records">
    <header>
      <div><h3>C3 · 主线首板检测记录</h3><p>C·主线扩散的研究子路线 · 独立于当前账户筛选 · 非执行信号，本记录未下单</p></div>
      <el-button :loading="loading" @click="load">刷新记录</el-button>
    </header>
    <el-alert type="warning" :closable="false" show-icon
      title="确认不等于涨停、成交或盈利。这里只查询原始影子事件，不触发扫描/下单；未涨停、失败和拦截事件仍保留。默认查询全部历史版本，不因版本升级隐藏旧记录。" />
    <form class="filters" @submit.prevent="search">
      <label>交易日（留空为全部）
        <el-date-picker v-model="filters.trade_date" type="date" value-format="YYYY-MM-DD" placeholder="全部交易日" clearable @change="search" />
      </label>
      <label>代码 / 名称
        <el-input v-model="keyword" placeholder="输入代码或股票名称" maxlength="80" clearable @clear="search" />
      </label>
      <label>事件
        <el-select v-model="filters.event_type" @change="search">
          <el-option v-for="item in eventOptions" :key="item.value" :value="item.value" :label="item.label" />
        </el-select>
      </label>
      <label>版本范围
        <el-select v-model="filters.version" @change="search">
          <el-option value="all" label="全部历史版本（默认）" />
          <el-option value="current" label="仅当前配置版本" />
        </el-select>
      </label>
      <el-button native-type="submit" type="primary">查询</el-button>
    </form>
    <p class="scope">当前配置版本：{{ result?.current_version || '—' }} · 服务端筛选后分页，不是最近50条内搜索</p>
    <div v-if="error" class="error" role="alert">
      <el-alert :title="error" type="error" :closable="false" show-icon />
      <el-button @click="load">重试</el-button>
    </div>
    <div v-loading="loading" class="results" :aria-busy="loading">
      <template v-if="result && !error">
        <div class="count">符合条件的原始事件共 {{ result.total }} 条（非股票数、非全市场分母） · 检测钟为 observed_at，非成交钟 · 历史未入队不等于发送失败；通道成功不代表用户已读/收到</div>
        <el-empty v-if="!result.items.length" description="当前筛选无记录。可清空日期/名称或切换全部事件、全部历史版本；不据此判断策略未检测。" />
        <div v-else class="table-scroll">
          <el-table :data="result.items" row-key="id" stripe class="records-table">
            <el-table-column type="expand">
              <template #default="{ row }">
                <dl class="event-detail">
                  <dt>原始 ID / event_key</dt><dd>#{{ row.id }} · {{ row.event_key }}</dd>
                  <dt>原始版本</dt><dd>{{ row.route_version }}</dd>
                  <dt>源行情钟（若已记录）</dt><dd>{{ row.source_quote_at || '未记录' }}</dd>
                  <dt>研究因果标记</dt><dd>{{ row.causal_status || '未记录' }}</dd>
                  <dt>原始确认摘要</dt>
                  <dd>状态 {{ row.confirmation?.ready == null ? '未记录' : String(row.confirmation.ready) }} · 样本 {{ row.confirmation?.sample_count ?? '—' }} · 持续 {{ row.confirmation?.persistence_sec ?? '—' }} 秒 · 首样本 {{ row.confirmation?.first_sample_at || '—' }}</dd>
                  <dt>详情可用性</dt><dd>{{ snapshotLabel(row.snapshot_status) }}；仅展示白名单摘要，不读取大体积全量详情。</dd>
                  <dt>投递审计</dt>
                  <dd>状态 {{ row.notification?.status || 'unknown' }} · 原因 {{ row.notification?.cause || '未记录' }} · 信号日志 {{ row.notification?.signal_log_id ?? '—' }} · 投递日志 {{ row.notification?.delivery_log_id ?? '—' }}</dd>
                  <dd>发送前复核 {{ row.notification?.checked_at || '未记录' }} · 上次状态/原因 {{ row.notification?.previous_status || '—' }} / {{ row.notification?.previous_cause || '—' }} · 用户收到时间不可验证</dd>
                  <dt>交易边界</dt><dd>非执行信号，本研究记录未下单；不代表该股票在其他策略中有/无订单。</dd>
                </dl>
              </template>
            </el-table-column>
            <el-table-column label="股票" width="135">
              <template #default="{ row }"><strong>{{ row.name || '未记录名称' }}</strong><br><span>{{ row.code || '全局事件' }}</span></template>
            </el-table-column>
            <el-table-column label="检测钟" width="215">
              <template #default="{ row }">{{ row.observed_at?.replace('T', ' ') }}<br><small>交易日 {{ row.trade_date }}</small></template>
            </el-table-column>
            <el-table-column label="事件 / 研究状态" width="195">
              <template #default="{ row }">{{ eventLabel(row.event_type) }}<br><small>{{ row.event_type }} / {{ row.status }}</small></template>
            </el-table-column>
            <el-table-column label="确认价 / 检测价（元）" width="170">
              <template #default="{ row }">{{ price(row.confirmed_price) }} / {{ price(row.price) }}<br><small>均非订单成交价</small></template>
            </el-table-column>
            <el-table-column label="飞书投递 / 原因" width="285">
              <template #default="{ row }">
                <strong>{{ deliveryLabel(row.notification?.status) }}</strong>
                <small class="block">{{ deliveryCause(row.notification) }}</small>
                <small class="block">原确认 {{ row.event_type === 'confirmed' ? row.observed_at?.replace('T', ' ') : '不适用' }}</small>
                <small class="block">发送开始 {{ row.notification?.send_started_at?.replace('T', ' ') || '未记录' }}</small>
                <small class="block">发送完成 {{ row.notification?.send_completed_at?.replace('T', ' ') || '未记录' }}</small>
              </template>
            </el-table-column>
            <el-table-column label="原始原因 / 证据" min-width="270">
              <template #default="{ row }">
                <span>{{ row.reason || '未记录文本原因' }}</span>
                <small v-if="row.confirmation?.sample_count != null" class="block">确认样本 {{ row.confirmation.sample_count }} · 持续 {{ row.confirmation.persistence_sec ?? '—' }} 秒</small>
                <small v-if="row.snapshot_status !== 'available'" class="block">{{ snapshotLabel(row.snapshot_status) }}</small>
              </template>
            </el-table-column>
            <el-table-column label="原ID / 版本" width="245">
              <template #default="{ row }">#{{ row.id }}<br><small>{{ row.route_version }}</small></template>
            </el-table-column>
          </el-table>
        </div>
        <div class="pagination">
          <span>第 {{ page }} 页 · 每页</span>
          <el-select v-model="pageSize" aria-label="每页记录数" @change="search">
            <el-option v-for="size in [20, 50, 100]" :key="size" :value="size" :label="String(size)" />
          </el-select>
          <el-button :disabled="page <= 1 || loading" @click="go(page - 1)">上一页</el-button>
          <el-button :disabled="page * pageSize >= result.total || loading" @click="go(page + 1)">下一页</el-button>
        </div>
      </template>
      <p v-else-if="loading" role="status">正在查询原始检测记录…</p>
    </div>
  </section>
</template>

<script setup>
import { onMounted, onUnmounted, reactive, ref } from 'vue'
import { getPaperC3Records } from '@/api'

const filters = reactive({ trade_date: '', event_type: 'confirmed', version: 'all' })
const keyword = ref('')
const appliedKeyword = ref('')
const page = ref(1)
const pageSize = ref(20)
const result = ref(null)
const error = ref('')
const loading = ref(false)
let requestId = 0
let controller
const eventOptions = [
  { value: 'confirmed', label: '已确认 confirmed' },
  { value: 'eligible', label: '具备资格 eligible' },
  { value: 'reset', label: '确认重置 reset' },
  { value: 'block', label: '所有拦截 block' },
  { value: 'all', label: '全部事件（含失败/未涨停）' },
]
const eventLabel = value => ({
  confirmed: '研究确认', eligible: '具备资格', confirmation_reset: '确认重置',
  coverage_blocked: '覆盖拦截', evidence_blocked: '证据拦截',
  session_blocked: '会话拦截', outcome_blocked: '结算拦截',
  structural_pool: '结构候选', confirmation_sample: '确认采样', universe_audit: '覆盖审计',
  session_ready: '会话就绪', session_outcome: '会话结果', control: '对照样本',
}[value] || value)
const snapshotLabel = value => ({
  available: '摘要可用', oversized: '详情过大，未加载', invalid: '详情损坏，无法解析',
}[value] || '详情未知')
const deliveryLabel = status => ({
  not_queued: '未入队 / 无投递记录（非失败）', not_applicable: '非确认事件，不适用',
  pending: '已入队，待发送', attempting: '正在尝试发送（未确认成功）',
  sent: '通道发送成功（非用户收讫）', failed: '发送失败', rejected: '发送前校验拒绝',
  expired: '信号已过期', throttled: '限流待处理', disabled: '推送关闭', skipped: '跳过发送',
}[status] || status || '投递状态未知')
const deliveryCause = notification => {
  if (notification?.status === 'not_queued') return '历史检测可能未进入提醒队列；不据此推断发送失败'
  if (notification?.status === 'not_applicable') return '仅 confirmed 研究确认关联提醒'
  return notification?.cause || '未记录原因'
}
const price = value => value == null ? '—' : Number(value).toFixed(2)

async function load() {
  const id = ++requestId
  controller?.abort()
  controller = new AbortController()
  loading.value = true
  error.value = ''
  result.value = null // Do not show previous results under new filter labels.
  try {
    const data = await getPaperC3Records({
      ...filters, trade_date: filters.trade_date || undefined,
      keyword: appliedKeyword.value, page: page.value, page_size: pageSize.value,
    }, controller.signal)
    if (id !== requestId) return
    result.value = data
  } catch (err) {
    if (id !== requestId) return
    error.value = 'C3检测记录加载失败，请重试。不会以演示数据或旧筛选结果替代。'
  } finally {
    if (id === requestId) loading.value = false
  }
}
function search() {
  appliedKeyword.value = keyword.value.trim()
  page.value = 1
  load()
}
function go(value) { page.value = value; load() }
onMounted(load)
onUnmounted(() => { ++requestId; controller?.abort() })
</script>

<style scoped>
.c3-records { min-width: 0; color: var(--claw-text); }
header { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 14px; }
h3 { margin: 0 0 8px; }
p, small, .count { color: var(--claw-text-muted); line-height: 1.6; }
header p { margin: 0; }
.filters { display: flex; flex-wrap: wrap; gap: 12px; align-items: flex-end; margin: 18px 0 10px; }
.filters label { display: flex; flex-direction: column; gap: 6px; width: 220px; font-size: 13px; }
.filters :deep(.el-date-editor) { width: 100%; }
.scope { overflow-wrap: anywhere; font-size: 12px; }
.results { min-height: 150px; }
.count { margin-bottom: 12px; font-size: 13px; }
.table-scroll { max-width: 100%; overflow-x: auto; }
.records-table { min-width: 1555px; }
.block { display: block; }
.event-detail { padding: 10px 24px; max-width: 850px; overflow-wrap: anywhere; }
.event-detail dt { font-weight: 600; margin-top: 8px; }
.event-detail dd { margin: 4px 0; color: var(--claw-text-muted); }
.pagination { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-top: 16px; }
.pagination .el-select { width: 85px; }
.error { display: flex; gap: 8px; margin-bottom: 14px; }
@media (max-width: 640px) {
  header { flex-direction: column; align-items: stretch; }
  .filters label { width: 100%; }
  .filters > .el-button { width: 100%; }
  .error { flex-direction: column; }
}
</style>
