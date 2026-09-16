<template>
  <div>
    <div class="kline-toolbar mb-16">
      <el-select
        v-model="klineSectorCode"
        filterable
        remote
        :remote-method="searchSectors"
        :loading="klineSearchLoading"
        placeholder="搜索板块名称..."
        size="default"
        style="width: 280px"
        @change="onKlineSectorChange"
      >
        <el-option
          v-for="s in klineSectorOptions"
          :key="s.sector_code"
          :label="s.sector_name"
          :value="s.sector_code"
        >
          <span>{{ s.sector_name }}</span>
          <el-tag size="small" :type="s.sector_type === 'concept' ? 'danger' : 'warning'" effect="light" class="ml-8">
            {{ s.sector_type === 'concept' ? '概念' : '行业' }}
          </el-tag>
        </el-option>
      </el-select>
      <el-radio-group v-model="klineDays" size="small" @change="loadKlineData" class="ml-16">
        <el-radio-button :value="30">30天</el-radio-button>
        <el-radio-button :value="60">60天</el-radio-button>
        <el-radio-button :value="120">120天</el-radio-button>
        <el-radio-button :value="250">250天</el-radio-button>
      </el-radio-group>
    </div>

    <div v-if="klineLoading" class="loading-container">
      <el-skeleton :rows="10" animated />
    </div>
    <template v-else>
      <el-empty v-if="!klineData.kline?.length && klineSectorCode" description="暂无K线数据" />
      <template v-if="klineData.kline?.length">
        <div class="kline-summary mb-16" v-if="klineData.latest">
          <div class="summary-item">
            <span class="summary-label">{{ klineData.sector_info?.sector_name }}</span>
            <span class="summary-value big" :class="klineData.latest.change_pct >= 0 ? 'text-up' : 'text-down'">
              {{ klineData.latest.close?.toFixed(2) }}
            </span>
          </div>
          <div class="summary-item">
            <span class="summary-label">涨跌幅</span>
            <span class="summary-value" :class="klineData.latest.change_pct >= 0 ? 'text-up' : 'text-down'">
              {{ formatChange(klineData.latest.change_pct) }}
            </span>
          </div>
          <div class="summary-item">
            <span class="summary-label">最高</span>
            <span class="summary-value">{{ klineData.latest.high?.toFixed(2) }}</span>
          </div>
          <div class="summary-item">
            <span class="summary-label">最低</span>
            <span class="summary-value">{{ klineData.latest.low?.toFixed(2) }}</span>
          </div>
          <div class="summary-item">
            <span class="summary-label">成交量</span>
            <span class="summary-value">{{ formatVolume(klineData.latest.volume) }}</span>
          </div>
        </div>

        <v-chart :option="klineChartOption" style="height: 480px" autoresize />
        <v-chart :option="klineMAOption" style="height: 280px; margin-top: 12px" autoresize />
      </template>

      <div v-if="!klineSectorCode" class="kline-quick-pick">
        <div class="quick-title">选择板块查看K线</div>
        <div class="quick-grid">
          <el-card
            v-for="s in hotSectorsForKline"
            :key="s.sector_code"
            shadow="hover"
            class="quick-card"
            @click="selectQuickSector(s.sector_code)"
          >
            <div class="quick-name">{{ s.sector_name }}</div>
            <div class="quick-meta">
              <el-tag :type="s.sector_type === 'concept' ? 'danger' : 'warning'" size="small" effect="light">
                {{ s.sector_type === 'concept' ? '概念' : '行业' }}
              </el-tag>
              <span :class="s.change_pct >= 0 ? 'text-up' : 'text-down'">
                {{ formatChange(s.change_pct) }}
              </span>
            </div>
          </el-card>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup>
import { ref, computed, watch, defineAsyncComponent } from 'vue'
import { ensureKlineChartsRegistered } from '@/composables/echarts/kline'
import { getSectorStrength, getSectorKline } from '@/api'
import { formatChange } from '@/composables/useUtils'

ensureKlineChartsRegistered()
const VChart = defineAsyncComponent(() => import('vue-echarts'))

const props = defineProps({
  sectorCategory: { type: String, required: true },
  strengthList: { type: Array, default: () => [] },
})

