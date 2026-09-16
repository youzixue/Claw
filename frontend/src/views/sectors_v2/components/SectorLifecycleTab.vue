<template>
  <div class="lifecycle-tab">
    <!-- 状态说明 + 汇总(一行紧凑) -->
    <el-row class="summary-bar" :gutter="8" align="middle">
      <el-col :span="24">
        <div class="summary-row">
          <span class="summary-item" v-for="stat in allStateStats" :key="stat.state"
            @click="filterByState(stat.state)"
            :class="{ active: currentFilter === stat.state }">
            <i class="dot" :class="`dot-${stat.state}`"></i>
            <span class="label">{{ stat.label }}</span>
            <span class="count">{{ stat.count }}</span>
          </span>
        </div>
      </el-col>
    </el-row>

    <!-- 状态图例说明 -->
    <div class="state-legend-bar">
      <el-tooltip v-for="s in stateDescriptions" :key="s.state" :content="s.desc" placement="top">
        <span class="legend-item">
          <i class="dot dot-tiny" :class="`dot-${s.state}`"></i>{{ s.label }}
        </span>
      </el-tooltip>
    </div>

    <!-- 板块列表 -->
    <el-table
      :data="sectorList"
      stripe
      size="small"
      v-loading="loading"
      @row-click="onRowClick"
    >
      <el-table-column prop="sector_name" label="板块" min-width="120">
        <template #default="{ row }">
          <div class="sector-name">
            <span>{{ row.sector_name }}</span>
            <el-tag v-if="row.is_main_line" type="danger" size="small" class="ml-4">主线</el-tag>
          </div>
        </template>
      </el-table-column>

      <el-table-column label="状态" width="90" align="center">
        <template #default="{ row }">
          <el-tag
            :type="stateTagType(row.lifecycle_state)"
            size="small"
            effect="dark"
          >
            {{ stateLabel(row.lifecycle_state) }}
          </el-tag>
        </template>
      </el-table-column>

      <el-table-column label="强度" width="70" align="center">
        <template #default="{ row }">
          <span :class="strengthClass(row.strength_score)">{{ row.strength_score != null ? Math.round(row.strength_score) : '-' }}</span>
        </template>
      </el-table-column>

      <el-table-column label="涨停/首板/连板" width="130" align="center">
        <template #default="{ row }">
          <span class="text-red">{{ row.limit_up_count ?? '-' }}</span>
          <span class="text-gray"> / </span>
          <span>{{ row.first_board_count ?? '-' }}</span>
          <span class="text-gray"> / </span>
          <span class="text-orange">{{ row.consecutive_board_count ?? '-' }}</span>
        </template>
      </el-table-column>

      <el-table-column prop="max_board_height" label="最高板" width="70" align="center">
        <template #default="{ row }">
          <span v-if="row.max_board_height > 0" :class="heightClass(row.max_board_height)">{{ row.max_board_height }}板</span>
          <span v-else class="text-gray">-</span>
        </template>
      </el-table-column>

      <el-table-column label="龙头股" min-width="150">
        <template #default="{ row }">
          <div v-if="row.leader_stocks && row.leader_stocks.length" class="leader-list">
            <el-tag
              v-for="leader in row.leader_stocks.slice(0, 2)"
              :key="leader.code"
              size="small"
              class="mr-4"
            >
              {{ leader.name }}({{ leader.height }}板)
            </el-tag>
          </div>
          <span v-else class="text-gray">-</span>
        </template>
      </el-table-column>

      <el-table-column prop="fund_flow" label="资金流(亿)" width="100" align="right">
        <template #default="{ row }">
          <span v-if="row.fund_flow != null && row.fund_flow !== ''" :class="fundFlowClass(row.fund_flow)">
            {{ row.fund_flow > 0 ? '+' : '' }}{{ Number(row.fund_flow).toFixed(1) }}
          </span>
          <span v-else class="text-gray">-</span>
        </template>
      </el-table-column>

      <el-table-column prop="quality_score" label="质量分" width="80" align="center">
        <template #default="{ row }">
          <span v-if="row.quality_score != null && row.quality_score > 0">{{ Math.round(row.quality_score) }}</span>
          <span v-else class="text-gray">-</span>
        </template>
      </el-table-column>

      <el-table-column label="梯队" min-width="200">
        <template #default="{ row }">
          <div v-if="row.ladders && row.ladders.length" class="ladder-preview">
            <span
              v-for="ladder in row.ladders.slice(0, 3)"
              :key="ladder.height"
              class="ladder-item"
            >
              {{ ladder.height }}板({{ ladder.stocks.length }}只)
            </span>
          </div>
          <span v-else class="text-gray">-</span>
        </template>
      </el-table-column>
    </el-table>

    <!-- 分页 -->
    <div class="pagination-wrap" v-if="total > pageSize">
      <el-pagination
        v-model:current-page="currentPage"
        :page-size="pageSize"
        :total="total"
        layout="prev, pager, next"
        small
        @current-change="onPageChange"
      />
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch } from 'vue'
import { useRouter } from 'vue-router'
import { notifyError } from '@/utils/message'

