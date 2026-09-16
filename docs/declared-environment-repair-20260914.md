# 第28轮：声明依赖重建与数据库启动修复（未部署）

## 范围

原服务仍保留原监督者/8000，不重启、不迁移、不访问业务API或下单。本轮先核对第27轮562个backend输入SHA全保持，再在私密目录创建独立venv，不带system-site-packages，不升级/降级全机环境。网络仅用于从明确PyPI安装依赖，不用于pytest或市场取数。

## 已确认缺口

1. 从冻结requirements与requirements-dev首次安装失败：PyPI未提供适合本次环境的pywencai==0.14.2，可见版本最高0.13.1；当前原环境实际也为0.13.1。仅将该直接声明改为0.13.1，没有修改其分页/重试/数据源逻辑，也不把安装成功当接口真实覆盖。
2. 原app.db.session向所有默认连接池传pool_size/max_overflow/pool_timeout。现有SQLAlchemy2.0.48下，三种内存URL均在导入时报StaticPool非法参数；两种文件URL正常。新增回归改前3 failed/3 passed，另独立最小复现exit1。
3. 独立环境真实安装SQLAlchemy2.0.35后，对第27轮冻结源码/临时文件库的最小导入同样 **exit1/NullPool非法pool_size/max_overflow/pool_timeout**。因此这不只是内存测试问题，而是声明依赖下原文件库启动也失败；现有2.0.48恰好隐式默认异步队列池掩盖了它。没有在生产上复现失败。

## 修改

- 新app/db/engine_options.py仅构造参数，不导入settings、不建engine/连接。SQLite文件显式AsyncAdaptedQueuePool，保留原20/30/60/1800配置输入、pre_ping和30秒busy timeout；内存空URL/:memory:/mode=memory用StaticPool，避免不同槽位产生不同数据库，不传队列专属参数。
- SQLite专属check_same_thread/timeout不再传给非SQLite驱动。非SQLite仅测试参数构造，不声称PostgreSQL真实连接/迁移已验收。
- session.py只替换engine参数构造，全部既有函数/类AST保持，未改init_db、迁移、账本、交易风控或策略公式。
- 新11项边界：5种实际应用engine独立子进程、连接池容量/回收、文件双连接可见性、事务rollback、内存同库、仅允许临时SQLite且拒绝网络/子进程；5种纯参数合同；声明pin/asyncio extra。首轮当前依赖11 passed/2.31s、主审计联网0。

## 独立环境与验收

- 修正pywencai声明后完整安装成功，`pip check`报告No broken requirements found。100份已安装分发（含venv引导工具）全部位于私密venv，sys.prefix不同于base_prefix，user-site关闭；没有混入全机/user-site路径。
- 关键实际版本现在与声明一致：SQLAlchemy2.0.35、pandas2.2.3、numpy2.1.1、FastAPI0.115.0、pywencai0.13.1。直接pin以外仍解析了当时的传递依赖；保留pip安装来源/hash报告和完整版本表，而非把一次联网解析叫稳定锁定。
- 声明环境首3文件 **95 passed / 8.77s，联网0、exit0**，包含完整临时030→033真实Alembic/禁调度lifespan及源分页状态测试。pytest-asyncio0.24提示loop_scope未显式声明及旧event_loop覆写弃用，未隐藏；当前固定版本下可运行，不代表未来pytest兼容。
- 首版冻结564个backend输入，归档SHA6e922679d1a228a046e1bf839c0101a0899bc06c85a2dbf01b62be76d0be14f9。完整声明环境56文件 **2664 passed / 19 failed / 404.85s，联网0但整体exit1**。19项全部为真实IC入口缺少scipy；pip check只能验证已声明依赖，不能证明项目遗漏的业务import可用。现有环境实际SciPy1.17.1，Requires-Python>=3.11、numpy>=1.26.4,<2.7兼容本轮2.1.1。补为直接依赖，不改Spearman公式/阈值，不做假IC降级。首版venv/wheelhouse/冻结包/失败报告全部保留，另建含SciPy的最终离线环境复跑。现有环境7文件执行链 **461 passed / 137.11s、联网0**；独立13GB已033副本严格禁调度/禁网络/精确库身份smoke成功（内部2.685s、1个DB连接、禁用动作0），前后78表计数/33核心全字段摘要/既有schema与15保护触发器门禁通过。此处仅复用独立已033恢复副本，不冒称再做原库031–033迁移或更新原服务。
- 按本次98个实际下载SHA构建独立wheelhouse，逐wheel名称/版本与安装报告一致，保存最终wheel字节SHA（包含jsonpath/PyExecJS两个本地构建wheel）；第二个全新venv用`--no-index --only-binary=:all: --no-deps --require-hashes`离线安装成功，审计联网/子进程尝试0、pip check通过。不修改第一个测试venv或原服务解释器。bootstrap使用本机Python3.11的venv/pip/setuptools；这是本机macOS arm64轮子集合，不是跨平台锁、供应链安全审计或独立Python镜像。

