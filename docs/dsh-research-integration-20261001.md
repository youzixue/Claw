# Claw DSH 研究接入验收（2026-10-01）

> 首轮接入记录，保留原测试/日期。后续审查发现5条模型GET有缺表DDL风险，现已改独立只读连接，
> 工具已扩为22个、bundle1.1.0/MCP1.3.0；双阶段门禁、任务与真实覆盖以
> [最新实施记录](<dsh-two-phase-review-implementation-20261001.md>)为准。下文14工具“只读”不可当物理写保护证明。

## 状态

本机 desktop profile 的研究接入已生效。BrowserSkill 已安装和启用，但浏览器连接及页面操作验收尚未完成。

| 项目 | 验收结果 |
| --- | --- |
| A 股复盘 | `skill ashare-daily-review` 成功加载 |
| 十二策略复盘 | `skill claw-paper-review` 成功加载 |
| 策略实验 | `skill claw-strategy-experiment` 成功加载 |
| 安全迭代 | `skill claw-safe-feature-iteration` 成功加载 |
| 本地 MCP | `claw_ashare` 已注册 14 个只读工具、3 个契约资源 |
| BrowserSkill | 插件与 CLI 均为 0.3.2；插件已启用、Skill 可加载；连接数 0 |

这是接入验收，不是策略盈利能力、生产模型晋级或浏览器页面功能验收。

## 已落地的入口

- [A 股每日复盘](<../.dsh/skills/ashare-daily-review/SKILL.md>)
- [十二策略复盘](<../.dsh/skills/claw-paper-review/SKILL.md>)
- [策略实验](<../.dsh/skills/claw-strategy-experiment/SKILL.md>)
- [安全迭代](<../.dsh/skills/claw-safe-feature-iteration/SKILL.md>)
- [MCP 实现](<../backend/scripts/ashare_review_mcp.py>)，服务版本 1.2.0。
- [本地 bundle 定义](<../.dsh/plugins/claw-research/package.json>)与[MCP 配置](<../.dsh/plugins/claw-research/cordis.patch.yml>)。
- [工具契约](<../.dsh/skills/ashare-daily-review/references/tool-contract.md>)。

desktop profile 已安装、启用本地 bundle `@local/claw-readonly-research@1.0.0`，
并启用 filesystem Skill 提供方。配置保存在[profile 配置](</Users/youzix/.dsh/profiles/desktop/cordis.patch.yml>)
和[profile 包声明](</Users/youzix/.dsh/profiles/desktop/package.json>)。
profile 的包管理器版本为 pnpm 10.33.0，以兼容既有依赖安装状态；没有更改全局包管理器设置。
恢复轮没有重装插件，也没有覆盖并行任务的 OAuth 补丁。

本地 MCP 配置使用本机 Python 3.11、当前项目绝对路径与 `http://127.0.0.1:8000/api/v1`。
移动项目或迁移机器后需要对应更新路径；其他 workspace 不应误用此项目的本机证据。

## 只读白名单与限制

保留既有 10 个 A 股复盘/预测/训练记录/影子评估工具，新增：

| 工具 | 来源与边界 |
| --- | --- |
| `paper_experiment_report` | 十二独立账户当前协议的即时完整周期汇总；不是历史 PIT 快照；不建账户 |
| `paper_daily_outcomes` | 显式日期读取已结算结果、控制样本；不结算、不补扫 |
| `paper_candidate_shadow` | 显式交易日、有界文件读取；保留缺失/截断/失败分母；参考标签不是成交净收益 |
| `paper_c3_events` | 显式日期 SELECT-only 事件与既有通知回执；C3 无执行账户；发送状态不等于用户已收到 |

所有工具拒绝未知参数、非法类型、非法枚举及数值越界；新增模拟盘工具严格检查 YYYY-MM-DD。
HTTP 重定向被拒绝，响应上限 5 MiB，错误返回为工具错误而非伪造空数据。
不提供下单、参数变更、回放、训练触发、账户刷新、结算、补采、推送或直接数据库访问能力。

