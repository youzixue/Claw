<template>
  <div class="anomaly-tab-content">
    <div class="anomaly-session-note mb-16" aria-live="polite">
      <strong>{{ sessionLabel }}<template v-if="snapshotMeta.detectionPaused === true"> · 实时买点暂停</template></strong>
      <span> · {{ observationOnly ? '仅观察 · 手动推送不可用' : '检测中 · 当前快照' }}</span>
      <span v-if="snapshotMeta.detectionPaused === true && capitalAvailable"> · 历史记录仍可查看（不代表后台采集停机）</span>
      <span v-if="snapshotMeta.cachePolicy === 'lunch_read_only'"> · 午休只读快照，保留原记录时点；当前快照事件不代表当前有效买点</span>
    </div>
    <div class="section-block sectors-metrics-panel">
      <div class="section-title">市场温度计</div>
      <div class="stats-grid">
        <div class="stat-card-v2" v-if="anomalySentiment.market_environment">
          <div class="stat-icon-wrap">
            <el-icon :size="18"><TrendCharts /></el-icon>
          </div>
          <div class="stat-content">
            <div class="stat-label">大盘环境</div>
            <div class="stat-value" :class="marketEnvClass(anomalySentiment.market_environment)">{{ marketEnvLabel(anomalySentiment.market_environment) }}</div>
          </div>
        </div>
        <div class="stat-card-v2" v-if="anomalySentiment.sentiment_cycle">
          <div class="stat-icon-wrap">
            <el-icon :size="18"><Sunny /></el-icon>
          </div>
          <div class="stat-content">
            <div class="stat-label">情绪周期</div>
            <div class="stat-value" :class="sentimentCycleClass(anomalySentiment.sentiment_cycle)">{{ sentimentCycleLabel(anomalySentiment.sentiment_cycle) }}</div>
          </div>
        </div>
        <div class="stat-card-v2">
          <div class="stat-icon-wrap">
            <el-icon :size="18"><Warning /></el-icon>
          </div>
          <div class="stat-content">
            <div class="stat-label">异动总数</div>
            <div class="stat-value">{{ marketTemp.anomaly_total }}</div>
          </div>
        </div>
        <div class="stat-card-v2 hot">
          <div class="stat-icon-wrap hot-icon">
            <el-icon :size="18"><Top /></el-icon>
          </div>
          <div class="stat-content">
            <div class="stat-label">涨停</div>
            <div class="stat-value text-up">{{ marketTemp.limit_up }}</div>
          </div>
        </div>
        <div
          class="stat-card-v2 inflow stat-card-v2--clickable"
          :class="{ 'is-active': activeAnomalyFilter === 'capital' }"
          @click="openCapitalList"
        >
          <div class="stat-icon-wrap flow-icon">
            <el-icon :size="18"><TrendCharts /></el-icon>
          </div>
          <div class="stat-content">
            <div class="stat-label">资金异动 · 当前快照</div>
            <button
              type="button"
              class="stat-value text-up capital-count-button"
              :aria-label="'查看当前资金异动清单，' + marketTemp.capital_anomaly + '只'"
              @click.stop="openCapitalList"
            >{{ marketTemp.capital_anomaly }}</button>
            <el-button size="small" type="primary" plain class="capital-list-open" @click.stop="openCapitalList">查看当前清单</el-button>
            <strong class="capital-recorded-count">本交易日已记录 {{ capitalAvailable ? capitalActivity.stock_count + '只' : '--' }}</strong>
            <div class="stat-hint">{{ capitalAvailable ? '已记录≠当前买点' : '历史不可用，可查看当前清单' }}</div>
            <el-button size="small" plain type="primary" class="capital-history-open" :disabled="!capitalAvailable || loading" :title="capitalAvailable ? '' : capitalNote" @click.stop="openCapitalHistory">查看已记录</el-button>
          </div>
        </div>
        <div
          class="stat-card-v2 buy-point stat-card-v2--clickable"
          :class="{ 'is-active': activeBuyPointOnly }"
          role="button"
          tabindex="0"
          @click="toggleBuyPointFilter"
          @keydown.enter.prevent="toggleBuyPointFilter"
          @keydown.space.prevent="toggleBuyPointFilter"
        >
          <div class="stat-icon-wrap buy-point-icon">
            <el-icon :size="18"><Aim /></el-icon>
          </div>
          <div class="stat-content">
            <div class="stat-label">到达买点</div>
            <div class="stat-value text-up">{{ marketTemp.buy_point || 0 }}</div>
            <div class="stat-hint" v-if="!marketTemp.buy_point">当前无可推买点</div>
            <div class="stat-hint" v-else>点击只看买点</div>
          </div>
        </div>
        <template v-if="secondaryCardsReady">
          <div class="stat-card-v2" v-if="anomalySentiment.seal_rate > 0">
            <div class="stat-icon-wrap">
              <el-icon :size="18"><Finished /></el-icon>
            </div>
            <div class="stat-content">
              <div class="stat-label">封板率</div>
              <div class="stat-value">{{ anomalySentiment.seal_rate.toFixed(1) }}%</div>
            </div>
          </div>
          <div class="stat-card-v2" v-if="anomalySentiment.board_height > 0">
            <div class="stat-icon-wrap">
              <el-icon :size="18"><Top /></el-icon>
            </div>
            <div class="stat-content">
              <div class="stat-label">最高连板</div>
              <div class="stat-value text-up">{{ anomalySentiment.board_height }}</div>
            </div>
          </div>
        <div
          class="stat-card-v2 rapid-rise stat-card-v2--clickable"
          :class="{ 'is-active': activeAnomalyFilter === 'rapid_rise' }"
          role="button"
          tabindex="0"
          @click="focusRapidRiseFilter"
          @keydown.enter.prevent="focusRapidRiseFilter"
          @keydown.space.prevent="focusRapidRiseFilter"
        >
          <div class="stat-icon-wrap rapid-rise-icon">
            <el-icon :size="18"><Promotion /></el-icon>
          </div>
          <div class="stat-content">
            <div class="stat-label">快速拉升</div>
            <div class="stat-value text-up">{{ marketTemp.rapid_rise }}</div>
            <div class="stat-hint" v-if="marketTemp.rapid_rise === 0">当前暂无快速拉升信号</div>
            <div class="stat-hint" v-else>点击查看快速拉升机会</div>
          </div>
        </div>
        <div class="stat-card-v2 breakthrough">
          <div class="stat-icon-wrap breakthrough-icon">
            <el-icon :size="18"><Aim /></el-icon>
          </div>
          <div class="stat-content">
            <div class="stat-label">突破信号</div>
            <div class="stat-value text-up">{{ marketTemp.breakthrough }}</div>
          </div>
        </div>
        </template>
      </div>
    </div>

    <div class="section-block">
      <div class="fund-source-legend">
        资金字段说明：当前接入
        <strong>腾讯主力资金</strong>
        <span class="legend-note">（主力 = 超大单 + 大单；来源以逐股标注为准）</span>
        <span class="legend-divider">·</span>
        事件资金与最近资金按时点区分；缺证据显示 --，不以盘口委差替代；展示不代表买点确认
      </div>
      <div class="state-filters mt-20 mb-16 anomaly-filter-row">
        <el-radio-group v-model="activeAnomalyFilterModel" size="small" class="anomaly-filter-group">
          <el-radio-button value="">全部</el-radio-button>
          <el-radio-button value="limit_up"><span class="state-pill-label">涨停</span></el-radio-button>
          <el-radio-button value="limit_down"><span class="state-pill-label">跌停</span></el-radio-button>
          <el-radio-button value="capital"><span class="state-pill-label">资金异动</span></el-radio-button>
          <el-radio-button value="low_absorb"><span class="state-pill-label">低吸买点</span></el-radio-button>
          <el-radio-button value="rapid_rise"><span class="state-pill-label">快速拉升</span></el-radio-button>
          <el-radio-button value="breakthrough"><span class="state-pill-label">突破信号</span></el-radio-button>
          <el-radio-button value="pump_dump"><span class="state-pill-label">冲高回落</span></el-radio-button>
        </el-radio-group>
        <span class="filter-divider" aria-hidden="true"></span>
        <div class="filter-inline-label">执行风格</div>
        <el-radio-group v-model="activeSetupTrackFilterModel" size="small" class="anomaly-filter-group">
          <el-radio-button value="">全部风格</el-radio-button>
          <el-radio-button value="趋势/资金型"><span class="state-pill-label">趋势/资金型</span></el-radio-button>
          <el-radio-button value="打板型"><span class="state-pill-label">打板型</span></el-radio-button>
        </el-radio-group>
        <el-checkbox v-model="activeBuyPointOnlyModel" size="small" border class="buy-point-checkbox">只看买点</el-checkbox>
        <span class="filter-divider" aria-hidden="true"></span>
        <div class="filter-inline-label">排序</div>
        <el-select v-model="activeSortKeyModel" size="small" class="anomaly-sort-select" popper-class="anomaly-sort-popper">
          <el-option v-for="option in sortOptions" :key="option.value" :label="option.label" :value="option.value" />
        </el-select>
        <div class="filter-inline-label filter-inline-label--muted">当前排序：{{ currentSortLabel }}</div>
      </div>

      <div v-if="snapshotTime" class="mb-16 anomaly-snapshot-note">
        <span>{{ snapshotMeta.cachePolicy === 'lunch_read_only' ? '午休只读快照' : snapshotMeta.cachePolicy === 'post_close_read_only' ? '盘后快照' : '异动快照' }} · {{ snapshotTime.replace('T', ' ') }}</span>
        <span v-if="snapshotMeta.tradeDate">交易日 {{ snapshotMeta.tradeDate }}</span>
        <span v-if="typeof snapshotMeta.ageSeconds === 'number'">响应时快照年龄 {{ Math.max(0, Math.floor(snapshotMeta.ageSeconds)) }} 秒</span>
        <span v-if="snapshotMeta.stale">快照已超过实时刷新窗口</span>
        <span>资金展示与买点时效分别校验；展示缓存不代表信号有效期</span>
        <span v-if="loading">刷新中</span>
      </div>
      <div v-if="loading" class="mb-16" role="status" aria-live="polite">
        正在加载异动数据{{ displayRows.length ? '，暂时保留上次结果' : '' }}…
      </div>
      <div v-if="error" class="mb-16" role="alert">
        <span>{{ error }}</span>
        <el-button size="small" :disabled="loading" @click="emit('retry')">重试</el-button>
      </div>
      <div v-if="activeAnomalyFilter === 'capital'" ref="capitalListAnchor" class="capital-filter-note mb-16" tabindex="-1">
        <h3 class="section-title capital-list-title" aria-live="polite">
          资金异动股票清单 · {{ loading ? '加载中…' : error ? '查询失败，请重试' : '共 ' + total + ' 只' }}
        </h3>
        <div v-if="!loading && !error" class="capital-list-page">本页 {{ rows.length }} 只 · 第 {{ page }} 页 · 点击股票看详情，左侧展开同股事件</div>
        <div>按股票去重，仅查询当前快照，不包含交易日历史记录；资金异动不等于买点或已成交。</div>
        <div v-if="activeSetupTrackFilter || activeBuyPointOnly">
          已叠加{{ activeSetupTrackFilter ? '「' + activeSetupTrackFilter + '」' : '' }}{{ activeBuyPointOnly ? '「只看买点」' : '' }}筛选，名单可能少于卡片数量。
          <el-button size="small" text type="primary" @click="openCapitalList">查看全部当前资金异动</el-button>
        </div>
        <el-button size="small" plain type="primary" class="capital-history-open" :disabled="!capitalAvailable || loading" :title="capitalAvailable ? '' : capitalNote" @click="openCapitalHistory">查看已记录</el-button>
      </div>
      <div class="table-container anomaly-table-wrap" :aria-busy="loading">
        <template v-if="tableReady">
          <el-table :data="displayRows" stripe size="small" :empty-text="loading ? '正在加载异动数据…' : error ? '异动数据加载失败，请重试' : activeAnomalyFilter === 'capital' ? '当前筛选下暂无资金异动股票' : '暂无异动数据'" row-key="key" @row-click="handleRowClick" class="anomaly-table">
            <el-table-column type="expand" width="44">
              <template #default="{ row }">
                <div class="anomaly-expand">
                  <div class="anomaly-expand__title">同股异动明细</div>
                  <div v-if="row.events?.length" class="anomaly-event-list">
                    <div v-for="event in row.events" :key="event.key" class="anomaly-event-item">
                      <div class="anomaly-event-item__main">
                        <div class="cell-tags">
                          <el-tag v-if="event.detail?.watchlist_member" size="small" type="warning" effect="plain">重点观察</el-tag>
                          <el-tag v-if="event.detail?.low_base_setup_confirmed" size="small" type="warning" effect="dark">形态确认</el-tag>
                          <el-tag size="small" :type="eventTypeTag(event.event_filter_type || event.event_type)" effect="light">{{ event.event_label }}</el-tag>
                          <el-tag v-if="event.event_filter_type === 'rapid_rise'" size="small" type="success" effect="plain">日内加速</el-tag>
                          <el-tag v-if="event.rolling_60s_tier" size="small" :type="event.rolling_60s_tier === 'strong' ? 'danger' : 'warning'" effect="plain">
                            60秒{{ rapidRiseTierLabel(event.rolling_60s_tier) }}
                          </el-tag>
                          <el-tag size="small" :type="setupGradeTagType(event)" effect="light">{{ compactSetupGrade(event) }}</el-tag>
                          <el-tag v-if="event.feishu_pushable" size="small" type="success" effect="plain">飞书可推</el-tag>
                          <el-tag v-if="event.buy_point_pushable" size="small" type="danger" effect="plain">买点到达</el-tag>
                        </div>
                        <div class="cell-title">{{ event.description || '暂无描述' }}</div>
                        <div class="cell-subtitle">
                          现价 ¥{{ formatPrice(event.price) }} · 涨幅 {{ formatChange(event.change_pct) }} · 事件时点资金 {{ formatNetAmount(displayFundValue(event)) }} · {{ formatTimeLabel(event.latest_as_of, snapshotTime) }}
                        </div>
                        <div class="cell-subtitle">
                          资金来源 {{ detailSourceLabel(event) || '未标明' }}<span v-if="detailAsOf(event)"> · 资金源时点 {{ detailAsOf(event) }}</span> · {{ fundStatusNote(event) }}
                        </div>
                        <div v-if="event.rolling_60s_tier" class="cell-subtitle">
                          滚动60秒 {{ formatChange(event.rolling_60s_change_pct) }} · 成交速率 {{ formatNumber(event.rolling_60s_amount_pace_ratio, 1) }}倍 · {{ event.rolling_60s_path_confirmed ? '末端保持强势' : '等待强度恢复' }}
                        </div>
                      </div>
                      <div class="anomaly-event-item__side">
                        <div class="cell-subtitle">评分 {{ Math.round(event.display_score || event.score || 0) }}</div>
                        <div class="cell-subtitle">量比 {{ formatNumber(event.volume_ratio, 1) }} · 承接 {{ formatScore(event.support_strength_score) }}</div>
                        <div class="cell-subtitle">封单 {{ formatScore(event.seal_quality_score) }} · 撤单 {{ formatRatio(event.withdrawal_ratio, 0) }}</div>
                      </div>
                    </div>
                  </div>
                  <div v-else class="cell-subtitle">暂无同股明细</div>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="股票" min-width="150" fixed>
              <template #default="{ row }">
                <div class="stock-cell">
                  <div class="stock-name">
                    {{ row.name }}
                    <el-tag v-if="row.detail?.watchlist_member" size="small" type="warning" effect="plain">核心成长</el-tag>
                    <el-tag v-if="row.detail?.low_base_setup_confirmed" size="small" type="warning" effect="dark">成长启动</el-tag>
                  </div>
                  <div class="stock-meta">{{ row.code }} · {{ formatTimeLabel(row.latest_as_of, snapshotTime) }}</div>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="机会" min-width="220">
              <template #default="{ row }">
                <div class="opportunity-cell">
                  <div class="cell-tags">
                    <el-tag
                      v-for="item in row.event_badges"
                      :key="`${row.key}-${item.value}`"
                      size="small"
                      :type="eventTypeTag(item.value)"
                      effect="light"
                    >
                      {{ item.label }}
                    </el-tag>
                    <el-tag v-if="row.event_count > 1" size="small" type="info" effect="plain">共{{ row.event_count }}项</el-tag>
                  </div>
                  <div class="cell-title">{{ row.primary_reason }}</div>
                  <div class="cell-subtitle">{{ row.secondary_reason }}</div>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="执行" width="190">
              <template #default="{ row }">
                <el-tooltip placement="top" effect="light" :width="360">
                  <template #content>
                    <div class="anomaly-tooltip">
                      <div class="tooltip-title">执行分级</div>
                      <div class="tooltip-line"><strong>当前等级：</strong>{{ fullSetupGrade(row) }}</div>
                      <div class="tooltip-line" v-if="setupTrack(row)">
                        <strong>执行风格：</strong>{{ setupTrack(row) }}
                      </div>
                      <div class="tooltip-line">
                        <strong>买点推送：</strong>{{ hasBuyPoint(row) ? '已到达买点' : '仅页面观察' }}
                      </div>
                      <div class="tooltip-line" v-if="hasBuyPoint(row)">
                        <strong>买点类型：</strong>{{ buyPointSummary(row) }}
                      </div>
                      <div
                        v-for="(reason, idx) in buyPointReasons(row)"
                        :key="`${row.key}-buy-point-reason-${idx}`"
                        class="tooltip-line tooltip-indent"
                      >
                        - {{ reason }}
                      </div>
                      <div class="tooltip-line" v-if="buyPointBlockers(row).length">
                        <strong>买点阻断：</strong>
                      </div>
                      <div
                        v-for="(reason, idx) in buyPointBlockers(row)"
                        :key="`${row.key}-buy-point-blocker-${idx}`"
                        class="tooltip-line tooltip-indent"
                      >
                        - {{ reason }}
                      </div>
                      <div class="tooltip-line" v-if="row?.b1_signal_status_label">
                        <strong>B1状态：</strong>{{ row.b1_signal_status_label }}
                        <span v-if="row?.b1_signal"> · {{ row.b1_signal }}</span>
                        <span v-if="row?.b1_pushable"> · 可进入状态机推送</span>
                        <span v-else> · 仅列表跟踪</span>
                      </div>
                      <div class="tooltip-line" v-if="setupBlockers(row).length">
                        <strong>未进 A1 原因：</strong>
                      </div>
                      <div
                        v-for="(reason, idx) in setupBlockers(row)"
                        :key="`${row.key}-blocker-${idx}`"
                        class="tooltip-line tooltip-indent"
                      >
                        - {{ reason }}
                      </div>
                    </div>
                  </template>
                  <div class="execution-cell">
                    <div class="execution-tags">
                      <el-tag size="small" :type="setupGradeTagType(row)" effect="light">
                        {{ compactSetupGrade(row) }}
                      </el-tag>
                      <el-tag
                        v-if="row?.b1_signal_status_label"
                        size="small"
                        :type="row?.b1_signal_status === 'close_confirmed' ? 'success' : 'warning'"
                        effect="plain"
                      >
                        {{ row.b1_signal_status_label }}
                      </el-tag>
                      <el-tag
                        v-for="(flag, idx) in riskFlags(row)"
                        :key="`${row.key}-risk-flag-${idx}`"
                        size="small"
                        :type="riskFlagTagType(flag)"
                        effect="plain"
                      >
                        {{ flag.label }}
                      </el-tag>
                      <el-tag v-if="hasBuyPoint(row)" size="small" type="danger" effect="plain">
                        买点已到
                      </el-tag>
                    </div>
                    <div class="cell-subtitle">{{ executionSummary(row) }}</div>
                  </div>
                </el-tooltip>
              </template>
            </el-table-column>
            <el-table-column label="行情快照" width="190" align="left">
              <template #default="{ row }">
                <div class="snapshot-cell">
                  <div class="snapshot-price-line">
                    <span class="snapshot-price">¥{{ formatPrice(row.detail?.price) }}</span>
                    <span class="snapshot-change" :class="changeColorClass(row.detail?.change_pct)">
                      {{ formatChange(row.detail?.change_pct) }}
                    </span>
                  </div>
                  <div class="cell-subtitle">
                    振幅 {{ formatPctAbs(row.detail?.amplitude) }} · 换手 {{ formatPctAbs(row.detail?.turnover) }}
                  </div>
                  <div class="cell-subtitle">
                    量比 {{ formatNumber(row.detail?.volume_ratio, 1) }} · 评分 {{ Math.round(row.display_score) }}
                  </div>
                </div>
              </template>
            </el-table-column>
            <el-table-column width="220">
              <template #header>
                <div class="column-header-with-note">
                  <span>资金 / 盘口</span>
                  <span class="column-header-note">主力资金 · 盘口独立</span>
                </div>
              </template>
              <template #default="{ row }">
                <el-tooltip placement="top" effect="light" :width="340">
                  <template #content>
                    <div class="anomaly-tooltip">
                      <div class="tooltip-title">资金与盘口明细</div>
                      <div class="tooltip-line"><strong>确认说明：</strong>{{ row.primary_reason || '暂无说明' }}</div>
                      <div class="tooltip-line" v-if="detailSourceLabel(row) || detailAsOf(row)">
                        <strong>资金来源：</strong>{{ detailSourceLabel(row) || '未知' }}<span v-if="detailAsOf(row)"> · {{ detailAsOf(row) }}</span>
                      </div>
                      <div class="tooltip-line"><strong>资金状态：</strong>{{ fundStatusNote(row) }}</div>
                      <div class="tooltip-line" v-if="detailSourceLabel(row)">
                        <strong>口径：</strong>{{ detailSourceMethod(row) }}
                      </div>
                      <div class="tooltip-line" v-if="detailSourceLabel(row) && fundDetail(row).source_version">
                        <strong>来源版本：</strong>{{ fundDetail(row).source_version }}
                      </div>
                      <div class="tooltip-line" v-if="detailSourceLabel(row) && fundDetail(row).received_at">
                        <strong>资金接收：</strong>{{ fundDetail(row).received_at }}
                      </div>
                      <div class="tooltip-line" v-if="detailSourceLabel(row) && fundDetail(row).observed_at">
                        <strong>资金观测：</strong>{{ fundDetail(row).observed_at }}
                      </div>
                      <div class="tooltip-line" v-if="hasEventType(row, 'limit_up') && displayFundValue(row) < 0">
                        <strong>资金说明：</strong>当前为涨停状态，但资金净额为负，更偏高位换手/资金分歧，并不等于封板转弱。
                      </div>
                      <div class="tooltip-line" v-if="hasOrderbook(row)">
                        <strong>买一/卖一：</strong>{{ formatPrice(row?.detail?.bid1_price) }} / {{ formatPrice(row?.detail?.ask1_price) }}
                        <span class="tooltip-muted">（{{ formatLot(row?.detail?.bid1_volume) }} / {{ formatLot(row?.detail?.ask1_volume) }}）</span>
                      </div>
                      <div class="tooltip-line" v-if="hasOrderbook(row)">
                        <strong>买/卖五档合计：</strong>{{ formatLot(row?.detail?.bid_depth_5) }} / {{ formatLot(row?.detail?.ask_depth_5) }}
                      </div>
                      <div class="tooltip-line" v-if="hasOrderbook(row)">
                        <strong>盘口失衡：</strong>{{ formatRatio(row?.detail?.orderbook_imbalance, 1) }}
                      </div>
                      <div class="tooltip-line" v-if="hasOrderbook(row) && supportStrength(row) !== null">
                        <strong>承接强度：</strong>{{ Math.round(supportStrength(row)) }}
                      </div>
                      <div class="tooltip-line" v-if="hasOrderbook(row) && sealQuality(row) !== null">
                        <strong>封单质量：</strong>{{ Math.round(sealQuality(row)) }}
                      </div>
                      <div class="tooltip-line" v-if="hasOrderbook(row) && withdrawalRatio(row) !== null">
                        <strong>买盘撤单：</strong>{{ formatRatio(withdrawalRatio(row), 0) }}
                      </div>
                      <div class="tooltip-line" v-if="!hasOrderbook(row)">当前未提供有效五档盘口快照，不能据此判断承接或封单强弱。</div>
                      <div class="tooltip-line"><strong>盘口状态：</strong>{{ orderbookBadgeLabel(row) }}<span v-if="row.orderbook_display?.source_quote_at"> · {{ row.orderbook_display.source_quote_at }}</span></div>
                      <div class="tooltip-muted">承接、封单均为0–100分评分；挂单量单位为手，不等于主力资金。盘口标签不代表买点确认或已推送。</div>
                    </div>
                  </template>
                  <div class="capital-cell">
                    <div class="snapshot-price-line">
                      <span
                        v-if="!hasEventType(row, 'limit_up')"
                        class="capital-value"
                        :class="capitalValueClass(displayFundValue(row))"
                      >
                        {{ formatNetAmount(displayFundValue(row)) }}
                      </span>
                      <el-tag size="small" effect="plain" :type="capitalPrimaryTagType(row)" class="orderbook-chip">
                        {{ capitalPrimaryLabel(row) }}
                      </el-tag>
                      <el-tag size="small" effect="plain" :type="orderbookTagType(row)" class="orderbook-chip">
                        {{ orderbookBadgeLabel(row) }}
                      </el-tag>
                    </div>
                    <div class="cell-subtitle capital-source-line" v-if="detailSourceLabel(row)">
                      {{ detailSourceLabel(row) }}
                    </div>
                    <div class="cell-subtitle capital-timing-line" v-if="fundDisplayTiming(row)">
                      {{ fundDisplayTiming(row) }}
                    </div>
                    <div class="cell-subtitle">
                      {{ capitalSecondarySummary(row) }}
                    </div>
                    <div class="cell-subtitle">
                      封单评分 {{ hasOrderbook(row) ? formatScore(sealQuality(row)) : '--' }} · 撤单 {{ hasOrderbook(row) ? formatRatio(withdrawalRatio(row), 0) : '--' }}
                    </div>
                  </div>
                </el-tooltip>
              </template>
            </el-table-column>
            <el-table-column label="主驱动板块" min-width="240">
              <template #default="{ row }">
                <el-tooltip placement="top" effect="light" :width="360" v-if="row.driver_detail_lines.length || row.reference_driver_lines.length">
                  <template #content>
                    <div class="anomaly-tooltip">
                      <div class="tooltip-title">主驱动板块</div>
                      <div class="tooltip-line" v-for="(line, idx) in row.driver_detail_lines" :key="`${row.key}-driver-${idx}`">
                        {{ line }}
                      </div>
                      <div class="tooltip-line" v-if="row.reference_driver_lines.length">
                        <strong>参考板块：</strong>
                      </div>
                      <div class="tooltip-line tooltip-indent" v-for="(line, idx) in row.reference_driver_lines" :key="`${row.key}-ref-${idx}`">
                        - {{ line }}
                      </div>
                    </div>
                  </template>
                  <div class="driver-cell">
                    <div class="cell-title">{{ row.driver_primary }}</div>
                    <div class="cell-subtitle">{{ row.driver_secondary }}</div>
                    <div class="cell-subtitle" v-if="row.driver_peer_summary">{{ row.driver_peer_summary }}</div>
                  </div>
                </el-tooltip>
                <div v-else class="driver-cell">
                  <div class="cell-title">{{ row.driver_primary }}</div>
                  <div class="cell-subtitle">{{ row.driver_secondary }}</div>
                </div>
              </template>
            </el-table-column>
          </el-table>
        </template>
        <div v-else class="anomaly-table-skeleton">
          <div class="anomaly-table-skeleton__row" v-for="idx in 6" :key="idx">
            <div class="skeleton-block skeleton-block--stock"></div>
            <div class="skeleton-block skeleton-block--opportunity"></div>
            <div class="skeleton-block skeleton-block--execution"></div>
            <div class="skeleton-block skeleton-block--snapshot"></div>
            <div class="skeleton-block skeleton-block--capital"></div>
            <div class="skeleton-block skeleton-block--driver"></div>
          </div>
        </div>
      </div>
      <div class="table-footer">
        <div class="table-footer__meta">共 {{ capitalListPending ? '--' : total }} 只股票 · 当前排序：{{ currentSortLabel }}</div>
        <div class="table-footer__actions">
          <el-button
            size="small"
            type="danger"
            plain
            :loading="pushBuyPointLoading"
            :disabled="pushBuyPointLoading || observationOnly"
            @click.stop="handlePushBuyPoints"
          >
            <el-icon><Promotion /></el-icon> 推送买点
          </el-button>
          <el-dropdown trigger="click" @command="handleExport" size="small">
            <el-button size="small" type="primary" plain :disabled="!displayRows.length">
              <el-icon><Download /></el-icon> 导出
            </el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="xlsx">导出 Excel</el-dropdown-item>
                <el-dropdown-item command="csv">导出 CSV</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
        <el-pagination
          v-model:current-page="pageModel"
          background
          layout="prev, pager, next"
          :total="total"
          :page-size="pageSize"
          :disabled="capitalListPending"
        />
      </div>
    </div>
    <el-dialog v-model="capitalHistoryOpen" title="本交易日已记录 · 资金候选历史" top="5vh" width="min(920px, 96vw)" class="capital-history-dialog">
      <p>只读历史：已落库候选生命周期按股票去重，非买点、非成交，记录数不是股票数；不承诺无漏记。不受页面事件筛选或最小分数影响。</p>
      <p>{{ capitalNote }}</p>
      <template v-if="capitalAvailable">
        <p>交易日 {{ capitalActivity.trade_date }} · 最近记录 {{ historyTime(capitalActivity.last_seen_at) }} · 查询时点 {{ historyTime(capitalActivity.as_of_at) }}</p>
        <p>股票 {{ capitalActivity.stock_count }} 只 · 身份记录 {{ capitalActivity.record_count }} 条</p>
        <el-table :data="capitalHistoryPageRows" max-height="45vh" size="small" stripe row-key="code" empty-text="本交易日尚无已记录资金候选">
          <el-table-column label="股票" min-width="150">
            <template #default="{ row }"><router-link :to="'/stocks/' + row.code">{{ row.name }} {{ row.code }}</router-link></template>
          </el-table-column>
          <el-table-column label="首次记录" min-width="175"><template #default="{ row }">{{ historyTime(row.first_seen_at) }}</template></el-table-column>
          <el-table-column label="末次记录" min-width="175"><template #default="{ row }">{{ historyTime(row.last_seen_at) }}</template></el-table-column>
          <el-table-column prop="record_count" label="身份记录数" width="110" />
        </el-table>
        <el-pagination v-model:current-page="capitalHistoryPage" :page-size="20" :total="capitalHistoryStocks.length" layout="prev, pager, next" small />
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { Aim, Warning, Top, TrendCharts, Sunny, Finished, Download, Promotion } from '@element-plus/icons-vue'
import { formatChange, changeColorClass, sentimentCycleLabel } from '@/composables/useUtils'

