# 买入延期单确认观测（confirmation:deferred_buy_v1）

## 缺口与修复
原提交链在更新页面候选的order_confirmation_evidence之前，就将下单前candidate序列化到订单risk_json。后续延期撮合读取的是这份原始证据，不能直接作为新轮次执行结果；六个无成交分支还曾丢失deferred，旧单日志版本会冒用当前账户版本。

本补丁保留各分支原deferred，paper._reconcile_deferred_order_logs对买入显式传本轮真实outcome进入普通日志冻结器；卖出独立观察原样保留。所有延期日志使用原委托版本，缺失为legacy_unversioned。仅候选里伪装marker不能绕过原提交前freeze_log_evidence。

## 四层语义
- 历史确认：只保留原订单候选已有事实；确实缺失仍unknown，不凭filled补历史confirmed。
- 当前形态：延期撮合没有重跑完整形态确认，因此current_setup_valid=unknown。原形态及原观察钟保存在original_decision_confirmation，不能冒充本轮形态。
- 本轮执行：匹配本订单/股票/买入身份的实际本轮成交回报且event/status一致时，execution_permitted=true，仅表示本轮已报告执行，不是允许再次下单；risk_blocked/rejected/canceled且本轮回报空时false；waiting一律unknown，即使订单累计status=partial也不能用上一轮成交证明本轮许可。矛盾、缺回报或坏身份unknown。
- 订单结果：直接保留本轮委托状态，与本轮许可分开；已提交后被拒绝/撤销不得重写成not_submitted。

## 时钟与故障
原报价轮参数、真实物理响应观察钟、订单decision/trade_date分列，commit_known_at=null、replay_ready=false。不接受跨日/未来观察参数；可选观察失败时当前证据降unknown，原业务日志照常，警告仅错误类型，不吞取消。没有新增查询、风险判断、提交、撮合、成交费用计算或业务提交，不改历史订单/日志/持仓。

## 独立研究适配（Round23，ab_dependency_research_v2）
新协议只有在批次冻结时间已到达物理response_observed_at时才可读，且必须匹配日志实例/原版本、订单账户/股票/买入身份、原决策轮与本轮、事件/状态/数量/执行标签以及原历史证据。缺失、冲突、错误marker、未来时钟仍unknown，保留样本槽位而不是删掉失败分母；旧协议的observed_at<=created_at门原样保留。

输出response_available_at和decision_at两种时钟：前者仅约束结果可见性，后者仍是原轮次参数。资金/情绪/竞价附件仍按原decision_at截断，不能用稍后收到的执行结果延长事前信息窗口。原历史确认不得来自建仓决策之后；跨日撤单的旧历史不能冒充当天确认。该适配只验证owned日志投影合同，不提供原始归档认证或未知commit首知，不产生场景下单许可/可成交收益。

## 验证边界
test_deferred_buy_confirmation.py走真实submit_order→reconcile_paper_deferred_orders→_add_auto_log，报价/风控/券商仅隔离夹具。覆盖初次提交序列化、12种分支（含warn及部分成交后的报价/深度等待）、有/无原证据、同轮重入和旧日志/候选不变。不是自然交易或收益证明；测试和部署结果在当轮outputs台账记录。