const props = defineProps({
  sectorType: String
})

const router = useRouter()
const loading = ref(false)
const sectorList = ref([])
const total = ref(0)
const currentPage = ref(1)
const pageSize = ref(20)
const stateStats = ref({})
const currentFilter = ref('')

// 全部7个状态(用于汇总栏)
const allStateStats = computed(() => [
  { state: 'emerging', label: '刚启动', count: stateStats.value.emerging || 0 },
  { state: 'accelerating', label: '加速', count: stateStats.value.accelerating || 0 },
  { state: 'climax', label: '高潮', count: stateStats.value.climax || 0 },
  { state: 'diverging', label: '分化', count: stateStats.value.diverging || 0 },
  { state: 'declining', label: '退潮', count: stateStats.value.declining || 0 },
  { state: 'one_day', label: '一日游', count: stateStats.value.one_day || 0 },
  { state: 'dormant', label: '休眠', count: stateStats.value.dormant || 0 },
])

// 状态说明(用于tooltip)
const stateDescriptions = [
  { state: 'emerging', label: '刚启动', desc: '首板出现(1-2只),资金初进,新热点萌芽' },
  { state: 'accelerating', label: '加速', desc: '连板梯队形成(>=3只),资金持续流入,板块加速' },
  { state: 'climax', label: '高潮', desc: '批量涨停(>=10只),龙头高度>5板,注意分化风险' },
  { state: 'diverging', label: '分化', desc: '从高潮/加速回落,掉队股增多,后排开始跌' },
  { state: 'declining', label: '退潮', desc: '龙头断板,批量跌停,资金大幅流出,回避' },
  { state: 'one_day', label: '一日游', desc: '当天涨停次日无持续,无溢价,避免追高' },
  { state: 'dormant', label: '休眠', desc: '无涨停,无资金关注,等待信号' },
]

const qualityColor = [
  { color: '#ef4444', percentage: 80 },
  { color: '#f59e0b', percentage: 60 },
  { color: '#3b82f6', percentage: 40 },
  { color: '#6b7280', percentage: 0 },
]

const stateTagType = (state) => {
  const map = {
    emerging: 'success',
    accelerating: 'primary',
    climax: 'danger',
    diverging: 'warning',
    declining: 'info',
    one_day: 'info',
    dormant: 'info',
  }
  return map[state] || 'info'
}

const stateLabel = (state) => {
  const map = {
    emerging: '刚启动',
    accelerating: '加速',
    climax: '高潮',
    diverging: '分化',
    declining: '退潮',
    one_day: '一日游',
    dormant: '休眠',
  }
  return map[state] || state
}

const heightClass = (height) => {
  if (height >= 5) return 'text-red font-bold'
  if (height >= 3) return 'text-orange'
  return 'text-gray'
}

