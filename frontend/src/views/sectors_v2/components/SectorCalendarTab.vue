<template>
  <div class="calendar-tab">
    <!-- 日历控制 -->
    <div class="calendar-controls mb-16">
      <el-radio-group v-model="days" size="small" @change="fetchData">
        <el-radio-button :value="7">7天</el-radio-button>
        <el-radio-button :value="10">10天</el-radio-button>
        <el-radio-button :value="15">15天</el-radio-button>
        <el-radio-button :value="20">20天</el-radio-button>
      </el-radio-group>
      <el-checkbox v-model="onlyActive" class="ml-16" @change="fetchData">
        仅显示活跃板块
      </el-checkbox>
    </div>

    <!-- 图例 -->
    <div class="legend mb-16">
      <span class="legend-title">状态图例:</span>
      <span
        v-for="(color, state) in stateColors"
        :key="state"
        class="legend-item"
      >
        <span class="legend-dot" :style="{ background: color }"></span>
        {{ stateLabel(state) }}
      </span>
    </div>

    <!-- 轮动日历表格 -->
    <div class="calendar-table-wrapper" v-loading="loading">
      <table class="calendar-table" v-if="calendarData.rows">
        <thead>
          <tr>
            <th class="sector-header">板块</th>
            <th class="stat-header">活跃</th>
            <th
              v-for="date in calendarData.dates"
              :key="date"
              class="date-header"
            >
              {{ formatDate(date) }}
            </th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="row in filteredRows"
            :key="row.sector_code"
            :class="{ 'main-line': row.is_main_line }"
          >
            <td class="sector-cell">
              <div class="sector-info">
                <span class="sector-name">{{ row.sector_name }}</span>
                <el-tag
                  v-if="row.is_main_line"
                  type="danger"
                  size="small"
                  class="ml-4"
                >
                  主线
                </el-tag>
              </div>
            </td>
            <td class="stat-cell">
              <span :class="activeDaysClass(row.active_days)">
                {{ row.active_days }}天
              </span>
            </td>
            <td
              v-for="cell in row.cells"
              :key="cell.date"
              class="calendar-cell"
              :class="{
                'cell-active': cell.is_active,
                'cell-upgraded': cell.state_change === 'upgraded',
                'cell-downgraded': cell.state_change === 'downgraded',
              }"
              :style="{ background: cell.state_color + '20' }"
              @click="showCellDetail(row, cell)"
            >
              <div
                class="cell-state"
                :style="{ background: cell.state_color }"
              ></div>
              <div v-if="cell.limit_up_count > 0" class="cell-count">
                {{ cell.limit_up_count }}
              </div>
              <div v-if="cell.max_height >= 3" class="cell-height">
                {{ cell.max_height }}板
              </div>
            </td>
          </tr>
        </tbody>
      </table>

      <el-empty v-else description="暂无数据" />
    </div>

    <!-- 单元格详情弹窗 -->
    <el-dialog
      v-model="detailVisible"
      :title="detailTitle"
      width="500px"
    >
      <div v-if="selectedCell" class="cell-detail">
        <el-descriptions :column="2" border>
          <el-descriptions-item label="日期">
            {{ selectedCell.date }}
          </el-descriptions-item>
          <el-descriptions-item label="状态">
            <el-tag :type="stateTagType(selectedCell.state)">
              {{ stateLabel(selectedCell.state) }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="涨停数">
            {{ selectedCell.limit_up_count }}
          </el-descriptions-item>
          <el-descriptions-item label="最高板">
            {{ selectedCell.max_height }}板
          </el-descriptions-item>
          <el-descriptions-item label="资金流" :span="2">
            <span :class="selectedCell.fund_flow >= 0 ? 'text-red' : 'text-green'">
              {{ selectedCell.fund_flow >= 0 ? '+' : '' }}{{ selectedCell.fund_flow }}亿
            </span>
          </el-descriptions-item>
        </el-descriptions>

        <div v-if="selectedCell.leader_stocks && selectedCell.leader_stocks.length" class="mt-16">
          <div class="detail-section-title">龙头股</div>
          <el-tag
            v-for="stock in selectedCell.leader_stocks"
            :key="stock.code"
            class="mr-8"
          >
            {{ stock.name }}({{ stock.height }}板)
          </el-tag>
        </div>
      </div>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'

const props = defineProps({
  sectorType: String
})

const loading = ref(false)
const days = ref(10)
const onlyActive = ref(true)
const calendarData = ref({ dates: [], rows: [] })
const detailVisible = ref(false)
const selectedCell = ref(null)
const selectedRow = ref(null)

const stateColors = {
  dormant: '#6b7280',
  emerging: '#22c55e',
  accelerating: '#3b82f6',
  climax: '#ef4444',
  diverging: '#f59e0b',
  declining: '#8b5cf6',
  one_day: '#94a3b8',
}

const filteredRows = computed(() => {
  let rows = calendarData.value.rows || []
  if (onlyActive.value) {
    rows = rows.filter(r => r.active_days >= 2)
  }
  return rows
})

const detailTitle = computed(() => {
  if (selectedRow.value && selectedCell.value) {
    return `${selectedRow.value.sector_name} - ${selectedCell.value.date}`
  }
  return '详情'
})

const formatDate = (dateStr) => {
  const d = new Date(dateStr)
  return `${d.getMonth() + 1}/${d.getDate()}`
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

const activeDaysClass = (days) => {
  if (days >= 5) return 'text-red font-bold'
  if (days >= 3) return 'text-orange'
  return 'text-gray'
}

const showCellDetail = (row, cell) => {
  selectedRow.value = row
  selectedCell.value = cell
  detailVisible.value = true
}

const fetchData = async () => {
  loading.value = true
  try {
    // 模拟数据
    const dates = []
    for (let i = 9; i >= 0; i--) {
      const d = new Date()
      d.setDate(d.getDate() - i)
      dates.push(d.toISOString().split('T')[0])
    }

    const rows = [
      {
        sector_code: 'AI001',
        sector_name: '人工智能',
        sector_type: 'concept',
        active_days: 8,
        is_main_line: true,
        cells: dates.map((d, i) => ({
          date: d,
          state: i < 2 ? 'dormant' : i < 4 ? 'emerging' : i < 7 ? 'accelerating' : 'climax',
          state_color: stateColors[i < 2 ? 'dormant' : i < 4 ? 'emerging' : i < 7 ? 'accelerating' : 'climax'],
          limit_up_count: i < 2 ? 0 : i < 4 ? 3 : i < 7 ? 8 : 15,
          max_height: i < 2 ? 0 : i < 4 ? 2 : i < 7 ? 4 : 7,
          fund_flow: i < 2 ? -2 : i < 4 ? 5 : i < 7 ? 15 : 25,
          is_active: i >= 2,
          state_change: i === 2 ? 'new' : i === 4 ? 'upgraded' : i === 7 ? 'upgraded' : 'unchanged',
          leader_stocks: i >= 4 ? [{ name: '龙头A', height: Math.min(i - 1, 7) }] : [],
        })),
      },
      {
        sector_code: 'CHIP001',
        sector_name: '芯片',
        sector_type: 'concept',
        active_days: 5,
        is_main_line: true,
        cells: dates.map((d, i) => ({
          date: d,
          state: i < 5 ? 'dormant' : i < 6 ? 'emerging' : 'accelerating',
          state_color: stateColors[i < 5 ? 'dormant' : i < 6 ? 'emerging' : 'accelerating'],
          limit_up_count: i < 5 ? 0 : i < 6 ? 2 : 8,
          max_height: i < 5 ? 0 : i < 6 ? 1 : 4,
          fund_flow: i < 5 ? 0 : i < 6 ? 3 : 12,
          is_active: i >= 5,
          state_change: i === 5 ? 'new' : i === 6 ? 'upgraded' : 'unchanged',
        })),
      },
    ]

    calendarData.value = { dates, rows }
  } finally {
    loading.value = false
  }
}

fetchData()
</script>

<style scoped lang="scss">
.calendar-tab {
  padding: 8px 0;
}

.calendar-controls {
  display: flex;
  align-items: center;
}

.ml-16 {
  margin-left: 16px;
}

.legend {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.legend-title {
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.legend-item {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
}

.legend-dot {
  width: 10px;
  height: 10px;
  border-radius: 2px;
}

.calendar-table-wrapper {
  overflow-x: auto;
}

.calendar-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;

  th, td {
    border: 1px solid var(--el-border-color);
    padding: 8px;
    text-align: center;
  }

  th {
    background: var(--el-fill-color-light);
    font-weight: 500;
  }

  .sector-header {
    min-width: 120px;
    text-align: left;
  }

  .stat-header {
    min-width: 50px;
  }

  .date-header {
    min-width: 50px;
    font-size: 11px;
  }

  .sector-cell {
    text-align: left;
    background: var(--el-fill-color);
  }

  .sector-info {
    display: flex;
    align-items: center;
  }

  .sector-name {
    font-weight: 500;
  }

  .stat-cell {
    font-size: 11px;
  }

  .calendar-cell {
    position: relative;
    min-width: 50px;
    height: 50px;
    cursor: pointer;
    transition: all 0.2s;

    &:hover {
      filter: brightness(0.95);
    }

    &.cell-active {
      font-weight: bold;
    }

    &.cell-upgraded::after,
    &.cell-downgraded::after {
      content: '';
      position: absolute;
      top: 2px;
      right: 2px;
      width: 0;
      height: 0;
      border-style: solid;
    }

    &.cell-upgraded::after {
      border-width: 0 8px 8px 0;
      border-color: transparent #22c55e transparent transparent;
    }

    &.cell-downgraded::after {
      border-width: 0 0 8px 8px;
      border-color: transparent transparent #ef4444 transparent;
    }
  }

  .cell-state {
    position: absolute;
    top: 2px;
    left: 2px;
    right: 2px;
    height: 4px;
    border-radius: 2px;
  }

  .cell-count {
    margin-top: 8px;
    font-size: 14px;
    color: #ef4444;
  }

  .cell-height {
    font-size: 10px;
    color: var(--el-text-color-secondary);
  }

  tr.main-line {
    .sector-cell {
      background: rgba(239, 68, 68, 0.05);
    }
  }
}

.cell-detail {
  padding: 8px;
}

.detail-section-title {
  font-size: 14px;
  font-weight: 500;
  margin-bottom: 8px;
  color: var(--el-text-color-primary);
}

.mt-16 {
  margin-top: 16px;
}

.mr-8 {
  margin-right: 8px;
}

.mb-16 {
  margin-bottom: 16px;
}

.text-red { color: #ef4444; }
.text-green { color: #22c55e; }
.text-orange { color: #f59e0b; }
.text-gray { color: #6b7280; }
.font-bold { font-weight: bold; }
</style>