const props = defineProps({
  capitalActivity: { type: Object, default: null },
  observationOnly: { type: Boolean, default: true },
  marketTemp: { type: Object, default: () => ({}) },
  anomalySentiment: { type: Object, default: () => ({}) },
  activeAnomalyFilter: { type: String, default: '' },
  activeSetupTrackFilter: { type: String, default: '' },
  activeBuyPointOnly: { type: Boolean, default: false },
  activeSortKey: { type: String, default: 'priority' },
  sortOptions: { type: Array, default: () => [] },
  currentSortLabel: { type: String, default: '综合优先级' },
  rows: { type: Array, default: () => [] },
  loading: { type: Boolean, default: false },
  error: { type: String, default: '' },
  snapshotTime: { type: String, default: '' },
  snapshotMeta: { type: Object, default: () => ({}) },
  total: { type: Number, default: 0 },
  page: { type: Number, default: 1 },
  pageSize: { type: Number, default: 50 },
  pushBuyPointLoading: { type: Boolean, default: false },
})

const emit = defineEmits([
  'update:activeAnomalyFilter',
  'update:activeSetupTrackFilter',
  'update:activeBuyPointOnly',
  'update:activeSortKey',
  'update:page',
  'row-click',
  'export',
  'push-buy-points',
  'retry',
])