const klineSectorCode = ref('')
const klineDays = ref(60)
const klineLoading = ref(false)
const klineData = ref({})
const klineSearchLoading = ref(false)
const klineSectorOptions = ref([])
const sectorSearchPool = ref([])

const hotSectorsForKline = computed(() => {
  return props.strengthList.slice(0, 8).map((s) => ({
    sector_code: s.sector_code,
    sector_name: s.sector_name,
    sector_type: props.sectorCategory,
    change_pct: s.change_pct,
  }))
})

watch(() => props.sectorCategory, () => {
  klineSectorCode.value = ''
  klineData.value = {}
  klineSectorOptions.value = []
  sectorSearchPool.value = []
})

async function searchSectors(query) {
  const keyword = (query || '').trim()
  klineSearchLoading.value = true
  try {
    if (!sectorSearchPool.value.length) {
      const firstPage = await getSectorStrength({
        sector_type: props.sectorCategory,
        page: 1,
        page_size: 200,
      })
      const total = firstPage?.total || 0
      const pages = Math.max(1, Math.ceil(total / 200))
      const allItems = [...(firstPage?.items || [])]

      for (let p = 2; p <= pages; p++) {
        const nextPage = await getSectorStrength({
          sector_type: props.sectorCategory,
          page: p,
          page_size: 200,
        })
        allItems.push(...(nextPage?.items || []))
      }
      sectorSearchPool.value = allItems
    }

    const pool = sectorSearchPool.value
    const filtered = keyword
      ? pool.filter((s) => s.sector_name?.includes(keyword))
      : pool

    klineSectorOptions.value = filtered.slice(0, 50).map((s) => ({
      sector_code: s.sector_code,
      sector_name: s.sector_name,
      sector_type: s.sector_type || props.sectorCategory,
    }))
  } finally {
    klineSearchLoading.value = false
  }
}

async function loadKlineData() {
  if (!klineSectorCode.value) return
  klineLoading.value = true
  try {
    const res = await getSectorKline({
      sector_code: klineSectorCode.value,
      days: klineDays.value,
    })
    klineData.value = res || {}
  } catch (e) {
    console.error('K线数据加载失败:', e)
    klineData.value = {}
  } finally {
    klineLoading.value = false
  }
}

function onKlineSectorChange() {
  loadKlineData()
}

function selectQuickSector(code) {
  klineSectorCode.value = code
  onKlineSectorChange()
}

function formatVolume(val) {
  if (val == null || isNaN(val)) return '--'
  if (val >= 100000000) return `${(val / 100000000).toFixed(2)}亿手`
  if (val >= 10000) return `${(val / 10000).toFixed(1)}万手`
  return `${val.toFixed(0)}手`
}

