# 9/14 股票身份与风险投影修复（第6轮，源码阶段）

## 已确认缺陷
1. StockTagger.get_board_type只检查前缀；filter_signals对缺代码原样放行，缺tag/错误board_tag可默认is_tradeable=true；mark_observe=False或保留ST研究样本时也可能错误变为true。
2. mapping的batch_tag默认False覆盖停牌/ST/涨跌停/次新字段，且未用is_delisting派生禁止标签。
3. scheduler股票状态任务把一次查询缺席当复牌/摘帽/解除退市风险，删除所有普通StockTag；对风险代码DELETE+INSERT会抹掉IPO/停牌原因和手工黑名单的来源/起始日/期限。
4. trading._pre_trade_risk_check缺tag默认tradeable且不读取StockBlacklist，不识别现有spot的ST名称。risk/check也允许请求字段代替缺失数据库身份。

## 本轮契约与实际影响
- 在既有StockTagger复用纯resolve_status、load_status。代码要求精确ASCII六位和已有板块映射；is_tradeable(code)保留“仅板块准入”兼容语义，明确不代表完整资格。
- 新status版本stock_status_projection_v1_20260914，basis=current_projection_not_historical_pit、execution_authorized=false。known_projection只表示当前投影字段未冲突，不是官方身份认证/状态新鲜度或历史可见性证书。
- 名称ST/*ST/SST/S*ST、退市字样/退前后缀只增加风险；is_st/is_delisting/is_suspended、人工/有效黑名单及原禁止/停牌/观察限制取并集。未知代码/缺tag/缺name/board_type冲突/名称冲突/未知board_tag均不能新买。创业板等不能被错误tradeable字段改为主板。
- filter_signals保留未知项供观察但is_tradeable=false，已有buy_allowed字段同步false；即使mark_observe=false或exclude_blocked=false也不恢复交易资格。不修改原候选dict/旧预测台账。当前名称冲突不能靠猜简称或自动纠名解除。
- load_status在实际下单前风控和risk/check复用；当前spot名称只能增加风险/冲突，不能补全缺tag；风险API请求仅可增加限制。原T+1/限价排队/涨跌停可成交/账户风控仍由原链路处理。风险身份规则仍仅限制新买，不把退出路径一律锁死。
- tag_stock/batch_tag改为增量合并：缺省/False不清除已知ST/停牌/退市风险；保留IPO及未提供的涨跌停字段，已有IPO日期按原60日参数重新推导次新。未提供解禁的业务入口。
- scheduler只合并正向风险；保留loop=True四查询，类型/列/代码格式/交易所前后缀/空名称/同码不同名/跨日均校验。坏批全拒；单查询空但其余有效则只存正向风险并返回degraded，四空失败。非明确交易日不运行。
- *ST在现有is_delisting风险字段保留为“退市风险”，不是实际退市事实；同时算ST。四查询交集只计唯一代码，不重计北交所。所有既有tag正常行不删除；旧blacklist所有字段不动，新名单auto_expire=false；覆盖率不作保证、automatic_clear_count=0。这会保留未核验的旧限制，直到未来有逐代码真实解除证据和显式审计流程，不能用漏列表解除。
- 本轮不改模型公式、买入阈值、生产数据/配置、DB schema或历史证据。

## 实际只读对照（非部署）
21:06:59对实际backend/claw.db使用SQLite mode=ro、query_only及一致读事务，不调用业务GET。产物：outputs/repair_validation_20260914_round6/current-identity-readonly.json。
- 5228标签/185黑名单/5225行情名称；并集5228，按新源码当前投影2967板块身份允许、1968观察、293禁止（不是可买股票池/收益样本）。
- 85个名称冲突、2个未知代码，未自动“修正”原名。
- 002743：标签富煌钢构/is_st=0，spot为ST富煌；新投影禁止。600228：原board_tag=tradeable但is_delisting=1且有退市风险黑名单；新投影禁止。
- 688835/601123仍缺tag和spot，新投影只能未知观察；不猜身份/补行情。
- 两个表全字段排序repr摘要前后均027162ca83a4869e97276e8a304af77d0f2f4620ffd113f31f09089cd515bb43；只证明本次只读快照一致，不证明其他服务永不写入。

## 验证记录
- 初始4文件38 passed / 6 failed（4.17s）：六个成交/排队fixture只有spot，没有独立StockTag。已给正常成交身份显式构造，保留原成交、FIFO、版本取消、撤单断言，不放宽生产规则。
- 新身份边界+基础tagger+采集77 passed（6.54s）。
- 10文件521 passed / 3 failed（55.74s）：晋级API正常候选fixture缺tag使其不再进入ranked。已仅给000010、000020场景增加真实隔离StockTag，未改概率/排名断言。
- 第一轮扩展回归1947 passed（162.52s）；期间父又发现仅候选名称可能陈旧、没有消费当前spot的ST名称，已让get_signal_filter/filter_signals额外合并当前quote_name（只增加风险），并增补旧候选名不能藏住002743现名ST风险的测试。所有共享状态读取在首次await前固定当前判断时点；非法代码load_status直接返回未知而不发起无效主键查询。此处新增后另跑最终大组，不拿前一轮结果冒充最终源码验收。
- 进一步阅读DataQualityGuard确认record_success在未提供expected_count时默认完整率100%，与coverage_verified=false矛盾。父改为全量状态仍degraded、positive_merge_status=ok，健康记录明确未知覆盖/未核验解除；即使四列表非空也不冒充全量健康。测试明确验证不写success/100%记录。
- 含当前quote_name过滤的扩展大组 **2081 passed / 181.17s**（bash-177）。启动后仅scheduler健康语义从旧ok改为degraded，并追加交易日/跨日/过期名单保护测试；这些最终变更由随后6文件 **139 passed / 15.16s**（bash-178）验收。两组重叠，不相加作独立样本；不声称最后一次全量重跑覆盖全仓库。新身份文件合计68例。所有父bash-173至178已收取，初始失败如上保留说明。
- 当前四个源文件AST语法检查通过；scheduler本轮状态方法无delete调用和尾随空格。后端仅既有python_multipart弃用警告。
- 两个只读审查子任务仍以实际结束消息为准，本记录不将尚未收到的费用/NAV或身份审查结论作为通过证据。

## 部署与剩余风险
- 21:08左右ps确认原8000仍PID67367，启动20:06:43，为第4轮发布；第5/6轮源码尚未部署。前端无本轮文件变更，不假称构建/交互已验收。
- 当前StockTag/StockBlacklist是可变风险投影，不是版本化官方证券目录，不能用于恢复历史身份时点。合法更名与源错误目前均保守冲突；需要后续前向来源/角色/时间证据，不能直接覆盖这85条。
- 多处is_tradeable(code)仍是板块预筛选/展示，缓存和其他原始策略路径未在本轮全部证明复用新身份投影；不能声称全系统身份已穷尽。
- 本轮未动RiskEngine异常WARN逻辑、原始paper_buy业务入口、候选→确认→推送→订单物理时间、真实日初NAV/未来部分卖出费用。这些独立风险仍需后轮审计修复。
- git diff --check在scheduler其他既有并行段报尾随空格（本轮1649–1761局部外），未顺手格式化覆盖他人修改。