const capitalListAnchor = ref(null)
// 切换筛选及失败时，不将上一次其他事件的名单冒充资金清单。
const capitalListPending = computed(() => props.activeAnomalyFilter === 'capital' && (props.loading || Boolean(props.error)))
const displayRows = computed(() => capitalListPending.value ? [] : props.rows)
const capitalHistoryOpen = ref(false)
const capitalHistoryPage = ref(1)
const capitalAvailable = computed(() => !props.error
  && ['ok', 'empty'].includes(props.capitalActivity?.status)
  && props.capitalActivity?.basis === 'recorded_candidates_distinct_stock'
  && Number.isInteger(props.capitalActivity?.stock_count) && props.capitalActivity.stock_count >= 0
  && Array.isArray(props.capitalActivity?.stocks))
const capitalNote = computed(() => props.error ? '本次查询失败，历史记录不可用，请重试'
  : !capitalAvailable.value ? (props.capitalActivity?.note || '历史台账不可用（后端未提供或尚未就绪），不以当前快照推算')
    : (props.capitalActivity.note || '已落库候选历史，仅供观察'))
const sessionLabel = computed(() => ({
  morning: '上午交易时段', afternoon: '下午交易时段', lunch_break: '午休',
  closed: '休市', weekend: '周末休市', holiday: '节假日休市',
  after_hours: '盘后', pre_auction: '集合竞价', night_session: '夜间观察',
}[props.snapshotMeta.marketSession] || '时段未确认'))
const historyTime = value => typeof value === 'string' && value ? value.replace('T', ' ') : '--'
const capitalHistoryStocks = computed(() => capitalAvailable.value
  ? [...props.capitalActivity.stocks].sort((a, b) => String(b.last_seen_at || '').localeCompare(String(a.last_seen_at || '')))
  : [])
