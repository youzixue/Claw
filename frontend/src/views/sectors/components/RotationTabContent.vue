<template>
  <div>
    <div v-if="loading" class="loading-container">
      <el-skeleton :rows="5" animated />
    </div>
    <template v-else>
      <el-alert
        v-if="!signals.length"
        title="暂无轮动信号"
        description="轮动信号需要至少2天排名数据对比，且排名变化≥5位时才触发"
        type="info"
        :closable="false"
        show-icon
        class="mb-16"
      />
      <div class="table-container">
        <el-table :data="signals" stripe size="small" empty-text="暂无轮动信号" class="rotation-table">
          <el-table-column prop="from_name" label="流出板块" min-width="130">
            <template #default="{ row }">
              <div class="sector-flow outflow">
                <span :class="{ 'text-down': row.from_name !== '其他' }">{{ row.from_name }}</span>
                <el-tag v-if="row.from_type" size="small" :type="sectorTagType(row.from_type)" effect="light" class="ml-8">
                  {{ row.from_type === 'concept' ? '概念' : '行业' }}
                </el-tag>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="流向" width="70" align="center">
            <template #default>
              <span class="flow-arrow">
                <el-icon :size="20" color="var(--claw-primary)"><Right /></el-icon>
              </span>
            </template>
          </el-table-column>
          <el-table-column prop="to_name" label="流入板块" min-width="130">
            <template #default="{ row }">
              <div class="sector-flow inflow">
                <span :class="{ 'text-up': row.to_name !== '其他' }">{{ row.to_name }}</span>
                <el-tag v-if="row.to_type" size="small" :type="sectorTagType(row.to_type)" effect="light" class="ml-8">
                  {{ row.to_type === 'concept' ? '概念' : '行业' }}
                </el-tag>
              </div>
            </template>
          </el-table-column>
          <el-table-column prop="flow_amount" label="流向(亿)" width="110" align="right">
            <template #default="{ row }">{{ formatFundFlow(row.flow_amount) }}</template>
          </el-table-column>
          <el-table-column prop="rotation_type" label="类型" width="100" align="center">
            <template #default="{ row }">
              <el-tag :type="row.rotation_type === 'sudden' ? 'danger' : 'info'" size="small" effect="light">
                {{ row.rotation_type_label || (row.rotation_type === 'sudden' ? '突然切换' : '渐进轮动') }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="confidence" label="置信度" width="90" align="center">
            <template #default="{ row }">
              <span :class="confidenceClass(row.confidence)" class="confidence-value">
                {{ (row.confidence * 100).toFixed(0) }}%
              </span>
            </template>
          </el-table-column>
        </el-table>
      </div>
    </template>
  </div>
</template>

<script setup>
import { Right } from '@element-plus/icons-vue'

defineProps({
  loading: { type: Boolean, default: false },
  signals: { type: Array, default: () => [] },
  sectorTagType: { type: Function, required: true },
  formatFundFlow: { type: Function, required: true },
  confidenceClass: { type: Function, required: true },
})
</script>

<style scoped lang="scss">
.mb-16 {
  margin-bottom: var(--spacing-4);
}

.ml-8 {
  margin-left: var(--spacing-2);
}

.loading-container {
  padding: var(--spacing-10) 0;
}

.table-container {
  border-radius: var(--radius-lg);
  overflow: hidden;
  border: 1px solid var(--claw-border);
}

.sector-flow {
  display: flex;
  align-items: center;
  gap: var(--spacing-2);
  font-weight: 500;

  &.outflow {
    color: var(--claw-down);
  }

  &.inflow {
    color: var(--claw-up);
  }
}

.flow-arrow {
  display: inline-flex;
  align-items: center;
  justify-content: center;
}

.confidence-value {
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}
</style>