const fundFlowClass = (flow) => {
  if (flow > 5) return 'text-red font-bold'
  if (flow > 0) return 'text-red'
  if (flow < -5) return 'text-green font-bold'
  return 'text-green'
}

const strengthClass = (score) => {
  if (score >= 80) return 'text-red font-bold'
  if (score >= 60) return 'text-orange'
  if (score >= 40) return 'text-blue'
  if (score > 0) return ''
  return 'text-gray'
}

const filterByState = (state) => {
  currentFilter.value = currentFilter.value === state ? '' : state
  currentPage.value = 1
  fetchData()
}

const onPageChange = () => {
  fetchData()
}

const onRowClick = (row) => {
  router.push(`/sectors/${row.sector_code}`)
}

const fetchData = async () => {
  loading.value = true
  try {
    const params = {
      page: currentPage.value,
      page_size: pageSize.value,
    }
    if (props.sectorType) params.sector_type = props.sectorType
    if (currentFilter.value) params.state = currentFilter.value

    const res = await fetch(`/api/v2/sectors/lifecycle?${new URLSearchParams(params).toString()}`)
    const data = await res.json()

    sectorList.value = data.items || []
    total.value = data.total || 0
    stateStats.value = data.state_stats || {}
  } catch (e) {
    console.error('获取生命周期数据失败:', e)
    notifyError('获取生命周期数据失败')
  } finally {
    loading.value = false
  }
}

watch(() => props.sectorType, fetchData, { immediate: true })
</script>

<style scoped lang="scss">
.lifecycle-tab {
  padding: 8px 0;
}

/* 一行汇总栏 */
.summary-bar {
  margin-bottom: 8px;
}

.summary-row {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
  align-items: center;
}

.summary-item {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  padding: 4px 10px;
  border-radius: 14px;
  cursor: pointer;
  transition: all 0.2s;
  background: var(--el-fill-color-lighter);
  font-size: 12px;
  user-select: none;

  &:hover, &.active {
    transform: scale(1.05);
    box-shadow: 0 1px 6px rgba(0,0,0,0.1);
  }
  &.active {
    font-weight: bold;
  }

  .dot {
    width: 8px; height: 8px; border-radius: 50%; display: inline-block; flex-shrink: 0;
  }

  .label { color: var(--el-text-color-secondary); white-space: nowrap; }
  .count { font-weight: 600; min-width: 16px; text-align: right; }
}

.dot-emerging { background: #22c55e; }
.dot-accelerating { background: #3b82f6; }
.dot-climax { background: #ef4444; }
.dot-diverging { background: #f59e0b; }
.dot-declining { background: #8b5cf6; }
.dot-one_day { background: #94a3b8; }
.dot-dormant { background: #d1d5db; }
.dot-tiny {
  width: 7px; height: 7px;
}

/* 状态图例说明 */
.state-legend-bar {
  display: flex;
  gap: 10px;
  margin-bottom: 12px;
  padding: 6px 10px;
  background: var(--el-fill-color-lighter);
  border-radius: 6px;
  overflow-x: auto;
  white-space: nowrap;
}

.legend-item {
  font-size: 11px;
  color: var(--el-text-color-regular);
  cursor: default;
  display: inline-flex;
  align-items: center;
  gap: 3px;
}

.sector-name {
  display: flex;
  align-items: center;
}

.leader-list {
  display: flex;
  gap: 4px;
}

.ladder-preview {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.ladder-item {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  background: var(--el-fill-color-light);
  padding: 2px 6px;
  border-radius: 4px;
}

.mb-16 { margin-bottom: 16px; }
.mr-4 { margin-right: 4px; }
.ml-4 { margin-left: 4px; }

.pagination-wrap {
  margin-top: 16px;
  display: flex;
  justify-content: center;
}

.text-red { color: #ef4444; }
.text-green { color: #22c55e; }
.text-orange { color: #f59e0b; }
.text-blue { color: #3b82f6; }
.text-gray { color: #6b7280; }
.font-bold { font-weight: bold; }
</style>