const capitalHistoryPageRows = computed(() => capitalHistoryStocks.value.slice((capitalHistoryPage.value - 1) * 20, capitalHistoryPage.value * 20))
function openCapitalHistory() {
  if (!capitalAvailable.value || props.loading) return
  capitalHistoryPage.value = 1
  capitalHistoryOpen.value = true
}
watch(() => props.capitalActivity, () => { capitalHistoryPage.value = 1 })

const marketEnvLabel = (env) => ({ strong: '强势', neutral: '震荡', weak: '弱势' }[env] || env || '--')
const marketEnvClass = (env) => ({ strong: 'text-up', neutral: '', weak: 'text-down' }[env] || '')
const sentimentCycleClass = (cycle) => ({ climax: 'text-up', recovery: 'text-up', divergence: '', freezing: 'text-down', pending: '' }[cycle] || '')
const formatPrice = (value) => (value ? Number(value).toFixed(2) : '-')
const formatNumber = (value, digits = 1) => {
  const num = Number(value || 0)
  if (!num) return '--'
  return num.toFixed(digits)
}
// 展示层保留真实零，拒绝 null/空串/布尔/非有限值；不参与交易确认。
const displayNumber = (value) => {
  if (typeof value !== 'number' && typeof value !== 'string') return null
  if (typeof value === 'string' && !value.trim()) return null
  const num = Number(value)
  return Number.isFinite(num) ? num : null
}
const formatLot = (value) => {
  const num = displayNumber(value)
  if (num === null || num < 0) return '--'
  if (num >= 10000) return `${(num / 10000).toFixed(1)}万手`
  return `${num.toFixed(0)}手`
}
const formatPct = (value, digits = 1) => {
  const num = displayNumber(value)
  if (num === null) return '--'
  return `${num > 0 ? '+' : ''}${num.toFixed(digits)}%`
}
const formatRatio = (value, digits = 1) => {
  const num = displayNumber(value)
  return num === null ? '--' : formatPct(num * 100, digits)
}
const formatScore = (value) => {
  const num = displayNumber(value)
  return num === null ? '--' : Math.round(num)
}
const formatPctAbs = (value, digits = 2) => {
  const num = Number(value || 0)
  if (!num) return '--'
  return `${num.toFixed(digits)}%`
}
const formatNetAmount = (value) => {
  const num = displayNumber(value)
  if (num === null) return '--'
  return `${num > 0 ? '+' : ''}${(num / 1e8).toFixed(2)}亿`
}
const hasEventType = (row, eventType) => Array.isArray(row?.event_types) && row.event_types.includes(eventType)
const formatTimeLabel = (value, fallback = '') => {
  const raw = String(value || fallback || '')
  if (!raw) return '时点 --'
  const date = new Date(raw)
  if (Number.isNaN(date.getTime())) return raw
  const hh = `${date.getHours()}`.padStart(2, '0')
  const mm = `${date.getMinutes()}`.padStart(2, '0')
  const ss = `${date.getSeconds()}`.padStart(2, '0')
  return `时点 ${hh}:${mm}:${ss}`
}
const capitalValueClass = (value) => {
  const num = Number(value || 0)
  if (num > 0) return 'text-up'
  if (num < 0) return 'text-down'
  return 'text-flat'
}
// Display projection is separate from detail's current execution evidence.
// An explicit unavailable projection must not fall back to an old numeric field.
const fundDetail = (row) => row?.fund_display ?? row?.detail ?? row ?? {}
const detailSourceLabel = (row) => {
  const detail = fundDetail(row)
  // fund_flow 是表级别名，不能据此推断为同花顺；以行级供应商为准。
  const provider = detail.provider_source || ''
  if (provider === 'tencent' || (!provider && detail.source_version === 'tencent_hsfundtab_v1')) return '腾讯主力资金'
  if (['eastmoney', 'eastmoney_via_akshare'].includes(provider)) return '东方财富主力资金'
  if (provider === 'ths_via_akshare') return '同花顺资金净额（非主力口径）'
  if (!provider && String(detail.source || '').startsWith('eastmoney_main_fund') && !String(detail.source).includes('unavailable')) return '东方财富主力资金'
  return provider ? '未识别资金来源' : ''
}
const fundStatus = (row) => {
  const detail = fundDetail(row)
  const source = String(detail.source || '')
  if (row?.fund_display) {
    if (source === 'stock_spot' || source.includes('unavailable')) return '资金缺失'
    if (detail.schema !== 'anomaly_fund_display_v1' || detail.purpose !== 'display_only') return '资金待核'
    if (['future', 'invalid'].includes(detail.clock_status)) return '时钟异常'
    if (detail.clock_status === 'read_error') return '资金读取异常'
    if (detail.clock_status === 'unsupported_source') return '资金待核'
    if (!detail.available) return '资金缺失'
    const supported = {
      tencent: 'tencent_hsfundtab_v1', eastmoney: 'individual_fund_flow_v3_f124',
      eastmoney_via_akshare: 'individual_fund_flow_eastmoney_akshare_v1',
    }
    if (supported[detail.provider_source] !== detail.source_version || !detail.source_version) return '资金待核'
    if (!['event_snapshot', 'latest_snapshot'].includes(detail.basis)
      || !detail.source_quote_at || !detail.received_at || !detail.observed_at
      || (detail.basis === 'event_snapshot' && !detail.event_at)) return '资金待核'
    if (displayNumber(detail.main_net_inflow) === null || displayNumber(detail.main_net_inflow_pct) === null) return '资金缺失'
    return detail.clock_status === 'ok' ? 'ok' : detail.clock_status === 'stale' ? 'snapshot' : '资金待核'
  }
  if (!source || source === 'stock_spot' || source.includes('unavailable')) return '资金缺失'
  if (detail.clock_status === 'stale' || source.endsWith('_stale')) return '资金过期'
  if (['future', 'invalid'].includes(detail.clock_status)) return '时钟异常'
  if (detail.is_stale === true || detail.clock_status !== 'ok') return '资金待核'
  if (!detailSourceLabel(row) || detailSourceLabel(row).includes('未识别') || detail.provider_source === 'ths_via_akshare') return '资金待核'
  if (displayNumber(detail.main_net_inflow) === null || displayNumber(detail.main_net_inflow_pct) === null) return '资金缺失'
  return 'ok'
}
const displayFundValue = (row, key = 'main_net_inflow') => (
  ['ok', 'snapshot'].includes(fundStatus(row)) ? displayNumber(fundDetail(row)[key]) : null
)
const capitalPrimaryLabel = (row) => {
  const status = fundStatus(row)
  if (!['ok', 'snapshot'].includes(status)) return status
  const net = displayFundValue(row)
  if (hasEventType(row, 'limit_up') && net < 0) return '资金分歧'
  if (net > 0) return '资金流入'
  if (net < 0) return '资金流出'
  return '资金持平'
}
const capitalPrimaryTagType = (row) => {
  if (!['ok', 'snapshot'].includes(fundStatus(row))) return 'info'
  const net = displayFundValue(row)
  if (hasEventType(row, 'limit_up') && net < 0) return 'warning'
  if (net > 0) return 'danger'
  if (net < 0) return 'success'
  return 'info'
}
const supportStrength = (row) => displayNumber(row?.detail?.support_strength_score)
const sealQuality = (row) => displayNumber(row?.detail?.seal_quality_score)
const withdrawalRatio = (row) => displayNumber(row?.detail?.withdrawal_ratio)
const bookStatus = (row) => row?.orderbook_display?.clock_status || 'unknown'
const hasOrderbook = (row) => !['invalid', 'future'].includes(bookStatus(row))
  && (displayNumber(row?.detail?.bid_depth_5) > 0 || displayNumber(row?.detail?.ask_depth_5) > 0)
