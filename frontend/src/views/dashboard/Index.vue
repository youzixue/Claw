<template>
  <div class="page-container overview-v2-page">
    <div class="page-head">
      <div>
        <h2 class="page-title">🦅 Dashboard 2.0</h2>
        <div class="page-subtitle">跨市场联动总览，自动读取后台快照</div>
      </div>
      <div class="snapshot-time">
        <span class="time-label">快照时间</span>
        <span class="time-value">{{ formatDate(snapshotTime, 'MM-DD HH:mm:ss') }}</span>
      </div>
    </div>

    <div v-if="loading" class="loading-wrapper">
      <el-skeleton :rows="10" animated />
    </div>

    <template v-else>
      <div class="section-title">结论卡片</div>
      <div class="conclusion-grid">
        <div v-for="item in conclusions" :key="item.key" class="conclusion-card" :class="`tone-${item.tone || 'neutral'}`">
          <div class="stat-label">{{ item.label }}</div>
          <div class="conclusion-value">{{ item.value }}</div>
          <div class="mapping-note">{{ item.note || '--' }}</div>
        </div>
      </div>

      <div class="section-title">外部联动因子</div>
      <div class="factor-groups">
        <div v-for="group in groupedFactors" :key="group.key" class="factor-group-card">
          <div class="group-title">{{ group.label }}</div>
          <div class="factor-grid">
            <div v-for="item in group.items" :key="item.key" class="stat-card factor-card">
              <div class="stat-label">{{ item.label }}</div>
              <div class="stat-value big" :class="changeColorClass(item.change_pct)">
                {{ item.price != null ? Number(item.price).toFixed(2) : '--' }}
              </div>
              <div class="stat-sub" :class="changeColorClass(item.change_pct)">
                {{ formatChange(item.change_pct) }}
              </div>
              <div class="factor-meta">
                <span class="market-tag">{{ item.market || '--' }}</span>
                <span class="stat-date">{{ item.trade_time || '--' }}</span>
              </div>
            </div>
          </div>
        </div>
        <el-empty v-if="externalFactors.length === 0" description="暂无外部联动数据" :image-size="40" />
      </div>

      <div class="section-title">外盘主题 → A股映射</div>
      <div class="mapping-list">
        <div v-for="item in mappingInsights" :key="item.source_key" class="mapping-card">
          <div class="mapping-head">
            <div>
              <div class="mapping-title">{{ item.source_label }}</div>
              <div class="mapping-themes">
                <el-tag v-for="theme in item.a_share_themes" :key="theme" size="small" effect="plain" class="theme-tag">
                  {{ theme }}
                </el-tag>
              </div>
            </div>
            <el-tag :type="mappingStatusType(item.status)" round>
              {{ mappingStatusLabel(item.status) }}
            </el-tag>
          </div>
          <div class="mapping-note">{{ item.note || '--' }}</div>
        </div>
        <el-empty v-if="mappingInsights.length === 0" description="暂无映射判断" :image-size="40" />
      </div>
    </template>

    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" style="margin-top: 16px" />
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { getDashboardOverviewV2 } from '@/api'
import { changeColorClass, formatChange, formatDate } from '@/composables/useUtils'

const data = ref({
  snapshot_time: null,
  conclusions: [],
  external_factors: [],
  mapping_insights: [],
})
const loading = ref(true)
const error = ref('')

const snapshotTime = computed(() => data.value.snapshot_time)
const conclusions = computed(() => data.value.conclusions || [])
const externalFactors = computed(() => data.value.external_factors || [])
const mappingInsights = computed(() => data.value.mapping_insights || [])

const factorGroupMeta = {
  HK: { key: 'hk', label: '港股' },
  US: { key: 'us', label: '美股' },
  US_CN: { key: 'us_cn', label: '中概' },
  A50: { key: 'a50', label: 'A50 / 权重' },
  US_RATE: { key: 'macro', label: '宏观' },
  CMDTY: { key: 'commodity', label: '商品' },
  FX: { key: 'fx', label: '汇率' },
}

