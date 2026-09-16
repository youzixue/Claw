<template>
  <div>
    <div v-if="loading" class="loading-container">
      <el-skeleton :rows="10" animated />
    </div>
    <template v-else>
      <div class="calendar-toolbar mb-16">
        <el-radio-group :model-value="calendarDays" size="small" @change="$emit('change-days', $event)">
          <el-radio-button :value="7">7天</el-radio-button>
          <el-radio-button :value="10">10天</el-radio-button>
          <el-radio-button :value="15">15天</el-radio-button>
          <el-radio-button :value="20">20天</el-radio-button>
        </el-radio-group>
      </div>

      <div class="calendar-table-wrapper" v-if="calendarData.dates?.length">
        <table class="calendar-table">
          <thead>
            <tr>
              <th class="sector-col">板块</th>
              <th v-for="d in calendarData.dates" :key="d" class="date-col">
                {{ formatDateShort(d) }}
              </th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="sector in calendarData.sectors" :key="sector.code">
              <td class="sector-cell">
                <div class="sector-name">{{ sector.name }}</div>
                <el-tag size="small" :type="sector.type === 'concept' ? 'danger' : 'warning'" effect="light">
                  {{ sector.type === 'concept' ? '概念' : '行业' }}
                </el-tag>
              </td>
              <td
                v-for="d in calendarData.dates"
                :key="d"
                class="state-cell"
                :class="calendarCellClass(calendarData.matrix[sector.code]?.[d])"
              >
                <template v-if="calendarData.matrix[sector.code]?.[d]">
                  <div class="state-badge">{{ calendarStateLabel(calendarData.matrix[sector.code][d]) }}</div>
                  <div v-if="calendarData.leaders[sector.code]?.[d]?.length" class="leader-hint">
                    {{ calendarData.leaders[sector.code][d][0].name }}
                  </div>
                </template>
                <span v-else class="empty-cell">-</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <el-empty v-else description="暂无日历数据" />
    </template>
  </div>
</template>

<script setup>
defineProps({
  loading: { type: Boolean, default: false },
  calendarData: { type: Object, default: () => ({}) },
  calendarDays: { type: Number, default: 10 },
  formatDateShort: { type: Function, required: true },
  calendarCellClass: { type: Function, required: true },
  calendarStateLabel: { type: Function, required: true },
})

defineEmits(['change-days'])
</script>

<style scoped lang="scss">
.mb-16 {
  margin-bottom: var(--spacing-4);
}

.loading-container {
  padding: var(--spacing-10) 0;
}

.calendar-toolbar {
  display: flex;
  justify-content: flex-end;
  margin-bottom: var(--spacing-4);
}

.calendar-table-wrapper {
  overflow-x: auto;
  max-height: 600px;
  overflow-y: auto;
  background: var(--claw-bg-card);
  border: 1px solid var(--claw-border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm);
}

.calendar-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.8125rem;

  th, td {
    border: 1px solid var(--claw-border-light);
    padding: var(--spacing-2);
    text-align: center;
  }

  th {
    background: var(--neutral-50);
    font-weight: 600;
    position: sticky;
    top: 0;
    z-index: 1;
    color: var(--claw-text-secondary);
  }

  .sector-col {
    min-width: 120px;
    position: sticky;
    left: 0;
    background: var(--claw-bg-card);
    z-index: 2;
  }

  .date-col {
    min-width: 60px;
  }

  .sector-cell {
    text-align: left;
    background: var(--claw-bg-card);
    position: sticky;
    left: 0;
  }

  .sector-name {
    font-weight: 500;
    margin-bottom: var(--spacing-1);
  }

  .state-cell {
    min-width: 60px;
    height: 50px;
    vertical-align: middle;
  }

  .state-badge {
    font-size: 0.6875rem;
    padding: var(--spacing-1) var(--spacing-2);
    border-radius: var(--radius-sm);
    display: inline-block;
    font-weight: 500;
  }

  .leader-hint {
    font-size: 0.625rem;
    color: var(--claw-text-muted);
    margin-top: var(--spacing-1);
  }

  .empty-cell {
    color: var(--claw-text-muted);
  }

  .cell-emerging { background: rgba(34, 197, 94, 0.12); .state-badge { background: rgba(34, 197, 94, 0.3); color: var(--success-700); } }
  .cell-accelerating { background: rgba(14, 165, 233, 0.12); .state-badge { background: rgba(14, 165, 233, 0.3); color: var(--primary-700); } }
  .cell-climax { background: rgba(239, 68, 68, 0.12); .state-badge { background: rgba(239, 68, 68, 0.3); color: var(--error-700); } }
  .cell-diverging { background: rgba(245, 158, 11, 0.12); .state-badge { background: rgba(245, 158, 11, 0.3); color: var(--warning-700); } }
  .cell-declining { background: rgba(100, 116, 139, 0.12); .state-badge { background: rgba(100, 116, 139, 0.3); color: var(--neutral-700); } }
  .cell-oneday { background: rgba(203, 213, 225, 0.12); .state-badge { background: rgba(203, 213, 225, 0.5); color: var(--neutral-600); } }
  .cell-dormant { background: transparent; .state-badge { background: var(--neutral-100); color: var(--neutral-500); } }
}
</style>
