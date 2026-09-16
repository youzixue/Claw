<template>
  <div class="page-container">
    <div class="page-shell promotion-page">
      <div class="page-hero">
        <div>
          <h2 class="page-title"><el-icon class="title-icon"><TrendCharts /></el-icon>晋级预测</h2>
          <div class="page-subtitle">统一查看连板梯队、晋级强度与个股晋级概率</div>
        </div>
        <div class="hero-chip">
          <el-icon><TrendCharts /></el-icon>
          <span>连板晋级观察台</span>
        </div>
      </div>

      <div class="metrics-panel promotion-metrics-panel">
        <div class="stat-row">
          <div class="stat-card promotion-stat-card" data-testid="market-board-height">
            <div class="metric-head"><span>连板高度</span></div>
            <div class="stat-value text-red">{{ boardHeight.height ?? '--' }}</div>
          </div>
          <div class="stat-card promotion-stat-card" data-testid="market-limit-up-count">
            <div class="metric-head"><span>全市场涨停</span></div>
            <div class="stat-value">{{ boardHeight.limit_up_count ?? '--' }}</div>
          </div>
          <div class="stat-card promotion-stat-card" data-testid="market-seal-rate">
            <div class="metric-head"><span>{{ sealRateLabel }}</span></div>
            <div class="stat-value">{{ boardHeight.seal_rate != null ? Number(boardHeight.seal_rate).toFixed(1) + '%' : '--' }}</div>
          </div>
          <div class="stat-card promotion-stat-card" data-testid="market-promotion-rate">
            <div class="metric-head"><span>全市场续板率</span></div>
            <div class="stat-value text-yellow">{{ boardHeight.promotion_rate != null ? (Number(boardHeight.promotion_rate) * 100).toFixed(1) + '%' : '--' }}</div>
          </div>
        </div>
        <div v-if="boardScopeSummary" class="market-scope-note" data-testid="market-scope-note">
          {{ boardScopeSummary }}
        </div>
      </div>

      <el-alert
        v-if="predictionHealth.should_warn"
        class="snapshot-alert"
        type="warning"
        show-icon
        :closable="false"
        :title="predictionHealth.message || '预测快照异常'"
        :description="predictionHealth.detail || '最新交易日没有 promotion_prediction_record，不能展示旧预测当今天预测。'"
      />
      <div v-if="auctionHealth.trade_date" class="prediction-health-strip" data-testid="auction-health-strip">
        <el-tag :type="auctionHealth.status === 'ok' ? 'success' : 'warning'" effect="light">
          竞价 {{ auctionHealth.status === 'ok' ? '完整' : auctionHealth.status === 'missing' ? '缺失' : '降级' }}
        </el-tag>
        <span>最终帧 {{ auctionHealth.latest_snapshot_time || '--' }}</span>
        <el-tooltip content="只校验快照时间落在 09:24-09:30，不代表成交字段完整">
          <span>及时快照（仅时点） {{ auctionHealth.timely_snapshot_count || 0 }}/{{ auctionHealth.latest_code_count || 0 }}</span>
        </el-tooltip>
        <el-tooltip content="价格、增量成交量、成交额和量比四项同时有效才计为完整">
          <span>竞价四字段完整 {{ auctionHealth.feed_complete_count || 0 }}/{{ auctionHealth.latest_code_count || 0 }}</span>
        </el-tooltip>
        <span>强高开可执行（字段齐全） {{ auctionHealth.executable_strong_open_count || 0 }}/{{ auctionHealth.strong_open_count || 0 }}</span>
        <span>独立快照上下文 {{ snapshotContextCount }}</span>
        <span>消息截止 {{ promotionCandidates.prediction_news_end_time || '--' }}</span>
        <span data-testid="current-prediction-model">当前候选模型 {{ promotionCandidates.prediction_model_version || '--' }}</span>
        <el-tag size="small" :type="modelRuntimeTagType" effect="plain" data-testid="model-runtime-mode">
          {{ modelRuntimeLabel }}
        </el-tag>
        <span>数据版本 {{ promotionCandidates.data_version || '--' }}</span>
        <span>特征版本 {{ promotionCandidates.feature_version || '--' }}</span>
      </div>

      <div class="section-block learning-review-section">
        <div class="section-title-with-action">
          <div>
            <div class="section-title">预测复盘与每日学习</div>
            <div class="candidate-subtitle">
              前一交易日收盘正式主榜 vs 下一交易日可交易主板真实首/二板；页面补位观察股不计入预测数
            </div>
          </div>
          <el-tag :type="learningReviewTagType(learningRecommendation.status)" effect="light">
            {{ learningRecommendation.label || '等待复盘数据' }}
          </el-tag>
        </div>
        <div class="panel-card learning-review-card">
          <div v-if="learningLatest.actual_trade_date" class="learning-review-meta">
            <span>最新复盘 {{ learningLatest.prediction_trade_date }} → {{ learningLatest.actual_trade_date }}</span>
            <el-tag :type="learningLatest.snapshot_complete ? 'success' : 'warning'" size="small" effect="light">
              {{ learningLatest.snapshot_complete ? '预测记录完整' : '预测记录不完整' }}
            </el-tag>
            <span v-if="learningLatest.prediction_model_version" data-testid="review-prediction-model">
              复盘模型 {{ learningLatest.prediction_model_version }}
            </span>
          </div>
          <div v-if="learningReviewScopeNote" class="learning-scope-note" data-testid="review-scope-note">
            {{ learningReviewScopeNote }}
          </div>
          <div class="learning-scope-note" data-testid="review-directional-contract">
            次日上涨目标 80% 是研究目标，未承诺、未认证。只评价正式收盘批次及记录日历真实 T+1；
            缺失结局为未知，不删原名单凑达标。显示概率是模型预测，上涨实际率是事后统计，
            涨停命中按首/二板标签统计；可执行子集命中不是成交或交易收益，Champion 与执行闸门不变。
          </div>
          <div v-for="summary in learningDirectionalSummaries" :key="summary.key"
            class="learning-directional-summary" :data-testid="`review-directional-${summary.key}`">
            <div class="learning-directional-heading">
              <strong>{{ summary.label }} · 次日上涨目标 {{ formatPercent(summary.metrics.directional_target_precision) }}</strong>
              <el-tag :type="directionalTargetType(summary.metrics)" size="small" effect="light">
                {{ directionalTargetLabel(summary.metrics) }}
              </el-tag>
              <span>{{ reviewEvaluationLabel(summary.metrics) }}</span>
            </div>
            <div class="learning-directional-values">
              <span>完整上涨实际率 <b>{{ formatDirectionalPrecision(summary.metrics) }}</b></span>
              <span>可评价 / 原预测 <b>{{ summary.metrics.directional_evaluable_count ?? '--' }} / {{ summary.metrics.predicted_count ?? '--' }}</b></span>
              <span>未知 <b>{{ summary.metrics.directional_unknown_count ?? '--' }}</b></span>
              <span>覆盖率 <b>{{ formatPercent(summary.metrics.directional_coverage) }}</b></span>
              <span>已评价子集上涨率 <b>{{ formatPercent(summary.metrics.directional_observed_precision) }}</b></span>
              <span>原名单上涨率上下界 <b>{{ formatDirectionalBounds(summary.metrics) }}</b></span>
            </div>
            <div v-if="reviewEvaluationReasons(summary.metrics)" class="candidate-subtitle">
              评价原因：{{ reviewEvaluationReasons(summary.metrics) }}
            </div>
          </div>
          <div class="learning-metric-grid">
            <div class="learning-metric-card" data-testid="review-first-actual">
              <span>实际主板首板</span>
              <strong>{{ learningFirstBoard.actual_count ?? '--' }}</strong>
            </div>
            <div class="learning-metric-card" data-testid="review-first-predicted">
              <span>正式主板首板 Top12</span>
              <strong>{{ learningFirstBoard.predicted_count ?? '--' }}</strong>
            </div>
            <div class="learning-metric-card" data-testid="review-first-hit">
              <span>主板首板命中</span>
              <strong class="text-red">{{ learningFirstBoard.hit_count ?? '--' }}</strong>
            </div>
            <div class="learning-metric-card" data-testid="review-first-pool-recall">
              <span>主板首板全池召回</span>
              <strong>{{ formatPercent(learningFirstBoard.pool_recall) }}</strong>
            </div>
            <div class="learning-metric-card" data-testid="review-first-top30-hit">
              <span>Top30 主板首板命中</span>
              <strong v-if="learningFirstBoard.recall_ranked_available === true">{{ learningFirstBoard.recall_hit_count ?? 0 }}</strong>
              <strong v-else class="metric-unavailable">旧版未记录</strong>
            </div>
            <div class="learning-metric-card" data-testid="review-second-actual">
              <span>实际主板二板</span>
              <strong>{{ learningSecondBoard.actual_count ?? '--' }}</strong>
            </div>
            <div class="learning-metric-card" data-testid="review-second-hit">
              <span>主板二板命中</span>
              <strong>{{ learningSecondBoard.hit_count ?? '--' }}</strong>
            </div>
            <div class="learning-metric-card" data-testid="review-first-precision">
              <span>主板首板精度</span>
              <strong>{{ formatPercent(learningFirstBoard.precision) }}</strong>
            </div>
            <div class="learning-metric-card" data-testid="review-first-recall">
              <span>主板首板正式召回</span>
              <strong>{{ formatPercent(learningFirstBoard.recall) }}</strong>
            </div>
          </div>
          <el-alert
            v-if="learningRecommendation.headline"
            class="learning-recommendation"
            :type="learningReviewAlertType(learningRecommendation.status)"
            :closable="false"
            show-icon
            :title="learningRecommendation.headline"
            :description="learningRecommendation.auto_policy"
            data-testid="review-recommendation"
          />
          <div class="learning-review-grid">
            <div>
              <div class="candidate-title learning-subtitle">分赛道成绩</div>
              <el-table :data="learningLaneRows" size="small" stripe empty-text="暂无分赛道复盘">
                <el-table-column prop="target_label" label="赛道" width="74" />
                <el-table-column prop="actual_count" label="实际" width="64" align="center" />
                <el-table-column prop="predicted_count" label="预测" width="64" align="center" />
                <el-table-column prop="hit_count" label="命中" width="64" align="center" />
                <el-table-column label="精度" width="76" align="center">
                  <template #default="{ row }">{{ formatPercent(row.precision) }}</template>
                </el-table-column>
                <el-table-column label="正式召回" width="82" align="center">
                  <template #default="{ row }">{{ formatPercent(row.recall) }}</template>
                </el-table-column>
                <el-table-column label="全池召回" width="82" align="center">
                  <template #default="{ row }">{{ formatPercent(row.pool_recall) }}</template>
                </el-table-column>
                <el-table-column label="宽召回命中" width="108" align="center">
                  <template #default="{ row }">
                    {{ row.recall_ranked_available === true ? `${row.recall_hit_count || 0}/${row.recall_ranked_count || 0}` : '旧版未记录' }}
                  </template>
                </el-table-column>
                <el-table-column label="可执行" width="76" align="center">
                  <template #default="{ row }">{{ row.actionability_labeled_count ? row.actionable_predicted_count : '--' }}</template>
                </el-table-column>
                <el-table-column label="执行精度" width="84" align="center">
                  <template #default="{ row }">{{ row.actionable_predicted_count ? formatPercent(row.actionable_precision) : '--' }}</template>
                </el-table-column>
              </el-table>
            </div>
            <div>
              <div class="candidate-title learning-subtitle">最新漏选样本</div>
              <el-table :data="learningMissedExamples" size="small" stripe empty-text="暂无漏选样本">
                <el-table-column label="个股" min-width="150">
                  <template #default="{ row }">
                    <span class="candidate-stock-name" @click="$router.push(`/stocks/${row.code}`)">{{ row.name }}({{ row.code }})</span>
                  </template>
                </el-table-column>
                <el-table-column prop="target_label" label="结果" width="64" />
                <el-table-column label="漏选原因" min-width="120">
                  <template #default="{ row }">{{ learningMissReasonLabel(row.status) }}</template>
                </el-table-column>
              </el-table>
            </div>
          </div>
          <div class="candidate-title learning-subtitle">
            启动前证据分组（近 {{ learningReview.lookback_days || 10 }} 日正式首板主榜）
          </div>
          <div class="candidate-subtitle">
            同一只股票可属于多个证据组；预测表现与可执行子集分开，低样本组只观察不自动放宽阈值
          </div>
          <div class="mobile-table-wrap">
            <el-table :data="learningLaunchCohortRows" size="small" stripe empty-text="等待新版正式快照积累样本" data-testid="review-directional-cohorts">
              <el-table-column prop="label" label="证据组" min-width="150" />
              <el-table-column prop="sample_count" label="样本" width="68" align="center" />
              <el-table-column label="完整上涨实际率" width="126" align="center">
                <template #default="{ row }">{{ formatDirectionalPrecision(row) }}</template>
              </el-table-column>
              <el-table-column label="可评价/原名单" width="120" align="center">
                <template #default="{ row }">{{ row.directional_evaluable_count ?? '--' }} / {{ row.predicted_count ?? row.sample_count ?? '--' }}</template>
              </el-table-column>
              <el-table-column label="未知" width="68" align="center">
                <template #default="{ row }">{{ row.directional_unknown_count ?? '--' }}</template>
              </el-table-column>
              <el-table-column label="覆盖率" width="82" align="center">
                <template #default="{ row }">{{ formatPercent(row.directional_coverage) }}</template>
              </el-table-column>
              <el-table-column label="已评价子集上涨率" width="138" align="center">
                <template #default="{ row }">{{ formatPercent(row.directional_observed_precision) }}</template>
              </el-table-column>
              <el-table-column label="原名单上涨率上下界" width="170" align="center">
                <template #default="{ row }">{{ formatDirectionalBounds(row) }}</template>
              </el-table-column>
              <el-table-column label="上涨目标 / 达标状态" min-width="220" align="center">
                <template #default="{ row }">
                  <span>{{ formatPercent(row.directional_target_precision) }} · </span>
                  <el-tag :type="directionalTargetType(row)" size="small" effect="light">{{ directionalTargetLabel(row) }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="评价状态 / 原因" min-width="180" show-overflow-tooltip>
                <template #default="{ row }">{{ reviewEvaluationLabel(row) }} {{ reviewEvaluationReasons(row) }}</template>
              </el-table-column>
              <el-table-column label="上涨Lift" width="88" align="center">
                <template #default="{ row }">{{ formatLift(row.directional_lift) }}</template>
              </el-table-column>
              <el-table-column label="强涨率" width="82" align="center">
                <template #default="{ row }">{{ formatPercent(row.strong_rise_precision) }}</template>
              </el-table-column>
              <el-table-column label="强涨Lift" width="88" align="center">
                <template #default="{ row }">{{ formatLift(row.strong_rise_lift) }}</template>
              </el-table-column>
              <el-table-column label="首板精度" width="88" align="center">
                <template #default="{ row }">{{ formatPercent(row.limit_up_precision) }}</template>
              </el-table-column>
              <el-table-column label="首板Lift" width="88" align="center">
                <template #default="{ row }">{{ formatLift(row.limit_up_lift) }}</template>
              </el-table-column>
              <el-table-column label="可执行" width="74" align="center">
                <template #default="{ row }">{{ row.actionable_count || '--' }}</template>
              </el-table-column>
              <el-table-column label="执行精度" width="88" align="center">
                <template #default="{ row }">{{ row.actionable_count ? formatPercent(row.actionable_limit_up_precision) : '--' }}</template>
              </el-table-column>
            </el-table>
          </div>
          <div class="candidate-title learning-subtitle">近 {{ learningReview.lookback_days || 10 }} 日逐日成绩</div>
          <div class="mobile-table-wrap">
            <el-table :data="learningDailyRows" size="small" stripe empty-text="暂无逐日复盘" data-testid="review-directional-daily">
              <el-table-column prop="actual_trade_date" label="结果日" width="110" />
              <el-table-column prop="actual_target_limit_up_count" label="实际首/二板" width="100" align="center" />
              <el-table-column prop="predicted_count" label="正式预测" width="86" align="center" />
              <el-table-column prop="predicted_limit_up_hit_count" label="涨停命中" width="86" align="center" />
              <el-table-column prop="underestimated_limit_up_hit_count" label="概率低估命中" width="100" align="center" />
              <el-table-column label="精度" width="82" align="center">
                <template #default="{ row }">{{ formatPercent(row.limit_up_precision) }}</template>
              </el-table-column>
              <el-table-column label="可执行精度" width="96" align="center">
                <template #default="{ row }">{{ row.actionable_predicted_count ? formatPercent(row.actionable_limit_up_precision) : '--' }}</template>
              </el-table-column>
              <el-table-column label="召回" width="82" align="center">
                <template #default="{ row }">{{ formatPercent(row.limit_up_recall) }}</template>
              </el-table-column>
              <el-table-column prop="actual_rising_count" label="主板上涨" width="82" align="center" />
              <el-table-column label="完整上涨实际率" width="126" align="center">
                <template #default="{ row }">{{ formatDirectionalPrecision(row) }}</template>
              </el-table-column>
              <el-table-column label="可评价/原名单" width="120" align="center">
                <template #default="{ row }">{{ row.directional_evaluable_count ?? '--' }} / {{ row.predicted_count ?? row.sample_count ?? '--' }}</template>
              </el-table-column>
              <el-table-column label="未知" width="68" align="center">
                <template #default="{ row }">{{ row.directional_unknown_count ?? '--' }}</template>
              </el-table-column>
              <el-table-column label="覆盖率" width="82" align="center">
                <template #default="{ row }">{{ formatPercent(row.directional_coverage) }}</template>
              </el-table-column>
              <el-table-column label="已评价子集上涨率" width="138" align="center">
                <template #default="{ row }">{{ formatPercent(row.directional_observed_precision) }}</template>
              </el-table-column>
              <el-table-column label="原名单上涨率上下界" width="170" align="center">
                <template #default="{ row }">{{ formatDirectionalBounds(row) }}</template>
              </el-table-column>
              <el-table-column label="上涨目标 / 达标状态" min-width="220" align="center">
                <template #default="{ row }">
                  <span>{{ formatPercent(row.directional_target_precision) }} · </span>
                  <el-tag :type="directionalTargetType(row)" size="small" effect="light">{{ directionalTargetLabel(row) }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="评价状态 / 原因" min-width="180" show-overflow-tooltip>
                <template #default="{ row }">{{ reviewEvaluationLabel(row) }} {{ reviewEvaluationReasons(row) }}</template>
              </el-table-column>
              <el-table-column label="Brier误差" width="96" align="center">
                <template #default="{ row }">{{ row.brier_score == null || String(row.brier_score).trim() === '' || !Number.isFinite(Number(row.brier_score)) ? '--' : Number(row.brier_score).toFixed(3) }}</template>
              </el-table-column>
            </el-table>
          </div>
        </div>
      </div>

      <div class="section-block">
        <div class="section-title">连板梯队</div>
        <div class="panel-card chart-card">
          <v-chart :option="ladderChartOption" style="height: 350px" autoresize />
        </div>

        <div class="panel-card">
          <div class="mobile-table-wrap">
          <el-table :data="ladderData" stripe size="small" empty-text="暂无数据" row-key="consecutive_days"
            :row-class-name="({ row }) => row.consecutive_days >= 4 ? 'high-ladder' : ''">
      <el-table-column prop="consecutive_days" label="连板" width="70" align="center">
        <template #default="{ row }">
          <strong :class="row.consecutive_days >= 5 ? 'text-red' : row.consecutive_days >= 3 ? 'text-yellow' : ''">
            {{ row.consecutive_days }}板
          </strong>
        </template>
      </el-table-column>
      <el-table-column prop="count" label="个数" width="60" align="center" />
      <el-table-column prop="seal_rate" label="封板率" width="80" align="center">
        <template #default="{ row }">{{ Number(row.seal_rate || 0).toFixed(1) }}%</template>
      </el-table-column>
      <el-table-column label="个股" min-width="300">
        <template #default="{ row }">
          <el-tag v-for="s in row.stocks?.slice(0, 5)" :key="s.code" size="small" class="stock-tag"
            @click="$router.push(`/stocks/${s.code}`)">
            {{ s.name }}({{ s.code }})
          </el-tag>
        </template>
      </el-table-column>
          </el-table>
          </div>
        </div>
      </div>

      <div class="section-block">
        <div class="section-title">晋级候选</div>
        <div class="candidate-grid">
          <div class="panel-card">
            <div class="candidate-panel-head">
              <div class="candidate-title">首板冲刺候选</div>
              <div class="candidate-subtitle">优先展示 1-2 个交易日内点火的首板预判；当冲刺池过窄时，会优先补位准冲刺，其次才回落到普通观察</div>
              <div class="candidate-legend">
                <el-tag size="small" type="warning" effect="light">盘中预判</el-tag>
                <el-tag size="small" type="success" effect="light">收盘确认</el-tag>
              </div>
            </div>
            <el-table :data="firstBoardCandidates" stripe size="small" empty-text="暂无首板候选" row-key="code">
              <el-table-column label="概率" width="92" align="center">
                <template #default="{ row }">
                  <span class="text-red">{{ formatProbability(row.probability) }}</span>
                </template>
              </el-table-column>
              <el-table-column label="路由" width="112" align="center">
                <template #default="{ row }">
                  <el-tag :type="routeTagType(row.candidate_route)" size="small" effect="light">
                    {{ row.candidate_route_label || '--' }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="状态" width="108" align="center">
                <template #default="{ row }">
                  <el-tooltip :content="row.signal_status_reason || row.kline_confirmation?.summary || '暂无说明'">
                    <el-tag :type="signalStatusTagType(row.signal_status)" size="small" effect="light">
                      {{ row.signal_status_label || '--' }}
                    </el-tag>
                  </el-tooltip>
                </template>
              </el-table-column>
              <el-table-column label="个股" min-width="170">
                <template #default="{ row }">
                  <div class="candidate-stock-cell">
                    <span class="candidate-stock-name" @click="$router.push(`/stocks/${row.code}`)">{{ row.name }}({{ row.code }})</span>
                    <el-tag v-if="row.tag" size="small" type="info">{{ row.tag }}</el-tag>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="涨幅" width="80" align="center">
                <template #default="{ row }">{{ formatSignedPercent(row.change_pct) }}</template>
              </el-table-column>
              <el-table-column label="首波记忆" width="110" align="center">
                <template #default="{ row }">
                  <div class="metric-stack">
                    <strong>{{ formatScore(row.memory_features?.memory_score) }}</strong>
                    <span>{{ formatMemorySummary(row.memory_features) }}</span>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="长周期画像" width="176" align="center">
                <template #default="{ row }">
                  <div class="metric-stack">
                    <strong>{{ formatLongCycleLabel(row) }}</strong>
                    <span>{{ formatLongCycleDetail(row) }}</span>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="分阶段概率" width="132" align="center">
                <template #default="{ row }">
                  <div class="metric-stack">
                    <span>3日突破 {{ formatProbability(row.sub_probabilities?.breakout_3d) }}</span>
                    <span>5日首板 {{ formatProbability(row.sub_probabilities?.first_limitup_5d || row.probability) }}</span>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="窗口" width="104" align="center">
                <template #default="{ row }">
                  <el-tooltip :content="row.time_horizon_reason || '暂无说明'">
                    <el-tag :type="timeHorizonTagType(row.time_horizon)" size="small" effect="light">{{ row.time_horizon_label || '--' }}</el-tag>
                  </el-tooltip>
                </template>
              </el-table-column>
              <el-table-column label="买点依据" min-width="220" show-overflow-tooltip>
                <template #default="{ row }">{{ row.primary_reason || row.signal_summary || '-' }}</template>
              </el-table-column>
              <el-table-column label="差哪一口气" min-width="260" show-overflow-tooltip>
                <template #default="{ row }">
                  {{ row.next_threshold_hint || (row.candidate_route === 'support_squeeze_start' ? '支撑位已经稳住，继续等首次放量点火' : '-') }}
                </template>
              </el-table-column>
            </el-table>
          </div>

          <div class="panel-card">
            <div class="candidate-panel-head">
              <div class="candidate-title">板后贴板横盘预备池</div>
              <div class="candidate-subtitle">最近真实涨停后仍贴着涨停锚点/前高附近横盘，优先看十字星和再点火结构</div>
            </div>
            <div v-if="limitUpPlatformOverview.headline" class="candidate-subtitle" style="margin-bottom: 12px;">
              {{ limitUpPlatformOverview.headline }}
            </div>
            <el-table :data="limitUpPlatformCandidates" stripe size="small" empty-text="暂无板后贴板横盘预备股" row-key="code">
              <el-table-column label="形态" width="132" align="center">
                <template #default="{ row }">
                  <el-tag :type="row.limit_up_nearby_doji_confirmation ? 'danger' : 'warning'" size="small" effect="light">
                    {{ row.limit_up_nearby_pattern_label || '板后贴板横盘' }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="窗口" width="104" align="center">
                <template #default="{ row }">
                  <el-tooltip :content="row.time_horizon_reason || '暂无说明'">
                    <el-tag :type="timeHorizonTagType(row.time_horizon)" size="small" effect="light">
                      {{ row.time_horizon_label || '--' }}
                    </el-tag>
                  </el-tooltip>
                </template>
              </el-table-column>
              <el-table-column label="个股" min-width="170">
                <template #default="{ row }">
                  <div class="candidate-stock-cell">
                    <span class="candidate-stock-name" @click="$router.push(`/stocks/${row.code}`)">{{ row.name }}({{ row.code }})</span>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="距板锚点" width="98" align="center">
                <template #default="{ row }">{{ formatAnchorGap(row.limit_up_anchor_gap_pct) }}</template>
              </el-table-column>
              <el-table-column label="首波记忆" width="118" align="center">
                <template #default="{ row }">
                  <div class="metric-stack">
                    <strong>{{ formatScore(row.memory_features?.memory_score) }}</strong>
                    <span>{{ formatLastLimitUpDate(row.memory_features) }}</span>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="分阶段概率" width="132" align="center">
                <template #default="{ row }">
                  <div class="metric-stack">
                    <span>3日突破 {{ formatProbability(row.sub_probabilities?.breakout_3d) }}</span>
                    <span>5日首板 {{ formatProbability(row.sub_probabilities?.first_limitup_5d || row.probability) }}</span>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="观察依据" min-width="260" show-overflow-tooltip>
                <template #default="{ row }">
                  {{ row.secondary_reason || row.primary_reason || row.signal_summary || '-' }}
                </template>
              </el-table-column>
            </el-table>
            <div class="platform-diagnostic-section">
              <div class="candidate-panel-head">
                <div class="candidate-title">为什么没进贴板池</div>
                <div class="candidate-subtitle">专门盯“离板锚点差多少 / 还差十字星还是差量窒息”，只看最近真实涨停后的那一圈票。</div>
                <div class="diagnostic-overview">
                  <el-tag size="small" type="info" effect="light">未进池样本 {{ limitUpPlatformDiagnostics.blocked_total || 0 }} 只</el-tag>
                  <el-tag
                    v-for="note in limitUpPlatformDiagnostics.notes || []"
                    :key="note"
                    size="small"
                    type="info"
                    effect="plain"
                  >
                    {{ note }}
                  </el-tag>
                </div>
              </div>

              <el-empty v-if="!limitUpPlatformDiagnosticGroups.length" description="暂无贴板横盘未入池样本" />

              <template v-else>
                <div v-if="limitUpPlatformDiagnosticsOverview.headline" class="diagnostic-headline-card platform-diagnostic-headline-card">
                  <div class="diagnostic-headline-title">贴板横盘池当前最缺什么</div>
                  <div class="diagnostic-headline-text">{{ limitUpPlatformDiagnosticsOverview.headline }}</div>
                </div>

                <div class="diagnostic-summary-grid">
                  <div v-for="item in limitUpPlatformDiagnosticSummary" :key="item.reason" class="diagnostic-summary-card">
                    <div class="diagnostic-summary-label">
                      <span>{{ item.label }}</span>
                      <el-tag :type="limitUpPlatformDiagnosticTagType(item.reason)" size="small" effect="light">{{ item.count }}只</el-tag>
                    </div>
                    <div class="diagnostic-summary-value">{{ item.count }}</div>
                    <div class="diagnostic-summary-subtitle">这类缺口占未进池样本 {{ formatPercent(item.share) }}</div>
                  </div>
                </div>

                <div class="diagnostic-groups">
                  <div v-for="group in limitUpPlatformDiagnosticGroups" :key="group.reason" class="diagnostic-group">
                    <div class="diagnostic-group-head">
                      <div>
                        <div class="diagnostic-group-title">{{ group.label }}</div>
                        <div class="candidate-subtitle">共 {{ group.count }} 只，下面展示最值得复盘的样例</div>
                      </div>
                      <el-tag :type="limitUpPlatformDiagnosticTagType(group.reason)" size="small" effect="light">{{ group.count }}只</el-tag>
                    </div>
                    <div v-if="group.playbook?.length" class="diagnostic-playbook">
                      <div v-for="(tip, tipIndex) in group.playbook" :key="`${group.reason}-${tipIndex}`" class="diagnostic-playbook-item">
                        {{ tip }}
                      </div>
                    </div>
                    <el-table :data="group.examples || []" stripe size="small" empty-text="暂无样例" row-key="code">
                      <el-table-column label="个股" min-width="170">
                        <template #default="{ row }">
                          <div class="candidate-stock-cell">
                            <span class="candidate-stock-name" @click="$router.push(`/stocks/${row.code}`)">{{ row.name }}({{ row.code }})</span>
                          </div>
                        </template>
                      </el-table-column>
                      <el-table-column label="距板锚点" width="120" align="center">
                        <template #default="{ row }">
                          <div class="metric-stack">
                            <strong>{{ formatAnchorGap(row.limit_up_anchor_gap_pct) }}</strong>
                            <span>{{ row.memory_last_limit_up_date ? `最近涨停 ${row.memory_last_limit_up_date}` : '最近涨停日 --' }}</span>
                          </div>
                        </template>
                      </el-table-column>
                      <el-table-column label="当前形态" width="220" align="center">
                        <template #default="{ row }">
                          <div class="metric-stack">
                            <strong>
                              {{
                                row.limit_up_nearby_doji_confirmation
                                  ? '板附近十字星已到位'
                                  : row.qualified_doji_confirmation || row.has_doji_confirmation
                                    ? '十字星待确认'
                                    : '还没出贴板十字星'
                              }}
                            </strong>
                            <span>
                              {{
                                row.has_volume_suffocation
                                  ? `量窒息已到位 (${formatRatio(row.volume_suffocation_ratio)})`
                                  : row.has_volume_contraction
                                    ? `缩量中，量窒息比 ${formatRatio(row.volume_suffocation_ratio)}`
                                    : '量能仍未明显收敛'
                              }}
                            </span>
                          </div>
                        </template>
                      </el-table-column>
                      <el-table-column label="没进池原因" min-width="240" show-overflow-tooltip>
                        <template #default="{ row }">
                          <div class="metric-stack">
                            <strong>{{ row.primary_reason || '--' }}</strong>
                            <span>{{ (row.blockers || []).join(' / ') || '--' }}</span>
                          </div>
                        </template>
                      </el-table-column>
                      <el-table-column label="可操作建议" min-width="280" show-overflow-tooltip>
                        <template #default="{ row }">
                          <div class="metric-stack">
                            <strong>{{ row.next_threshold_hint || '--' }}</strong>
                            <span>{{ (row.actionable_suggestions || []).slice(1).join(' / ') || '先补最靠前的一项结构缺口即可' }}</span>
                          </div>
                        </template>
                      </el-table-column>
                    </el-table>
                  </div>
                </div>
              </template>
            </div>
          </div>

          <div class="panel-card">
            <div class="candidate-panel-head">
              <div class="candidate-title">准冲刺</div>
              <div class="candidate-subtitle">静默蓄势里已经靠近临盘点火区，但涨幅还在 0.8%-1.2% 附近，优先看下一次放量和涨幅抬升</div>
            </div>
            <div v-if="firstBoardPreSprintOverview.headline" class="candidate-subtitle" style="margin-bottom: 12px;">
              {{ firstBoardPreSprintOverview.headline }}
            </div>
            <div v-if="firstBoardPreSprintGroups.length" class="diagnostic-summary-grid" style="margin-bottom: 12px;">
              <div v-for="group in firstBoardPreSprintGroups" :key="group.bucket" class="diagnostic-summary-card">
                <div class="diagnostic-summary-label">
                  <span>{{ group.label }}</span>
                  <el-tag :type="watchBucketTagType(group.bucket)" size="small" effect="light">{{ group.count }}只</el-tag>
                </div>
                <div class="diagnostic-summary-value">{{ group.main_uptrend_ready_count || 0 }}</div>
                <div class="diagnostic-summary-subtitle">临盘待点火</div>
              </div>
            </div>
            <el-empty v-if="!firstBoardPreSprintGroups.length" description="暂无准冲刺样本" />
            <div v-else class="diagnostic-groups">
              <div v-for="group in firstBoardPreSprintGroups" :key="group.bucket" class="diagnostic-group">
                <div class="diagnostic-group-head">
                  <div>
                    <div class="diagnostic-group-title">{{ group.label }}</div>
                    <div class="candidate-subtitle">{{ group.description || '暂无说明' }}</div>
                  </div>
                  <el-tag :type="watchBucketTagType(group.bucket)" size="small" effect="light">
                    {{ group.count }}只 / 主升浪预备 {{ group.main_uptrend_ready_count || 0 }}只
                  </el-tag>
                </div>
                <el-table :data="group.examples || []" stripe size="small" empty-text="暂无样例" row-key="code">
                  <el-table-column label="概率" width="92" align="center">
                    <template #default="{ row }">
                      <span>{{ formatProbability(row.probability) }}</span>
                    </template>
                  </el-table-column>
                  <el-table-column label="个股" min-width="170">
                    <template #default="{ row }">
                      <div class="candidate-stock-cell">
                        <span class="candidate-stock-name" @click="$router.push(`/stocks/${row.code}`)">{{ row.name }}({{ row.code }})</span>
                      </div>
                    </template>
                  </el-table-column>
                  <el-table-column label="路由" width="112" align="center">
                    <template #default="{ row }">
                      <el-tag :type="routeTagType(row.candidate_route)" size="small" effect="light">
                        {{ row.candidate_route_label || '--' }}
                      </el-tag>
                    </template>
                  </el-table-column>
                  <el-table-column label="窗口" width="104" align="center">
                    <template #default="{ row }">
                      <el-tooltip :content="row.time_horizon_reason || '暂无说明'">
                        <el-tag :type="timeHorizonTagType(row.time_horizon)" size="small" effect="light">
                          {{ row.time_horizon_label || '--' }}
                        </el-tag>
                      </el-tooltip>
                    </template>
                  </el-table-column>
                  <el-table-column label="涨幅" width="96" align="center">
                    <template #default="{ row }">{{ formatSignedPercent(row.change_pct) }}</template>
                  </el-table-column>
                  <el-table-column label="差哪一口气" min-width="260" show-overflow-tooltip>
                    <template #default="{ row }">
                      {{ row.next_threshold_hint || row.time_horizon_reason || '继续等下一次放量和涨幅抬升' }}
                    </template>
                  </el-table-column>
                </el-table>
              </div>
            </div>
          </div>

          <div class="panel-card">
            <div class="candidate-panel-head">
              <div class="candidate-title">首板预测梯队</div>
              <div class="candidate-subtitle">未进 1-2 日冲刺主池，但仍保留在 3-5 日首板预测层继续跟踪；这里只保留收平到小红的观察预备样本</div>
            </div>
            <div v-if="firstBoardWatchOverview.headline" class="candidate-subtitle" style="margin-bottom: 12px;">
              {{ firstBoardWatchOverview.headline }}
            </div>
            <div v-if="firstBoardWatchGroups.length" class="diagnostic-summary-grid" style="margin-bottom: 12px;">
              <div v-for="group in firstBoardWatchGroups" :key="group.bucket" class="diagnostic-summary-card">
                <div class="diagnostic-summary-label">
                  <span>{{ group.label }}</span>
                  <el-tag :type="watchBucketTagType(group.bucket)" size="small" effect="light">{{ group.count }}只</el-tag>
                </div>
                <div class="diagnostic-summary-value">{{ group.main_uptrend_ready_count || 0 }}</div>
                <div class="diagnostic-summary-subtitle">主升浪预备</div>
              </div>
            </div>
            <el-empty v-if="!firstBoardWatchGroups.length" description="暂无首板梯队" />
            <div v-else class="diagnostic-groups">
              <div v-for="group in firstBoardWatchGroups" :key="group.bucket" class="diagnostic-group">
                <div class="diagnostic-group-head">
                  <div>
                    <div class="diagnostic-group-title">{{ group.label }}</div>
                    <div class="candidate-subtitle">{{ group.description || '暂无说明' }}</div>
                  </div>
                  <el-tag :type="watchBucketTagType(group.bucket)" size="small" effect="light">
                    {{ group.count }}只 / 主升浪预备 {{ group.main_uptrend_ready_count || 0 }}只
                  </el-tag>
                </div>
                <el-table :data="group.examples || []" stripe size="small" empty-text="暂无样例" row-key="code">
                  <el-table-column label="概率" width="92" align="center">
                    <template #default="{ row }">
                      <span>{{ formatProbability(row.probability) }}</span>
                    </template>
                  </el-table-column>
                  <el-table-column label="主升浪阶段" width="122" align="center">
                    <template #default="{ row }">
                      <el-tooltip :content="row.main_uptrend_reason || '暂无说明'">
                        <el-tag :type="mainUptrendTagType(row.main_uptrend_label)" size="small" effect="light">
                          {{ row.main_uptrend_label || '--' }}
                        </el-tag>
                      </el-tooltip>
                    </template>
                  </el-table-column>
                  <el-table-column label="个股" min-width="170">
                    <template #default="{ row }">
                      <div class="candidate-stock-cell">
                        <span class="candidate-stock-name" @click="$router.push(`/stocks/${row.code}`)">{{ row.name }}({{ row.code }})</span>
                      </div>
                    </template>
                  </el-table-column>
                  <el-table-column label="路由" width="112" align="center">
                    <template #default="{ row }">
                      <el-tag :type="routeTagType(row.candidate_route)" size="small" effect="light">
                        {{ row.candidate_route_label || '--' }}
                      </el-tag>
                    </template>
                  </el-table-column>
                  <el-table-column label="首波记忆" width="110" align="center">
                    <template #default="{ row }">
                      <div class="metric-stack">
                        <strong>{{ formatScore(row.memory_features?.memory_score) }}</strong>
                        <span>{{ formatMemorySummary(row.memory_features) }}</span>
                      </div>
                    </template>
                  </el-table-column>
                  <el-table-column label="分阶段概率" width="132" align="center">
                    <template #default="{ row }">
                      <div class="metric-stack">
                        <span>3日突破 {{ formatProbability(row.sub_probabilities?.breakout_3d) }}</span>
                        <span>5日首板 {{ formatProbability(row.sub_probabilities?.first_limitup_5d || row.probability) }}</span>
                      </div>
                    </template>
                  </el-table-column>
                  <el-table-column label="观察依据" min-width="260" show-overflow-tooltip>
                    <template #default="{ row }">
                      {{ row.secondary_reason || row.primary_reason || row.signal_summary || '-' }}
                    </template>
                  </el-table-column>
                  <el-table-column label="差哪一口气" min-width="280" show-overflow-tooltip>
                    <template #default="{ row }">
                      {{ row.next_threshold_hint || (row.candidate_route === 'support_squeeze_start' ? '先补最靠前的一项确认，再看是否升格冲刺' : '-') }}
                    </template>
                  </el-table-column>
                </el-table>
              </div>
            </div>
          </div>

          <div class="panel-card">
            <div class="candidate-panel-head">
              <div class="candidate-title">弱观察</div>
              <div class="candidate-subtitle">当天收绿但形态还没坏的样本单独放这里，只看止跌翻红和重新补确认K</div>
            </div>
            <div v-if="firstBoardWeakWatchOverview.headline" class="candidate-subtitle" style="margin-bottom: 12px;">
              {{ firstBoardWeakWatchOverview.headline }}
            </div>
            <div v-if="firstBoardWeakWatchGroups.length" class="diagnostic-summary-grid" style="margin-bottom: 12px;">
              <div v-for="group in firstBoardWeakWatchGroups" :key="group.bucket" class="diagnostic-summary-card">
                <div class="diagnostic-summary-label">
                  <span>{{ group.label }}</span>
                  <el-tag :type="watchBucketTagType(group.bucket)" size="small" effect="light">{{ group.count }}只</el-tag>
                </div>
                <div class="diagnostic-summary-value">{{ group.main_uptrend_ready_count || 0 }}</div>
                <div class="diagnostic-summary-subtitle">仍保留主升浪轮廓</div>
              </div>
            </div>
            <el-empty v-if="!firstBoardWeakWatchGroups.length" description="暂无弱观察样本" />
            <div v-else class="diagnostic-groups">
              <div v-for="group in firstBoardWeakWatchGroups" :key="group.bucket" class="diagnostic-group">
                <div class="diagnostic-group-head">
                  <div>
                    <div class="diagnostic-group-title">{{ group.label }}</div>
                    <div class="candidate-subtitle">{{ group.description || '暂无说明' }}</div>
                  </div>
                  <el-tag :type="watchBucketTagType(group.bucket)" size="small" effect="light">
                    {{ group.count }}只 / 主升浪轮廓 {{ group.main_uptrend_ready_count || 0 }}只
                  </el-tag>
                </div>
                <el-table :data="group.examples || []" stripe size="small" empty-text="暂无样例" row-key="code">
                  <el-table-column label="概率" width="92" align="center">
                    <template #default="{ row }">
                      <span>{{ formatProbability(row.probability) }}</span>
                    </template>
                  </el-table-column>
                  <el-table-column label="主升浪阶段" width="122" align="center">
                    <template #default="{ row }">
                      <el-tooltip :content="row.main_uptrend_reason || '暂无说明'">
                        <el-tag :type="mainUptrendTagType(row.main_uptrend_label)" size="small" effect="light">
                          {{ row.main_uptrend_label || '--' }}
                        </el-tag>
                      </el-tooltip>
                    </template>
                  </el-table-column>
                  <el-table-column label="个股" min-width="170">
                    <template #default="{ row }">
                      <div class="candidate-stock-cell">
                        <span class="candidate-stock-name" @click="$router.push(`/stocks/${row.code}`)">{{ row.name }}({{ row.code }})</span>
                      </div>
                    </template>
                  </el-table-column>
                  <el-table-column label="路由" width="112" align="center">
                    <template #default="{ row }">
                      <el-tag :type="routeTagType(row.candidate_route)" size="small" effect="light">
                        {{ row.candidate_route_label || '--' }}
                      </el-tag>
                    </template>
                  </el-table-column>
                  <el-table-column label="涨幅" width="96" align="center">
                    <template #default="{ row }">
                      <span :class="row.change_pct >= 0 ? 'text-red' : 'text-green'">{{ formatPct(row.change_pct) }}</span>
                    </template>
                  </el-table-column>
                  <el-table-column label="可操作建议" min-width="260" show-overflow-tooltip>
                    <template #default="{ row }">
                      <div class="metric-stack">
                        <strong>{{ row.next_threshold_hint || '--' }}</strong>
                        <span>{{ (row.actionable_suggestions || []).slice(1).join(' / ') || row.time_horizon_reason || '等待止跌翻红' }}</span>
                      </div>
                    </template>
                  </el-table-column>
                </el-table>
              </div>
            </div>
          </div>

          <div class="panel-card">
            <div class="candidate-panel-head">
              <div class="candidate-title">二板候选</div>
              <div class="candidate-subtitle">从当日首板池里评估次日晋级二板的强弱顺序</div>
            </div>
            <el-table :data="secondBoardCandidates" stripe size="small" empty-text="暂无二板候选" row-key="code">
              <el-table-column label="概率" width="92" align="center">
                <template #default="{ row }">
                  <span class="text-yellow">{{ formatProbability(row.probability) }}</span>
                </template>
              </el-table-column>
              <el-table-column label="状态" width="108" align="center">
                <template #default="{ row }">
                  <el-tooltip :content="row.signal_status_reason || '暂无说明'">
                    <el-tag :type="signalStatusTagType(row.signal_status)" size="small" effect="light">
                      {{ row.signal_status_label || '--' }}
                    </el-tag>
                  </el-tooltip>
                </template>
              </el-table-column>
              <el-table-column label="交易口径" width="118" align="center">
                <template #default="{ row }">
                  <el-tooltip :content="(row.relay_blockers || []).join(' / ') || '需等待次日盘口确认'">
                    <el-tag :type="row.relay_pool_ready ? 'success' : 'warning'" size="small" effect="light">
                      {{ row.relay_action_label || (row.relay_pool_ready ? '可交易监控' : '只预测·不可追') }}
                    </el-tag>
                  </el-tooltip>
                </template>
              </el-table-column>
              <el-table-column label="个股" min-width="170">
                <template #default="{ row }">
                  <div class="candidate-stock-cell">
                    <span class="candidate-stock-name" @click="$router.push(`/stocks/${row.code}`)">{{ row.name }}({{ row.code }})</span>
                    <el-tag v-if="row.tag" size="small" type="info">{{ row.tag }}</el-tag>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="封板" width="80" align="center">
                <template #default="{ row }">{{ formatSealAmount(row.seal_amount) }}</template>
              </el-table-column>
              <el-table-column label="龙虎榜" min-width="180">
                <template #default="{ row }">
                  <el-tooltip :content="formatDragonTigerTooltip(row.dragon_tiger)">
                    <div class="metric-stack">
                      <el-tag :type="dragonTigerTagType(row.dragon_tiger)" size="small" effect="light">
                        {{ dragonTigerLabel(row.dragon_tiger) }}
                      </el-tag>
                      <span>{{ formatDragonTigerSummary(row.dragon_tiger) }}</span>
                    </div>
                  </el-tooltip>
                </template>
              </el-table-column>
              <el-table-column label="晋级依据" min-width="220" show-overflow-tooltip>
                <template #default="{ row }">{{ row.primary_reason || row.secondary_reason || '-' }}</template>
              </el-table-column>
            </el-table>
          </div>
        </div>

        <div class="panel-card diagnostic-panel">
          <div class="candidate-panel-head">
            <div class="candidate-title">首板未入池诊断</div>
            <div class="candidate-subtitle">把被主筛选挡掉的票按主因分组。这样就算首板冲刺为空，也能看见今天到底卡在主线、记忆、量能还是结构。</div>
            <div class="diagnostic-overview">
              <el-tag size="small" type="info" effect="light">未入池样本 {{ firstBoardDiagnostics.blocked_total || 0 }} 只</el-tag>
              <el-tag
                v-for="note in firstBoardDiagnostics.notes || []"
                :key="note"
                size="small"
                type="info"
                effect="plain"
              >
                {{ note }}
              </el-tag>
            </div>
          </div>

          <el-empty v-if="!firstBoardDiagnosticGroups.length" description="暂无首板未入池样本" />

          <template v-else>
            <div v-if="firstBoardDiagnosticsOverview.headline" class="diagnostic-headline-card">
              <div class="diagnostic-headline-title">今日首板最缺什么</div>
              <div class="diagnostic-headline-text">{{ firstBoardDiagnosticsOverview.headline }}</div>
              <div v-if="firstBoardDiagnosticsOverview.market_takeaway" class="diagnostic-headline-subtitle">
                {{ firstBoardDiagnosticsOverview.market_takeaway }}
              </div>
              <div v-if="firstBoardOverviewFocuses.length" class="diagnostic-focus-grid">
                <div v-for="focus in firstBoardOverviewFocuses" :key="focus.key" class="diagnostic-focus-card">
                  <div class="diagnostic-focus-label">
                    <span>{{ focus.label || '--' }}</span>
                    <el-tag :type="diagnosticReasonTagType(focus.reason)" size="small" effect="light">
                      {{ focus.count }}只
                    </el-tag>
                  </div>
                  <div class="diagnostic-focus-value">{{ formatPercent(focus.share) }}</div>
                  <div class="diagnostic-focus-hint">{{ focus.sample_hint || '--' }}</div>
                </div>
              </div>
            </div>

            <div class="diagnostic-summary-grid">
              <div v-for="item in firstBoardDiagnosticSummary" :key="item.reason" class="diagnostic-summary-card">
                <div class="diagnostic-summary-label">
                  <span>{{ item.label }}</span>
                  <el-tag :type="diagnosticReasonTagType(item.reason)" size="small" effect="light">{{ item.count }}只</el-tag>
                </div>
                <div class="diagnostic-summary-value">{{ item.count }}</div>
                <div class="diagnostic-summary-subtitle">被主筛选拦下的主因数量</div>
              </div>
            </div>

            <div class="diagnostic-groups">
              <div v-for="group in firstBoardDiagnosticGroups" :key="group.reason" class="diagnostic-group">
                <div class="diagnostic-group-head">
                  <div>
                    <div class="diagnostic-group-title">{{ group.label }}</div>
                    <div class="candidate-subtitle">共 {{ group.count }} 只，下面展示最值得复盘的样例</div>
                  </div>
                  <el-tag :type="diagnosticReasonTagType(group.reason)" size="small" effect="light">{{ group.count }}只</el-tag>
                </div>
                <div v-if="group.playbook?.length" class="diagnostic-playbook">
                  <div v-for="(tip, tipIndex) in group.playbook" :key="`${group.reason}-${tipIndex}`" class="diagnostic-playbook-item">
                    {{ tip }}
                  </div>
                </div>
                <el-table :data="group.examples || []" stripe size="small" empty-text="暂无样例" row-key="code">
                  <el-table-column label="个股" min-width="170">
                    <template #default="{ row }">
                      <div class="candidate-stock-cell">
                        <span class="candidate-stock-name" @click="$router.push(`/stocks/${row.code}`)">{{ row.name }}({{ row.code }})</span>
                      </div>
                    </template>
                  </el-table-column>
                  <el-table-column label="关键指标" width="190" align="center">
                    <template #default="{ row }">
                      <div class="metric-stack">
                        <strong>主线 {{ formatScore(row.sector_strength_score) }} / 记忆 {{ formatScore(row.memory_score) }}</strong>
                        <span>承接 {{ formatScore(row.support_strength_score) }} / 牛股 {{ formatScore(row.bull_score) }}</span>
                        <span>量比 {{ formatRatio(row.volume_ratio) }} / 涨幅 {{ formatSignedPercent(row.change_pct) }}</span>
                      </div>
                    </template>
                  </el-table-column>
                  <el-table-column label="当前线索" min-width="200" show-overflow-tooltip>
                    <template #default="{ row }">
                      {{ row.signal_summary || row.trigger_reason || row.sector_name || '-' }}
                    </template>
                  </el-table-column>
                  <el-table-column label="拦截说明" min-width="300" show-overflow-tooltip>
                    <template #default="{ row }">
                      <div class="metric-stack">
                        <strong>{{ row.primary_reason || '--' }}</strong>
                        <span>{{ (row.blockers || []).join(' / ') || '--' }}</span>
                      </div>
                    </template>
                  </el-table-column>
                  <el-table-column label="可操作建议" min-width="280" show-overflow-tooltip>
                    <template #default="{ row }">
                      <div class="metric-stack">
                        <strong>{{ row.next_threshold_hint || '--' }}</strong>
                        <span>{{ (row.actionable_suggestions || []).slice(1).join(' / ') || '先补最靠前的一项阈值即可' }}</span>
                      </div>
                    </template>
                  </el-table-column>
                </el-table>
              </div>
            </div>
          </template>
        </div>
      </div>

      <div class="section-block" data-testid="direction-research-panel">
        <div class="section-title-with-action">
          <div class="section-title">次日上涨研究榜Top12</div>
          <el-tag :type="directionResearch.status === 'available' ? 'info' : 'warning'" effect="light">
            {{ directionResearchStatusLabel }}
          </el-tag>
        </div>
        <div class="panel-card">
          <div class="learning-scope-note">
            仅研究观察，不是买入建议，不代表新模型已通过验证。按独立次日上涨概率排序，与涨停经验主榜独立；
            不替换原榜、不自动执行，Champion 与交易风控不变。80% 是目标，未承诺、未认证。
          </div>
          <div class="learning-directional-values">
            <span>研究目标 {{ formatPercent(directionResearch.target_precision) }}</span>
            <span>候选 {{ directionResearch.candidate_count ?? '--' }}</span>
            <span>合资格 {{ directionResearch.eligible_count ?? '--' }}</span>
            <span>缺方向概率 {{ directionResearch.missing_probability_count ?? '--' }}</span>
            <span>已选 {{ directionResearch.selected_count ?? '--' }} / {{ directionResearch.rank_limit ?? '--' }}</span>
            <span>版本 {{ directionResearch.version || '--' }} / {{ directionResearch.label_version || '--' }}</span>
          </div>
          <div v-if="directionResearch.reason" class="candidate-subtitle">{{ directionResearch.reason }}</div>
          <div v-for="(note, index) in directionResearchNotes" :key="index" class="candidate-subtitle">{{ note }}</div>
          <div class="candidate-subtitle">候选数不是时间外验证样本数；概率方法含“样本不足”时只作研究观察，不能据此宣称80%达标。</div>
          <div class="mobile-table-wrap">
            <el-table :data="directionResearchRows" stripe size="small" row-key="code"
              :empty-text="directionResearchEmptyText" data-testid="direction-research-table">
              <el-table-column label="研究序号" width="86" align="center">
                <template #default="{ row }">{{ row.probability_factors?.direction_research?.rank_position ?? '--' }}</template>
              </el-table-column>
              <el-table-column label="个股" min-width="170">
                <template #default="{ row }">
                  <span class="candidate-stock-name" @click="$router.push(`/stocks/${row.code}`)">{{ row.name || '--' }}({{ row.code }})</span>
                </template>
              </el-table-column>
              <el-table-column label="次日上涨概率" width="116" align="center">
                <template #default="{ row }">{{ formatProbability(row.direction_probability) }}</template>
              </el-table-column>
              <el-table-column label="次日首板涨停概率" width="144" align="center">
                <template #default="{ row }">{{ formatProbability(row.limit_up_probability ?? row.probability) }}</template>
              </el-table-column>
              <el-table-column label="方向概率方法 / 样本状态" min-width="280">
                <template #default="{ row }">{{ directionResearchMethodLabel(row) }}</template>
              </el-table-column>
            </el-table>
          </div>
        </div>
      </div>

      <div class="section-block">
        <div class="section-title section-title-with-action">
          <span>分赛道概率榜</span>
          <el-dropdown trigger="click" @command="handlePromotionExport" class="export-dropdown">
            <el-button size="small" type="primary" plain :disabled="!rankedFirstBoardCandidates.length && !rankedSecondBoardCandidates.length">
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
        <div class="candidate-grid">
          <div class="panel-card">
            <div class="candidate-panel-head">
              <div class="candidate-title">首板正式 Top12 重点短名单</div>
              <div class="candidate-subtitle">Top12 是精排审计短名单，不等于市场首板总数；预测概率与交易可执行性独立，未过盘口闸门仍不可交易</div>
            </div>
            <el-table data-testid="first-board-top12-table" :data="rankedFirstBoardCandidates" stripe size="small" empty-text="暂无首板观察标的" row-key="rank_key">
              <el-table-column label="排序" width="72" align="center">
                <template #default="{ $index }">{{ $index + 1 }}</template>
              </el-table-column>
              <el-table-column label="路由" width="112" align="center">
                <template #default="{ row }">
                  <el-tag :type="routeTagType(row.candidate_route)" size="small" effect="light">
                    {{ row.candidate_route_label || '--' }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="性质" width="100" align="center">
                <template #default="{ row }">
                  <el-tooltip :content="row.rank_fallback_reason || (row.prediction_watch_only ? '仅预测形态，不代表可以买' : '进入概率榜，仍需盘口确认')">
                    <el-tag :type="row.trade_actionability_status === 'confirmed_monitor' ? 'success' : row.prediction_watch_only ? 'info' : 'warning'" size="small" effect="light">
                      {{ row.trade_actionability_label || row.prediction_actionability_label || (row.prediction_watch_only ? '形态预测' : '条件观察') }}
                    </el-tag>
                  </el-tooltip>
                </template>
              </el-table-column>
              <el-table-column label="可执行分" width="86" align="center">
                <template #default="{ row }">{{ formatScore(row.trade_actionability_score) }}</template>
              </el-table-column>
              <el-table-column label="校准首板" width="102" align="center">
                <template #default="{ row }">
                  <span class="text-red">{{ formatProbability(row.limit_up_probability ?? row.probability) }}</span>
                </template>
              </el-table-column>
              <el-table-column label="时序/路由" width="126" align="center">
                <template #default="{ row }">
                  <div class="metric-stack">
                    <span>时序 {{ formatProbability(row.temporal_event_probability) }}</span>
                    <span>路由 {{ formatProbability(row.route_calibrated_probability) }}</span>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="历史启动分" width="104" align="center">
                <template #default="{ row }">{{ formatScore(row.historical_ignition_rank_score) }}</template>
              </el-table-column>
              <el-table-column label="窗口" width="104" align="center">
                <template #default="{ row }">
                  <el-tooltip :content="row.time_horizon_reason || '暂无说明'">
                    <el-tag :type="timeHorizonTagType(row.time_horizon)" size="small" effect="light">
                      {{ row.time_horizon_label || '--' }}
                    </el-tag>
                  </el-tooltip>
                </template>
              </el-table-column>
              <el-table-column label="方向/阶段概率" width="158" align="center">
                <template #default="{ row }">
                  <div class="metric-stack">
                    <span>次日上涨 {{ formatProbability(row.direction_probability ?? row.sub_probabilities?.next_day_rise) }}</span>
                    <span>次日强涨 {{ formatProbability(row.strong_rise_probability ?? row.sub_probabilities?.next_day_strong_rise) }}</span>
                    <span>3日突破 {{ formatProbability(row.sub_probabilities?.breakout_3d) }}</span>
                    <span>5日首板 {{ formatProbability(row.sub_probabilities?.first_limitup_5d || row.probability) }}</span>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="个股" min-width="170">
                <template #default="{ row }">
                  <div class="candidate-stock-cell">
                    <span class="candidate-stock-name" @click="$router.push(`/stocks/${row.code}`)">{{ row.name }}({{ row.code }})</span>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="首波记忆" width="110" align="center">
                <template #default="{ row }">
                  <div class="metric-stack">
                    <strong>{{ formatScore(row.memory_features?.memory_score) }}</strong>
                    <span>{{ formatMemorySummary(row.memory_features) }}</span>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="长周期画像" width="176" align="center">
                <template #default="{ row }">
                  <div class="metric-stack">
                    <strong>{{ formatLongCycleLabel(row) }}</strong>
                    <span>{{ formatLongCycleDetail(row) }}</span>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="启动前证据" width="190" align="center">
                <template #default="{ row }">
                  <el-tooltip :content="launchEvidenceTooltip(row)">
                    <div class="metric-stack">
                      <el-tag :type="launchEvidenceTagType(row)" size="small" effect="light">
                        {{ launchEvidenceLabel(row) }}
                      </el-tag>
                      <span>{{ row.primary_industry_name || '主营行业待补' }} / {{ formatScore(row.primary_industry_ignition_score) }}</span>
                      <span>3日资金 {{ formatSignedPercent(row.funding_main_inflow_pct_3d) }} / {{ row.funding_positive_days_3d ?? 0 }}日流入</span>
                    </div>
                  </el-tooltip>
                </template>
              </el-table-column>
              <el-table-column label="主线" min-width="130" show-overflow-tooltip>
                <template #default="{ row }">{{ row.sector_name || '-' }}</template>
              </el-table-column>
              <el-table-column label="依据" min-width="220" show-overflow-tooltip>
                <template #default="{ row }">{{ row.primary_reason || row.secondary_reason || row.signal_summary || '-' }}</template>
              </el-table-column>
            </el-table>

            <div class="candidate-panel-head recall-rank-head">
              <div class="candidate-title">首板 Top30 宽召回观察层</div>
              <div class="candidate-subtitle">前12名与正式短名单顺序完全一致，13-30名按无未来函数时序概率补充；宽召回只用于跟踪与复盘，不是买入清单</div>
            </div>
            <el-table data-testid="first-board-top30-table" :data="rankedFirstBoardRecallCandidates" stripe size="small" empty-text="暂无宽召回候选" row-key="rank_key">
              <el-table-column label="排序" width="64" align="center">
                <template #default="{ $index }">{{ $index + 1 }}</template>
              </el-table-column>
              <el-table-column label="层级" width="88" align="center">
                <template #default="{ row, $index }">
                  <el-tag :type="$index < 12 ? 'danger' : 'info'" size="small" effect="light">
                    {{ $index < 12 ? '正式' : '宽召回' }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="个股" min-width="160">
                <template #default="{ row }">
                  <span class="candidate-stock-name" @click="$router.push(`/stocks/${row.code}`)">{{ row.name }}({{ row.code }})</span>
                </template>
              </el-table-column>
              <el-table-column label="次日首板" width="96" align="center">
                <template #default="{ row }">{{ formatProbability(row.limit_up_probability ?? row.probability) }}</template>
              </el-table-column>
              <el-table-column label="时序概率" width="96" align="center">
                <template #default="{ row }">{{ formatProbability(row.temporal_event_probability) }}</template>
              </el-table-column>
              <el-table-column label="交易口径" width="132" align="center">
                <template #default="{ row }">
                  <el-tag :type="row.prediction_actionable ? 'success' : 'info'" size="small" effect="light">
                    {{ row.prediction_actionable ? '条件可监控' : '仅预测·不可交易' }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="sector_name" label="主线" min-width="130" show-overflow-tooltip />
            </el-table>
          </div>

          <div class="panel-card">
            <div class="candidate-panel-head">
              <div class="candidate-title">二板概率榜</div>
              <div class="candidate-subtitle">同口径比较次日晋级二板概率，只和首板池内晋级赛道比</div>
            </div>
            <el-table data-testid="second-board-table" :data="rankedSecondBoardCandidates" stripe size="small" empty-text="暂无二板概率榜" row-key="rank_key">
              <el-table-column label="排序" width="72" align="center">
                <template #default="{ $index }">{{ $index + 1 }}</template>
              </el-table-column>
              <el-table-column label="次日二板" width="102" align="center">
                <template #default="{ row }">
                  <span class="text-yellow">{{ formatProbability(row.limit_up_probability ?? row.probability) }}</span>
                </template>
              </el-table-column>
              <el-table-column label="个股" min-width="170">
                <template #default="{ row }">
                  <div class="candidate-stock-cell">
                    <span class="candidate-stock-name" @click="$router.push(`/stocks/${row.code}`)">{{ row.name }}({{ row.code }})</span>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="交易口径" width="118" align="center">
                <template #default="{ row }">
                  <el-tooltip :content="(row.relay_blockers || []).join(' / ') || '需等待次日盘口确认'">
                    <el-tag :type="row.relay_pool_ready ? 'success' : 'warning'" size="small" effect="light">
                      {{ row.relay_action_label || (row.relay_pool_ready ? '可交易监控' : '只预测·不可追') }}
                    </el-tag>
                  </el-tooltip>
                </template>
              </el-table-column>
              <el-table-column label="可执行分" width="86" align="center">
                <template #default="{ row }">{{ formatScore(row.trade_actionability_score) }}</template>
              </el-table-column>
              <el-table-column label="封板" width="88" align="center">
                <template #default="{ row }">{{ formatSealAmount(row.seal_amount) }}</template>
              </el-table-column>
              <el-table-column label="龙虎榜" min-width="180">
                <template #default="{ row }">
                  <el-tooltip :content="formatDragonTigerTooltip(row.dragon_tiger)">
                    <div class="metric-stack">
                      <el-tag :type="dragonTigerTagType(row.dragon_tiger)" size="small" effect="light">
                        {{ dragonTigerLabel(row.dragon_tiger) }}
                      </el-tag>
                      <span>{{ formatDragonTigerSummary(row.dragon_tiger) }}</span>
                    </div>
                  </el-tooltip>
                </template>
              </el-table-column>
              <el-table-column label="路由" width="110" align="center">
                <template #default="{ row }">
                  <el-tag :type="routeTagType(row.candidate_route)" size="small" effect="light">
                    {{ row.candidate_route_label || '--' }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="主线" min-width="130" show-overflow-tooltip>
                <template #default="{ row }">{{ row.sector_name || '-' }}</template>
              </el-table-column>
              <el-table-column label="依据" min-width="220" show-overflow-tooltip>
                <template #default="{ row }">{{ row.primary_reason || row.secondary_reason || '-' }}</template>
              </el-table-column>
            </el-table>
          </div>
        </div>
      </div>

      <div class="section-block">
        <div class="section-title">晋级概率查询</div>
        <div class="panel-card query-panel">
          <div class="query-row">
            <el-input v-model="queryCode" placeholder="输入股票代码" style="width: 200px" @keyup.enter="queryProbability" />
            <el-radio-group v-model="queryTarget" size="small">
              <el-radio-button :value="1">首板口径</el-radio-button>
              <el-radio-button :value="2">二板口径</el-radio-button>
            </el-radio-group>
            <el-button type="primary" @click="queryProbability" :loading="querying">查询</el-button>
          </div>

          <div v-if="promotionResult" class="result-card">
            <div class="result-row">
              <span>当前连板: <strong class="text-red">{{ promotionResult.current_days }}板</strong></span>
              <span>→ 晋级: <strong class="text-yellow">{{ promotionResult.next_days }}板</strong></span>
              <span>{{ promotionResult.main_probability_name || '晋级概率' }}: <strong :class="promotionResult.probability >= 0.5 ? 'text-red' : 'text-green'">{{ (promotionResult.probability * 100).toFixed(1) }}%</strong></span>
              <span>置信度: {{ promotionResult.confidence_label || '--' }}</span>
              <span v-if="promotionResult.candidate_route_label">路由: <el-tag :type="routeTagType(promotionResult.candidate_route)" size="small" effect="light">{{ promotionResult.candidate_route_label }}</el-tag></span>
              <span v-if="promotionResult.time_horizon_label">阶段: <el-tag :type="timeHorizonTagType(promotionResult.time_horizon)" size="small" effect="light">{{ promotionResult.time_horizon_label }}</el-tag></span>
              <span v-if="promotionResult.watch_bucket_label">梯队: <el-tag :type="watchBucketTagType(promotionResult.watch_bucket)" size="small" effect="light">{{ promotionResult.watch_bucket_label }}</el-tag></span>
              <span v-if="promotionResult.main_uptrend_label">主升浪阶段: <el-tag :type="mainUptrendTagType(promotionResult.main_uptrend_label)" size="small" effect="light">{{ promotionResult.main_uptrend_label }}</el-tag></span>
            </div>
            <el-progress :percentage="promotionResult.probability * 100" :color="promotionResult.probability >= 0.5 ? '#ef4444' : '#22c55e'" :stroke-width="12" />
            <div v-if="resultStageCards.length" class="result-section">
              <div class="result-section-title">分阶段概率</div>
              <div class="result-grid">
                <div v-for="item in resultStageCards" :key="item.key" class="result-metric-card">
                  <div class="result-metric-label">{{ item.label }}</div>
                  <div class="result-metric-value">{{ item.value }}</div>
                </div>
              </div>
            </div>
            <div v-if="resultMemoryCards.length" class="result-section">
              <div class="result-section-title">首波记忆</div>
              <div class="result-grid">
                <div v-for="item in resultMemoryCards" :key="item.key" class="result-metric-card">
                  <div class="result-metric-label">{{ item.label }}</div>
                  <div class="result-metric-value">{{ item.value }}</div>
                </div>
              </div>
            </div>
            <div v-if="resultDragonTigerCards.length" class="result-section">
              <div class="result-section-title">龙虎榜成员</div>
              <div class="result-grid">
                <div v-for="item in resultDragonTigerCards" :key="item.key" class="result-metric-card">
                  <div class="result-metric-label">{{ item.label }}</div>
                  <div class="result-metric-value">{{ item.value }}</div>
                </div>
              </div>
              <div v-if="resultDragonTigerMembers.length" class="factors-list">
                <div v-for="item in resultDragonTigerMembers" :key="item.key" class="factor-item">
                  <span class="factor-name">{{ item.label }}</span>
                  <span class="factor-val">{{ item.value }}</span>
                </div>
              </div>
            </div>
            <div v-if="promotionResult.next_threshold_hint" class="result-section">
              <div class="result-section-title">差哪一口气</div>
              <div class="result-grid">
                <div class="result-metric-card">
                  <div class="result-metric-label">{{ promotionResult.support_squeeze_gap_label || '临门一脚' }}</div>
                  <div class="result-metric-value">{{ promotionResult.next_threshold_hint }}</div>
                </div>
              </div>
              <div v-if="(promotionResult.actionable_suggestions || []).length > 1" class="factors-list">
                <div
                  v-for="(tip, index) in (promotionResult.actionable_suggestions || []).slice(1)"
                  :key="`support-tip-${index}`"
                  class="factor-item"
                >
                  <span class="factor-name">补充提示</span>
                  <span class="factor-val">{{ tip }}</span>
                </div>
              </div>
            </div>
            <div v-if="promotionResult.factors" class="factors-list">
              <div v-for="(val, key) in promotionResult.factors" :key="key" class="factor-item">
                <span class="factor-name">{{ key }}</span>
                <span class="factor-val">{{ typeof val === 'number' ? val.toFixed(3) : val }}</span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { defineAsyncComponent, ref, computed, onMounted } from 'vue'
const VChart = defineAsyncComponent(() => import('vue-echarts'))
import { Download, TrendCharts } from '@element-plus/icons-vue'
import { ensureBarChartsRegistered } from '@/composables/echarts/bar'
import { getPromotionLadder, getBoardHeight, getPromotionCandidates, getPromotionLearningReview, getPromotionProbability } from '@/api'
import { notifySuccess } from '@/utils/message'

ensureBarChartsRegistered()

const ladderData = ref([])
const boardHeight = ref({})
const promotionCandidates = ref({})
const learningReview = ref({})
const queryCode = ref('')
const queryTarget = ref(1)
const querying = ref(false)
const promotionResult = ref(null)

const marketLadderCount = (days) => Number(
  (boardHeight.value.ladder_summary || []).find(item => Number(item.days) === days)?.count || 0,
)
const marketFirstBoardCount = computed(() => marketLadderCount(1))
const marketSecondBoardCount = computed(() => marketLadderCount(2))
const marketHigherBoardCount = computed(() => Math.max(
  Number(boardHeight.value.limit_up_count || 0) - marketFirstBoardCount.value - marketSecondBoardCount.value,
  0,
))
const sealRateLabel = computed(() => (
  boardHeight.value.seal_rate_method === 'historical_pool'
    ? '零炸板占比'
    : boardHeight.value.seal_rate_method === 'live_price_at_limit'
      ? '实时封板率'
      : '封板率'
))
const boardScopeSummary = computed(() => {
  if (!boardHeight.value.trade_date || boardHeight.value.limit_up_count == null) return ''
  return [
    `统计日 ${boardHeight.value.trade_date}`,
    `全市场涨停池：首板 ${marketFirstBoardCount.value} + 二板 ${marketSecondBoardCount.value} + 三板及以上 ${marketHigherBoardCount.value} = ${Number(boardHeight.value.limit_up_count)}`,
    '该口径过滤 ST/停牌/退市，但包含创业板、科创板、北交所等仅观察标的；下方复盘只考核可交易主板首/二板',
  ].join('；')
})

const firstBoardCandidates = computed(() => promotionCandidates.value.first_board_candidates || [])
const firstBoardPreSprintCandidates = computed(() => promotionCandidates.value.first_board_pre_sprint_candidates || [])
const firstBoardWatchCandidates = computed(() => promotionCandidates.value.first_board_watch_candidates || [])
const firstBoardWeakWatchCandidates = computed(() => promotionCandidates.value.first_board_weak_watch_candidates || [])
const limitUpPlatformCandidates = computed(() => promotionCandidates.value.limit_up_platform_candidates || [])
const limitUpPlatformOverview = computed(() => promotionCandidates.value.limit_up_platform_overview || {})
const limitUpPlatformDiagnostics = computed(() => promotionCandidates.value.limit_up_platform_diagnostics || {})
const limitUpPlatformDiagnosticsOverview = computed(() => limitUpPlatformDiagnostics.value.overview || {})
const limitUpPlatformDiagnosticSummary = computed(() => limitUpPlatformDiagnostics.value.reason_summary || [])
const limitUpPlatformDiagnosticGroups = computed(() => limitUpPlatformDiagnostics.value.blocked_reason_groups || [])
const firstBoardWatchOverview = computed(() => promotionCandidates.value.first_board_watch_overview || {})
const firstBoardPreSprintOverview = computed(() => promotionCandidates.value.first_board_pre_sprint_overview || {})
const firstBoardPreSprintGroups = computed(() => {
  const groups = promotionCandidates.value.first_board_pre_sprint_groups
  if (Array.isArray(groups) && groups.length) return groups
  if (!firstBoardPreSprintCandidates.value.length) return []
  return [
    {
      bucket: 'quiet_setup',
      label: '准冲刺',
      description: '已经靠近 1-2 日冲刺边缘，但涨幅和盘口确认还差一口气。',
      count: firstBoardPreSprintCandidates.value.length,
      main_uptrend_ready_count: firstBoardPreSprintCandidates.value.filter(item => item?.is_main_uptrend_ready).length,
      examples: firstBoardPreSprintCandidates.value,
    },
  ]
})
const firstBoardWatchGroups = computed(() => {
  const groups = promotionCandidates.value.first_board_watch_groups
  if (Array.isArray(groups) && groups.length) return groups
  if (!firstBoardWatchCandidates.value.length) return []
  return [
    {
      bucket: 'other',
      label: '首板预测梯队',
      description: '未进 1-2 日冲刺主池，但仍保留在首板预测层继续跟踪。',
      count: firstBoardWatchCandidates.value.length,
      main_uptrend_ready_count: firstBoardWatchCandidates.value.filter(item => item?.is_main_uptrend_ready).length,
      examples: firstBoardWatchCandidates.value,
    },
  ]
})
const firstBoardWeakWatchOverview = computed(() => promotionCandidates.value.first_board_weak_watch_overview || {})
const firstBoardWeakWatchGroups = computed(() => {
  const groups = promotionCandidates.value.first_board_weak_watch_groups
  if (Array.isArray(groups) && groups.length) return groups
  if (!firstBoardWeakWatchCandidates.value.length) return []
  return [
    {
      bucket: 'other',
      label: '弱观察',
      description: '当天收绿，但支撑/平台逻辑还没完全走坏，先单独放在弱观察层。',
      count: firstBoardWeakWatchCandidates.value.length,
      main_uptrend_ready_count: firstBoardWeakWatchCandidates.value.filter(item => item?.is_main_uptrend_ready).length,
      examples: firstBoardWeakWatchCandidates.value,
    },
  ]
})
const secondBoardCandidates = computed(() => (
  promotionCandidates.value.second_board_candidates
  || promotionCandidates.value.ranked_second_board_candidates
  || []
))
const predictionHealth = computed(() => promotionCandidates.value.prediction_health || {})
const modelRuntimeMode = computed(() => promotionCandidates.value?.model_runtime?.runtime_mode || 'legacy')
const modelRuntimeLabel = computed(() => ({
  legacy: 'Champion 生产',
  shadow: 'Champion + Shadow',
  compare: 'Champion 对比模式',
}[modelRuntimeMode.value] || `运行模式 ${modelRuntimeMode.value}`))
const modelRuntimeTagType = computed(() => (
  modelRuntimeMode.value === 'legacy' ? 'success' : modelRuntimeMode.value === 'shadow' ? 'warning' : 'info'
))
const auctionHealth = computed(() => predictionHealth.value.auction_health || {})
const snapshotContextCount = computed(() => {
  const dates = predictionHealth.value.snapshot_context_counts || {}
  return Object.values(dates).reduce((total, targets) => (
    total + Object.values(targets || {}).reduce((subtotal, contexts) => (
      subtotal + Object.keys(contexts || {}).length
    ), 0)
  ), 0)
})
const directionResearch = computed(() => promotionCandidates.value.direction_research || {})
const directionResearchRows = computed(() => (
  directionResearch.value.scope === 'research_only'
  && ['available', 'insufficient_candidates'].includes(directionResearch.value.status)
  && Array.isArray(directionResearch.value.candidates)
    ? directionResearch.value.candidates : []
))
const directionResearchNotes = computed(() => (
  Array.isArray(directionResearch.value.notes) ? directionResearch.value.notes.filter(note => typeof note === 'string') : []
))
const directionResearchStatusLabel = computed(() => ({
  available: '研究榜可用 · 非认证',
  blocked: '研究榜阻塞 · 数据不足',
  insufficient_candidates: '候选不足 · 不补齐旧榜',
}[directionResearch.value.status] || '等待新冻结批次'))
const directionResearchEmptyText = computed(() => (
  directionResearch.value.status === 'blocked' ? '方向研究证据不足，研究榜阻塞'
    : directionResearch.value.status === 'insufficient_candidates' ? '候选不足，不用涨停榜补位'
      : '等待新冻结批次，不补旧榜'
))
const learningLatest = computed(() => learningReview.value.latest || {})
const learningDirectionalSummaries = computed(() => [
  { key: 'latest', label: '最新复盘', metrics: learningLatest.value },
  { key: 'aggregate', label: `近 ${learningReview.value.lookback_days || 10} 日汇总`, metrics: learningReview.value.aggregate || {} },
])
const learningFirstBoard = computed(() => learningLatest.value?.lane_metrics?.target_1 || {})
const learningSecondBoard = computed(() => learningLatest.value?.lane_metrics?.target_2 || {})
const learningRecommendation = computed(() => learningReview.value.recommendation || {})
const learningReviewScopeNote = computed(() => {
  if (!learningLatest.value.actual_trade_date) return ''
  const notes = [
    learningLatest.value.evaluation_scope_warning,
    '“预测记录完整”只表示上一交易日收盘正式榜已独立持久化且数量达标，与上方竞价成交字段是否完整无关',
  ]
  const reviewedModel = String(learningLatest.value.prediction_model_version || '').trim()
  const currentModel = String(promotionCandidates.value.prediction_model_version || '').trim()
  if (reviewedModel && currentModel && reviewedModel !== currentModel) {
    notes.push(`本次成绩来自 ${reviewedModel}；当前候选模型 ${currentModel} 尚未产生下一交易日结果，禁止回填本次成绩`)
  } else if (reviewedModel) {
    notes.push(`本次复盘模型为 ${reviewedModel}`)
  }
  return notes.filter(Boolean).join('；')
})
const learningDailyRows = computed(() => learningReview.value.daily || [])
const learningLaneRows = computed(() => Object.values(learningLatest.value.lane_metrics || {}))
const learningLaunchCohortRows = computed(() => Object.entries(
  learningReview.value?.aggregate?.launch_precursor_metrics || {},
).map(([cohort, item]) => ({
  cohort,
  ...(item || {}),
})).filter(item => Number(item.sample_count || 0) > 0))
const learningMissedExamples = computed(() => (learningLatest.value.missed_examples || []).slice(0, 8))
const firstBoardDiagnostics = computed(() => promotionCandidates.value.first_board_diagnostics || {})
const firstBoardDiagnosticsOverview = computed(() => firstBoardDiagnostics.value.overview || {})
const firstBoardDiagnosticSummary = computed(() => firstBoardDiagnostics.value.reason_summary || [])
const firstBoardDiagnosticGroups = computed(() => firstBoardDiagnostics.value.blocked_reason_groups || [])
const firstBoardOverviewFocuses = computed(() => firstBoardDiagnosticsOverview.value.top_focuses || [])
const rankedFirstBoardCandidates = computed(() => (
  (promotionCandidates.value.ranked_first_board_candidates || firstBoardCandidates.value).map((item, index) => ({
    ...item,
    rank_key: `first-${item.code || 'unknown'}-${item.target_board || 0}-${index}`,
  }))
))
const rankedFirstBoardRecallCandidates = computed(() => (
  (promotionCandidates.value.ranked_first_board_recall_candidates || rankedFirstBoardCandidates.value).map((item, index) => ({
    ...item,
    rank_key: `first-recall-${item.code || 'unknown'}-${item.target_board || 0}-${index}`,
  }))
))
const rankedSecondBoardCandidates = computed(() => (
  (promotionCandidates.value.ranked_second_board_candidates || secondBoardCandidates.value).map((item, index) => ({
    ...item,
    rank_key: `second-${item.code || 'unknown'}-${item.target_board || 0}-${index}`,
  }))
))
const resultStageCards = computed(() => buildStageCards(promotionResult.value?.sub_probabilities))
const resultMemoryCards = computed(() => buildMemoryCards(promotionResult.value?.memory_features))
const resultDragonTigerCards = computed(() => buildDragonTigerCards(promotionResult.value?.dragon_tiger))
const resultDragonTigerMembers = computed(() => buildDragonTigerMembers(promotionResult.value?.dragon_tiger))

const PROMOTION_TRACK_EXPORT_COLUMNS = [
  { key: 'rank', label: '排名', width: 8 },
  { key: 'probability_track_label', label: '赛道', width: 12 },
  { key: 'main_probability_name', label: '概率口径', width: 16 },
  { key: 'candidate_route_label', label: '路由', width: 14 },
  { key: 'watch_bucket_label', label: '梯队分层', width: 14 },
  { key: 'main_uptrend_label', label: '主升浪阶段', width: 14 },
  { key: 'main_uptrend_score', label: '主升浪分', width: 12, format: v => v != null ? Number(v).toFixed(1) : '--' },
  { key: 'probability_pct', label: '主概率', width: 10 },
  { key: 'next_day_rise_pct', label: '次日上涨', width: 10 },
  { key: 'next_day_strong_rise_pct', label: '次日强涨', width: 10 },
  { key: 'breakout_3d_pct', label: '3日突破', width: 10 },
  { key: 'first_limitup_next_day_pct', label: '次日首板', width: 10 },
  { key: 'first_limitup_5d_pct', label: '5日首板', width: 10 },
  { key: 'become_core_10d_pct', label: '10日核心', width: 10 },
  { key: 'next_second_board_pct', label: '次日二板', width: 10 },
  { key: 'dragon_tiger_summary', label: '龙虎榜摘要', width: 28 },
  { key: 'dragon_tiger_delta_pct', label: '龙虎榜修正', width: 12 },
  { key: 'dragon_tiger_top_buyers', label: '买方席位', width: 32 },
  { key: 'route_score', label: '路由分', width: 10, format: v => v != null ? Number(v).toFixed(1) : '--' },
  { key: 'memory_score', label: '首波记忆', width: 10, format: v => v != null ? Number(v).toFixed(1) : '--' },
  { key: 'memory_summary', label: '记忆摘要', width: 18 },
  { key: 'long_cycle_summary', label: '长周期画像', width: 24 },
  { key: 'launch_evidence_label', label: '启动前证据', width: 18 },
  { key: 'primary_industry_name', label: '主营行业', width: 18 },
  { key: 'primary_industry_ignition_score', label: '行业点火分', width: 12, format: v => v != null ? Number(v).toFixed(1) : '--' },
  { key: 'funding_main_inflow_pct_3d', label: '3日主力占比', width: 12, format: v => v != null ? `${Number(v).toFixed(2)}%` : '--' },
  { key: 'funding_positive_days_3d', label: '3日正流入天数', width: 12 },
  { key: 'signal_status_label', label: '状态', width: 10 },
  { key: 'time_horizon_label', label: '时间窗口', width: 12 },
  { key: 'confidence_label', label: '置信度', width: 10 },
  { key: 'code', label: '代码', width: 12 },
  { key: 'name', label: '名称', width: 14 },
  { key: 'current_price', label: '最新价', width: 10, format: v => v != null && v !== '' ? Number(v).toFixed(2) : '--' },
  { key: 'change_pct', label: '涨幅', width: 10, format: v => v != null ? `${Number(v).toFixed(2)}%` : '--' },
  { key: 'sector_name', label: '主线', width: 18 },
  { key: 'primary_reason', label: '主要依据', width: 28 },
  { key: 'secondary_reason', label: '补充依据', width: 34 },
  { key: 'latest_as_of', label: '最新时间', width: 20 },
]

const PROMOTION_FIRST_BOARD_EXPORT_COLUMNS = [
  { key: 'rank', label: '排名', width: 8 },
  { key: 'signal_status_label', label: '状态', width: 10 },
  { key: 'time_horizon_label', label: '时间窗口', width: 12 },
  { key: 'probability_pct', label: '涨停概率', width: 10 },
  { key: 'direction_probability_pct', label: '上涨概率', width: 10 },
  { key: 'trade_actionability_label', label: '交易可执行性', width: 16 },
  { key: 'trade_actionability_score', label: '可执行分', width: 10, format: v => v != null ? Number(v).toFixed(1) : '--' },
  { key: 'code', label: '代码', width: 12 },
  { key: 'name', label: '名称', width: 14 },
  { key: 'sector_name', label: '主线', width: 18 },
  { key: 'candidate_route_label', label: '路由', width: 14 },
  { key: 'watch_bucket_label', label: '梯队分层', width: 14 },
  { key: 'main_uptrend_label', label: '主升浪阶段', width: 14 },
  { key: 'main_uptrend_score', label: '主升浪分', width: 12, format: v => v != null ? Number(v).toFixed(1) : '--' },
  { key: 'memory_score', label: '首波记忆', width: 10, format: v => v != null ? Number(v).toFixed(1) : '--' },
  { key: 'breakout_3d_pct', label: '3日突破', width: 10 },
  { key: 'first_limitup_5d_pct', label: '5日首板', width: 10 },
  { key: 'setup_grade_display', label: '执行等级', width: 16 },
  { key: 'signal_summary', label: '异动摘要', width: 20 },
  { key: 'primary_reason', label: '主要依据', width: 28 },
  { key: 'secondary_reason', label: '补充依据', width: 34 },
  { key: 'signal_status_reason', label: '状态说明', width: 26 },
  { key: 'time_horizon_reason', label: '时间窗口说明', width: 24 },
  { key: 'kline_confirmation_summary', label: 'K线确认', width: 30 },
  { key: 'change_pct', label: '涨幅', width: 10, format: v => v != null ? `${Number(v).toFixed(2)}%` : '--' },
  { key: 'turnover', label: '换手率', width: 10, format: v => v != null ? `${Number(v).toFixed(2)}%` : '--' },
  { key: 'volume_ratio', label: '量比', width: 10, format: v => v != null ? Number(v).toFixed(2) : '--' },
  { key: 'main_net_inflow', label: '主力净额', width: 12, format: v => v != null ? `${(Number(v) / 1e8).toFixed(2)}亿` : '--' },
  { key: 'launch_evidence_label', label: '启动前证据', width: 18 },
  { key: 'primary_industry_name', label: '主营行业', width: 18 },
  { key: 'funding_main_inflow_pct_3d', label: '3日主力占比', width: 12, format: v => v != null ? `${Number(v).toFixed(2)}%` : '--' },
  { key: 'latest_as_of', label: '最新时间', width: 20 },
]

const PROMOTION_SECOND_BOARD_EXPORT_COLUMNS = [
  { key: 'rank', label: '排名', width: 8 },
  { key: 'signal_status_label', label: '状态', width: 10 },
  { key: 'probability_pct', label: '涨停概率', width: 10 },
  { key: 'direction_probability_pct', label: '上涨概率', width: 10 },
  { key: 'trade_actionability_label', label: '交易可执行性', width: 16 },
  { key: 'trade_actionability_score', label: '可执行分', width: 10, format: v => v != null ? Number(v).toFixed(1) : '--' },
  { key: 'code', label: '代码', width: 12 },
  { key: 'name', label: '名称', width: 14 },
  { key: 'sector_name', label: '主线', width: 18 },
  { key: 'primary_reason', label: '晋级依据', width: 28 },
  { key: 'secondary_reason', label: '补充说明', width: 28 },
  { key: 'dragon_tiger_summary', label: '龙虎榜摘要', width: 28 },
  { key: 'dragon_tiger_delta_pct', label: '龙虎榜修正', width: 12 },
  { key: 'dragon_tiger_top_buyers', label: '买方席位', width: 32 },
  { key: 'dragon_tiger_top_sellers', label: '卖方席位', width: 32 },
  { key: 'signal_status_reason', label: '状态说明', width: 22 },
  { key: 'seal_amount', label: '封板资金', width: 12, format: v => v != null ? `${(Number(v) / 1e8).toFixed(2)}亿` : '--' },
  { key: 'break_count', label: '炸板次数', width: 10 },
  { key: 'turnover', label: '换手率', width: 10, format: v => v != null ? `${Number(v).toFixed(2)}%` : '--' },
  { key: 'sector_strength_score', label: '板块强度', width: 10, format: v => v != null ? Number(v).toFixed(1) : '--' },
  { key: 'sector_consecutive_days', label: '板块活跃天数', width: 12 },
  { key: 'limit_up_time', label: '封板时间', width: 12 },
  { key: 'latest_as_of', label: '交易日', width: 14 },
]

const PROMOTION_WATCH_EXPORT_COLUMNS = [
  { key: 'rank', label: '排名', width: 8 },
  { key: 'signal_status_label', label: '状态', width: 10 },
  { key: 'time_horizon_label', label: '时间窗口', width: 12 },
  { key: 'probability_pct', label: '涨停概率', width: 10 },
  { key: 'direction_probability_pct', label: '上涨概率', width: 10 },
  { key: 'trade_actionability_label', label: '交易可执行性', width: 16 },
  { key: 'trade_actionability_score', label: '可执行分', width: 10, format: v => v != null ? Number(v).toFixed(1) : '--' },
  { key: 'code', label: '代码', width: 12 },
  { key: 'name', label: '名称', width: 14 },
  { key: 'sector_name', label: '主线', width: 18 },
  { key: 'candidate_route_label', label: '路由', width: 14 },
  { key: 'watch_bucket_label', label: '梯队分层', width: 14 },
  { key: 'main_uptrend_label', label: '主升浪阶段', width: 14 },
  { key: 'main_uptrend_score', label: '主升浪分', width: 12, format: v => v != null ? Number(v).toFixed(1) : '--' },
  { key: 'memory_score', label: '首波记忆', width: 10, format: v => v != null ? Number(v).toFixed(1) : '--' },
  { key: 'breakout_3d_pct', label: '3日突破', width: 10 },
  { key: 'first_limitup_5d_pct', label: '5日首板', width: 10 },
  { key: 'primary_reason', label: '主要依据', width: 24 },
  { key: 'secondary_reason', label: '补充依据', width: 32 },
  { key: 'time_horizon_reason', label: '观察说明', width: 26 },
  { key: 'kline_confirmation_summary', label: 'K线确认', width: 30 },
  { key: 'change_pct', label: '涨幅', width: 10, format: v => v != null ? `${Number(v).toFixed(2)}%` : '--' },
  { key: 'turnover', label: '换手率', width: 10, format: v => v != null ? `${Number(v).toFixed(2)}%` : '--' },
  { key: 'volume_ratio', label: '量比', width: 10, format: v => v != null ? Number(v).toFixed(2) : '--' },
  { key: 'latest_as_of', label: '最新时间', width: 20 },
]

const PROMOTION_LIMIT_UP_PLATFORM_EXPORT_COLUMNS = [
  { key: 'rank', label: '排名', width: 8 },
  { key: 'signal_status_label', label: '状态', width: 10 },
  { key: 'time_horizon_label', label: '时间窗口', width: 12 },
  { key: 'limit_up_nearby_pattern_label', label: '形态标签', width: 18 },
  { key: 'limit_up_anchor_gap_pct_text', label: '距板锚点', width: 12 },
  { key: 'probability_pct', label: '首板观察参照概率', width: 14 },
  { key: 'code', label: '代码', width: 12 },
  { key: 'name', label: '名称', width: 14 },
  { key: 'sector_name', label: '主线', width: 18 },
  { key: 'candidate_route_label', label: '路由', width: 14 },
  { key: 'memory_score', label: '首波记忆', width: 10, format: v => v != null ? Number(v).toFixed(1) : '--' },
  { key: 'breakout_3d_pct', label: '3日突破', width: 10 },
  { key: 'first_limitup_5d_pct', label: '5日首板', width: 10 },
  { key: 'primary_reason', label: '主要依据', width: 24 },
  { key: 'secondary_reason', label: '补充依据', width: 32 },
  { key: 'latest_as_of', label: '最新时间', width: 20 },
]

const PROMOTION_SUMMARY_COLUMNS = [
  { key: 'item', label: '项目', width: 18 },
  { key: 'value', label: '值', width: 24 },
]

const ladderChartOption = computed(() => {
  const items = [...ladderData.value].reverse()
  if (!items.length) return {}
  return {
    backgroundColor: 'transparent',
    tooltip: { trigger: 'axis' },
    grid: { left: 60, right: 40, top: 10, bottom: 30 },
    xAxis: { type: 'category', data: items.map(i => `${i.consecutive_days}板`), axisLine: { lineStyle: { color: '#e8ebf0' } }, axisLabel: { color: '#667085' } },
    yAxis: { type: 'value', axisLine: { lineStyle: { color: '#e8ebf0' } }, axisLabel: { color: '#667085' }, splitLine: { lineStyle: { color: '#f0f2f5' } } },
    series: [{
      type: 'bar',
      data: items.map(i => ({
        value: i.count,
        itemStyle: { color: i.seal_rate >= 80 ? '#ef4444' : i.seal_rate >= 50 ? '#f59e0b' : '#22c55e' },
      })),
      barWidth: 30,
      label: { show: true, position: 'top', color: '#667085' },
    }],
  }
})

function directionResearchMethodLabel(row) {
  const method = row.probability_factors?.direction_research?.probability_method
  if (!method || method === 'unknown') return '-- / 概率方法未知，样本充分性未认证'
  return String(method).includes('insufficient_sample')
    ? `${method} / 样本不足，仅供观察`
    : `${method} / 非新模型验证结论`
}

function formatProbability(value) {
  return formatPercent(value)
}

function formatPercent(value) {
  if (value == null || typeof value === 'boolean' || String(value).trim() === '') return '--'
  const numeric = Number(value)
  if (!Number.isFinite(numeric) || numeric < 0 || numeric > 1) return '--'
  return `${(numeric * 100).toFixed(1)}%`
}

function formatDirectionalPrecision(metrics = {}) {
  // Overall quality may be partial for limit-up labels while direction is complete.
  // Legacy rates remain visible without inventing missing coverage or target flags.
  if (Number(metrics.directional_unknown_count) > 0
    || (metrics.directional_coverage != null && Number(metrics.directional_coverage) < 1)) return '--'
  return formatPercent(metrics.directional_precision)
}

function formatDirectionalBounds(metrics = {}) {
  const lower = formatPercent(metrics.directional_precision_lower_bound)
  const upper = formatPercent(metrics.directional_precision_upper_bound)
  return lower === '--' || upper === '--' ? '--' : `${lower} ～ ${upper}`
}

function directionalTargetLabel(metrics = {}) {
  if (metrics.directional_target_met === true) return '本样本达到目标（非认证）'
  if (metrics.directional_target_met === false) return '本样本未达目标'
  return '数据不足 · 达标未知'
}

function directionalTargetType(metrics = {}) {
  if (metrics.directional_target_met === true) return 'success'
  if (metrics.directional_target_met === false) return 'warning'
  return 'info'
}

function reviewEvaluationLabel(metrics = {}) {
  if (metrics.evaluation_status === 'partial') return '部分可评价'
  if (metrics.evaluation_status === 'unavailable') return '数据不足 / 不可评价'
  return metrics.evaluation_status || '评价状态 --'
}

function reviewEvaluationReasons(metrics = {}) {
  const reasons = metrics.evaluation_reasons
  if (Array.isArray(reasons)) return reasons.filter(item => typeof item === 'string').join('；')
  return typeof reasons === 'string' ? reasons : ''
}

function formatLift(value) {
  const numeric = Number(value)
  if (!Number.isFinite(numeric) || numeric <= 0) return '--'
  return `${numeric.toFixed(2)}×`
}

function learningReviewTagType(status) {
  if (status === 'stable_calibration') return 'success'
  if (status === 'insufficient_sample') return 'info'
  if (status === 'repair_signal_quality') return 'danger'
  return 'warning'
}

function learningReviewAlertType(status) {
  if (status === 'stable_calibration') return 'success'
  if (status === 'insufficient_sample') return 'info'
  if (status === 'repair_signal_quality') return 'error'
  return 'warning'
}

function learningMissReasonLabel(status) {
  const labels = {
    not_in_pool: '未进入预测池',
    score_low: '入池但分数偏低',
    rank_cutoff: '入池但被排序挤掉',
    snapshot_incomplete: '预测快照不完整',
    filtered: '非主板/风控过滤',
  }
  return labels[status] || status || '--'
}

function formatSignedPercent(value) {
  const numeric = Number(value || 0)
  if (!Number.isFinite(numeric)) return '--'
  return `${numeric >= 0 ? '+' : ''}${numeric.toFixed(1)}%`
}

function formatPct(value) {
  if (value === null || value === undefined || value === '') return '--'
  const numeric = Number(value)
  if (!Number.isFinite(numeric)) return '--'
  return `${numeric > 0 ? '+' : ''}${numeric.toFixed(2)}%`
}

function formatPrice(value) {
  const numeric = Number(value || 0)
  if (!numeric) return '--'
  return numeric.toFixed(2)
}

function formatScore(value) {
  const numeric = Number(value)
  if (!Number.isFinite(numeric)) return '--'
  return numeric.toFixed(1)
}

function formatRatio(value) {
  const numeric = Number(value)
  if (!Number.isFinite(numeric)) return '--'
  return numeric.toFixed(2)
}

function formatSealAmount(value) {
  const numeric = Number(value || 0)
  if (!numeric) return '--'
  return `${(numeric / 1e8).toFixed(1)}亿`
}

function formatAnchorGap(value) {
  const numeric = Number(value)
  if (!Number.isFinite(numeric)) return '--'
  return `${numeric.toFixed(1)}%`
}

function signalStatusTagType(status) {
  return status === 'close_confirmed' ? 'success' : 'warning'
}

function routeTagType(route) {
  if (route === 'platform_relaunch') return 'danger'
  if (route === 'support_squeeze_start') return 'warning'
  if (route === 'hot_primary') return 'warning'
  if (route === 'relay_fillup') return 'warning'
  if (route === 'quiet_setup') return 'info'
  return 'success'
}

function watchBucketTagType(bucket) {
  if (bucket === 'platform_relaunch') return 'danger'
  if (bucket === 'support_squeeze_watch') return 'warning'
  if (bucket === 'support_squeeze_start') return 'warning'
  if (bucket === 'mainline_relay') return 'warning'
  if (bucket === 'quiet_setup') return 'info'
  return 'success'
}

function timeHorizonTagType(horizon) {
  if (horizon === 'sprint') return 'danger'
  if (horizon === 'pre_sprint') return 'warning'
  return 'info'
}

function mainUptrendTagType(label) {
  if (label === '主升浪预备') return 'danger'
  if (label === '右侧待确认') return 'warning'
  return 'info'
}

function diagnosticReasonTagType(reason) {
  if (reason === '主线强度不足') return 'danger'
  if (reason === '缺少首波记忆') return 'warning'
  if (reason === '量能不够') return 'warning'
  return 'info'
}

function limitUpPlatformDiagnosticTagType(reason) {
  if (reason === '离板锚点仍偏远') return 'danger'
  if (reason === '还差十字星确认') return 'warning'
  if (reason === '还差量窒息') return 'info'
  return 'info'
}

function formatMemorySummary(memoryFeatures) {
  const features = memoryFeatures || {}
  const maxBoard = Number(features.memory_max_board_50d || 0)
  const hits = Number(features.memory_limit_up_hits_50d || 0)
  const daysSinceLast = Number(features.memory_days_since_last_limit_up || 0)
  if (!maxBoard && !hits) return '暂无记忆'
  return `${maxBoard}板/${hits}次/${daysSinceLast}天`
}

function resolveLongCycleProfile(row) {
  return row?.kline_confirmation?.long_cycle_profile || {}
}

function formatLongCycleLabel(row) {
  const profile = resolveLongCycleProfile(row)
  return profile.long_cycle_regime_label || '历史画像待补齐'
}

function formatLongCycleDetail(row) {
  const profile = resolveLongCycleProfile(row)
  if (!profile.long_cycle_regime_label) return '仅按短周期结构'
  const score = Number(profile.quality_setup_score || 0).toFixed(0)
  const position = Number(profile.position_250)
  const positionText = Number.isFinite(position) ? `${(position * 100).toFixed(0)}%` : '--'
  return `${score}分 / 250位${positionText} / ${profile.setup_phase_label || '待确认'}`
}

function launchEvidenceLabel(row) {
  if (row?.low_base_sector_ignition_confirmed) return '行业+资金共振'
  if (row?.low_base_sector_ignition_ready) return '低位行业点火'
  if (row?.funding_preheat_ready) return '三日资金预热'
  if (row?.kline_confirmation?.launch_profile_ready) return '中低位修复'
  return '暂无组合确认'
}

function launchEvidenceTagType(row) {
  if (row?.low_base_sector_ignition_confirmed) return 'danger'
  if (row?.low_base_sector_ignition_ready) return 'warning'
  if (row?.funding_preheat_ready) return 'success'
  return 'info'
}

function launchEvidenceTooltip(row) {
  const kline = row?.kline_confirmation || {}
  const position = Number(kline.launch_position_120)
  const positionText = Number.isFinite(position) ? `${(position * 100).toFixed(0)}%` : '--'
  const parts = [
    `120日位置 ${positionText}`,
    `20日修复 ${formatSignedPercent(kline.launch_return_20d)}`,
    `活跃量比 ${formatRatio(kline.launch_volume_ratio_20)}`,
    `主营行业 ${row?.primary_industry_name || '--'} / 点火分 ${formatScore(row?.primary_industry_ignition_score)}`,
    `3日主力占比 ${formatSignedPercent(row?.funding_main_inflow_pct_3d)} / 正流入 ${row?.funding_positive_days_3d ?? 0} 日`,
    '该证据只修正预测排序与上涨概率，不代表交易闸门已通过',
  ]
  return parts.join('；')
}

function formatLastLimitUpDate(memoryFeatures) {
  const latest = memoryFeatures?.memory_last_limit_up_date
  if (!latest) return '最近涨停日 --'
  return `最近涨停 ${latest}`
}

function buildStageCards(subProbabilities) {
  const items = [
    { key: 'next_day_rise', label: '次日上涨', value: subProbabilities?.next_day_rise },
    { key: 'next_day_strong_rise', label: '次日强涨', value: subProbabilities?.next_day_strong_rise },
    { key: 'breakout_3d', label: '3日内突破', value: subProbabilities?.breakout_3d },
    { key: 'first_limitup_next_day', label: '次日首板', value: subProbabilities?.first_limitup_next_day },
    { key: 'first_limitup_5d', label: '5日内首板', value: subProbabilities?.first_limitup_5d },
    { key: 'become_core_10d', label: '10日内成核心', value: subProbabilities?.become_core_10d },
    { key: 'next_second_board', label: '次日晋级二板', value: subProbabilities?.next_second_board },
  ]
  return items
    .filter(item => item.value != null)
    .map(item => ({
      ...item,
      value: formatProbability(item.value),
    }))
}

function buildMemoryCards(memoryFeatures) {
  const features = memoryFeatures || {}
  const score = Number(features.memory_score || 0)
  if (!score && !features.memory_max_board_50d && !features.memory_limit_up_hits_50d) {
    return []
  }
  return [
    { key: 'memory_score', label: '首波记忆分', value: formatScore(features.memory_score) },
    { key: 'memory_max_board_50d', label: '近50日最高连板', value: features.memory_max_board_50d ?? '--' },
    { key: 'memory_limit_up_hits_50d', label: '近50日涨停数', value: features.memory_limit_up_hits_50d ?? '--' },
    { key: 'memory_days_since_last_limit_up', label: '距最近涨停', value: features.memory_days_since_last_limit_up != null ? `${features.memory_days_since_last_limit_up}天` : '--' },
    { key: 'memory_last_limit_up_date', label: '最近涨停日', value: features.memory_last_limit_up_date || '--' },
    { key: 'memory_last_consecutive_days', label: '最近涨停连板', value: features.memory_last_consecutive_days != null ? `${features.memory_last_consecutive_days}板` : '--' },
  ]
}

function formatMoneyAmount(value, signed = false) {
  const numeric = Number(value || 0)
  if (!Number.isFinite(numeric) || numeric === 0) return signed ? '0' : '--'
  const prefix = signed && numeric > 0 ? '+' : ''
  const absValue = Math.abs(numeric)
  const text = absValue >= 1e8 ? `${(absValue / 1e8).toFixed(2)}亿` : `${(absValue / 1e4).toFixed(0)}万`
  return `${prefix}${numeric < 0 ? '-' : ''}${text}`
}

function dragonTigerLabel(dragonTiger) {
  if (!dragonTiger?.is_listed) return '未上榜'
  const delta = Number(dragonTiger.probability_delta || 0)
  if (delta > 0.015) return '席位加分'
  if (delta < -0.015) return '席位降权'
  return '席位中性'
}

function dragonTigerTagType(dragonTiger) {
  if (!dragonTiger?.is_listed) return 'info'
  const delta = Number(dragonTiger.probability_delta || 0)
  if (delta > 0.015) return 'danger'
  if (delta < -0.015) return 'success'
  return 'warning'
}

function formatDragonTigerSummary(dragonTiger) {
  if (!dragonTiger?.is_listed) return '未上龙虎榜'
  return dragonTiger.summary || '已上榜，暂无席位摘要'
}

function formatDragonTigerTooltip(dragonTiger) {
  if (!dragonTiger?.is_listed) return '当日未进入龙虎榜，二板概率不做席位修正'
  const reasons = (dragonTiger.reasons || []).join(' / ')
  const risks = (dragonTiger.risk_flags || []).join(' / ')
  return [
    dragonTiger.summary,
    reasons ? `原因：${reasons}` : '',
    `净买额：${formatMoneyAmount(dragonTiger.net_buy_amount, true)}`,
    `概率修正：${formatSignedPercent(Number(dragonTiger.probability_delta || 0) * 100)}`,
    risks ? `风险：${risks}` : '',
  ].filter(Boolean).join('；')
}

function formatDragonTigerMembers(members, amountKey = 'net_amount') {
  const rows = Array.isArray(members) ? members : []
  if (!rows.length) return '--'
  return rows
    .slice(0, 3)
    .map(member => `${member.name || '--'} ${formatMoneyAmount(member[amountKey], true)}`)
    .join(' / ')
}

function buildDragonTigerCards(dragonTiger) {
  if (!dragonTiger?.is_listed) return []
  return [
    { key: 'summary', label: '席位摘要', value: dragonTiger.summary || '--' },
    { key: 'net_buy', label: '龙虎榜净额', value: formatMoneyAmount(dragonTiger.net_buy_amount, true) },
    { key: 'score', label: '席位分', value: dragonTiger.member_score != null ? Number(dragonTiger.member_score).toFixed(1) : '--' },
    { key: 'delta', label: '概率修正', value: formatSignedPercent(Number(dragonTiger.probability_delta || 0) * 100) },
  ]
}

function buildDragonTigerMembers(dragonTiger) {
  if (!dragonTiger?.is_listed) return []
  return [
    { key: 'top_buy', label: '买方席位', value: formatDragonTigerMembers(dragonTiger.top_buy_members, 'net_amount') },
    { key: 'top_sell', label: '卖方席位', value: formatDragonTigerMembers(dragonTiger.top_sell_members, 'net_amount') },
  ]
}

function buildExportRow(row, index) {
  const klineConfirmation = row?.kline_confirmation || {}
  const riskFlags = Array.isArray(row?.risk_flags) ? row.risk_flags : []
  const subProbabilities = row?.sub_probabilities || {}
  const memoryFeatures = row?.memory_features || {}
  const dragonTiger = row?.dragon_tiger || {}
  return {
    ...row,
    rank: index + 1,
    probability_track_label: row?.target_board === 1 ? '首板概率榜（允许空缺）' : '二板概率榜',
    probability_pct: (row?.limit_up_probability ?? row?.probability) != null ? `${(Number(row.limit_up_probability ?? row.probability) * 100).toFixed(1)}%` : '--',
    direction_probability_pct: (row?.direction_probability ?? subProbabilities.next_day_rise) != null ? `${(Number(row.direction_probability ?? subProbabilities.next_day_rise) * 100).toFixed(1)}%` : '--',
    next_day_rise_pct: (row?.direction_probability ?? subProbabilities.next_day_rise) != null ? `${(Number(row.direction_probability ?? subProbabilities.next_day_rise) * 100).toFixed(1)}%` : '--',
    next_day_strong_rise_pct: subProbabilities.next_day_strong_rise != null ? `${(Number(subProbabilities.next_day_strong_rise) * 100).toFixed(1)}%` : '--',
    breakout_3d_pct: subProbabilities.breakout_3d != null ? `${(Number(subProbabilities.breakout_3d) * 100).toFixed(1)}%` : '--',
    first_limitup_next_day_pct: subProbabilities.first_limitup_next_day != null ? `${(Number(subProbabilities.first_limitup_next_day) * 100).toFixed(1)}%` : '--',
    first_limitup_5d_pct: subProbabilities.first_limitup_5d != null ? `${(Number(subProbabilities.first_limitup_5d) * 100).toFixed(1)}%` : '--',
    become_core_10d_pct: subProbabilities.become_core_10d != null ? `${(Number(subProbabilities.become_core_10d) * 100).toFixed(1)}%` : '--',
    next_second_board_pct: subProbabilities.next_second_board != null ? `${(Number(subProbabilities.next_second_board) * 100).toFixed(1)}%` : '--',
    dragon_tiger_summary: formatDragonTigerSummary(dragonTiger),
    dragon_tiger_delta_pct: dragonTiger.is_listed ? formatSignedPercent(Number(dragonTiger.probability_delta || 0) * 100) : '--',
    dragon_tiger_top_buyers: formatDragonTigerMembers(dragonTiger.top_buy_members, 'net_amount'),
    dragon_tiger_top_sellers: formatDragonTigerMembers(dragonTiger.top_sell_members, 'net_amount'),
    memory_score: memoryFeatures.memory_score,
    memory_summary: formatMemorySummary(memoryFeatures),
    long_cycle_summary: `${formatLongCycleLabel(row)} / ${formatLongCycleDetail(row)}`,
    launch_evidence_label: launchEvidenceLabel(row),
    limit_up_anchor_gap_pct_text: row?.limit_up_anchor_gap_pct != null ? `${Number(row.limit_up_anchor_gap_pct).toFixed(1)}%` : '--',
    kline_confirmation_summary: klineConfirmation.summary || '--',
    risk_flags_text: riskFlags.length
      ? riskFlags.map(flag => flag?.label || flag?.code || '--').join(' / ')
      : '--',
  }
}

const loadExportTool = async () => import('@/utils/export')

async function handlePromotionExport(command) {
  const dateStr = new Date().toISOString().slice(0, 10)
  const filename = `晋级预测_分赛道概率榜_${dateStr}`
  const rankedFirstRows = rankedFirstBoardCandidates.value.map((row, index) => buildExportRow(row, index))
  const rankedSecondRows = rankedSecondBoardCandidates.value.map((row, index) => buildExportRow(row, index))
  const rankedRows = [...rankedFirstRows, ...rankedSecondRows]
  const firstBoardRows = firstBoardCandidates.value.map((row, index) => buildExportRow(row, index))
  const firstBoardPreSprintRows = firstBoardPreSprintCandidates.value.map((row, index) => buildExportRow(row, index))
  const firstBoardWatchRows = firstBoardWatchCandidates.value.map((row, index) => buildExportRow(row, index))
  const firstBoardWeakWatchRows = firstBoardWeakWatchCandidates.value.map((row, index) => buildExportRow(row, index))
  const limitUpPlatformRows = limitUpPlatformCandidates.value.map((row, index) => buildExportRow(row, index))
  const secondBoardRows = secondBoardCandidates.value.map((row, index) => buildExportRow(row, index))
  const summaryRows = [
    { item: '首板交易日', value: promotionCandidates.value.first_board_trade_date || '--' },
    { item: '二板交易日', value: promotionCandidates.value.second_board_trade_date || '--' },
    { item: '快照时间', value: promotionCandidates.value.snapshot_time || '--' },
    { item: '首板展示候选数', value: `${firstBoardRows.length}` },
    { item: '贴板横盘预备数', value: `${limitUpPlatformRows.length}` },
    { item: '准冲刺数', value: `${firstBoardPreSprintRows.length}` },
    { item: '观察预备数', value: `${firstBoardWatchRows.length}` },
    { item: '弱观察数', value: `${firstBoardWeakWatchRows.length}` },
    { item: '首板未入池样本', value: `${firstBoardDiagnostics.value.blocked_total || 0}` },
    { item: '二板候选数', value: `${secondBoardRows.length}` },
    { item: '首板概率榜条数', value: `${rankedFirstRows.length}` },
    { item: '首板主动空缺', value: `${promotionCandidates.value.ranked_first_board_abstained_slots || 0}` },
    { item: '首板已确认可监控', value: `${promotionCandidates.value.ranked_first_board_actionable_count || 0}` },
    { item: '二板概率榜条数', value: `${rankedSecondRows.length}` },
    { item: '二板可交易监控', value: `${promotionCandidates.value.ranked_second_board_actionable_count || 0}` },
    { item: '调试混排榜', value: '不导出，接口仅保留在 debug 字段中' },
    {
      item: '首板市场环境',
      value: `涨停${promotionCandidates.value.market_context?.first_board?.limit_up_count ?? '--'} / 高度${promotionCandidates.value.market_context?.first_board?.board_height ?? '--'}`
    },
    {
      item: '二板市场环境',
      value: `涨停${promotionCandidates.value.market_context?.second_board?.limit_up_count ?? '--'} / 高度${promotionCandidates.value.market_context?.second_board?.board_height ?? '--'}`
    },
  ]

  const { exportToCSV, exportWorkbookToExcel } = await loadExportTool()
  if (command === 'csv') {
    await exportToCSV(rankedRows, PROMOTION_TRACK_EXPORT_COLUMNS, filename)
    notifySuccess(`已导出 ${rankedRows.length} 条分赛道概率榜数据，不含调试混排榜`)
    return
  }

  await exportWorkbookToExcel([
    { sheetName: '首板观察榜', data: rankedFirstRows, columns: PROMOTION_TRACK_EXPORT_COLUMNS },
    { sheetName: '二板概率榜', data: rankedSecondRows, columns: PROMOTION_TRACK_EXPORT_COLUMNS },
    { sheetName: '首板冲刺', data: firstBoardRows, columns: PROMOTION_FIRST_BOARD_EXPORT_COLUMNS },
    { sheetName: '贴板横盘预备', data: limitUpPlatformRows, columns: PROMOTION_LIMIT_UP_PLATFORM_EXPORT_COLUMNS },
    { sheetName: '准冲刺', data: firstBoardPreSprintRows, columns: PROMOTION_WATCH_EXPORT_COLUMNS },
    { sheetName: '观察预备', data: firstBoardWatchRows, columns: PROMOTION_WATCH_EXPORT_COLUMNS },
    { sheetName: '弱观察', data: firstBoardWeakWatchRows, columns: PROMOTION_WATCH_EXPORT_COLUMNS },
    { sheetName: '二板候选', data: secondBoardRows, columns: PROMOTION_SECOND_BOARD_EXPORT_COLUMNS },
    { sheetName: '导出摘要', data: summaryRows, columns: PROMOTION_SUMMARY_COLUMNS },
  ], filename)
  notifySuccess(`已导出 ${rankedRows.length} 条分赛道概率榜数据，不含调试混排榜`)
}

async function queryProbability() {
  if (!queryCode.value) return
  querying.value = true
  try {
    promotionResult.value = await getPromotionProbability(queryCode.value, { target: queryTarget.value })
  } catch { /* ignore */ }
  querying.value = false
}

onMounted(async () => {
  try {
    const [l, h, c, review] = await Promise.allSettled([
      getPromotionLadder(),
      getBoardHeight(),
      getPromotionCandidates({ limit: 12, ranked_limit: 30, compact: true }),
      getPromotionLearningReview({ lookback_days: 10 }),
    ])
    if (l.status === 'fulfilled') ladderData.value = l.value.ladder || []
    if (h.status === 'fulfilled') boardHeight.value = h.value
    if (c.status === 'fulfilled') promotionCandidates.value = c.value || {}
    if (review.status === 'fulfilled') learningReview.value = review.value || {}
  } catch { /* ignore */ }
})
</script>

<style scoped lang="scss">
.promotion-page { display: flex; flex-direction: column; gap: 18px; }
.promotion-metrics-panel { padding: 4px; }
.market-scope-note,
.learning-scope-note {
  margin: 4px 8px 8px;
  padding: 9px 12px;
  border-radius: 9px;
  background: rgba(37, 99, 235, 0.055);
  color: var(--claw-text-secondary);
  font-size: 12px;
  line-height: 1.7;
}
.learning-scope-note { margin: -4px 0 0; }
.snapshot-alert { border: 1px solid rgba(245, 158, 11, 0.28); background: rgba(245, 158, 11, 0.08); }
.prediction-health-strip {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  padding: 10px 14px;
  border: 1px solid var(--claw-border-light);
  border-radius: 12px;
  background: rgba(247, 250, 255, 0.82);
  color: var(--claw-text-secondary);
  font-size: 12px;
}
.learning-review-section { display: flex; flex-direction: column; gap: 12px; }
.learning-review-card { display: flex; flex-direction: column; gap: 16px; }
.learning-directional-summary { border-bottom: 1px solid var(--claw-border); padding-bottom: 12px; min-width: 0; }
.learning-directional-heading,
.learning-directional-values { display: flex; flex-wrap: wrap; align-items: center; gap: 10px 20px; font-size: 13px; line-height: 1.7; }
.learning-directional-heading { margin-bottom: 8px; }
.learning-directional-values { color: var(--claw-text-secondary); }
.learning-directional-values b { color: var(--claw-text-primary); }
.learning-directional-summary .candidate-subtitle { overflow-wrap: anywhere; }
.learning-review-meta { display: flex; align-items: center; gap: 10px; color: var(--claw-text-secondary); font-size: 13px; }
.learning-metric-grid { display: grid; grid-template-columns: repeat(9, minmax(0, 1fr)); gap: 10px; }
.learning-metric-card {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px 14px;
  border: 1px solid var(--claw-border-light);
  border-radius: 12px;
  background: rgba(247, 250, 255, 0.78);
}
.learning-metric-card span { color: var(--claw-text-muted); font-size: 12px; }
.learning-metric-card strong { color: var(--claw-text-primary); font-size: 22px; }
.learning-metric-card strong.metric-unavailable { font-size: 14px; color: var(--claw-text-muted); }
.learning-recommendation { border: 1px solid rgba(245, 158, 11, 0.24); }
.learning-review-grid { display: grid; grid-template-columns: minmax(0, 0.9fr) minmax(0, 1.1fr); gap: 16px; }
.learning-review-grid > div { min-width: 0; }
.learning-subtitle { margin-bottom: 8px; }
.stat-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 0; }
.promotion-stat-card { padding: 16px; }
.metric-head { color: var(--claw-text-muted); font-size: 13px; margin-bottom: 10px; }
.section-title-with-action { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.candidate-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
.candidate-grid > .panel-card { min-width: 0; }
.candidate-panel-head { display: flex; flex-direction: column; gap: 4px; margin-bottom: 12px; }
.recall-rank-head { margin-top: 22px; padding-top: 16px; border-top: 1px solid var(--border-color); }
.candidate-title { font-size: 15px; font-weight: 700; color: var(--claw-text-primary); }
.candidate-subtitle { font-size: 12px; color: var(--claw-text-muted); }
.candidate-legend { display: flex; gap: 8px; margin-top: 4px; flex-wrap: wrap; }
.platform-diagnostic-section {
  margin-top: 18px;
  padding-top: 18px;
  border-top: 1px solid var(--claw-border-light);
}
.platform-diagnostic-headline-card { margin-bottom: 12px; }
.diagnostic-panel { display: flex; flex-direction: column; gap: 14px; }
.diagnostic-overview { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 4px; }
.diagnostic-headline-card {
  padding: 16px 18px;
  border-radius: 14px;
  border: 1px solid var(--claw-border-light);
  background: linear-gradient(135deg, rgba(255, 249, 235, 0.88), rgba(247, 250, 255, 0.92));
}
.diagnostic-headline-title { font-size: 13px; font-weight: 700; color: var(--claw-text-secondary); }
.diagnostic-headline-text { margin-top: 8px; font-size: 20px; font-weight: 700; line-height: 1.5; color: var(--claw-text-primary); }
.diagnostic-headline-subtitle { margin-top: 8px; font-size: 13px; line-height: 1.6; color: var(--claw-text-secondary); }
.diagnostic-focus-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; margin-top: 14px; }
.diagnostic-focus-card {
  padding: 12px 14px;
  border-radius: 12px;
  border: 1px solid var(--claw-border-light);
  background: rgba(255, 255, 255, 0.82);
}
.diagnostic-focus-label { display: flex; align-items: center; justify-content: space-between; gap: 8px; color: var(--claw-text-muted); font-size: 12px; }
.diagnostic-focus-value { margin-top: 10px; font-size: 22px; font-weight: 700; color: var(--claw-text-primary); }
.diagnostic-focus-hint { margin-top: 6px; font-size: 12px; line-height: 1.5; color: var(--claw-text-secondary); }
.diagnostic-summary-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; }
.diagnostic-summary-card {
  padding: 12px 14px;
  border-radius: 12px;
  border: 1px solid var(--claw-border-light);
  background: rgba(247, 250, 255, 0.82);
}
.diagnostic-summary-label { display: flex; align-items: center; justify-content: space-between; gap: 8px; color: var(--claw-text-muted); font-size: 12px; }
.diagnostic-summary-value { font-size: 24px; font-weight: 700; color: var(--claw-text-primary); margin-top: 10px; }
.diagnostic-summary-subtitle { color: var(--claw-text-muted); font-size: 11px; margin-top: 4px; }
.diagnostic-groups { display: flex; flex-direction: column; gap: 16px; }
.diagnostic-group { display: flex; flex-direction: column; gap: 10px; }
.diagnostic-group-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.diagnostic-group-title { font-size: 14px; font-weight: 700; color: var(--claw-text-primary); }
.diagnostic-playbook {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap: 10px;
}
.diagnostic-playbook-item {
  padding: 10px 12px;
  border-radius: 10px;
  border: 1px dashed var(--claw-border);
  background: rgba(255, 249, 235, 0.66);
  color: var(--claw-text-secondary);
  font-size: 12px;
  line-height: 1.5;
}
.candidate-stock-cell { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.candidate-stock-name { cursor: pointer; color: var(--claw-text-primary); font-weight: 600; }
.metric-stack { display: flex; flex-direction: column; gap: 2px; line-height: 1.25; }
.metric-stack strong { color: var(--claw-text-primary); font-size: 13px; }
.metric-stack span { color: var(--claw-text-muted); font-size: 11px; }
.export-dropdown { margin-left: auto; }
.query-row { display: flex; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }
.result-card {
  background: rgba(245, 247, 250, 0.72);
  border: 1px solid var(--claw-border-light);
  border-radius: 12px;
  padding: 16px;
}
.result-row { display: flex; gap: 24px; margin-bottom: 12px; font-size: 15px; flex-wrap: wrap; }
.result-section { margin-top: 14px; }
.result-section-title { font-size: 13px; font-weight: 700; color: var(--claw-text-primary); margin-bottom: 8px; }
.result-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; }
.result-metric-card {
  padding: 10px 12px;
  border-radius: 10px;
  border: 1px solid var(--claw-border-light);
  background: rgba(255, 255, 255, 0.78);
}
.result-metric-label { font-size: 12px; color: var(--claw-text-muted); margin-bottom: 4px; }
.result-metric-value { font-size: 15px; font-weight: 700; color: var(--claw-text-primary); }
.factors-list { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 8px; margin-top: 12px; }
.factor-item { display: flex; justify-content: space-between; font-size: 12px; padding: 6px 10px; background: #f5f6fa; border: 1px solid #e8ebf0; border-radius: 8px; }
.factor-name { color: var(--claw-text-secondary); }
.stock-tag { cursor: pointer; margin: 2px; }
:deep(.high-ladder) { background: rgba(239, 68, 68, 0.08) !important; }
:deep(.el-table) { border: 1px solid var(--claw-border); border-radius: 14px; overflow: hidden; box-shadow: var(--claw-shadow-sm); }
:deep(.el-table th.el-table__cell) { background: #f7faff; }
@media (max-width: 1700px) {
  .learning-metric-grid { grid-template-columns: repeat(5, minmax(0, 1fr)); }
}
@media (max-width: 1600px) {
  .candidate-grid { grid-template-columns: minmax(0, 1fr); }
}
@media (max-width: 1500px) {
  .learning-review-grid { grid-template-columns: minmax(0, 1fr); }
}
@media (max-width: 1100px) {
  .learning-metric-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
}
@media (max-width: 768px) {
  .stat-row { grid-template-columns: repeat(2, 1fr); }
  .learning-metric-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .learning-metric-card:last-child { grid-column: 1 / -1; }
  .section-title-with-action { align-items: flex-start; flex-direction: column; }
}
</style>
