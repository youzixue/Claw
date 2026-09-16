# 第24轮：截面逐股上下文隔离（源码阶段，未部署）

## 实际缺陷与归因边界

实读 FactorEngine.compute_cross_section 确认旧实现把同一份 **kwargs 传给所有证券。个股ROE、估值、新闻、板块资金等一旦以该方式传入，会被当作每只股票的输入；显式按代码传入的未知参数又被默默忽略。新回归在修改前复现3项失败。

全项目Python调用检索中，此方法目前只有测试调用。单股API和显式批量存储走 compute_single，因此**不能把这项潜在截面计算缺陷认定为9/14雷达、晋级或模拟盘踏空的直接原因**。本轮不为历史结果补造因果解释。

## 改动与输入契约

局部修改：
- backend/app/factors/base.py：新增 conditional_context 声明；只重做 compute_cross_section 的参数收口/解释元数据并在概要增加声明，保留原 compute_single 和排名公式。
- news.py、margin.py、breakout.py：各加一个条件输入声明，不改 calculate 函数。
- computation_evidence.py：已加载实现描述纳入 conditional_context。
- 新 test_factor_cross_section_context_20260914.py；旧 test_factor_input_contract.py 的一项参数化排名测试仅把六个文字标签改成六位证券代码，保留所有零/缺失/置信度/方向断言。

新调用显式为 contexts_by_code={证券代码: {已声明参数: 标量}}：
1. 禁止旧的共享 **kwargs 广播，即使参数看起来是市场情绪，也需由调用者显式映射到每个证券。
2. stock_data 为显式dict，代码为ASCII六位、值为DataFrame、trade_date为明确date。缺少某只证券的上下文只用空dict；多余代码、拼错字段、非dict/嵌套容器/日期对象等在计算前拒绝。
3. 首个await前冻结全部股票成员和逐股标量。调用者在第一只计算期间修改另一只输入映射，不会串到后续股票。**不复制或证明DataFrame内容，原DataFrame仍由调用者负责**。
4. 真零、负数、有限数保留；非有限数/溢出数转未知，bool保持为bool供原验证器拒绝，不转换为0/1。某个数值无效只让依赖它的因子未知，不借用另一股票的值。
5. 延续原排名方向、有效值与置信度过滤、稳定同分顺序和百分位公式；不改阈值、权重或因子公式，不做隐式全市场续批。
6. 新闻平均重要性仅在非空新闻窗口需要，首板时间仍由原交易时钟规则校验，融资余额均值仍是原历史帧分支之外的条件输入；增加声明不把这些字段强制为无条件必需，不推断默认零。

每个结果附 cross_section_context：代码、声明的交易日、该因子声明参数中的实际提供值、缺失必需字段、明确 caller_supplied_unverified。argument_scope=declared_arguments_not_dynamic_usage：列出传入的声明参数，**不声称它们在每个条件分支中都被使用**。

point_in_time_verified、trading_authority、promotion_eligible、automatic_weight_update 均为false。这里不读取新闻/板块/财报源，不验证它们的实体归属、首次可用钟或报告期，也不把调用者给的数当作真实历史证据。

## 与既有捕获的关系

- 概要中的 conditional_context 为兼容性新增字段；前端未修改。
- 新实现描述包含条件字段，旧描述格式仍可只读；不能悄悄补声明/改SHA后假称旧版本复算。
- 同描述才允许复算的原规则不变。版本描述不同的旧记录将拒绝在当前实现下复算，需要匹配的旧实现；不是把记录删除或改写。
- 033捕获仍为原 bounded_per_stock 日线研究路径，context={}，单股API/批量存储不读取这份内存截面上下文，也不生成截面排名。此次没有给v1捕获塞入未定义的新上下文结构或迁移历史。
- 新的逐股传参能力**尚未接入雷达、正式晋级、纸盘决策引用**。完整真实时点上下文适配和共享采证仍待落实，不能称48个因子全部可用。

## 验证记录

- 起始复核第23轮全部36个backend输入SHA一致。
- 修改前针对旧广播/逐股ROE/板块新闻隔离的3个新回归全部失败（bash-298，3 failed / 94 deselected / 1.63s）；这是隔离基线，不是生产测试。
- 首轮新上下文97项加原输入契约643项：**740 passed / 1.81s**（bash-299）。
- 后续补旧格式只读/拒绝错版复算、单股存储不借截面内存两个真实临时SQL案例，共99个新增案例。
- 最终联合 **1261 passed / 75.65s，0 failed/skip**（bash-300）；覆盖因子/资金/日K/日历、候选证据、路线/容量/排名研究、迁移与部署审计，不是全项目测试。保留3条既有multipart/pct_change弃用警告，不以本轮修改经济公式来消除它们。
- 48个实际因子及基类calculate函数的AST与验前一致。未调整经济公式，不把正确传参等同于收益改善。
- 所有测试数据库由conftest/tmp_path隔离；未调用生产业务API、下单、推送、迁移、重启或修改前端。
- 最终37个backend输入AST/SHA匹配，3份原日志逐字节归档；原冻结复盘库803618816字节及SHA保持。04:45:59生产五表全列SHA/行数、PID/schema较04:40:35全部相同。首次归档核验脚本误把SQLite tuple与JSON list直接比较而失败；诊断并规范化表示后重验通过，未因此改源码或放宽数据合同。
- 证据入口 outputs/repair_validation_20260914_round24/final_manifest.json。

## 部署与剩余限制

04:40:35只读：原8000仍PID67367/schema030，033新表不存在；110交易/2258订单/293回报，factor_values及factor_evaluation_run各0。本轮只做源码变更，未受控发布；PID不变本身也不能证明旧进程所有惰性加载模块来自同一修订。

仍需真实逐股/时点上下文、三模块明确capture/decision引用、完整候选→确认→发送→订单链、时间外同预算收益/容量/退出研究及031–033和第5轮以后源码的受控发布/负载/前向验收。只读发布就绪审计另行提供下一轮路线，不在本轮顺手操作原服务。
