<template>
  <div class="page-container">
    <div class="stock-header">
      <div class="stock-info">
        <h2 class="page-title" style="margin-bottom:4px">
          {{ profile.name || code }} <span class="text-gray" style="font-size:14px">{{ code }}</span>
          <el-tag v-if="tag" size="small" :type="tag === '✅' ? 'success' : tag === '👁️' ? 'warning' : 'danger'" style="margin-left:8px">{{ tag }}</el-tag>
        </h2>
      </div>
      <div class="score-badge" v-if="score.total_score">
        <div class="score-val" :style="{ color: levelColor(score.level) }">{{ score.total_score }}</div>
        <el-tag :color="levelColor(score.level)" size="small" effect="dark" style="border:none;color:#fff">{{ score.level }}</el-tag>
      </div>
    </div>

    <el-tabs v-model="activeTab" >
      <el-tab-pane label="概况" name="profile">
        <div class="card-grid">
          <el-card shadow="never"><el-descriptions :column="2" size="small" border>
            <el-descriptions-item label="代码">{{ code }}</el-descriptions-item>
            <el-descriptions-item label="名称">{{ profile.name }}</el-descriptions-item>
            <el-descriptions-item label="行业">{{ profile.industry || '--' }}</el-descriptions-item>
            <el-descriptions-item label="板块">{{ profile.sector || '--' }}</el-descriptions-item>
          </el-descriptions></el-card>
          <el-card shadow="never">
            <div style="height:200px;display:flex;align-items:center;justify-content:center;color:#98a2b3">K线图 (待接入行情数据)</div>
          </el-card>
        </div>
      </el-tab-pane>

      <el-tab-pane label="因子" name="factors">
        <el-table :data="factorList" stripe size="small" empty-text="暂无数据">
          <el-table-column prop="name" label="因子" min-width="150" />
          <el-table-column prop="value" label="值" width="100" align="right">
            <template #default="{ row }">{{ row.value != null ? row.value.toFixed(4) : '--' }}</template>
          </el-table-column>
          <el-table-column prop="rank" label="排名" width="80" align="center" />
          <el-table-column prop="pct" label="百分位" width="80" align="center">
            <template #default="{ row }">
              <span :class="row.pct >= 70 ? 'text-red' : row.pct <= 30 ? 'text-green' : ''">{{ row.pct != null ? row.pct.toFixed(1) : '--' }}</span>
            </template>
          </el-table-column>
          <el-table-column prop="confidence" label="置信度" width="80" align="center">
            <template #default="{ row }">{{ row.confidence != null ? (row.confidence * 100).toFixed(0) + '%' : '--' }}</template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane label="资金流" name="fund-flow">
        <v-chart :option="fundFlowChartOption" style="height: 300px" autoresize />
        <el-table :data="fundFlowList" stripe size="small" empty-text="暂无数据" class="mt-16">
          <el-table-column prop="trade_date" label="日期" width="110" />
          <el-table-column prop="main_net_inflow" label="主力净流入" width="120" align="right">
            <template #default="{ row }"><span :class="changeColorClass(row.main_net_inflow)">{{ formatAmount(row.main_net_inflow) }}</span></template>
          </el-table-column>
          <el-table-column prop="big_net_inflow" label="大单" width="100" align="right">
            <template #default="{ row }"><span :class="changeColorClass(row.big_net_inflow)">{{ formatAmount(row.big_net_inflow) }}</span></template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane label="新闻" name="news">
        <el-timeline>
          <el-timeline-item v-for="n in newsList" :key="n.url" :timestamp="n.publish_time" placement="top">
            <el-card shadow="never">
              <div class="news-source"><el-tag size="small">{{ n.source }}</el-tag></div>
              <div class="news-title">{{ n.title }}</div>
              <div class="news-content" v-if="n.content">{{ n.content }}</div>
            </el-card>
          </el-timeline-item>
        </el-timeline>
        <el-empty v-if="!newsList.length" description="暂无新闻" :image-size="60" />
      </el-tab-pane>

      <el-tab-pane label="综合评分" name="score">
        <div class="score-section" v-if="score.total_score">
          <v-chart :option="radarOption" style="height: 350px" autoresize />
          <div v-if="score.risk_warnings?.length" class="section-title">⚠️ 风险预警</div>
          <el-alert v-for="w in score.risk_warnings || []" :key="w" :title="w" type="warning" :closable="false" show-icon style="margin-bottom:8px" />
          <div v-if="score.suggestion" class="section-title">🎯 操作建议</div>
          <el-alert v-if="score.suggestion" :title="score.suggestion" type="info" :closable="false" show-icon />
        </div>
        <el-empty v-else description="暂无评分数据" :image-size="60" />
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup>
import { defineAsyncComponent, ref, computed, onMounted } from 'vue'
import { useRoute } from 'vue-router'
const VChart = defineAsyncComponent(() => import('vue-echarts'))
import { ensureEChartsRegistered } from '@/composables/echarts'
ensureEChartsRegistered()
import { getStockProfile, getStockFactors, getStockNews, getStockFundFlow, getTenbaggerScore } from '@/api'
import { formatChange, changeColorClass, formatAmount, levelColor } from '@/composables/useUtils'