const capitalSecondarySummary = (row) => {
  const pct = formatPct(displayFundValue(row, 'main_net_inflow_pct'), 1)
  const net = displayFundValue(row)
  if (hasEventType(row, 'limit_up') && net !== null) return `净额 ${formatNetAmount(net)} · 占比 ${pct}`
  return `占比 ${pct} · 承接评分 ${hasOrderbook(row) ? formatScore(supportStrength(row)) : '--'}`
}
const detailSourceMethod = (row) => {
  const label = detailSourceLabel(row)
  if (label === '腾讯主力资金') return '主力 = 超大单 + 大单；占比按腾讯四类双向资金总额的一半计算'
  if (label === '东方财富主力资金') return '主力 = 超大单 + 大单；占比采用供应商原值'
  return '来源或主力口径未验证，不作为当前主力资金'
}
const detailAsOf = (row) => detailSourceLabel(row) ? (fundDetail(row).source_quote_at || fundDetail(row).as_of || '') : ''
const fundStatusNote = (row) => ({
  ok: '资金为标注时点的测量值；展示不代表买点确认或已经推送',
  snapshot: '保留已验证的该时点资金快照；不是当前交易资金，也不保证为收盘最终值',
  资金缺失: '没有可展示的合格资金证据，不能视为零流入',
  资金读取异常: '本次资金读取失败；没有用零或其他股票资金替代',
  资金过期: '旧资金快照仅供溯源，不作为当前资金确认',
  时钟异常: '资金时钟异常，不能作为当前资金确认',
  资金待核: '缺少有效来源或时钟证明，不作为当前主力资金',
}[fundStatus(row)])
const fundDisplayTiming = (row) => {
  const detail = fundDetail(row)
  if (!detail.source_quote_at) return ''
  const label = detail.basis === 'event_snapshot' ? '事件资金' : detail.basis === 'latest_snapshot' ? '最近资金' : '资金时点'
  return `${label} · ${String(detail.source_quote_at).replace('T', ' ')}`
}
const compactSetupGrade = (row) => ({
  'A1-趋势/资金型': 'A1趋势',
  'A1-打板型': 'A1打板',
  'A1 可直接执行': 'A1可执行',
  'A2 盘口确认后执行': 'A2待确认',
  'B类观察候选': 'B观察',
}[row?.setup_grade_display || row?.setup_grade || ''] || '未评级')
const fullSetupGrade = (row) => row?.setup_grade_display || row?.setup_grade || '未评级'
const setupTrack = (row) => row?.setup_track || ''
const hasBuyPoint = (row) => Boolean(row?.buy_point_reached || row?.buy_point_pushable)
const buyPointSummary = (row) => row?.buy_point_type || row?.buy_point_label || '买点确认'
const buyPointReasons = (row) => Array.isArray(row?.buy_point_reasons) ? row.buy_point_reasons : []
const buyPointBlockers = (row) => Array.isArray(row?.buy_point_blockers) ? row.buy_point_blockers : []
const setupGradeTagType = (row) => {
  const grade = row?.setup_grade_display || row?.setup_grade || ''
  if (grade === 'A1-打板型') return 'danger'
  if (grade.includes('A1')) return 'success'
  if (grade.includes('A2')) return 'warning'
  if (grade.includes('B')) return 'info'
  return 'info'
}
const setupBlockers = (row) => Array.isArray(row?.a1_blockers) ? row.a1_blockers : []
const riskFlags = (row) => Array.isArray(row?.risk_flags) ? row.risk_flags : []
const primaryRiskFlag = (row) => riskFlags(row)[0] || null
const riskFlagTagType = (flag) => (flag?.level === 'danger' ? 'danger' : 'warning')
const executionSummary = (row) => {
  if (hasBuyPoint(row)) return buyPointSummary(row)
  if (primaryRiskFlag(row)?.label) return primaryRiskFlag(row).label
  if (setupBlockers(row).length) return setupBlockers(row)[0]
  if (row?.feishu_pushable) return '已纳入飞书可执行流'
  return row?.secondary_reason || '等待更多确认'
}
const eventTypeTag = (t) => ({
  limit_up: 'danger',
  limit_down: 'success',
  capital: 'warning',
  low_absorb: 'success',
  rapid_rise: 'success',
  breakthrough: 'primary',
  pump_dump: 'info',
}[t] || 'info')
const rapidRiseTierLabel = (tier) => ({
  watch: '启动',
  medium: '急拉',
  strong: '强急拉',
}[tier] || '加速')
const orderbookBadgeLabel = (row) => {
  if (['future', 'invalid'].includes(bookStatus(row))) return '盘口时钟异常'
  if (!hasOrderbook(row)) return '盘口缺失'
  if (bookStatus(row) !== 'ok') return ({ historical: '盘口快照', stale: '盘口过期', single_sided: '单边盘口' }[bookStatus(row)] || '盘口待核')
  const eventType = row?.event_type
  if (eventType === 'limit_up') return sealQuality(row) >= 70 ? '封单强' : '封单看'
  if (eventType === 'pump_dump') return withdrawalRatio(row) >= 0.35 ? '撤单险' : '冲回落'
  if (eventType === 'capital') {
    if (withdrawalRatio(row) >= 0.35) return '撤单险'
    if (supportStrength(row) >= 65) return '承接强'
    if (supportStrength(row) > 0) return '盘口看'
    return '资金看'
  }
  // 仅表达盘口承接，不能把展示评分命名为买点或推送确认。
  if (eventType === 'low_absorb') return supportStrength(row) >= 65 ? '承接偏强' : '盘口观察'
  if (eventType === 'breakthrough') return supportStrength(row) >= 60 ? '承接偏强' : '盘口观察'
  return hasOrderbook(row) ? '盘口看' : '待确认'
}
const orderbookTagType = (row) => {
  const label = orderbookBadgeLabel(row)
  if (['封单强', '承接强', '承接偏强'].includes(label)) return 'success'
  if (['撤单险', '冲回落'].includes(label)) return 'danger'
  return 'info'
}