const klineChartOption = computed(() => {
  const kline = klineData.value.kline || []
  if (!kline.length) return {}

  const dates = kline.map((k) => k.trade_date)
  const ohlc = kline.map((k) => [k.open, k.close, k.low, k.high])
  const volumes = kline.map((k) => k.volume || 0)
  const changes = kline.map((k) => k.change_pct || 0)

  const ma = klineData.value.ma || {}
  const ma5Map = Object.fromEntries((ma.ma5 || []).map((m) => [m.trade_date, m.value]))
  const ma10Map = Object.fromEntries((ma.ma10 || []).map((m) => [m.trade_date, m.value]))
  const ma20Map = Object.fromEntries((ma.ma20 || []).map((m) => [m.trade_date, m.value]))
  const ma60Map = Object.fromEntries((ma.ma60 || []).map((m) => [m.trade_date, m.value]))

  return {
    backgroundColor: 'transparent',
    animation: false,
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
      backgroundColor: 'var(--claw-bg-card)',
      borderColor: 'var(--claw-border)',
      borderWidth: 1,
      textStyle: { color: 'var(--claw-text)', fontSize: 12 },
    },
    legend: {
      data: ['日K', 'MA5', 'MA10', 'MA20', 'MA60'],
      top: 0,
      textStyle: { color: 'var(--claw-text-muted)', fontSize: 11 },
      itemWidth: 12,
      itemHeight: 8,
    },
    grid: [
      { left: 70, right: 30, top: 40, height: '55%' },
      { left: 70, right: 30, top: '72%', height: '18%' },
    ],
    xAxis: [
      { type: 'category', data: dates, gridIndex: 0, axisLabel: { show: false }, axisTick: { show: false }, axisLine: { lineStyle: { color: 'var(--claw-border)' } } },
      { type: 'category', data: dates, gridIndex: 1, axisLabel: { color: 'var(--claw-text-muted)', fontSize: 10 }, axisTick: { show: false }, axisLine: { lineStyle: { color: 'var(--claw-border)' } } },
    ],
    yAxis: [
      { type: 'value', gridIndex: 0, scale: true, axisLabel: { color: 'var(--claw-text-muted)', fontSize: 10 }, splitLine: { lineStyle: { color: 'var(--claw-border-light)', type: 'dashed' } }, axisLine: { show: false } },
      { type: 'value', gridIndex: 1, axisLabel: { color: 'var(--claw-text-muted)', fontSize: 10, formatter: (v) => (v >= 10000 ? `${(v / 10000).toFixed(0)}万` : v) }, splitLine: { lineStyle: { color: 'var(--claw-border-light)', type: 'dashed' } }, axisLine: { show: false } },
    ],
    dataZoom: [
      { type: 'inside', xAxisIndex: [0, 1], start: Math.max(0, 100 - (60 / kline.length) * 100), end: 100 },
    ],
    series: [
      {
        name: '日K',
        type: 'candlestick',
        data: ohlc,
        xAxisIndex: 0,
        yAxisIndex: 0,
        itemStyle: {
          color: 'var(--claw-up)',
          color0: 'var(--claw-down)',
          borderColor: 'var(--claw-up)',
          borderColor0: 'var(--claw-down)',
        },
      },
      {
        name: 'MA5',
        type: 'line',
        data: dates.map((d) => ma5Map[d] ?? null),
        xAxisIndex: 0,
        yAxisIndex: 0,
        smooth: true,
        symbol: 'none',
        lineStyle: { width: 1, color: '#f7a528' },
      },
      {
        name: 'MA10',
        type: 'line',
        data: dates.map((d) => ma10Map[d] ?? null),
        xAxisIndex: 0,
        yAxisIndex: 0,
        smooth: true,
        symbol: 'none',
        lineStyle: { width: 1, color: '#4f6ef7' },
      },
      {
        name: 'MA20',
        type: 'line',
        data: dates.map((d) => ma20Map[d] ?? null),
        xAxisIndex: 0,
        yAxisIndex: 0,
        smooth: true,
        symbol: 'none',
        lineStyle: { width: 1, color: '#8b5cf6' },
      },
      {
        name: 'MA60',
        type: 'line',
        data: dates.map((d) => ma60Map[d] ?? null),
        xAxisIndex: 0,
        yAxisIndex: 0,
        smooth: true,
        symbol: 'none',
        lineStyle: { width: 1, color: '#0ea5e9' },
      },
      {
        name: '成交量',
        type: 'bar',
        data: volumes.map((v, i) => ({
          value: v,
          itemStyle: { color: (changes[i] || 0) >= 0 ? 'rgba(239,68,68,0.5)' : 'rgba(34,197,94,0.5)' },
        })),
        xAxisIndex: 1,
        yAxisIndex: 1,
      },
    ],
  }
})

