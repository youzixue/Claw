<template>
  <div class="page-container">
    <div class="page-shell review-page">
      <div class="page-hero">
        <div>
          <h2 class="page-title">
            <el-icon class="title-icon"><Notebook /></el-icon>
            每日复盘 · 盘后决策台
          </h2>
          <div class="page-subtitle">
            先读结论与下一交易日条件，再下钻资金、情绪、预测漏斗和不可变证据
          </div>
        </div>
        <div class="review-controls">
          <div class="control-field">
            <span>复盘交易日</span>
            <el-date-picker
              v-model="form.review_date"
              type="date"
              value-format="YYYY-MM-DD"
              placeholder="自动选择最近交易日"
              :disabled-date="disabledDate"
              @change="onReviewDateChange"
            />
          </div>
          <div class="control-field">
            <span>快照阶段</span>
            <el-radio-group v-model="form.phase" @change="switchPhase">
              <el-radio-button value="premarket">盘前</el-radio-button>
              <el-radio-button value="intraday">盘中</el-radio-button>
              <el-radio-button value="postmarket">盘后</el-radio-button>
            </el-radio-group>
          </div>
          <el-button type="primary" :loading="building" @click="buildSnapshot">
            {{ buildButtonLabel }}
          </el-button>
        </div>
      </div>

      <el-alert
        title="复盘只冻结证据，不会直接改算法或下单"
        type="info"
        :closable="false"
        description="日期表示复盘所属交易日；“行情数据截至”和“As-of”分别说明市场数据日与信息截止时点。单日结果只追加样本，不能反写原预测。"
        show-icon
      />

      <template v-if="hasReview">
        <section class="panel-card snapshot-header">
          <div class="snapshot-title-row">
            <div class="snapshot-tags">
              <el-tag effect="dark" size="large">{{ phaseLabel(review.phase) }}</el-tag>
              <el-tag :type="qualityTag(review.quality?.status || review.quality_status)">
                {{ qualityLabel(review.quality?.status || review.quality_status) }}
                · {{ pct(review.quality?.score ?? review.quality_score) }}
              </el-tag>
              <el-tag :type="stanceTagType(conclusion.stance)" effect="plain">
                {{ conclusion.stance_label || '等待结论' }}
              </el-tag>
            </div>
            <div class="version-line" :title="`${review.schema_version || ''} · ${review.data_version || ''}`">
              {{ shortVersion(review.schema_version) }} · {{ shortVersion(review.data_version) }}
            </div>
          </div>

          <div class="date-facts">
            <div>
              <span>复盘交易日</span>
              <strong>{{ formatTradeDate(review.review_date) }}</strong>
            </div>
            <div>
              <span>完整日线基准日</span>
              <strong>{{ formatTradeDate(review.technical_trade_date || review.analysis_trade_date) }}</strong>
            </div>
            <div>
              <span>市场状态交易日</span>
              <strong>{{ review.market_trade_date ? formatTradeDate(review.market_trade_date) : '旧快照未分离' }}</strong>
            </div>
            <div>
              <span>信息截止 As-of</span>
              <strong>{{ formatDateTime(review.as_of_at) }}</strong>
            </div>
            <div>
              <span>{{ review.phase === 'postmarket' ? '下一交易日' : '本阶段目标日' }}</span>
              <strong>{{ formatTradeDate(nextGuide.trade_date || review.next_trade_date) }}</strong>
            </div>
          </div>

          <el-alert
            v-if="dateMismatch"
            type="warning"
            :closable="false"
            title="这是旧版日期口径快照：市场状态与完整日线未可靠分离，不能把旧日涨停、资金和当日分时混作当天结论。历史记录保留，不事后改写。"
          />
          <el-alert
            v-if="review.phase === 'intraday' && review.market_trade_date"
            type="info"
            :closable="false"
            title="盘中市场状态与前一完整日线分开显示。涨停池和板块日表无逐次版本，现场读取不等于历史时点回放；缺失数据不借用昨日补齐，盘中表现不作为收盘胜率。"
          />
          <el-alert
            v-if="review.quality?.warnings?.length"
            type="warning"
            :closable="false"
            :title="review.quality.warnings.map(friendlyWarning).join('；')"
          />
        </section>

        <section class="panel-card conclusion-panel">
          <div class="section-heading">
            <div>
              <div class="panel-title"><el-icon><DataAnalysis /></el-icon>复盘结论</div>
              <div class="section-kicker">确定性规则根据冻结快照归纳，不依赖 GPT</div>
            </div>
            <el-tag :type="stanceTagType(conclusion.stance)" size="large" effect="dark">
              {{ conclusion.stance_label || '数据不足' }}
            </el-tag>
          </div>
          <h3 class="decision-headline">{{ conclusion.headline || '当前快照尚未形成可读结论' }}</h3>
          <p class="decision-summary">{{ conclusion.summary || '请重新生成新版快照，或检查关键数据源。' }}</p>

          <div class="evidence-grid">
            <article
              v-for="item in conclusion.evidence || []"
              :key="item.label"
              class="evidence-card"
              :class="`tone-${item.tone || 'muted'}`"
            >
              <span>{{ item.label }}</span>
              <strong>{{ item.value }}</strong>
              <p>{{ item.interpretation }}</p>
            </article>
          </div>

          <div v-if="conclusion.risks?.length" class="risk-strip">
            <strong><el-icon><Warning /></el-icon>需先处理的风险</strong>
            <span v-for="item in conclusion.risks" :key="item">{{ friendlyWarning(item) }}</span>
          </div>
        </section>

        <section class="panel-card guide-panel">
          <div class="section-heading">
            <div>
              <div class="panel-title"><el-icon><Calendar /></el-icon>下一交易日条件卡</div>
              <div class="section-kicker">
                目标日 {{ formatTradeDate(nextGuide.trade_date || review.next_trade_date) }} ·
                {{ nextGuide.disclaimer || '条件化观察，不构成自动交易指令' }}
              </div>
            </div>
            <el-tag :type="stanceTagType(nextGuide.stance)" effect="plain">
              基调：{{ nextGuide.stance_label || '--' }}
            </el-tag>
          </div>
          <p class="guide-summary">{{ nextGuide.summary || '当前快照没有下一交易日指引。' }}</p>

          <div v-if="nextGuide.focus?.length" class="focus-grid">
            <article v-for="item in nextGuide.focus" :key="item.title" class="focus-card">
              <strong>{{ item.title }}</strong>
              <div class="focus-chips">
                <el-tag v-for="name in item.items || []" :key="name" size="small" effect="plain">{{ name }}</el-tag>
              </div>
              <p><b>成立：</b>{{ item.trigger }}</p>
              <p><b>失效：</b>{{ item.invalidation }}</p>
            </article>
          </div>

          <div class="playbook-grid">
            <article
              v-for="item in nextGuide.playbook || []"
              :key="item.scenario"
              class="playbook-card"
              :class="`tone-${item.tone || 'neutral'}`"
            >
              <div><el-tag size="small" :type="toneTagType(item.tone)">{{ item.scenario }}</el-tag></div>
              <p><b>触发</b>{{ item.trigger }}</p>
              <p><b>应对</b>{{ item.response }}</p>
              <p><b>失效</b>{{ item.invalidation }}</p>
            </article>
          </div>

          <div v-if="nextGuide.watchlist?.length" class="watchlist-block">
            <div class="subsection-title">冻结模型观察池（非买入指令）</div>
            <el-table :data="nextGuide.watchlist" size="small" stripe max-height="320" table-layout="auto">
              <el-table-column label="排名" width="64" class-name="numeric-column">
                <template #default="{ row }">{{ integer(row.rank_position) }}</template>
              </el-table-column>
              <el-table-column label="代码" width="88" class-name="numeric-column">
                <template #default="{ row }">{{ text(row.code) }}</template>
              </el-table-column>
              <el-table-column label="名称" width="100">
                <template #default="{ row }">{{ text(row.name) }}</template>
              </el-table-column>
              <el-table-column prop="role" label="角色" width="90" />
              <el-table-column label="概率" width="82" class-name="numeric-column">
                <template #default="{ row }">{{ pct(row.probability) }}</template>
              </el-table-column>
              <el-table-column label="门禁" width="86">
                <template #default="{ row }">
                  <el-tag size="small" :type="row.actionable ? 'success' : 'info'">
                    {{ row.actionable ? '可复核' : '仅观察' }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="condition" label="盘中确认条件" min-width="250" />
            </el-table>
          </div>

          <div v-if="nextGuide.do_not?.length" class="do-not-line">
            <strong>禁止项</strong>
            <span v-for="item in nextGuide.do_not" :key="item">{{ item }}</span>
          </div>
        </section>

        <div class="dimension-grid">
          <section class="panel-card dimension-card">
            <div class="panel-title"><el-icon><Money /></el-icon>资金面</div>
            <div class="metric-row">
              <div>
                <span>{{ marketFlowLabel }}</span>
                <strong :class="valueClass(capital.market_main_net_inflow)">{{ formatYi(capital.market_main_net_inflow) }}</strong>
              </div>
              <div>
                <span>{{ sampledFlowLabel }}</span>
                <strong :class="valueClass(capital.sampled_stock_main_net_inflow)">{{ formatYi(capital.sampled_stock_main_net_inflow, true) }}</strong>
              </div>
              <div>
                <span>沪深成交额</span>
                <strong>{{ formatTurnover(technical.turnover_total_trillion) }}</strong>
              </div>
            </div>
            <el-table :data="capital.top_sectors || []" size="small" max-height="300" empty-text="板块资金数据缺失" table-layout="auto">
              <el-table-column prop="sector_name" label="强势板块" min-width="170" class-name="wrap-column" />
              <el-table-column label="涨幅" width="82" class-name="numeric-column">
                <template #default="{ row }"><span :class="valueClass(row.change_pct)">{{ signedPct(row.change_pct) }}</span></template>
              </el-table-column>
              <el-table-column label="强度" width="72" class-name="numeric-column">
                <template #default="{ row }">{{ number(row.strength_score, 1) }}</template>
              </el-table-column>
              <el-table-column label="资金" width="100" class-name="numeric-column">
                <template #default="{ row }">{{ formatYi(row.fund_flow) }}</template>
              </el-table-column>
              <el-table-column label="涨停" width="66" class-name="numeric-column">
                <template #default="{ row }">{{ integer(row.limit_up_count) }}</template>
              </el-table-column>
            </el-table>
            <div v-if="capital.sample_basis === 'top_main_net_inflow_30'" class="source-note">
              可交易池主力资金来自市场情绪快照（主板、非 ST/停牌/退市）；个股项为全 A 股净流入前
              {{ integer(capital.sampled_stock_count) }} 只合计，共有 {{ integer(capital.fund_flow_record_count) }} 条资金记录。
              板块资金单位为亿元；个股资金原始单位为元，页面已统一换算。沪深成交额取上证+深证指数聚合，不再累加混合来源个股 K 线。
            </div>
            <div v-else class="source-note">旧快照未冻结资金样本范围与可信沪深成交额口径；相关空值不再用 0 或混合 K 线汇总补齐。</div>
          </section>

          <section class="panel-card dimension-card">
            <div class="panel-title"><el-icon><TrendCharts /></el-icon>技术与情绪</div>
            <div class="metric-grid compact">
              <div class="metric-card"><span>上涨占比</span><strong>{{ pct(technical.advance_ratio) }}</strong></div>
              <div class="metric-card"><span>中位涨幅</span><strong :class="valueClass(technical.median_return)">{{ signedPct(technical.median_return) }}</strong></div>
              <div class="metric-card"><span>涨停 / 跌停</span><strong>{{ integer(technical.limit_up_count) }} / {{ integer(technical.limit_down_count) }}</strong></div>
              <div class="metric-card"><span>炸板 / 封板率</span><strong>{{ integer(technical.broken_limit_count) }} / {{ pctPoints(technical.seal_rate) }}</strong></div>
              <div class="metric-card"><span>最高板</span><strong>{{ integer(technical.board_height) }}板</strong></div>
              <div class="metric-card"><span>有效涨跌样本</span><strong>{{ integer(technical.universe_count) }}</strong></div>
            </div>
            <div class="source-note">
              来源：{{ sourceLabel(technical.source) }}
              <template v-if="hasNumeric(technical.change_coverage)"> · K线涨跌幅覆盖 {{ pct(technical.change_coverage) }}</template>
              · 收益离散度 {{ number(technical.return_dispersion) }}%
              · 中位换手 {{ pctPoints(technical.median_turnover) }}
              <template v-if="technical.sentiment_quality_status"> · 情绪源 {{ sentimentQualityLabel(technical.sentiment_quality_status) }}</template>
              <template v-if="technical.sentiment_observed_at"> · 观测于 {{ formatDateTime(technical.sentiment_observed_at) }}</template>
              <template v-if="technical.total_amount_source !== 'market_sentiment_index_aggregate'"> · 旧快照未冻结可信沪深成交额</template>
              <template v-if="technical.sentiment_quality_reason"> · {{ technical.sentiment_quality_reason }}</template>
            </div>
          </section>

          <section class="panel-card dimension-card news-card">
            <div class="section-heading compact-heading">
              <div>
                <div class="panel-title"><el-icon><Bell /></el-icon>消息影响筛选</div>
                <div class="section-kicker">先筛方向与影响对象，再阅读摘要；“利好”标签不等于买入信号</div>
              </div>
            </div>
            <div class="news-summary">
              <el-tag type="success">利好 {{ integer(news.positive_count) }}</el-tag>
              <el-tag type="danger">利空 {{ integer(news.negative_count) }}</el-tag>
              <el-tag>中性 {{ integer(news.neutral_count) }}</el-tag>
              <el-tag type="info">未分析 {{ integer(news.unanalyzed_count) }}</el-tag>
              <span>
                截至 {{ formatDateTime(news.as_of_at) }} ·
                {{ news.count_scope === 'as_of_window' ? '窗口共' : '旧快照入选' }} {{ integer(news.count) }} 条 ·
                纳入筛选 {{ integer(news.selected_count ?? news.items?.length) }} 条
              </span>
            </div>
            <div class="news-toolbar">
              <el-radio-group v-model="newsPolarityFilter" size="small" @change="resetNewsPage">
                <el-radio-button value="all">全部</el-radio-button>
                <el-radio-button value="bull">利好</el-radio-button>
                <el-radio-button value="bear">利空</el-radio-button>
                <el-radio-button value="neutral">中性</el-radio-button>
                <el-radio-button value="unanalyzed">未分析</el-radio-button>
              </el-radio-group>
              <el-input
                v-model="newsKeyword"
                clearable
                placeholder="搜索标题、摘要、板块或代码"
                @input="resetNewsPage"
              />
              <el-select v-model="newsSourceFilter" placeholder="全部来源" clearable @change="resetNewsPage">
                <el-option v-for="source in newsSourceOptions" :key="source" :label="source" :value="source" />
              </el-select>
              <el-checkbox v-model="newsOnlySpecificTarget" @change="resetNewsPage">只看有明确影响对象</el-checkbox>
            </div>
            <div class="news-filter-result">
              当前条件命中 <strong>{{ integer(newsFilteredItems.length) }}</strong> 条重点样本；本页显示
              {{ integer(newsPagedItems.length) }} 条。
            </div>
            <div class="news-list">
              <div v-for="item in newsPagedItems" :key="item.id" class="news-item">
                <div>
                  <el-tag size="small" :type="newsTag(newsItemPolarity(item))">{{ newsLabel(newsItemPolarity(item)) }}</el-tag>
                  <strong>{{ item.title }}</strong>
                </div>
                <p>{{ item.summary || item.impact_reason || '暂无结构化摘要' }}</p>
                <div class="news-impact-row">
                  <span>影响对象</span>
                  <el-tag v-for="sector in newsSectors(item).slice(0, 6)" :key="`sector-${item.id}-${sector}`" size="small" effect="plain">{{ sector }}</el-tag>
                  <el-tag v-for="code in newsCodes(item).slice(0, 5)" :key="`code-${item.id}-${code}`" size="small" type="info" effect="plain">{{ code }}</el-tag>
                  <em v-if="!hasSpecificNewsTarget(item)">未识别到具体板块或代码，仅作市场层信息</em>
                </div>
                <div v-if="item.impact_reason" class="news-impact-reason"><strong>影响逻辑：</strong>{{ item.impact_reason }}</div>
                <span>{{ formatDateTime(item.publish_time) }} · {{ item.source }} · 重要度 {{ integer(item.importance) }}</span>
              </div>
              <el-empty v-if="!newsFilteredItems.length" description="当前筛选条件没有重点消息；可清空筛选查看全部" :image-size="56" />
            </div>
            <el-pagination
              v-if="newsFilteredItems.length > newsPageSize"
              v-model:current-page="newsPage"
              class="news-pagination"
              :page-size="newsPageSize"
              :total="newsFilteredItems.length"
              layout="prev, pager, next"
              small
              background
            />
            <div class="source-note">
              筛选范围是当前不可变快照按“重要度、发布时间”冻结的重点样本，不会混入快照 As-of 之后的新闻；关联板块和代码来自新闻结构化字段，表示影响线索，不证明价格因果。
            </div>
          </section>

          <section class="panel-card dimension-card">
            <div class="section-heading compact-heading">
              <div>
                <div class="panel-title"><el-icon><Histogram /></el-icon>观察池基本面体检</div>
                <div class="section-kicker">回答“冻结观察池估值与盈利质量是否异常”，不负责预测大盘涨跌</div>
              </div>
            </div>
            <el-alert
              :type="fundamentalStatusTag(fundamental.status)"
              :closable="false"
              :title="fundamentalStatusLabel(fundamental.status)"
              :description="fundamental.scope || '基本面口径未提供'"
            />
            <template v-if="hasFundamentalData">
              <div class="metric-grid compact fundamentals">
                <div class="metric-card"><span>观察池覆盖</span><strong>{{ fundamentalCoverage(fundamental) }}</strong></div>
                <div class="metric-card"><span>PE 有效</span><strong>{{ validCoverage(fundamental.valid_pe_count, fundamental.candidate_count) }}</strong></div>
                <div class="metric-card"><span>PE 中位</span><strong>{{ number(fundamental.median_pe_ttm) }}</strong></div>
                <div class="metric-card"><span>PB 中位（有效 {{ integer(fundamental.valid_pb_count) }}）</span><strong>{{ number(fundamental.median_pb) }}</strong></div>
                <div class="metric-card"><span>利润增速中位（有效 {{ integer(fundamental.valid_growth_count) }}）</span><strong :class="valueClass(fundamental.median_net_profit_growth)">{{ signedPct(fundamental.median_net_profit_growth) }}</strong></div>
              </div>
            </template>
            <div v-else class="fundamental-empty-state">
              <strong>何时可用</strong>
              <p>{{ fundamentalActionHint }}</p>
              <span>这里不再用 0 填充缺失数据，避免把“没有统计对象”误读成“估值为 0”。</span>
            </div>
            <div class="source-note">{{ fundamental.purpose || '该模块只做冻结观察池的估值和盈利质量风险检查，不单独产生买点。' }}</div>
          </section>
        </div>

        <section class="panel-card">
          <div class="section-heading compact-heading">
            <div>
              <div class="panel-title"><el-icon><Flag /></el-icon>涨停池与高标反馈</div>
              <div class="section-kicker">用于判断短线容错与次日情绪锚，不等于追高名单</div>
            </div>
          </div>
          <div class="learning-summary">
            <el-tag>涨停 {{ integer(limitLearning.count) }}</el-tag>
            <el-tag type="success">首板 {{ integer(limitLearning.first_board_count) }}</el-tag>
            <el-tag type="warning">连板 {{ integer(limitLearning.consecutive_board_count) }}</el-tag>
            <el-tag
              v-if="hasNumeric(limitLearning.high_board_promotion_rate)"
              :type="Number(limitLearning.high_board_promotion_rate) >= 0.5 ? 'success' : 'danger'"
            >
              高标晋级率 {{ pct(limitLearning.high_board_promotion_rate) }}
            </el-tag>
            <el-tag v-if="hasNumeric(limitLearning.high_board_premium_pct)" type="info">
              昨日高标今日平均涨幅 {{ signedPct(limitLearning.high_board_premium_pct) }}
            </el-tag>
            <el-tag v-if="hasNumeric(limitLearning.previous_board_premium_pct)" type="info">
              昨日涨停今日平均涨幅 {{ signedPct(limitLearning.previous_board_premium_pct) }}
            </el-tag>
            <span
              v-for="item in (limitLearning.reason_distribution || []).slice(0, 6)"
              :key="item.reason"
              class="reason-chip"
            >{{ item.reason }} {{ item.count }}</span>
          </div>
          <el-table :data="limitLearning.high_boards || []" size="small" stripe max-height="480" empty-text="暂无涨停池数据" table-layout="auto">
            <el-table-column label="高度" width="70" class-name="numeric-column">
              <template #default="{ row }"><strong>{{ integer(row.consecutive_days) }}板</strong></template>
            </el-table-column>
            <el-table-column label="代码" width="88" class-name="numeric-column">
              <template #default="{ row }">{{ text(row.code) }}</template>
            </el-table-column>
            <el-table-column label="名称" width="105">
              <template #default="{ row }">{{ text(row.name) }}</template>
            </el-table-column>
            <el-table-column prop="reason" :label="limitReasonLabel" min-width="160" class-name="wrap-column" />
            <el-table-column label="封板时间" width="92" class-name="numeric-column">
              <template #default="{ row }">{{ formatBoardTime(row.limit_up_time) }}</template>
            </el-table-column>
            <el-table-column label="开板次数" width="82" class-name="numeric-column">
              <template #default="{ row }">{{ integer(row.break_count) }}</template>
            </el-table-column>
            <el-table-column label="换手" width="88" class-name="numeric-column">
              <template #default="{ row }">{{ pctPoints(row.turnover) }}</template>
            </el-table-column>
            <el-table-column label="封单资金" width="110" class-name="numeric-column">
              <template #default="{ row }">{{ formatYi(row.seal_amount, true, false) }}</template>
            </el-table-column>
          </el-table>
          <div class="source-note">
            {{ limitReasonNote }}；封板时间统一为 HH:MM:SS，换手率单位为百分数，封单资金原始单位为元并换算为亿元。列表按连板高度、封单资金排序，最多展示 20 只。
          </div>
        </section>

        <section class="panel-card">
          <div class="section-heading compact-heading">
            <div>
              <div class="panel-title"><el-icon><Aim /></el-icon>预测可信度与失败位置</div>
              <div class="section-kicker">
                {{ predictionReview.prediction_trade_date || '--' }} 的冻结预测
                → {{ predictionReview.outcome_trade_date || review.analysis_trade_date || '--' }} 的实际结果；先判断能不能信，再看命中率
              </div>
            </div>
            <el-tag :type="predictionReview.status === 'complete' ? 'success' : 'warning'">
              {{ predictionStatusLabel(predictionReview.status) }}
            </el-tag>
          </div>
          <div v-if="promotionLearningAligned" class="rolling-monitor">
            <div class="rolling-monitor-heading">
              <div>
                <strong>近 {{ integer(rollingReviewDays) }} 个已结算日真实表现</strong>
                <span>截至 {{ promotionLearning.latest_completed_trade_date }}；该监控独立于单日快照，不会反写模型</span>
              </div>
              <router-link to="/promotion"><el-button size="small" plain>查看晋级预测详细复盘</el-button></router-link>
            </div>
            <div class="rolling-lanes">
              <article v-for="target in [1, 2]" :key="`rolling-${target}`" :class="['rolling-lane', `state-${rollingFor(target).state}`]">
                <div>
                  <strong>{{ target === 1 ? '次日首板 Top12' : '首板晋二板主榜' }}</strong>
                  <el-tag size="small" :type="rollingTrustTag(rollingFor(target).state)">{{ rollingTrustLabel(rollingFor(target)) }}</el-tag>
                </div>
                <div class="rolling-metrics">
                  <span>主榜命中 <b>{{ integer(rollingFor(target).hits) }} / {{ integer(rollingFor(target).predicted) }}</b></span>
                  <span>主榜精度 <b>{{ pct(rollingFor(target).precision) }}</b></span>
                  <span>候选池覆盖 <b>{{ integer(rollingFor(target).poolHits) }} / {{ integer(rollingFor(target).actual) }}</b></span>
                  <span>实际召回 <b>{{ pct(rollingFor(target).recall) }}</b></span>
                </div>
                <p>{{ rollingDiagnosis(rollingFor(target)) }}</p>
              </article>
            </div>
          </div>
          <el-alert
            v-else-if="promotionLearningLoading"
            type="info"
            :closable="false"
            title="正在读取最近已结算日的模型真实表现…"
          />
          <el-alert
            v-else-if="promotionLearning.status === 'ok'"
            type="info"
            :closable="false"
            title="当前打开的是历史快照，不叠加后来才知道的滚动成绩"
            description="这是为了避免在历史复盘中混入未来信息；单日冻结漏斗仍可在下方审计。"
          />
          <div class="scorecards">
            <div v-for="target in [1, 2]" :key="target" class="scorecard">
              <div class="score-title">{{ target === 1 ? '次日首板' : '首板晋二板' }}</div>
              <template v-if="scoreFor(target).snapshot_complete">
                <strong>当日主榜精度 {{ pct(scoreFor(target).ranked_precision) }}</strong>
                <div class="funnel-line">
                  <span>实际 <b>{{ integer(scoreFor(target).actual_count) }}</b></span><i>→</i>
                  <span>已入池 <b>{{ integer(scoreFor(target).pool_hit_count) }}</b></span><i>→</i>
                  <span>主榜命中 <b>{{ integer(scoreFor(target).ranked_hit_count) }}</b></span><i>→</i>
                  <span>门禁后命中 <b>{{ integer(scoreFor(target).actionable_hit_count) }}</b></span>
                </div>
                <p>{{ dailyFunnelDiagnosis(target) }}</p>
              </template>
              <template v-else>
                <el-tag type="warning">快照不完整</el-tag>
                <span>候选 {{ integer(scoreFor(target).candidate_count) }} / 最低 {{ integer(scoreFor(target).minimum_candidate_count) }}</span>
                <span>实际 {{ integer(scoreFor(target).actual_count) }} · 禁止计算“未入池”</span>
              </template>
            </div>
          </div>
          <div v-if="(review.attributions || []).length" class="attribution-toolbar">
            <el-select v-model="attributionTargetFilter" size="small">
              <el-option label="全部赛道" value="all" />
              <el-option label="次日首板" value="1" />
              <el-option label="首板晋二板" value="2" />
            </el-select>
            <el-select v-model="attributionTypeFilter" size="small">
              <el-option label="全部失败位置" value="all" />
              <el-option label="未进入候选池" value="recall_miss" />
              <el-option label="已入池但未进主榜" value="ranking_miss" />
              <el-option label="主榜预测未实现" value="false_positive" />
              <el-option label="命中但门禁拦截" value="hit_not_actionable" />
              <el-option label="命中且通过门禁" value="hit" />
            </el-select>
            <span>筛选后 {{ integer(filteredAttributions.length) }} / {{ integer((review.attributions || []).length) }} 条</span>
          </div>
          <el-table :data="filteredAttributions" size="small" stripe max-height="420" empty-text="盘前/盘中不结算，或当前筛选条件无归因记录" table-layout="auto">
            <el-table-column prop="target_board" label="赛道" width="70" class-name="numeric-column">
              <template #default="{ row }">T{{ row.target_board }}</template>
            </el-table-column>
            <el-table-column prop="attribution_type" label="漏斗位置" width="142">
              <template #default="{ row }">
                <el-tag size="small" :type="attributionTag(row.attribution_type)">{{ attributionLabel(row.attribution_type) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="rank_position" label="排名" width="68" class-name="numeric-column">
              <template #default="{ row }">{{ integer(row.rank_position) }}</template>
            </el-table-column>
            <el-table-column prop="code" label="代码" width="88" class-name="numeric-column" />
            <el-table-column prop="name" label="名称" width="100" />
            <el-table-column prop="predicted_probability" label="概率" width="82" class-name="numeric-column">
              <template #default="{ row }">{{ pct(row.predicted_probability) }}</template>
            </el-table-column>
            <el-table-column label="结构原因" min-width="190">
              <template #default="{ row }">{{ reasonLabel(row.primary_reason) }}</template>
            </el-table-column>
            <el-table-column label="因果边界" min-width="185">
              <template #default="{ row }">{{ causalLabel(row.causal_status) }}</template>
            </el-table-column>
          </el-table>
          <div class="source-note">实际样本与晋级预测正式口径一致：仅统计主板/中小板可交易范围，并排除 ST、停牌、退市和隔离记录；“候选池覆盖”不能冒充主榜命中，“门禁后命中”也不能冒充可成交收益。</div>
        </section>

        <div class="bottom-grid">
          <section class="panel-card">
            <div class="panel-title"><el-icon><List /></el-icon>研究与数据后续（不直接改参）</div>
            <div v-for="item in review.action_plan || []" :key="`${item.priority}-${item.action}`" class="action-item">
              <el-tag size="small" :type="item.priority === 'P0' ? 'danger' : 'warning'">{{ item.priority }}</el-tag>
              <div><strong>{{ item.action }}</strong><p>{{ friendlyWarning(item.reason) }}</p></div>
            </div>
            <ul class="guardrail-list"><li v-for="item in review.guardrails || []" :key="item">{{ item }}</li></ul>
          </section>

          <section class="panel-card">
            <div class="panel-title"><el-icon><EditPen /></el-icon>人工复盘笔记（追加留痕）</div>
            <el-form :model="noteForm" label-position="top" size="small">
              <el-form-item label="类型">
                <el-select v-model="noteForm.category">
                  <el-option label="观察" value="observation" />
                  <el-option label="待验证假设" value="hypothesis" />
                  <el-option label="决策" value="decision" />
                  <el-option label="后续任务" value="follow_up" />
                </el-select>
              </el-form-item>
              <el-form-item label="内容">
                <el-input v-model="noteForm.content" type="textarea" :rows="3" maxlength="5000" show-word-limit />
              </el-form-item>
              <el-button type="primary" :disabled="!noteForm.content.trim()" :loading="savingNote" @click="saveNote">追加笔记</el-button>
            </el-form>
            <div class="note-list">
              <div v-for="item in notes" :key="item.id" class="note-item">
                <div><el-tag size="small">{{ noteLabel(item.category) }}</el-tag><span>{{ formatDateTime(item.created_at) }}</span></div>
                <p>{{ item.content }}</p>
              </div>
              <el-empty v-if="!notes.length" description="暂无人工笔记" :image-size="48" />
            </div>
          </section>
        </div>
      </template>

      <el-empty v-else description="请选择交易日与阶段；没有历史快照时可生成第一份不可变快照" />

      <section class="panel-card gpt-panel">
        <div class="section-heading">
          <div>
            <div class="panel-title"><el-icon><ChatDotRound /></el-icon>AI 深度解读（可选）</div>
            <div class="section-kicker">只读取当前选中的冻结快照；确定性结论与下一交易日条件卡无需 AI</div>
          </div>
          <el-tag :type="gptCapabilities.enabled ? 'success' : 'info'">
            {{ gptCapabilities.enabled ? '已配置' : '未配置' }}
          </el-tag>
        </div>

        <el-alert
          v-if="!gptCapabilities.enabled"
          type="info"
          :closable="false"
          :title="gptCapabilities.reason || 'AI 深度解读未配置'"
          description="按钮已安全禁用，不会再发送必然失败的请求；历史已生成报告仍可查看。"
        />

        <div class="gpt-report-bar">
          <div class="gpt-meta">
            <span>提供程序：{{ gptCapabilities.provider || '--' }}</span>
            <span>模型：{{ gptCapabilities.model || '--' }}</span>
            <span>绑定快照：{{ review.id ? `#${review.id}` : '--' }}</span>
          </div>
          <el-button
            type="primary"
            plain
            :loading="generatingReport"
            :disabled="!hasReview || !review.id || !gptCapabilities.enabled"
            @click="generateReport"
          >
            {{ gptReports.length ? '重新生成当前快照解读' : '生成当前快照解读' }}
          </el-button>
        </div>

        <div v-if="selectedReport" class="report-preview">
          <div class="report-preview-head">
            <div>
              <strong>{{ selectedReport.summary || 'AI 复盘报告' }}</strong>
              <span>
                {{ formatTradeDate(selectedReport.review_date) }} · {{ phaseLabel(selectedReport.phase) }} ·
                {{ selectedReport.provider || '--' }} / {{ selectedReport.model || '--' }} · 快照 #{{ selectedReport.snapshot_id || '--' }}
              </span>
            </div>
            <el-tag :type="selectedReport.status === 'generated' ? 'success' : 'danger'">
              {{ selectedReport.status === 'generated' ? '已生成' : '生成失败' }}
            </el-tag>
          </div>
          <el-alert
            v-if="selectedReport.error_message"
            type="error"
            :closable="false"
            :title="selectedReport.error_message"
          />
          <div v-if="reportSections.length" class="report-sections">
            <article v-for="section in reportSections" :key="section.title">
              <strong>{{ section.title }}</strong>
              <p>{{ section.body }}</p>
            </article>
          </div>
        </div>

        <el-table :data="gptReports" size="small" stripe empty-text="当前快照日期暂无 AI 报告" table-layout="auto">
          <el-table-column prop="review_date" label="复盘日" width="108" class-name="numeric-column" />
          <el-table-column prop="phase" label="阶段" width="78">
            <template #default="{ row }">{{ phaseLabel(row.phase) }}</template>
          </el-table-column>
          <el-table-column label="提供程序 / 模型" min-width="180">
            <template #default="{ row }">{{ row.provider || '--' }} / {{ row.model || '--' }}</template>
          </el-table-column>
          <el-table-column prop="status" label="状态" width="94">
            <template #default="{ row }">
              <el-tag size="small" :type="row.status === 'generated' ? 'success' : 'danger'">
                {{ row.status === 'generated' ? '已生成' : '失败' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="summary" label="摘要 / 错误" min-width="280" show-overflow-tooltip>
            <template #default="{ row }">{{ row.summary || row.error_message || '--' }}</template>
          </el-table-column>
          <el-table-column label="生成时间" width="160" class-name="numeric-column">
            <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="74">
            <template #default="{ row }"><el-button link type="primary" @click="openReport(row)">查看</el-button></template>
          </el-table-column>
        </el-table>
      </section>

      <section class="panel-card history-panel">
        <div class="section-heading">
          <div>
            <div class="panel-title"><el-icon><Clock /></el-icon>不可变快照历史</div>
            <div class="section-kicker">用途：还原当时看到了什么、用了哪个版本；同日多条代表数据或版本不同，不会覆盖旧记录</div>
          </div>
          <el-tag type="info">{{ phaseLabel(form.phase) }} · {{ history.length }} 份</el-tag>
        </div>
        <el-table
          :data="history"
          size="small"
          stripe
          table-layout="auto"
          :row-class-name="historyRowClass"
          @row-click="openSnapshot"
          empty-text="当前阶段暂无快照"
        >
          <el-table-column prop="review_date" label="复盘日" width="108" class-name="numeric-column" />
          <el-table-column prop="phase" label="阶段" width="76">
            <template #default="{ row }">{{ phaseLabel(row.phase) }}</template>
          </el-table-column>
          <el-table-column prop="analysis_trade_date" label="日线基准日" width="108" class-name="numeric-column" />
          <el-table-column prop="market_trade_date" label="市场状态日" width="108" class-name="numeric-column" />
          <el-table-column label="质量" width="98">
            <template #default="{ row }"><el-tag size="small" :type="qualityTag(row.quality_status)">{{ qualityLabel(row.quality_status) }}</el-tag></template>
          </el-table-column>
          <el-table-column label="市场风格" min-width="130">
            <template #default="{ row }">{{ marketRegimeName(row) }}</template>
          </el-table-column>
          <el-table-column label="结论" min-width="260" show-overflow-tooltip>
            <template #default="{ row }">{{ row.decision_headline || '旧版快照，打开后生成兼容视图' }}</template>
          </el-table-column>
          <el-table-column label="As-of" width="174" class-name="numeric-column">
            <template #default="{ row }">{{ formatDateTime(row.as_of_at) }}</template>
          </el-table-column>
          <el-table-column label="版本" width="220" show-overflow-tooltip>
            <template #default="{ row }">{{ shortVersion(row.data_version) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="84">
            <template #default="{ row }"><el-button link type="primary" @click.stop="openSnapshot(row)">打开</el-button></template>
          </el-table-column>
        </el-table>
      </section>

      <section class="panel-card automation-panel">
        <div class="section-heading">
          <div>
            <div class="panel-title"><el-icon><Operation /></el-icon>自动化、历史回放与告警</div>
            <div class="section-kicker">三者只负责冻结、重算与提示，不训练、不调参、不晋级、不下单</div>
          </div>
        </div>

        <div class="purpose-grid">
          <article>
            <strong>自动化</strong>
            <p>在盘前、盘中、盘后按时生成快照，并记录成功、降级或失败。</p>
          </article>
          <article>
            <strong>历史回放</strong>
            <p>用同一规则重算历史交易日，检查口径稳定性；默认只读，不写数据库。</p>
          </article>
          <article>
            <strong>告警</strong>
            <p>记录缺失关键源、质量降级或任务失败；它是诊断线索，不是交易信号。</p>
          </article>
        </div>

        <div class="replay-controls">
          <el-date-picker
            v-model="replayForm.range"
            type="daterange"
            value-format="YYYY-MM-DD"
            range-separator="至"
            start-placeholder="回放开始日"
            end-placeholder="回放结束日"
            :disabled-date="disabledDate"
          />
          <el-checkbox-group v-model="replayForm.phases">
            <el-checkbox-button value="premarket">盘前</el-checkbox-button>
            <el-checkbox-button value="intraday">盘中</el-checkbox-button>
            <el-checkbox-button value="postmarket">盘后</el-checkbox-button>
          </el-checkbox-group>
          <el-switch v-model="replayForm.persist" active-text="保存新快照" inactive-text="只读演练" />
          <el-button :loading="replaying" @click="runReplay">
            {{ replayForm.persist ? '执行幂等回放' : '执行只读演练' }}
          </el-button>
        </div>
        <el-alert
          title="只读演练只返回质量和版本，不写库；保存新快照按数据版本幂等追加，绝不覆盖历史。"
          type="info"
          :closable="false"
        />

        <div v-if="lastReplay" class="replay-result">
          <strong>
            最近回放：{{ lastReplay.trade_day_count }} 个交易日 · {{ lastReplay.execution_count }} 个阶段任务 ·
            {{ lastReplay.persisted ? '已保存' : '只读' }}
          </strong>
          <el-table :data="lastReplay.results || []" size="small" max-height="260" table-layout="auto">
            <el-table-column prop="review_date" label="交易日" width="108" class-name="numeric-column" />
            <el-table-column prop="phase" label="阶段" width="76">
              <template #default="{ row }">{{ phaseLabel(row.phase) }}</template>
            </el-table-column>
            <el-table-column label="结果" width="150">
              <template #default="{ row }">{{ automationStatusLabel(row.status) }}</template>
            </el-table-column>
            <el-table-column label="质量" width="96">
              <template #default="{ row }">{{ qualityLabel(row.quality_status) }}</template>
            </el-table-column>
            <el-table-column prop="error" label="说明" min-width="260" show-overflow-tooltip />
          </el-table>
        </div>

        <el-tabs v-model="automationTab" class="automation-tabs">
          <el-tab-pane label="任务运行" name="runs">
            <el-table :data="automationRuns" size="small" stripe max-height="420" empty-text="暂无自动化运行" table-layout="auto">
              <el-table-column prop="review_date" label="日期" width="108" class-name="numeric-column" />
              <el-table-column label="任务" min-width="150">
                <template #default="{ row }">{{ row.job_label || row.job_name }}</template>
              </el-table-column>
              <el-table-column prop="purpose" label="用途" min-width="260" show-overflow-tooltip />
              <el-table-column prop="phase" label="阶段" width="76">
                <template #default="{ row }">{{ phaseLabel(row.phase) }}</template>
              </el-table-column>
              <el-table-column prop="status" label="状态" width="118">
                <template #default="{ row }">
                  <el-tag size="small" :type="automationStatusTag(row.status)">{{ automationStatusLabel(row.status) }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="质量" width="96">
                <template #default="{ row }">{{ qualityLabel(row.quality_status) }}</template>
              </el-table-column>
              <el-table-column label="开始时间" width="160" class-name="numeric-column">
                <template #default="{ row }">{{ formatDateTime(row.started_at) }}</template>
              </el-table-column>
              <el-table-column prop="error_message" label="错误" min-width="200" show-overflow-tooltip />
            </el-table>
          </el-tab-pane>
          <el-tab-pane :label="`告警记录 ${alerts.length}`" name="alerts">
            <el-table :data="alerts" size="small" stripe max-height="420" empty-text="暂无自动化告警" table-layout="auto">
              <el-table-column prop="review_date" label="日期" width="108" class-name="numeric-column" />
              <el-table-column prop="severity" label="级别" width="80">
                <template #default="{ row }"><el-tag size="small" :type="row.severity === 'error' ? 'danger' : 'warning'">{{ row.severity === 'error' ? '错误' : '警告' }}</el-tag></template>
              </el-table-column>
              <el-table-column prop="phase" label="阶段" width="76">
                <template #default="{ row }">{{ phaseLabel(row.phase) }}</template>
              </el-table-column>
              <el-table-column label="类型" min-width="170">
                <template #default="{ row }">{{ row.alert_type_label || row.alert_type }}</template>
              </el-table-column>
              <el-table-column prop="message" label="告警信息" min-width="320" />
              <el-table-column label="时间" width="160" class-name="numeric-column">
                <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
              </el-table-column>
            </el-table>
          </el-tab-pane>
        </el-tabs>
      </section>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import {
  buildDailyReview,
  createDailyReviewNote,
  generateDailyReviewGptReport,
  getDailyReviewAlerts,
  getDailyReviewAutomationRuns,
  getDailyReviewGptCapabilities,
  getDailyReviewGptReports,
  getDailyReviewNotes,
  getDailyReviewSnapshot,
  getDailyReviewSnapshots,
  getPromotionLearningReview,
  replayDailyReviews,
} from '@/api'
import { notifySuccess, notifyWarning } from '@/utils/message'

const form = ref({ review_date: null, phase: 'postmarket', snapshot_context: '', persist: true })
const review = ref({})
const history = ref([])
const notes = ref([])
const automationRuns = ref([])
const alerts = ref([])
const gptReports = ref([])
const selectedReport = ref(null)
const gptCapabilities = ref({
  loaded: false,
  enabled: false,
  provider: '',
  model: '',
  reason: '正在检查 AI 深度解读能力…',
})
const generatingReport = ref(false)
const building = ref(false)
const savingNote = ref(false)
const replaying = ref(false)
const automationTab = ref('runs')
const lastReplay = ref(null)
const replayForm = ref({ range: [], phases: ['postmarket'], persist: false })
const noteForm = ref({ category: 'observation', content: '' })
const promotionLearning = ref({})
const promotionLearningLoading = ref(false)
const newsPolarityFilter = ref('all')
const newsSourceFilter = ref('')
const newsOnlySpecificTarget = ref(false)
const newsKeyword = ref('')
const newsPage = ref(1)
const newsPageSize = 8
const attributionTargetFilter = ref('all')
const attributionTypeFilter = ref('all')

const hasReview = computed(() => Boolean(review.value.id || review.value.review_key))
const dimensions = computed(() => review.value.dimensions || {})
const capital = computed(() => dimensions.value.capital || {})
const news = computed(() => dimensions.value.news || {})
const fundamental = computed(() => dimensions.value.fundamental || {})
const technical = computed(() => dimensions.value.technical || {})
const limitLearning = computed(() => dimensions.value.limit_up_learning || {})
const conclusion = computed(() => review.value.review_conclusion || {})
const nextGuide = computed(() => review.value.next_session_guide || {})
const predictionReview = computed(() => review.value.prediction_review || {})
const hasFundamentalData = computed(() => Number(fundamental.value.candidate_count || 0) > 0)
const fundamentalEmptyTitle = computed(() => ({
  prediction_plan_missing: '下一交易日观察池尚未冻结，当前没有统计对象',
  intraday_spot_missing: '观察池已存在，但盘中基本面截面尚未就绪',
  daily_snapshot_missing: '观察池已存在，但当日基本面截面缺失',
}[fundamental.value.availability_reason] || '当前快照没有可用的观察池基本面数据'))
const fundamentalActionHint = computed(() => {
  if (fundamental.value.availability_reason === 'prediction_plan_missing' && review.value.phase === 'postmarket') {
    return '正式盘后观察池使用 20:00 冻结预测批次；优先查看 20:35 自动生成的盘后复盘快照。'
  }
  if (fundamental.value.availability_reason === 'prediction_plan_missing') {
    return '先等待当前阶段预测观察池冻结，再统计这些候选的估值与盈利质量。'
  }
  if (fundamental.value.availability_reason === 'daily_snapshot_missing') {
    return '日终基本面截面通常在 16:05 后形成；历史缺口不会用今天的数据回填。'
  }
  return fundamental.value.scope || '当前没有可核验的基本面样本。'
})
const newsSourceOptions = computed(() => [...new Set(
  (news.value.items || []).map(item => String(item.source || '').trim()).filter(Boolean),
)].sort())
const newsFilteredItems = computed(() => {
  const keyword = newsKeyword.value.trim().toLowerCase()
  return (news.value.items || []).filter((item) => {
    if (newsPolarityFilter.value !== 'all' && newsItemPolarity(item) !== newsPolarityFilter.value) return false
    if (newsSourceFilter.value && String(item.source || '') !== newsSourceFilter.value) return false
    if (newsOnlySpecificTarget.value && !hasSpecificNewsTarget(item)) return false
    if (!keyword) return true
    const haystack = [
      item.title,
      item.summary,
      item.impact_reason,
      item.category,
      item.source,
      ...newsCodes(item),
      ...newsSectors(item),
    ].join(' ').toLowerCase()
    return haystack.includes(keyword)
  })
})
const newsPagedItems = computed(() => {
  const start = (newsPage.value - 1) * newsPageSize
  return newsFilteredItems.value.slice(start, start + newsPageSize)
})
const filteredAttributions = computed(() => (review.value.attributions || []).filter((item) => (
  (attributionTargetFilter.value === 'all' || String(item.target_board) === attributionTargetFilter.value)
  && (attributionTypeFilter.value === 'all' || item.attribution_type === attributionTypeFilter.value)
)))
const promotionLearningAligned = computed(() => (
  promotionLearning.value.status === 'ok'
  && Boolean(review.value.analysis_trade_date)
  && promotionLearning.value.latest_completed_trade_date === review.value.analysis_trade_date
))
const rollingReviewDays = computed(() => Math.max(
  rollingFor(1).days,
  rollingFor(2).days,
))
const rollingLanes = computed(() => ({
  1: buildRollingLane(1),
  2: buildRollingLane(2),
}))
const buildButtonLabel = computed(() => `生成并冻结${phaseLabel(form.value.phase)}快照`)
const marketFlowLabel = computed(() => (
  capital.value.market_main_net_inflow_scope === 'tradeable_non_st'
    ? '可交易池主力净流入'
    : '主力净流入（旧口径）'
))
const sampledFlowLabel = computed(() => (
  capital.value.sample_basis === 'top_main_net_inflow_30'
    ? `净流入 TOP${integer(capital.value.sampled_stock_count)} 合计`
    : '个股样本净流入（旧口径）'
))
const limitReasonLabel = computed(() => limitLearning.value.reason_label || '涨停归类')
const limitReasonNote = computed(() => ({
  industry_classification: '东财来源字段是“所属行业”，仅表示行业归类，不代表已确认事件因果',
  cause_category: '问财来源字段是“涨停原因类别”，属于数据源归类，不代表研究层已验证因果',
  mixed_source_classification: '当前列表混合行业与原因类别，须结合每条来源解读，不作统一因果断言',
}[limitLearning.value.reason_basis] || '旧快照未记录归类来源，只能按展示标签解读'))
const dateMismatch = computed(() => (
  (review.value.phase === 'intraday' && !review.value.market_trade_date)
  || (review.value.phase === 'postmarket'
    && review.value.review_date
    && review.value.analysis_trade_date
    && review.value.review_date !== review.value.analysis_trade_date)
))

const REGIME_LABELS = {
  risk_off: '退潮 / 风险规避',
  recovery: '冰点修复',
  sector_rotation: '板块轮动',
  sector_maintrend: '板块主升',
  individual_maintrend: '个股独立主升',
  high_board_speculation: '高标投机',
  broad_trend: '普涨趋势',
  balanced: '均衡震荡',
  unknown: '未知',
}
const SOURCE_LABELS = {
  stock_kline_close: '收盘日 K',
  intraday_spot: '盘中实时快照',
  daily_snapshot: '当日基本面截面',
  unavailable_for_historical_snapshot: '历史截面缺失',
}
const QUALITY_LABELS = { good: '完整', partial: '部分可用', insufficient: '不足' }
const AUTOMATION_STATUS_LABELS = {
  completed: '完成',
  completed_with_warnings: '完成但有警告',
  running: '运行中',
  already_completed: '已幂等完成',
  failed: '失败',
  failed_stale: '超时已释放',
  dry_run: '只读演练完成',
  skipped: '已跳过',
}
const SOURCE_NAMES = {
  stock_kline: '日 K',
  market_sentiment: '市场情绪',
  limit_up_pool: '涨停池',
  market_regime: '市场风格',
  promotion_outcome_review: '晋级预测结果复盘',
  promotion_prediction: '晋级预测快照',
  intraday_spot: '盘中行情',
  intraday_temporal_provenance: '盘中日表历史时点凭证',
  fund_flow: '资金流',
  sector: '板块',
  news: '新闻',
}

function numeric(value) {
  if (value === null || value === undefined || value === '') return null
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : null
}
const text = (value) => {
  if (value === null || value === undefined) return '--'
  const normalized = String(value).trim()
  return normalized || '--'
}
const hasNumeric = (value) => numeric(value) !== null
const pct = (value) => {
  const parsed = numeric(value)
  return parsed === null ? '--' : `${(parsed * 100).toFixed(1)}%`
}
const pctPoints = (value) => {
  const parsed = numeric(value)
  return parsed === null ? '--' : `${parsed.toFixed(1)}%`
}
const signedPct = (value) => {
  const parsed = numeric(value)
  return parsed === null ? '--' : `${parsed > 0 ? '+' : ''}${parsed.toFixed(2)}%`
}
const number = (value, digits = 2) => {
  const parsed = numeric(value)
  return parsed === null ? '--' : parsed.toFixed(digits)
}
const integer = (value) => {
  const parsed = numeric(value)
  return parsed === null ? '--' : Math.round(parsed).toLocaleString('zh-CN')
}
function formatYi(value, fromYuan = false, signed = true) {
  const parsed = numeric(value)
  if (parsed === null) return '--'
  const amount = fromYuan ? parsed / 100000000 : parsed
  const prefix = signed && amount > 0 ? '+' : ''
  return `${prefix}${amount.toFixed(2)}亿`
}
function formatTurnover(value) {
  const parsed = numeric(value)
  return parsed === null ? '--' : `${parsed.toFixed(2)}万亿`
}
function fundamentalCoverage(item) {
  const covered = numeric(item?.candidate_count)
  const requested = numeric(item?.requested_candidate_count)
  const ratio = numeric(item?.coverage_ratio)
  if (covered === null) return '--'
  if (requested === null || requested <= 0) return integer(covered)
  return `${integer(covered)} / ${integer(requested)}${ratio === null ? '' : ` · ${pct(ratio)}`}`
}
function validCoverage(valid, total) {
  const parsedValid = numeric(valid)
  const parsedTotal = numeric(total)
  if (parsedValid === null) return '--'
  if (parsedTotal === null || parsedTotal <= 0) return integer(parsedValid)
  return `${integer(parsedValid)} / ${integer(parsedTotal)}`
}
const valueClass = (value) => {
  const parsed = numeric(value)
  return parsed === null ? '' : parsed > 0 ? 'is-up' : parsed < 0 ? 'is-down' : ''
}

const phaseLabel = (phase) => ({ premarket: '盘前', intraday: '盘中', postmarket: '盘后' }[phase] || phase || '--')
const qualityLabel = (status) => QUALITY_LABELS[status] || status || '--'
const qualityTag = (status) => status === 'good' ? 'success' : status === 'partial' ? 'warning' : 'danger'
const newsTag = (value) => value === 'bull' ? 'success' : value === 'bear' ? 'danger' : 'info'
const newsLabel = (value) => ({ bull: '利好', bear: '利空', neutral: '中性', unanalyzed: '未分析' }[value] || '中性')
const attributionTag = (type) => type === 'hit' ? 'success' : type === 'hit_not_actionable' ? 'warning' : 'danger'
const attributionLabel = (type) => ({
  hit: '命中',
  hit_not_actionable: '命中但门禁拦截',
  ranking_miss: '排序漏失',
  recall_miss: '召回漏失',
  false_positive: '主榜误报',
}[type] || type || '--')
const reasonLabel = (reason) => ({
  not_in_candidate_pool: '未进入候选池',
  in_pool_but_not_ranked: '已入池但未进主榜',
  ranked_hit_blocked_by_trade_gate: '主榜命中但交易门禁拦截',
  ranked_actionable_hit: '主榜且通过门禁',
  ranked_prediction_not_realized: '主榜预测未实现',
}[reason] || reason || '--')
const causalLabel = (status) => ({
  observed_structural_location: '只确认漏斗位置，不证明市场因果',
  hypothesis_not_proven: '误差已观察，原因仍待验证',
}[status] || status || '--')
const noteLabel = (type) => ({ observation: '观察', hypothesis: '假设', decision: '决策', follow_up: '后续' }[type] || type)
const scoreFor = (target) => predictionReview.value.targets?.[String(target)] || {}
const predictionStatusLabel = (status) => ({
  complete: '已完成归因',
  snapshot_incomplete: '上一日快照不完整',
  not_applicable_before_close: '盘前 / 盘中不结算',
  no_previous_session: '无上一交易日样本',
}[status] || status || '未结算')
const automationStatusTag = (status) => status === 'completed' ? 'success' : status === 'completed_with_warnings' ? 'warning' : status === 'running' ? 'primary' : status === 'already_completed' || status === 'dry_run' ? 'info' : 'danger'
const automationStatusLabel = (status) => AUTOMATION_STATUS_LABELS[status] || status || '--'
const stanceTagType = (stance) => stance === 'offensive' ? 'success' : stance === 'defensive' ? 'danger' : stance === 'selective' ? 'warning' : 'info'
const toneTagType = (tone) => tone === 'positive' ? 'success' : tone === 'danger' ? 'danger' : tone === 'warning' ? 'warning' : 'info'
const sourceLabel = (source) => SOURCE_LABELS[source] || source || '--'
const sentimentQualityLabel = (status) => ({ ok: '完整', degraded: '降级', missing: '缺失' }[status] || status || '--')
const fundamentalStatusTag = (status) => status === 'daily_snapshot' || status === 'intraday_spot' ? 'success' : 'warning'
const fundamentalStatusLabel = (status) => ({
  daily_snapshot: '已使用当日冻结基本面截面',
  intraday_spot: '已使用盘中候选截面',
  unavailable_for_historical_snapshot: '历史基本面截面缺失',
}[status] || fundamentalEmptyTitle.value)

function normalizedStringList(value) {
  return Array.isArray(value) ? value.map(item => String(item || '').trim()).filter(Boolean) : []
}
function newsCodes(item) {
  return normalizedStringList(item?.related_codes)
}
function newsSectors(item) {
  return normalizedStringList(item?.related_sectors)
}
function hasSpecificNewsTarget(item) {
  if (typeof item?.has_specific_target === 'boolean') return item.has_specific_target
  return Boolean(newsCodes(item).length || newsSectors(item).length)
}
function newsItemPolarity(item) {
  const status = String(item?.nlp_status || '').trim().toLowerCase()
  if (status && !['analyzed', 'fallback'].includes(status)) return 'unanalyzed'
  return ['bull', 'bear', 'neutral'].includes(item?.bull_bear) ? item.bull_bear : 'neutral'
}
function resetNewsPage() {
  newsPage.value = 1
}

function buildRollingLane(target) {
  const key = `target_${target}`
  const rows = (promotionLearning.value.daily || [])
    .map(item => item?.lane_metrics?.[key])
    .filter(item => item?.snapshot_complete)
    .slice(0, 5)
  const sum = field => rows.reduce((total, item) => total + Number(item?.[field] || 0), 0)
  const predicted = sum('predicted_count')
  const hits = sum('hit_count')
  const actual = sum('actual_count')
  const poolHits = sum('pool_hit_count')
  const precision = predicted ? hits / predicted : null
  const recall = actual ? hits / actual : null
  const poolRecall = actual ? poolHits / actual : null
  const state = !predicted ? 'no_data' : !hits ? 'no_hits' : precision < 0.1 ? 'low' : 'observed'
  return {
    target,
    days: rows.length,
    predicted,
    hits,
    actual,
    poolHits,
    precision,
    recall,
    poolRecall,
    zeroHitDays: rows.filter(item => !Number(item?.hit_count || 0)).length,
    state,
  }
}
function rollingFor(target) {
  return rollingLanes.value?.[target] || buildRollingLane(target)
}
function rollingTrustTag(state) {
  return state === 'no_hits' || state === 'low' ? 'danger' : state === 'observed' ? 'warning' : 'info'
}
function rollingTrustLabel(item) {
  if (item.state === 'no_data') return '样本不足'
  if (item.state === 'no_hits') return `近${item.days}日未命中`
  if (item.state === 'low') return '命中偏低'
  return '有命中，仍需门禁'
}
function rollingDiagnosis(item) {
  if (!item.predicted) return '没有足够的正式主榜样本，不能评价预测能力。'
  if (!item.hits && item.poolHits) {
    return `候选池覆盖 ${integer(item.poolHits)} / ${integer(item.actual)} 个实际样本，但正式主榜没有命中，失败主要发生在排序层；当前不应把该榜单当作决策信号。`
  }
  if (!item.hits) return '候选池与正式主榜均未命中，需先补召回证据，再讨论排序。'
  return `近${item.days}日有 ${integer(item.hits)} 次主榜命中，但仍有 ${integer(item.actual - item.hits)} 个实际样本未被主榜覆盖，只能结合交易门禁作弱参考。`
}
function dailyFunnelDiagnosis(target) {
  const item = scoreFor(target)
  if (!item.snapshot_complete) return '快照不完整，不能把缺失记录归因为模型失败。'
  const notInPool = Number(item.not_in_pool_count || 0)
  const rankingMiss = Number(item.ranking_miss_count || 0)
  const rankedCount = Number(item.ranked_count || 0)
  const rankedHits = Number(item.ranked_hit_count || 0)
  if (!rankedHits && Number(item.pool_hit_count || 0) > 0) {
    return `${integer(notInPool)} 个实际样本未入池，${integer(rankingMiss)} 个已入池但未进主榜；主榜 ${integer(rankedCount)} 只零命中，主要失败点是排序。`
  }
  return `${integer(notInPool)} 个实际样本未入池，${integer(rankingMiss)} 个已入池但未进主榜；主榜 ${integer(rankedCount)} 只命中 ${integer(rankedHits)} 只。`
}

function friendlyWarning(value) {
  let text = String(value || '')
  for (const [code, label] of Object.entries(SOURCE_NAMES)) {
    text = text.replaceAll(code, label)
  }
  return text
}
function shortVersion(value) {
  const text = String(value || '--')
  if (text.length <= 28) return text
  return `${text.slice(0, 16)}…${text.slice(-8)}`
}
function parseDateOnly(value) {
  if (!value) return null
  const match = String(value).match(/^(\d{4})-(\d{2})-(\d{2})/)
  if (!match) return null
  return new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]))
}
function formatTradeDate(value) {
  if (!value) return '--'
  const parsed = parseDateOnly(value)
  if (!parsed || Number.isNaN(parsed.getTime())) return String(value)
  const weekday = ['日', '一', '二', '三', '四', '五', '六'][parsed.getDay()]
  return `${String(value).slice(0, 10)} 周${weekday}`
}
function formatDateTime(value) {
  if (!value) return '--'
  return String(value).replace('T', ' ').replace(/\.\d+$/, '').slice(0, 19)
}
function formatBoardTime(value) {
  const raw = String(value ?? '').trim()
  if (!raw) return '--'
  const colonTime = raw.match(/(?:^|\s|T)(\d{1,2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?(?:$|[Z+-])/)
  let hours
  let minutes
  let seconds
  if (colonTime) {
    hours = Number(colonTime[1])
    minutes = Number(colonTime[2])
    seconds = Number(colonTime[3] || 0)
  } else {
    const digits = raw.replace(/\.0+$/, '').replace(/\D/g, '')
    if (digits.length < 4 || digits.length > 6) return '--'
    const padded = digits.length === 4 ? `${digits}00` : digits.padStart(6, '0')
    hours = Number(padded.slice(0, 2))
    minutes = Number(padded.slice(2, 4))
    seconds = Number(padded.slice(4, 6))
  }
  if (hours > 23 || minutes > 59 || seconds > 59) return '--'
  return [hours, minutes, seconds].map(item => String(item).padStart(2, '0')).join(':')
}
function marketRegimeName(item) {
  if (!item) return '--'
  if (item.market_regime_name) return item.market_regime_name
  if (typeof item.market_regime === 'object') return item.market_regime.primary_regime_name || REGIME_LABELS[item.market_regime.primary_regime] || '--'
  return REGIME_LABELS[item.market_regime] || item.market_regime || '--'
}
function disabledDate(value) {
  const endOfToday = new Date()
  endOfToday.setHours(23, 59, 59, 999)
  return value.getTime() > endOfToday.getTime()
}
function contentToText(value) {
  if (value === null || value === undefined) return ''
  if (typeof value === 'string') return value
  if (Array.isArray(value)) {
    return value.map((item) => typeof item === 'string' ? item : JSON.stringify(item, null, 2)).join('\n')
  }
  if (typeof value === 'object') {
    return Object.entries(value).map(([key, item]) => `${key}：${contentToText(item)}`).join('\n')
  }
  return String(value)
}

const reportSections = computed(() => {
  const content = selectedReport.value?.content
  if (!content || typeof content !== 'object') return []
  if (Array.isArray(content.sections)) {
    return content.sections.map((section, index) => ({
      title: section?.title || `第 ${index + 1} 节`,
      body: contentToText(section?.body ?? section?.content ?? section?.points ?? section),
    }))
  }
  const labels = {
    market_state: '市场状态',
    conclusion: '核心结论',
    next_session: '下一交易日',
    risks: '风险',
    observations: '观察',
  }
  return Object.entries(content)
    .filter(([key]) => !['summary', 'title', 'snapshot_version'].includes(key))
    .slice(0, 8)
    .map(([key, value]) => ({ title: labels[key] || key, body: contentToText(value) }))
})

async function loadHistory() {
  try {
    const result = await getDailyReviewSnapshots({ phase: form.value.phase, limit: 100 })
    history.value = result.snapshots || []
  } catch {
    history.value = []
  }
}

async function loadAutomation() {
  const [runsResult, alertsResult] = await Promise.allSettled([
    getDailyReviewAutomationRuns({ limit: 100 }),
    getDailyReviewAlerts({ limit: 100 }),
  ])
  automationRuns.value = runsResult.status === 'fulfilled' ? (runsResult.value.runs || []) : []
  alerts.value = alertsResult.status === 'fulfilled' ? (alertsResult.value.alerts || []) : []
}

async function loadPromotionLearning() {
  promotionLearningLoading.value = true
  try {
    promotionLearning.value = await getPromotionLearningReview({ lookback_days: 5 })
  } catch {
    promotionLearning.value = {}
  } finally {
    promotionLearningLoading.value = false
  }
}

async function loadGptCapabilities() {
  try {
    gptCapabilities.value = { loaded: true, ...(await getDailyReviewGptCapabilities()) }
  } catch {
    gptCapabilities.value = {
      loaded: true,
      enabled: false,
      provider: '',
      model: '',
      reason: '当前后端尚未提供 AI 能力状态；确定性复盘不受影响。',
    }
  }
}

async function loadGptReports() {
  if (!review.value.review_date) {
    gptReports.value = []
    selectedReport.value = null
    return
  }
  try {
    const result = await getDailyReviewGptReports({
      review_date: review.value.review_date,
      phase: review.value.phase,
      snapshot_id: review.value.id,
      limit: 20,
    })
    gptReports.value = result.reports || []
    const selectedId = selectedReport.value?.id
    selectedReport.value = gptReports.value.find((item) => item.id === selectedId) || gptReports.value[0] || null
  } catch {
    gptReports.value = []
    selectedReport.value = null
  }
}

async function generateReport() {
  if (!gptCapabilities.value.enabled || !review.value.id) return
  generatingReport.value = true
  try {
    const report = await generateDailyReviewGptReport({
      review_date: review.value.review_date,
      phase: review.value.phase,
      snapshot_id: review.value.id,
    })
    selectedReport.value = report
    notifySuccess(report.status === 'generated' ? '当前冻结快照的 AI 解读已生成' : `报告状态：${report.status}`)
    await loadGptReports()
  } catch {
    // Global interceptor shows the backend reason; a previous good report remains available.
  } finally {
    generatingReport.value = false
  }
}

async function loadNotes() {
  if (!review.value.review_date) {
    notes.value = []
    return
  }
  try {
    const result = await getDailyReviewNotes({
      review_date: review.value.review_date,
      phase: review.value.phase,
    })
    notes.value = result.notes || []
  } catch {
    notes.value = []
  }
}

async function openSnapshot(row) {
  try {
    review.value = await getDailyReviewSnapshot(row.id, { include_attributions: true })
    newsPage.value = 1
    attributionTargetFilter.value = 'all'
    attributionTypeFilter.value = 'all'
    form.value.review_date = review.value.review_date
    form.value.phase = review.value.phase
    if (!replayForm.value.range?.length && review.value.analysis_trade_date) {
      replayForm.value.range = [review.value.analysis_trade_date, review.value.analysis_trade_date]
    }
    await Promise.all([loadNotes(), loadGptReports()])
  } catch {
    // Global interceptor handles the error.
  }
}

async function switchPhase() {
  const selectedDate = form.value.review_date
  review.value = {}
  notes.value = []
  gptReports.value = []
  selectedReport.value = null
  await loadHistory()
  const matching = history.value.find((item) => item.review_date === selectedDate)
  if (matching) {
    await openSnapshot(matching)
  } else {
    form.value.review_date = selectedDate
  }
}

async function onReviewDateChange(value) {
  if (!value) return
  const matching = history.value.find((item) => item.review_date === value)
  if (matching) {
    await openSnapshot(matching)
  } else {
    review.value = {}
    notes.value = []
    gptReports.value = []
    selectedReport.value = null
  }
}

async function buildSnapshot() {
  building.value = true
  try {
    const result = await buildDailyReview({ ...form.value })
    review.value = result
    form.value.review_date = result.review_date
    form.value.phase = result.phase
    if (result.quality?.status === 'good') notifySuccess(`已冻结${phaseLabel(result.phase)}复盘快照`)
    else notifyWarning(`快照已冻结，但数据质量为“${qualityLabel(result.quality?.status)}”`)
    await Promise.all([loadHistory(), loadNotes(), loadAutomation(), loadGptReports(), loadPromotionLearning()])
  } catch {
    // Global interceptor provides the precise date or data-quality reason.
  } finally {
    building.value = false
  }
}

async function runReplay() {
  if (!replayForm.value.range?.length || !replayForm.value.phases.length) {
    notifyWarning('请选择回放日期范围和至少一个阶段')
    return
  }
  replaying.value = true
  try {
    const result = await replayDailyReviews({
      start_date: replayForm.value.range[0],
      end_date: replayForm.value.range[1],
      phases: replayForm.value.phases,
      persist: replayForm.value.persist,
      force: false,
    })
    lastReplay.value = result
    notifySuccess(`${result.persisted ? '回放快照已幂等保存' : '只读演练完成'}：${result.execution_count} 个阶段任务`)
    await Promise.all([loadHistory(), loadAutomation()])
  } catch {
    // Global interceptor handles the error.
  } finally {
    replaying.value = false
  }
}

async function saveNote() {
  if (!review.value.review_date || !noteForm.value.content.trim()) return
  savingNote.value = true
  try {
    await createDailyReviewNote({
      review_date: review.value.review_date,
      phase: review.value.phase,
      category: noteForm.value.category,
      content: noteForm.value.content,
      tags: [],
    })
    noteForm.value.content = ''
    notifySuccess('复盘笔记已追加并留痕')
    await loadNotes()
  } catch {
    // Global interceptor handles the error.
  } finally {
    savingNote.value = false
  }
}

function openReport(row) {
  selectedReport.value = row
}
function historyRowClass({ row }) {
  return row.id === review.value.id ? 'selected-snapshot-row' : ''
}

onMounted(async () => {
  loadPromotionLearning()
  await Promise.all([loadHistory(), loadAutomation(), loadGptCapabilities()])
  if (history.value.length) await openSnapshot(history.value[0])
})
</script>

<style scoped lang="scss">
.review-page {
  display: flex;
  flex-direction: column;
  gap: 18px;
}

.review-controls {
  display: flex;
  align-items: flex-end;
  justify-content: flex-end;
  gap: 10px;
  flex-wrap: wrap;
}

.control-field {
  display: flex;
  flex-direction: column;
  gap: 6px;

  > span {
    color: var(--claw-text-muted);
    font-size: 11px;
    font-weight: 600;
  }
}

.snapshot-header {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.snapshot-title-row,
.section-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 14px;
}

.snapshot-tags {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.version-line,
.section-kicker,
.source-note {
  color: var(--claw-text-muted);
  font-size: 12px;
}

.version-line {
  max-width: 360px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.date-facts {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;

  > div {
    min-width: 0;
    padding: 12px 14px;
    border: 1px solid var(--claw-border);
    border-radius: 10px;
    background: var(--claw-bg-soft);
  }

  span {
    display: block;
    margin-bottom: 5px;
    color: var(--claw-text-muted);
    font-size: 11px;
  }

  strong {
    color: var(--claw-text-primary);
    font-size: 14px;
    white-space: nowrap;
  }
}

.decision-headline {
  margin: 6px 0 8px;
  color: var(--claw-text-primary);
  font-size: clamp(20px, 2vw, 28px);
  line-height: 1.35;
}

.decision-summary,
.guide-summary {
  margin: 0;
  color: var(--claw-text-secondary);
  font-size: 14px;
  line-height: 1.8;
}

.evidence-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
  margin-top: 16px;
}

.evidence-card,
.focus-card,
.playbook-card,
.purpose-grid article,
.report-sections article {
  min-width: 0;
  padding: 13px;
  border: 1px solid var(--claw-border);
  border-radius: 10px;
  background: var(--claw-bg-soft);
}

.evidence-card {
  border-left-width: 3px;

  > span {
    display: block;
    color: var(--claw-text-muted);
    font-size: 11px;
  }

  > strong {
    display: block;
    margin: 7px 0;
    color: var(--claw-text-primary);
    font-size: 14px;
    line-height: 1.45;
  }

  p {
    margin: 0;
    color: var(--claw-text-secondary);
    font-size: 12px;
    line-height: 1.6;
  }
}

.tone-positive { border-left-color: var(--claw-success, #10b981); }
.tone-warning { border-left-color: var(--claw-warning, #f59e0b); }
.tone-danger { border-left-color: var(--claw-danger, #ef4444); }
.tone-neutral { border-left-color: var(--claw-primary); }
.tone-muted { border-left-color: var(--claw-border); }

.risk-strip,
.do-not-line {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 14px;
  padding: 11px 13px;
  border: 1px solid rgba(245, 158, 11, 0.32);
  border-radius: 10px;
  background: rgba(245, 158, 11, 0.07);
  color: var(--claw-text-secondary);
  font-size: 12px;

  strong {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    color: #b45309;
  }

  span + span::before {
    content: '·';
    margin-right: 8px;
    color: var(--claw-text-muted);
  }
}

.guide-panel {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.focus-grid,
.playbook-grid,
.purpose-grid,
.report-sections {
  display: grid;
  gap: 10px;
}

.focus-grid {
  grid-template-columns: repeat(3, minmax(0, 1fr));
}

.playbook-grid,
.purpose-grid {
  grid-template-columns: repeat(3, minmax(0, 1fr));
}

.focus-card > strong,
.purpose-grid strong,
.report-sections strong {
  color: var(--claw-text-primary);
  font-size: 13px;
}

.focus-chips {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  margin: 9px 0;
}

.focus-card p,
.playbook-card p,
.purpose-grid p,
.report-sections p {
  margin: 6px 0 0;
  color: var(--claw-text-secondary);
  font-size: 12px;
  line-height: 1.65;
  white-space: pre-wrap;
}

.focus-card b,
.playbook-card b {
  display: inline-block;
  min-width: 38px;
  color: var(--claw-text-primary);
}

.watchlist-block {
  border-top: 1px solid var(--claw-border);
  padding-top: 13px;
}

.subsection-title {
  margin-bottom: 10px;
  color: var(--claw-text-primary);
  font-size: 13px;
  font-weight: 700;
}

.dimension-grid,
.bottom-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 18px;
}

.dimension-card {
  min-width: 0;
}

.metric-row {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 9px;
  margin-bottom: 12px;
}

.metric-row > div,
.metric-card {
  min-width: 0;
  padding: 11px;
  border: 1px solid var(--claw-border);
  border-radius: 10px;
  background: var(--claw-bg-soft);
}

.metric-row span,
.metric-card span {
  display: block;
  margin-bottom: 5px;
  color: var(--claw-text-muted);
  font-size: 11px;
}

.metric-row strong,
.metric-card strong {
  color: var(--claw-text-primary);
  font-size: 16px;
  white-space: nowrap;
}

.metric-grid.compact {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 9px;
}

.metric-grid.fundamentals {
  margin-top: 12px;
}

.source-note {
  margin-top: 11px;
  line-height: 1.55;
}

.news-summary,
.learning-summary {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 12px;
  color: var(--claw-text-muted);
  font-size: 12px;
}

.news-toolbar {
  display: grid;
  grid-template-columns: minmax(210px, 1fr) 120px auto;
  align-items: center;
  gap: 9px;
  margin-bottom: 8px;

  :deep(.el-radio-group) {
    grid-column: 1 / -1;
  }
}

.news-filter-result {
  margin-bottom: 10px;
  color: var(--claw-text-muted);
  font-size: 12px;

  strong {
    color: var(--claw-text-primary);
  }
}

.news-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
  max-height: 360px;
  overflow: auto;
}

.news-item {
  padding-bottom: 9px;
  border-bottom: 1px solid var(--claw-border);

  > div {
    display: flex;
    align-items: flex-start;
    gap: 7px;
  }

  strong {
    color: var(--claw-text-primary);
    font-size: 13px;
    line-height: 1.5;
  }

  p {
    margin: 5px 0;
    color: var(--claw-text-secondary);
    font-size: 12px;
    line-height: 1.55;
  }

  > span {
    color: var(--claw-text-muted);
    font-size: 11px;
  }
}

.news-impact-row {
  align-items: center !important;
  flex-wrap: wrap;
  margin: 7px 0;

  > span {
    color: var(--claw-text-muted);
    font-size: 11px;
    font-weight: 700;
  }

  em {
    color: var(--claw-text-muted);
    font-size: 11px;
    font-style: normal;
  }
}

.news-impact-reason {
  margin: 6px 0;
  color: var(--claw-text-secondary);
  font-size: 11px;
  line-height: 1.55;
}

.news-pagination {
  justify-content: flex-end;
  margin-top: 12px;
}

.fundamental-empty-state {
  margin-top: 12px;
  padding: 18px;
  border: 1px dashed var(--claw-border);
  border-radius: 10px;
  background: var(--claw-bg-soft);

  strong,
  span {
    display: block;
  }

  strong {
    color: var(--claw-text-primary);
    font-size: 14px;
  }

  p {
    margin: 8px 0;
    color: var(--claw-text-secondary);
    font-size: 12px;
    line-height: 1.6;
  }

  span {
    color: var(--claw-text-muted);
    font-size: 11px;
  }
}

.reason-chip {
  padding: 3px 9px;
  border: 1px solid var(--claw-border);
  border-radius: 999px;
  background: var(--claw-bg-soft);
}

.compact-heading {
  margin-bottom: 12px;
}

.scorecards {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 12px;
  margin-bottom: 14px;
}

.rolling-monitor {
  margin-bottom: 14px;
  padding: 14px;
  border: 1px solid var(--claw-border);
  border-radius: 12px;
  background: var(--claw-bg-soft);
}

.rolling-monitor-heading,
.rolling-lane > div:first-child,
.attribution-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
}

.rolling-monitor-heading {
  margin-bottom: 12px;

  strong,
  span {
    display: block;
  }

  strong {
    color: var(--claw-text-primary);
    font-size: 14px;
  }

  span {
    margin-top: 4px;
    color: var(--claw-text-muted);
    font-size: 11px;
  }
}

.rolling-lanes {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}

.rolling-lane {
  padding: 12px;
  border: 1px solid var(--claw-border);
  border-left: 3px solid var(--claw-warning, #f59e0b);
  border-radius: 10px;
  background: var(--claw-bg-card, var(--claw-bg-soft));

  &.state-no_hits,
  &.state-low {
    border-left-color: var(--claw-danger, #ef4444);
  }

  > div:first-child > strong {
    color: var(--claw-text-primary);
    font-size: 13px;
  }

  > p {
    margin: 9px 0 0;
    color: var(--claw-text-secondary);
    font-size: 12px;
    line-height: 1.6;
  }
}

.rolling-metrics {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 7px;
  margin-top: 10px;

  span {
    color: var(--claw-text-muted);
    font-size: 11px;
  }

  b {
    color: var(--claw-text-primary);
  }
}

.scorecard {
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: 9px;
  padding: 13px;
  border: 1px solid var(--claw-border);
  border-radius: 12px;
  background: var(--claw-bg-soft);
  color: var(--claw-text-secondary);
  font-size: 13px;
}

.score-title {
  color: var(--claw-text-primary);
  font-weight: 700;
}

.scorecard > strong {
  color: var(--claw-accent);
}

.scorecard > p {
  margin: 0;
  color: var(--claw-text-secondary);
  font-size: 12px;
  line-height: 1.55;
}

.funnel-line {
  display: flex;
  align-items: center;
  gap: 7px;
  flex-wrap: wrap;

  span {
    padding: 5px 8px;
    border-radius: 7px;
    background: var(--claw-bg-card, rgba(127, 127, 127, 0.08));
    color: var(--claw-text-muted);
    font-size: 11px;
  }

  b {
    color: var(--claw-text-primary);
  }

  i {
    color: var(--claw-text-muted);
    font-style: normal;
  }
}

.attribution-toolbar {
  justify-content: flex-start;
  margin-bottom: 10px;
  color: var(--claw-text-muted);
  font-size: 12px;

  :deep(.el-select) {
    width: 180px;
  }
}

.action-item {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 10px 0;
  border-bottom: 1px solid var(--claw-border);

  strong {
    color: var(--claw-text-primary);
    font-size: 13px;
  }

  p {
    margin: 4px 0 0;
    color: var(--claw-text-muted);
    font-size: 12px;
    line-height: 1.5;
  }
}

.guardrail-list {
  padding-left: 18px;
  color: var(--claw-text-secondary);
  font-size: 12px;
  line-height: 1.8;
}

.note-list {
  max-height: 260px;
  margin-top: 14px;
  overflow: auto;
}

.note-item {
  padding: 9px 0;
  border-top: 1px solid var(--claw-border);

  > div {
    display: flex;
    justify-content: space-between;
    color: var(--claw-text-muted);
    font-size: 11px;
  }

  p {
    margin: 7px 0 0;
    color: var(--claw-text-secondary);
    font-size: 13px;
    line-height: 1.6;
    white-space: pre-wrap;
  }
}

.gpt-panel,
.automation-panel {
  display: flex;
  flex-direction: column;
  gap: 13px;
}

.gpt-report-bar,
.gpt-meta,
.replay-controls {
  display: flex;
  align-items: center;
  gap: 9px;
  flex-wrap: wrap;
}

.gpt-report-bar {
  justify-content: space-between;
}

.gpt-meta {
  color: var(--claw-text-muted);
  font-size: 12px;
}

.report-preview {
  padding: 14px;
  border: 1px solid var(--claw-border);
  border-radius: 12px;
  background: var(--claw-bg-soft);
}

.report-preview-head {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;

  strong,
  span {
    display: block;
  }

  strong {
    color: var(--claw-text-primary);
  }

  span {
    margin-top: 5px;
    color: var(--claw-text-muted);
    font-size: 11px;
  }
}

.report-sections {
  grid-template-columns: repeat(2, minmax(0, 1fr));
  margin-top: 12px;
}

.history-panel :deep(.el-table__row) {
  cursor: pointer;
}

.history-panel :deep(.selected-snapshot-row td.el-table__cell) {
  background: rgba(14, 165, 233, 0.09) !important;
}

.purpose-grid article {
  border-left: 3px solid var(--claw-primary);
}

.replay-controls {
  justify-content: flex-end;
}

.replay-result {
  display: flex;
  flex-direction: column;
  gap: 9px;
  padding: 12px;
  border: 1px solid var(--claw-border);
  border-radius: 10px;
  background: var(--claw-bg-soft);
  color: var(--claw-text-primary);
  font-size: 13px;
}

.automation-tabs {
  margin-top: 2px;
}

.is-up {
  color: var(--claw-up, #ef4444) !important;
}

.is-down {
  color: var(--claw-down, #10b981) !important;
}

.review-page :deep(.el-table .cell) {
  word-break: normal;
  overflow-wrap: anywhere;
}

.review-page :deep(.el-table th.el-table__cell),
.review-page :deep(.el-table td.el-table__cell) {
  padding-left: 0;
  padding-right: 0;
}

.review-page :deep(.el-table .wrap-column .cell) {
  overflow: visible;
  text-overflow: clip;
  white-space: normal;
  line-height: 1.45;
}

.review-page :deep(.el-table .numeric-column .cell) {
  white-space: nowrap;
  overflow-wrap: normal;
}

@media (max-width: 1200px) {
  .date-facts,
  .evidence-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .focus-grid,
  .playbook-grid,
  .purpose-grid {
    grid-template-columns: 1fr;
  }

  .dimension-grid,
  .bottom-grid {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 1000px) {
  .report-sections {
    grid-template-columns: 1fr;
  }

  .news-toolbar {
    grid-template-columns: 1fr 1fr;
  }
}

@media (max-width: 720px) {
  .review-controls {
    align-items: stretch;
    justify-content: flex-start;
  }

  .control-field,
  .review-controls :deep(.el-date-editor),
  .review-controls > .el-button {
    width: 100%;
  }

  .snapshot-title-row,
  .section-heading,
  .report-preview-head {
    flex-direction: column;
  }

  .date-facts,
  .evidence-grid,
  .metric-row,
  .metric-grid.compact,
  .scorecards,
  .rolling-lanes,
  .news-toolbar {
    grid-template-columns: 1fr;
  }

  .news-toolbar :deep(.el-radio-group),
  .news-toolbar :deep(.el-input),
  .news-toolbar :deep(.el-select),
  .attribution-toolbar :deep(.el-select) {
    width: 100%;
  }

  .date-facts strong {
    white-space: normal;
  }

  .replay-controls {
    justify-content: flex-start;
  }

  .replay-controls :deep(.el-date-editor) {
    width: 100%;
  }
}
</style>