const activeAnomalyFilterModel = computed({
  get: () => props.activeAnomalyFilter,
  set: value => emit('update:activeAnomalyFilter', value),
})
const activeSetupTrackFilterModel = computed({
  get: () => props.activeSetupTrackFilter,
  set: value => emit('update:activeSetupTrackFilter', value),
})
const activeBuyPointOnlyModel = computed({
  get: () => props.activeBuyPointOnly,
  set: value => emit('update:activeBuyPointOnly', value),
})
const activeSortKeyModel = computed({
  get: () => props.activeSortKey,
  set: value => emit('update:activeSortKey', value),
})
const pageModel = computed({
  get: () => props.page,
  set: value => emit('update:page', value),
})

const tableReady = ref(false)
const secondaryCardsReady = ref(false)
let tableReadyHandle = null
let secondaryCardsHandle = null

async function openCapitalList() {
  // 卡片为全局资金股数；清除附加过滤后复用现有分页查询，保留用户排序。
  const sameQuery = props.activeAnomalyFilter === 'capital'
    && !props.activeSetupTrackFilter && !props.activeBuyPointOnly && props.page === 1
  activeAnomalyFilterModel.value = 'capital'
  activeSetupTrackFilterModel.value = ''
  activeBuyPointOnlyModel.value = false
  pageModel.value = 1
  if (sameQuery && props.error && !props.loading) emit('retry')
  await nextTick()
  capitalListAnchor.value?.focus({ preventScroll: true })
  capitalListAnchor.value?.scrollIntoView({ block: 'start' })
}