## 最终候选与离线重建

- 最终564文件归档SHA **d26704b31ea8f950a6fdb7449f0a3708f0c684eab1d729b9240800a7f0537c32**；相对首版仅requirements新增SciPy与声明回归增加对应断言，运行app/alembic源码与已做大库smoke的版本保持。
- 保留前两环境作为诊断证据，另建`logs/repair-rehearsal-20260915/final-env-round28`：99个wheel离线安装成功，联网/子进程尝试0；`pip check`通过。101个安装分发（含2个bootstrap工具）与首版100个名称/版本完全一致再加SciPy1.17.1，24条生产/开发直接声明全部满足，所有分发位于新prefix且user-site关闭。
- 最终wheelhouse为`logs/repair-rehearsal-20260915/wheelhouse-round28-final`；`outputs/repair_validation_20260914_round28/final-offline-wheel-lock.txt`固定99个实际wheel哈希。重建必须使用同架构Python3.11、新venv、该wheelhouse和该锁；`--no-index --only-binary=:all: --no-deps --require-hashes`后仍需pip check和实际业务回归。SciPy1.17.1要求Python>=3.11，本轮不验证Python3.10、Linux或其他架构。
- 首版离线环境95 passed/8.34s、联网0仅是启动/迁移/源契约聚焦，不包含失败的IC；最终新环境56文件联合 **2683 passed / 372.66s，0失败/跳过，联网0、整体exit0**（bash-346，06:50:23至06:56:52）；3条测试摘要警告，另保留asyncio默认loop_scope的启动弃用提示。共225个测试文件选择56个，不称全项目/全分支覆盖，先前聚焦及重复环境测试不叠加计数。最终大库smoke内部2.443s、1个DB连接、198个app模块均来自最终冻结目录、禁用动作0；78表计数/33核心全字段摘要/schema/15保护再次通过（bash-347），仍只独立已033副本。

## 最终保全

- 06:57:56原8000仍PID67367（9/14 20:06:43启动）/schema030；与本轮06:43:21以及第27轮相比，110交易日志/2258订单/293回报/因子值0/评估运行0五表全字段SHA一致。仅该子集，未声称活跃原库全部内容不变。
- 原9/14冻结复盘库803618816字节及SHA68a7d1fb336da3bb46a92b6672dda3e3bff8c1de28a0588a0aa400fa00f7ae05保持。564个最终工作区/归档/解包输入SHA与Python AST验后匹配；99个最终wheel字节SHA全部匹配。
- 本轮4个backend输出：requirements.txt、app/db/session.py、app/db/engine_options.py、tests/test_database_engine_options_20260914.py。原562基线中仅两个旧文件修改，其余560不改；旧session全部函数/类AST保持。共享session改动只在全局engine初始化，合并时保留该局部。
- 21份中间原日志（含安装/启动/19项IC失败）逐成员SHA归档；最终联合原日志另存tar，避免工具长行显示截断。候选清单、99轮子清单与锁、三环境版本和安装报告、两轮大库前后报告、源库子集及最终文档快照由outputs/repair_validation_20260914_round28/final_manifest.json串联。

## 正式发布及总目标剩余

原服务仍030，未完成维护停写窗口、当时一致性备份/回滚单元、原库精确031–033迁移、禁调度核验、单独恢复既有调度和真实交易日前向验收。本轮没有修改业务公式/阈值、推送或交易入口、ORM/Alembic或前端；不需要前端构建。只读独立发布审查本轮尚无可采纳结果，不当成已通过的独立审核。

真实逐股时点上下文/三模块因子证据引用、完整候选→确认→发送→订单原因链、历史10/193对账、同预算时间外路线/容量/退出研究仍未完成。不把环境重建、pytest或私密副本smoke当作生产部署、策略收益或市场PIT证明。