const klineMAOption = computed(() => {
  const kline = klineData.value.kline || []
  if (!kline.length) return {}

  const dates = kline.map((k) => k.trade_date)
  const ma = klineData.value.ma || {}
  const ma5Map = Object.fromEntries((ma.ma5 || []).map((m) => [m.trade_date, m.value]))
  const ma10Map = Object.fromEntries((ma.ma10 || []).map((m) => [m.trade_date, m.value]))
  const ma20Map = Object.fromEntries((ma.ma20 || []).map((m) => [m.trade_date, m.value]))
  const ma60Map = Object.fromEntries((ma.ma60 || []).map((m) => [m.trade_date, m.value]))

  return {
    backgroundColor: 'transparent',
    animation: false,
    tooltip: {
      trigger: 'axis',
      backgroundColor: 'var(--claw-bg-card)',
      borderColor: 'var(--claw-border)',
      borderWidth: 1,
      textStyle: { color: 'var(--claw-text)', fontSize: 12 },
    },
    legend: {
      data: ['MA5', 'MA10', 'MA20', 'MA60'],
      top: 0,
      textStyle: { color: 'var(--claw-text-muted)', fontSize: 11 },
      itemWidth: 12,
      itemHeight: 8,
    },
    grid: { left: 70, right: 30, top: 30, bottom: 30 },
    xAxis: {
      type: 'category',
      data: dates,
      axisLabel: { color: 'var(--claw-text-muted)', fontSize: 10 },
      axisTick: { show: false },
      axisLine: { lineStyle: { color: 'var(--claw-border)' } },
    },
    yAxis: {
      type: 'value',
      scale: true,
      axisLabel: { color: 'var(--claw-text-muted)', fontSize: 10 },
      splitLine: { lineStyle: { color: 'var(--claw-border-light)', type: 'dashed' } },
      axisLine: { show: false },
    },
    dataZoom: [
      { type: 'inside', start: Math.max(0, 100 - (60 / kline.length) * 100), end: 100 },
    ],
    series: [
      { name: 'MA5', type: 'line', data: dates.map((d) => ma5Map[d] ?? null), smooth: true, symbol: 'none', lineStyle: { width: 1.5, color: '#f7a528' } },
      { name: 'MA10', type: 'line', data: dates.map((d) => ma10Map[d] ?? null), smooth: true, symbol: 'none', lineStyle: { width: 1.5, color: '#4f6ef7' } },
      { name: 'MA20', type: 'line', data: dates.map((d) => ma20Map[d] ?? null), smooth: true, symbol: 'none', lineStyle: { width: 1.5, color: '#8b5cf6' } },
      { name: 'MA60', type: 'line', data: dates.map((d) => ma60Map[d] ?? null), smooth: true, symbol: 'none', lineStyle: { width: 1.5, color: '#0ea5e9' } },
    ],
  }
})
</script>

<style scoped lang="scss">
.mb-16 {
  margin-bottom: var(--spacing-4);
}

.ml-8 {
  margin-left: var(--spacing-2);
}

.ml-16 {
  margin-left: var(--spacing-4);
}

.loading-container {
  padding: var(--spacing-10) 0;
}

.kline-toolbar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--spacing-3);
  padding: var(--spacing-4);
  background: var(--claw-bg-card);
  border: 1px solid var(--claw-border);
  border-radius: var(--radius-lg);
  margin-bottom: var(--spacing-4);
}

.kline-summary {
  display: flex;
  gap: var(--spacing-6);
  flex-wrap: wrap;
  padding: var(--spacing-4) var(--spacing-5);
  background: var(--neutral-50);
  border: 1px solid var(--claw-border-light);
  border-radius: var(--radius-lg);

  .summary-item {
    display: flex;
    flex-direction: column;
    align-items: center;
    min-width: 80px;
  }

  .summary-label {
    font-size: 0.75rem;
    color: var(--claw-text-muted);
    margin-bottom: var(--spacing-1);
    font-weight: 500;
  }

  .summary-value {
    font-size: 1.125rem;
    font-weight: 600;
    font-variant-numeric: tabular-nums;

    &.big {
      font-size: 1.5rem;
    }
  }
}

.kline-quick-pick {
  padding: var(--spacing-5);
  background: var(--claw-bg-card);
  border: 1px solid var(--claw-border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm);

  .quick-title {
    font-size: 1rem;
    font-weight: 600;
    margin-bottom: var(--spacing-4);
    color: var(--claw-text-primary);
  }

  .quick-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
    gap: var(--spacing-3);
  }

  .quick-card {
    cursor: pointer;
    transition: all var(--transition-base);

    &:hover {
      transform: translateY(-2px);
      box-shadow: var(--shadow-md);
      border-color: var(--claw-primary);
    }

    :deep(.el-card__body) {
      padding: var(--spacing-4);
    }
  }

  .quick-name {
    font-size: 1rem;
    font-weight: 600;
    margin-bottom: var(--spacing-3);
    color: var(--claw-text-primary);
  }

  .quick-meta {
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
}

@media (max-width: 767px) {
  .kline-toolbar {
    flex-direction: column;
    align-items: stretch;

    .el-select {
      width: 100% !important;
    }
  }

  .kline-summary {
    justify-content: center;
  }
}
</style>
