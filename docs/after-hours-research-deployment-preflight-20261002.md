# 盘后研究部署前检查：历史检查记录

> 用户16:47确认非交易日部署后，17:05已按建议完成仅研究发布包上线。当前状态以[部署结果](</Users/youzix/WorkBuddy/Claw/docs/after-hours-research-deployment-20261002.md>)为准，以下“等待确认/尚未部署”为16:45前置检查的历史状态。

检查日期：2026-10-02（Asia/Shanghai）。用户16:35批准部署；授权范围沿用腾讯/新浪盘后强弱与次日研究，不包含盘后撮合、实盘、旧长期目标恢复或其他功能上线。

## 实测结论

- 原服务：`127.0.0.1:8000`，监督者 `gui/501/com.claw.dev.backend`，PID80654，2026-09-30 22:47:43启动。前端5173的PID1218未动。
- [最近发布记录](</Users/youzix/WorkBuddy/Claw/outputs/buy_point_release_20260930/restart_result.json>)与当前PID一致；[基线验收](</Users/youzix/WorkBuddy/Claw/outputs/buy_point_release_20260930/acceptance.json>)明确保留候选影子研究worker既有异常。本次health返回ok、调度运行、54个作业，不代表所有模块健康。
- 当前运行库是既有SQLite；只读事务确认Alembic版本为`037_limit_pool_source_evidence`，不存在`stock_after_hours_observation`及任何after_hours命名schema对象。研究上线需要精确研究迁移038，不应升head或应用039/040撮合资源迁移。
- 原.env和实际LaunchAgents配置的SHA与9月30日发布清单一致。未公开配置内容或复制数据库。原库约41.33GB，不能为本任务重复制作整库副本。
- 当前源码与最近254文件发布基线相比，14个既有生产模块不同；还有新增模块。不是14项故障，而是尚未加载的变更集合，不能因工作树dirty直接认定都未经验证。

## 直接重启的范围冲突

14个既有差异模块如下。仅根据发布清单比对与局部差异检查；未声称完成其全量部署审查：

1. [paper API](</Users/youzix/WorkBuddy/Claw/backend/app/api/v1/paper.py>)
2. [trading API](</Users/youzix/WorkBuddy/Claw/backend/app/api/v1/trading.py>)
3. [settings](</Users/youzix/WorkBuddy/Claw/backend/app/config/settings.py>)
4. [外盘展示来源](</Users/youzix/WorkBuddy/Claw/backend/app/dashboard2/external_sources.py>)
5. [scheduler](</Users/youzix/WorkBuddy/Claw/backend/app/data/scheduler.py>)
6. [同花顺日K来源](</Users/youzix/WorkBuddy/Claw/backend/app/data/sources/ths_kline_source.py>)
7. [stock models](</Users/youzix/WorkBuddy/Claw/backend/app/models/stock.py>)
8. [trading models](</Users/youzix/WorkBuddy/Claw/backend/app/models/trading.py>)
9. [外盘新闻来源](</Users/youzix/WorkBuddy/Claw/backend/app/news/sources/global_market.py>)
10. [持仓策略](</Users/youzix/WorkBuddy/Claw/backend/app/paper/position_policy.py>)
11. [复盘服务](</Users/youzix/WorkBuddy/Claw/backend/app/review/service.py>)
12. [纸面授权](</Users/youzix/WorkBuddy/Claw/backend/app/trading/paper_authorization.py>)
13. [执行完整性](</Users/youzix/WorkBuddy/Claw/backend/app/trading/paper_execution_integrity.py>)
14. [交易服务](</Users/youzix/WorkBuddy/Claw/backend/app/trading/service.py>)

共享scheduler还含盘后人工意图失效作业，除三项研究作业外会新增交易相关调度；当前trading models还有撮合资源表。普通重启会加载整个当前工作树并执行init_db的create_all，不能保证仅部署研究038，也不能把“没有开启撮合”替代“没有同时部署交易变更”。

[通用启动脚本](</Users/youzix/WorkBuddy/Claw/scripts/dev_services.sh>)的restart会停前后端、清端口及重写监督配置，不用于本次窄部署。实际监督配置与该脚本保存位置不同，已核对实际LaunchAgents配置，未修改。

## 建议下一步（待用户确认）

基于[9月30日发布清单](</Users/youzix/WorkBuddy/Claw/outputs/buy_point_release_20260930/manifest.json>)和已有源码归档准备独立、可核对的**仅研究发布包**；不覆盖或回滚当前工作树。只纳入腾讯/新浪原生盘后量额、研究ORM/038保护、必要研究调度与次日只读依赖；排除订单API、人工意图失效作业、撮合资源模型/039/040及无关外盘采集改动。

准备阶段需明确研究依赖闭包、发布包与基线差异、配置及代码回退方式，再对确切发布包做隔离验证。之后精确受控更新原服务，核对加载路径、来源配置、迁移/保护、调度与账务前后指纹。保留现有模拟盘普通功能，不连实盘、不新增长期自动任务、不恢复旧目标。若必要依赖无法隔离，先报告，不擅自扩大范围。

## 本次实际动作与未做事项

只做工作树/归档SHA/进程/监督配置/健康与SQLite只读schema检查，并记录本报告。**尚未部署**：未迁移、未重启、未变更运行配置、未运行生产采集、未调用交易/账户/推送接口、未新增任务。没有改生产代码，也没有因本次只读检查重跑并冒称上一轮405项测试为新测试。

本报告不是数据库完整备份、运行加载版本证明、供应商SLA或自然交易日验收。原服务保持原状态；既有影子研究worker异常不在本次修复范围。