审查了[模拟盘路由](<../backend/app/api/v1/paper.py#L11870-L12095>)、
[实验报告](<../backend/app/paper/experiment_report.py#L188-L290>)、
[C3 台账](<../backend/app/paper/c3_records.py#L61-L153>)与其[通知回执读取](<../backend/app/push/paper_buy_points.py#L1509-L1538>)。
未暴露有创建/刷新账户副作用的 `/paper/challengers/comparison`；GET 不等于无副作用。

## 实测证据与测试

### 本机 MCP 冒烟读取

- `ashare_review_history`：读取 2026-09-30 盘后冻结快照 ID 91，
  as-of 2026-09-30 20:30:00（Asia/Shanghai），
  `data_version=review_data_9afe8dff7e910de8`，
  `schema_version=daily_review_workbench_v5`，`quality_status=good`。
- `paper_experiment_report`：返回十二个独立账户；
  生成于 2026-10-01 07:58:05.949329（Asia/Shanghai），
  `protocol_version=continuous_paper_v1_pm`。这是本次生成的即时汇总，不是 9 月 30 日冻结快照。
- `paper_daily_outcomes`：显式查询 2026-09-30 的 default 已结算结果，
  返回当前执行版本及单独排除绩效的控制样本，没有触发结算。
- `paper_candidate_shadow`：2026-09-30、route A、limit 1 请求成功，
  返回零条可见行；这里只验证读取与空态，不据此推断全日没有候选或路线盈利。
- `paper_c3_events`：2026-09-30 confirmed、跨版本、page_size 1，
  返回 total 38 与 1 条分页记录，`read_only=true`；
  当前版本 `c3_continuity_v2:0e5542f967af`。总量不是当前版本的成交数。
- `claw://contracts/tools` 读取成功，返回本次修正后的 `route` 参数名。

### 自动验证

四个项目 Skill 通过 skill-creator 的 quick_validate，并分别通过运行中的 Skill 工具加载。
MCP `--self-test` 通过，14 工具与 3 资源完整。

在 backend 目录执行：

```sh
/Library/Frameworks/Python.framework/Versions/3.11/bin/python3 -m pytest \
  tests/test_ashare_review_mcp.py \
  tests/test_continuous_paper_experiment.py \
  tests/test_c3_records_visibility_20260922.py \
  tests/test_candidate_shadow_storage_20260924.py \
  tests/test_candidate_audit_api_20260923.py -q
```

结果：**134 passed, 1 warning in 9.54s**，退出码 0。
测试使用[隔离测试库保护](<../backend/tests/conftest.py#L10-L59>)，
不是当前交易数据库。

[MCP 测试](<../backend/tests/test_ashare_review_mcp.py>)覆盖正常读取、空结果、缺日期、
非法日期/类型/账户、范围边界、禁止写入参数和工具、重定向拒绝、响应体大小、
空/非法 JSON、HTTP 503 与连接错误；恢复轮新增 9 个响应及错误传播用例。
相关测试补充验证账户协议、C3 读取与文件预算/分页边界。
本任务 diff 检查通过；未改前端代码，未执行前端构建或页面验收。

## BrowserSkill 的剩余人工步骤

已配置绝对 CLI 路径，`lazyTools=true`，按需加载 `browser-skill` 后才公布原生浏览器工具。
本次 CLI 状态显示 daemon 0.3.2、protocol 1.3、连接数 0、活动 session 0。
DSH 原生工具目录中可见加载后的 browser_* 定义；
当前恢复轮的固定工具桥未提供可调用的 browser_* 方法，因此没有绕过插件改用 CLI 操作页面。

1. 在要使用的浏览器中安装或启用官方扩展：
   [Chrome 商店](https://chromewebstore.google.com/detail/hhcmgoofomhgciiibhipgmgkgnoenaoi) /
   [Edge 商店](https://microsoftedge.microsoft.com/addons/detail/browserskill/emacgiaaaiojkkpkddmmdfhmokgmnikg)。
2. 打开扩展，启用本地连接，确认显示 Connected；不要为此关闭借用确认或人工帮助安全设置。
3. 新消息中使用 `/browser-skill`，先对无交易副作用页面做
   “启动 Agent Window → 打开页面 → observe → stop”的最小验证。
   如果有指定 profile，先验证浏览器实例映射并显式绑定，不能替换其他已登录 profile。
4. 验证 Claw 页面前，审查初始化 API 是否会刷新/创建账户；默认用测试环境或经审查只读页面。
   Agent Window 共享所选浏览器的登录权限，不是交易安全隔离环境。

参照 [Tencent 官方安装说明](https://github.com/Tencent/BrowserSkill#quick-start)
和 [DSH 插件说明](https://github.com/Tencent/BrowserSkill/tree/main/packages/dsh-plugin-browserskill)。
浏览器页面操作与截图尚未实测；不得将“插件已启用”写成“页面已验收”。

## 影响与权限

改动仅限研究 MCP 适配器、测试、项目 Skill、接入文档与 DSH profile 配置。
没有修改买卖参数、信号算法、风控、订单、持仓、收益核算或回测逻辑；
没有人工写交易库、重启 Claw、触发推送或批准生产模型。
工作树有大量既有并行改动，均保留；本轮未扩大到业务核心文件。
Skill 规定流程与证据边界，不自动授予调参、写库、交易、部署或模型晋级权限。
