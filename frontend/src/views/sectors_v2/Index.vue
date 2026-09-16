<template>
  <div class="page-container">
    <h2 class="page-title"><el-icon><DataBoard /></el-icon> 板块营地 v2 — 生命周期与轮动</h2>

    <!-- 板块类型切换 -->
    <div class="category-tabs mb-16">
      <el-radio-group v-model="sectorType" size="default" @change="onTypeChange">
        <el-radio-button value="concept">概念板块</el-radio-button>
        <el-radio-button value="industry">行业板块</el-radio-button>
      </el-radio-group>
      <span class="count-badge">{{ stateStatsText }}</span>
    </div>

    <!-- 子Tab -->
    <el-tabs v-model="activeTab" @tab-change="onTabChange">
      <!-- 生命周期状态 -->
      <el-tab-pane label="生命周期" name="lifecycle">
        <SectorLifecycleTab :sector-type="sectorType" />
      </el-tab-pane>

      <!-- 轮动日历 -->
      <el-tab-pane label="轮动日历" name="calendar">
        <SectorCalendarTab :sector-type="sectorType" />
      </el-tab-pane>

      <!-- 主线板块 -->
      <el-tab-pane label="主线追踪" name="mainlines">
        <MainLineTab :sector-type="sectorType" />
      </el-tab-pane>

      <!-- 机会挖掘 -->
      <el-tab-pane label="机会挖掘" name="opportunities">
        <OpportunityTab :sector-type="sectorType" />
      </el-tab-pane>

      <!-- 风险提示 -->
      <el-tab-pane label="风险提示" name="risks">
        <RiskTab :sector-type="sectorType" />
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'
import SectorLifecycleTab from './components/SectorLifecycleTab.vue'
import SectorCalendarTab from './components/SectorCalendarTab.vue'
import MainLineTab from './components/MainLineTab.vue'
import OpportunityTab from './components/OpportunityTab.vue'
import RiskTab from './components/RiskTab.vue'

const sectorType = ref('concept')
const activeTab = ref('lifecycle')

const stateStatsText = computed(() => {
  return sectorType.value === 'concept' ? '概念板块' : '行业板块'
})

const onTypeChange = () => {
  // 切换类型时刷新数据
}

const onTabChange = () => {
  // Tab切换
}
</script>

<style scoped lang="scss">
.page-container {
  padding: 16px;
}

.page-title {
  margin: 0 0 16px 0;
  font-size: 20px;
  color: var(--el-text-color-primary);
  display: flex;
  align-items: center;
  gap: 8px;
}

.category-tabs {
  display: flex;
  align-items: center;
  gap: 12px;
}

.count-badge {
  font-size: 14px;
  color: var(--el-text-color-secondary);
}

.mb-16 {
  margin-bottom: 16px;
}
</style>