function focusRapidRiseFilter() {
  activeAnomalyFilterModel.value = 'rapid_rise'
}

function toggleBuyPointFilter() {
  activeBuyPointOnlyModel.value = !activeBuyPointOnlyModel.value
}

function handleRowClick(row) {
  emit('row-click', row)
}

function handleExport(command) {
  emit('export', command)
}

function handlePushBuyPoints() {
  if (props.observationOnly || props.pushBuyPointLoading) return
  emit('push-buy-points')
}

onMounted(() => {
  if (typeof window !== 'undefined' && typeof window.requestIdleCallback === 'function') {
    secondaryCardsHandle = window.requestIdleCallback(() => {
      secondaryCardsReady.value = true
      secondaryCardsHandle = null
    }, { timeout: 180 })
  } else {
    secondaryCardsHandle = window.setTimeout(() => {
      secondaryCardsReady.value = true
      secondaryCardsHandle = null
    }, 32)
  }
  if (typeof window !== 'undefined' && typeof window.requestIdleCallback === 'function') {
    tableReadyHandle = window.requestIdleCallback(() => {
      tableReady.value = true
      tableReadyHandle = null
    }, { timeout: 240 })
  } else {
    tableReadyHandle = window.setTimeout(() => {
      tableReady.value = true
      tableReadyHandle = null
    }, 48)
  }
})

onUnmounted(() => {
  if (secondaryCardsHandle) {
    if (typeof window.cancelIdleCallback === 'function') {
      window.cancelIdleCallback(secondaryCardsHandle)
    } else {
      window.clearTimeout(secondaryCardsHandle)
    }
    secondaryCardsHandle = null
  }
  if (!tableReadyHandle) return
  if (typeof window.cancelIdleCallback === 'function') {
    window.cancelIdleCallback(tableReadyHandle)
  } else {
    window.clearTimeout(tableReadyHandle)
  }
  tableReadyHandle = null
})
</script>

<style scoped lang="scss">
.capital-history-open:not(.is-disabled) { color: var(--el-color-primary); background: transparent; border: 1px solid currentColor; }
.capital-count-button { display: block; padding: 0; border: 0; background: transparent; font-family: inherit; text-align: left; cursor: pointer; }
.capital-count-button:focus-visible { outline: 2px solid var(--el-color-primary); outline-offset: 3px; }
.capital-list-open { margin-top: 6px; color: var(--el-color-primary); background: transparent; border: 1px solid currentColor; }
.capital-filter-note { scroll-margin-top: 80px; }
.capital-filter-note:focus { outline: none; }
.capital-list-title { margin: 0; }
.capital-list-page { color: var(--claw-text-secondary); }
.capital-recorded-count { display: block; color: var(--el-color-primary); margin-top: 8px; }
.anomaly-session-note, .capital-filter-note { line-height: 1.7; overflow-wrap: anywhere; }
.capital-history-dialog p { line-height: 1.7; overflow-wrap: anywhere; }

.anomaly-tab-content {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-4);
}

.section-block {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-3);
}

.section-title {
  font-size: 1rem;
  font-weight: 600;
  color: var(--claw-text-primary);
}

.mb-16 { margin-bottom: var(--spacing-4); }

.table-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--spacing-3);
  margin-top: var(--spacing-3);

  @media (max-width: 767px) {
    flex-direction: column;
    align-items: flex-start;
  }
}

.table-footer__meta {
  font-size: 0.875rem;
  color: var(--claw-text-muted);
}

.table-footer__actions {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 8px;
}

.anomaly-expand {
  padding: var(--spacing-3) var(--spacing-4);
  background: linear-gradient(180deg, rgba(64, 158, 255, 0.04), rgba(255, 255, 255, 0.92));
}

.anomaly-expand__title {
  font-size: 0.875rem;
  font-weight: 600;
  color: var(--claw-text-primary);
  margin-bottom: var(--spacing-2);
}

.anomaly-event-list {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-2);
}

.anomaly-event-item {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--spacing-3);
  padding: var(--spacing-3);
  border-radius: var(--radius-md);
  background: rgba(255, 255, 255, 0.9);
  border: 1px solid rgba(15, 23, 42, 0.06);

  @media (max-width: 767px) {
    flex-direction: column;
  }
}

.anomaly-event-item__main,
.anomaly-event-item__side {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}

.anomaly-event-item__side {
  min-width: 160px;
  align-items: flex-end;

  @media (max-width: 767px) {
    min-width: 0;
    align-items: flex-start;
  }
}