const route = useRoute()
const code = route.params.code

const activeTab = ref('profile')
const profile = ref({})
const tag = ref('')
const factorList = ref([])
const fundFlowList = ref([])
const newsList = ref([])
const score = ref({})

const fundFlowChartOption = computed(() => {
  const items = fundFlowList.value.slice(0, 20)
  if (!items.length) return {}
  return {
    backgroundColor: 'transparent',
    tooltip: { trigger: 'axis' },
    legend: { data: ['主力', '大单', '中单', '小单'], textStyle: { color: '#667085' } },
    grid: { left: 60, right: 20, top: 40, bottom: 30 },
    xAxis: { type: 'category', data: items.map(i => i.trade_date?.slice(5) || ''), axisLabel: { color: '#667085' } },
    yAxis: { type: 'value', axisLabel: { color: '#667085' }, splitLine: { lineStyle: { color: '#f0f2f5' } } },
    series: [
      { name: '主力', type: 'bar', stack: 'flow', data: items.map(i => i.main_net_inflow), itemStyle: { color: '#ef4444' } },
      { name: '大单', type: 'bar', stack: 'flow', data: items.map(i => i.big_net_inflow), itemStyle: { color: '#f97316' } },
      { name: '中单', type: 'bar', stack: 'flow', data: items.map(i => i.mid_net_inflow), itemStyle: { color: '#3b82f6' } },
      { name: '小单', type: 'bar', stack: 'flow', data: items.map(i => i.small_net_inflow), itemStyle: { color: '#22c55e' } },
    ],
  }
})

const radarOption = computed(() => {
  const dims = score.value.dimensions || {}
  const names = Object.keys(dims)
  if (!names.length) return {}
  return {
    backgroundColor: 'transparent',
    radar: { indicator: names.map(n => ({ name: n, max: 100 })), axisName: { color: '#667085' }, splitArea: { areaStyle: { color: ['rgba(79,110,247,0.05)', 'rgba(79,110,247,0.1)'] } } },
    series: [{ type: 'radar', data: [{ value: names.map(n => dims[n] || 0), areaStyle: { color: 'rgba(6,182,212,0.3)' }, lineStyle: { color: '#06b6d4' } }] }],
  }
})

onMounted(async () => {
  try {
    const [p, f, n, ff, s] = await Promise.allSettled([
      getStockProfile(code), getStockFactors(code), getStockNews(code), getStockFundFlow(code), getTenbaggerScore(code),
    ])
    if (p.status === 'fulfilled') { profile.value = p.value.profile || {}; tag.value = p.value.profile?.tag || '' }
    if (f.status === 'fulfilled') {
      const factors = f.value.factors || {}
      factorList.value = Object.entries(factors).map(([name, v]) => ({ name, ...v }))
    }
    if (n.status === 'fulfilled') newsList.value = n.value.news || []
    if (ff.status === 'fulfilled') fundFlowList.value = ff.value.fund_flow || []
    if (s.status === 'fulfilled') score.value = s.value
  } catch { /* ignore */ }
})
</script>

<style scoped lang="scss">
.stock-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 16px; }
.score-badge { text-align: center; }
.score-val { font-size: 36px; font-weight: 700; }
.mt-16 { margin-top: 16px; }
.news-source { margin-bottom: 4px; }
.news-title { font-weight: 600; margin-bottom: 4px; }
.news-content { color: var(--claw-text-secondary); font-size: 12px; }
</style>