const groupedFactors = computed(() => {
  const groups = new Map()
  externalFactors.value.forEach((item) => {
    const meta = factorGroupMeta[item.market] || { key: 'other', label: '其他' }
    if (!groups.has(meta.key)) {
      groups.set(meta.key, { ...meta, items: [] })
    }
    groups.get(meta.key).items.push(item)
  })
  return Array.from(groups.values())
})

const mappingStatusLabel = (status) => {
  const map = {
    synced: '同步',
    diverging: '背离',
    lagging: '滞后',
    missing: '缺失',
    pending: '待定',
  }
  return map[status] || status || '--'
}

const mappingStatusType = (status) => {
  const map = {
    synced: 'success',
    diverging: 'danger',
    lagging: 'warning',
    missing: 'info',
    pending: 'info',
  }
  return map[status] || 'info'
}

onMounted(async () => {
  try {
    loading.value = true
    data.value = await getDashboardOverviewV2()
  } catch (e) {
    error.value = 'Dashboard 2.0 加载失败'
    console.error('overview-v2 数据加载失败:', e)
  } finally {
    loading.value = false
  }
})
</script>

<style scoped lang="scss">
.overview-v2-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.page-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  gap: 16px;
}

.page-subtitle {
  color: var(--claw-text-muted, #909399);
  font-size: 13px;
  margin-top: 4px;
}

.snapshot-time {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 4px;
}

.time-label {
  color: var(--claw-text-muted, #909399);
  font-size: 12px;
}

.time-value {
  font-size: 13px;
  color: var(--el-text-color-primary);
}

.conclusion-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 16px;
}

.conclusion-card {
  border-radius: 10px;
  padding: 16px;
  border: 1px solid var(--claw-border);
  background: var(--claw-bg-card);
}

.conclusion-value {
  font-size: 20px;
  font-weight: 700;
  margin: 8px 0 10px;
}

.tone-positive {
  border-color: rgba(103, 194, 58, 0.35);
  background: rgba(103, 194, 58, 0.08);
}

.tone-negative {
  border-color: rgba(245, 108, 108, 0.35);
  background: rgba(245, 108, 108, 0.08);
}

.tone-warning {
  border-color: rgba(230, 162, 60, 0.35);
  background: rgba(230, 162, 60, 0.08);
}

.tone-neutral {
  border-color: var(--claw-border);
}

.factor-groups {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.factor-group-card {
  background: var(--claw-bg-card);
  border: 1px solid var(--claw-border);
  border-radius: 10px;
  padding: 16px;
}

.group-title {
  font-size: 16px;
  font-weight: 700;
  margin-bottom: 12px;
  color: var(--el-text-color-primary);
}

.factor-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 16px;
}

.factor-card {
  min-height: 132px;
}

.factor-meta {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-top: 10px;
  gap: 8px;
}

.market-tag {
  display: inline-flex;
  align-items: center;
  padding: 2px 8px;
  border-radius: 999px;
  background: rgba(64, 158, 255, 0.12);
  color: #409eff;
  font-size: 12px;
}

.mapping-list {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 16px;
}

.mapping-card {
  background: var(--claw-bg-card);
  border: 1px solid var(--claw-border);
  border-radius: 8px;
  padding: 16px;
}

.mapping-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
}

.mapping-title {
  font-size: 16px;
  font-weight: 600;
  color: var(--el-text-color-primary);
  margin-bottom: 8px;
}

.mapping-themes {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.theme-tag {
  margin: 0;
}

.mapping-note {
  margin-top: 12px;
  color: var(--claw-text-muted, #909399);
  line-height: 1.6;
}

.stat-date {
  font-size: 11px;
  color: var(--claw-text-muted, #909399);
}

.loading-wrapper {
  padding: 40px 0;
}

@media (max-width: 1200px) {
  .conclusion-grid,
  .factor-grid {
    grid-template-columns: repeat(2, 1fr);
  }
}

@media (max-width: 768px) {
  .page-head {
    flex-direction: column;
    align-items: flex-start;
  }

  .snapshot-time {
    align-items: flex-start;
  }

  .conclusion-grid,
  .factor-grid,
  .mapping-list {
    grid-template-columns: 1fr;
  }
}
</style>