.sectors-metrics-panel {
  .stats-grid {
    display: grid;
    grid-template-columns: repeat(9, minmax(0, 1fr));
    gap: var(--spacing-2);

    @media (max-width: 1680px) {
      grid-template-columns: repeat(5, minmax(0, 1fr));
    }

    @media (max-width: 1365px) {
      grid-template-columns: repeat(3, minmax(0, 1fr));
    }

    @media (max-width: 767px) {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
  }

  .stat-card-v2 {
    background: #fff;
    border: 1px solid rgba(0,0,0,0.06);
    border-radius: var(--radius-md);
    padding: 10px 12px;
    display: flex;
    align-items: center;
    gap: 10px;
    transition: all var(--transition-base);
    box-shadow: var(--shadow-sm);
    min-width: 0;
    min-height: 74px;

    &:hover {
      box-shadow: var(--shadow-md);
      transform: translateY(-1px);
    }

    &.stat-card-v2--clickable {
      cursor: pointer;
      user-select: none;
    }

    &.is-active {
      border-color: rgba(245,158,11,0.32);
      box-shadow: 0 8px 22px rgba(245,158,11,0.12);
    }

    &.hot {
      border-color: rgba(239,68,68,0.12);

      .stat-icon-wrap {
        background: rgba(239,68,68,0.08);
        color: #ef4444;
      }

      .stat-value { color: #ef4444; }
    }

    &.inflow,
    &.breakthrough,
    &.buy-point {
      .stat-icon-wrap {
        background: rgba(34,197,94,0.08);
        color: #22c55e;
      }

      .stat-value { color: #22c55e; }
    }

    &.rapid-rise {
      border-color: rgba(245,158,11,0.16);

      .stat-value { color: #d97706; }
    }

    .stat-icon-wrap {
      width: 32px;
      height: 32px;
      border-radius: 10px;
      display: flex;
      align-items: center;
      justify-content: center;
      background: rgba(64,158,255,0.08);
      color: var(--primary-500);
      flex-shrink: 0;

      &.hot-icon { background: rgba(239,68,68,0.08); color: #ef4444; }
      &.flow-icon { background: rgba(249,115,22,0.08); color: #f97316; }
      &.rapid-rise-icon { background: rgba(245,158,11,0.10); color: #f59e0b; }
      &.breakthrough-icon { background: rgba(34,197,94,0.08); color: #22c55e; }
      &.buy-point-icon { background: rgba(239,68,68,0.08); color: #ef4444; }
    }

    .stat-content {
      min-width: 0;
      display: flex;
      flex-direction: column;
      justify-content: center;
      gap: 2px;
    }

    .stat-label {
      font-size: 0.7rem;
      font-weight: 500;
      color: var(--claw-text-muted);
      line-height: 1.3;
      margin-bottom: 0;
      white-space: nowrap;
    }

    .stat-value {
      font-size: 1.2rem;
      font-weight: 700;
      font-variant-numeric: tabular-nums;
      color: var(--claw-text-primary);
      line-height: 1.2;
      white-space: nowrap;
    }

    .stat-hint {
      margin-top: 0;
      font-size: 0.66rem;
      line-height: 1.25;
      color: var(--claw-text-secondary);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      max-width: 100%;
    }

    .text-up { color: #ef4444; }
    .text-down { color: #22c55e; }
  }
}

.state-filters {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--spacing-3);
  margin-top: var(--spacing-4);
  margin-bottom: var(--spacing-4);

  .anomaly-filter-group {
    :deep(.el-radio-button__inner) {
      height: 24px !important;
      line-height: 22px !important;
      padding: 0 10px !important;
      font-size: 12px !important;
    }
  }

  :deep(.el-radio-group) {
    display: flex;
    flex-wrap: wrap;
    gap: var(--spacing-2);
  }

  :deep(.el-radio-button) {
    margin: 0 !important;
  }

  :deep(.el-radio-button__inner) {
    height: 32px;
    line-height: 32px;
    padding: 0 12px;
    font-size: 0.875rem;
    font-weight: 500;
    border-radius: var(--radius-md) !important;
    margin-left: 0 !important;
    box-shadow: none !important;
    color: var(--claw-text-secondary);
    background: var(--claw-bg-card);
    transition: all var(--transition-fast);

    .state-pill-label {
      display: inline-flex;
      align-items: center;
      gap: 4px;
    }

    &:hover {
      border-color: var(--primary-300) !important;
      color: var(--claw-primary);
      background: var(--primary-50);
    }
  }

  :deep(.el-radio-button.is-active .el-radio-button__inner),
  :deep(.el-radio-button.is-active .el-radio-button__original-radio:not(:disabled)+.el-radio-button__inner) {
    box-shadow: none !important;
    color: #fff !important;
    background: linear-gradient(135deg, var(--primary-500) 0%, var(--primary-600) 100%) !important;
    border-color: var(--primary-500) !important;
  }

  :deep(.el-radio-button:first-child .el-radio-button__inner),
  :deep(.el-radio-button:last-child .el-radio-button__inner),
  :deep(.el-radio-button:first-child:last-child .el-radio-button__inner) {
    border-radius: var(--radius-md) !important;
    box-shadow: none !important;
  }
}

.anomaly-filter-row {
  row-gap: var(--spacing-2);

  .filter-divider {
    width: 1px;
    height: 22px;
    background: var(--claw-border);
    opacity: 0.9;
  }

  .filter-inline-label {
    font-size: 12px;
    font-weight: 600;
    color: var(--claw-text-muted);
    margin-right: -4px;
    white-space: nowrap;
  }

  .filter-inline-label--muted {
    margin-left: -6px;
    opacity: 0.9;
  }

  .anomaly-sort-select {
    width: 138px;

    :deep(.el-select__wrapper) {
      min-height: 28px;
      border-radius: var(--radius-md);
      box-shadow: 0 0 0 1px var(--claw-border) inset;
    }
  }
}

.buy-point-checkbox {
  height: 28px;
  display: inline-flex;
  align-items: center;

  :deep(.el-checkbox__label) {
    font-size: 12px;
    font-weight: 600;
  }
}

.anomaly-snapshot-note {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 12px;
}

.fund-source-legend,
.anomaly-snapshot-note {
  margin-top: 4px;
  margin-bottom: 12px;
  font-size: 12px;
  line-height: 1.6;
  color: #7e8ca3;
}

.fund-source-legend strong {
  color: #34455f;
  font-weight: 600;
}

.legend-note {
  color: #98a6bd;
}

.legend-divider {
  margin: 0 8px;
  color: #c2cad8;
}

.table-container,
.anomaly-table-wrap {
  border-radius: var(--radius-lg);
  overflow: hidden;
  border: 1px solid var(--claw-border);
  margin-top: var(--spacing-4);
}

.anomaly-table-wrap {
  box-shadow: var(--shadow-sm);
}

.anomaly-table-skeleton {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px;
  background: #fff;
}

.anomaly-table-skeleton__row {
  display: grid;
  grid-template-columns: 160px 220px 180px 180px 220px 240px;
  gap: 12px;

  @media (max-width: 1440px) {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }

  @media (max-width: 767px) {
    grid-template-columns: 1fr;
  }
}

.skeleton-block {
  height: 52px;
  border-radius: 12px;
  background: linear-gradient(90deg, rgba(226, 232, 240, 0.72) 25%, rgba(241, 245, 249, 0.95) 37%, rgba(226, 232, 240, 0.72) 63%);
  background-size: 400% 100%;
  animation: anomaly-skeleton-shimmer 1.4s ease infinite;
}

@keyframes anomaly-skeleton-shimmer {
  0% { background-position: 100% 0; }
  100% { background-position: 0 0; }
}

.stock-cell,
.opportunity-cell,
.execution-cell,
.snapshot-cell,
.capital-cell,
.driver-cell {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}

.stock-name,
.cell-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--claw-text-primary);
  line-height: 1.4;
}

.stock-meta,
.cell-subtitle {
  font-size: 12px;
  line-height: 1.45;
  color: var(--claw-text-muted);
}

.capital-source-line {
  color: #6e7f98;
}

.cell-tags,
.execution-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.snapshot-price-line {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.snapshot-price,
.capital-value {
  font-size: 15px;
  font-weight: 700;
  color: var(--claw-text-primary);
  font-variant-numeric: tabular-nums;
}

.snapshot-change {
  font-size: 13px;
  font-weight: 600;
}

.text-flat {
  color: var(--claw-text-secondary);
}

.anomaly-tooltip {
  display: flex;
  flex-direction: column;
  gap: 4px;
  max-width: 320px;
}

.tooltip-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--claw-text-primary);
}

.tooltip-line {
  font-size: 12px;
  line-height: 1.55;
  color: var(--claw-text-secondary);
}

.tooltip-indent {
  padding-left: 10px;
}

.tooltip-muted {
  color: var(--claw-text-muted);
}

:deep(.el-table) {
  border-radius: 0;
  border: none;
  cursor: pointer;

  th.el-table__cell {
    background: var(--neutral-50);
    font-weight: 600;
    color: var(--claw-text-secondary);
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.025em;
    padding: 10px 0;
  }

  td.el-table__cell {
    padding: 9px 0;
  }

  .el-table__row:hover > td {
    background: var(--primary-50);
  }
}

.orderbook-chip {
  flex-shrink: 0;
}

.column-header-with-note {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
  line-height: 1.2;
}

.column-header-note {
  font-size: 11px;
  font-weight: 500;
  color: #98a6bd;
}

.anomaly-table-wrap :deep(td.el-table__cell .cell) {
  white-space: normal;
}

@media (max-width: 767px) {
  .anomaly-tab-content {
    gap: var(--spacing-3);
  }

  .sectors-metrics-panel {
    .stats-grid {
      grid-template-columns: 1fr;
    }

    .stat-card-v2 {
      min-height: 68px;
    }
  }
}
</style>
