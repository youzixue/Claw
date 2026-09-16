# 新闻页 AI 接入与清洗展示

## 配置入口

新闻页右上角「AI 接入配置」。作用域是项目共享 AI Provider（情感、事件、摘要等），
不是一个隔离的新闻专用模型。切换影响后续 AI 请求及其分析输出；历史新闻不会自动重算。
交易下单、风控、新闻方向分权重与规则降级算法没有改动。

- API Key：保留 Anthropic/MiniMax 兼容调用，增加 OpenAI Chat Completions 兼容调用。
- OpenAI 账号：通过官方 Codex App Server 管理 ChatGPT 登录，不把 OAuth token 当作
  OpenAI Platform API key，不访问其他工具的个人登录缓存。
- 保存与测试分开。测试使用**已保存配置**发送一条短请求，可能消耗额度。
- 切换协议不会擅自替换模型或地址。填写提供商支持的模型名称；账号模型可留空。
- API Key 留空保留；明确勾选才清除。更换 API 地址而不重新填 key 时清除旧 key，
  避免意外向新服务发送已有凭据。

## 部署

后端没有自动重载时需安排重启才能启用新接口。请避开采集/交易关键任务，不为了刷新
一个页面任意重启生产调度器。前端运行 `npm run build`，按现有部署方式发布构建产物。

默认保持原有环境变量。新增配置项：

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| AI_AUTH_MODE | api_key | api_key / openai_oauth |
| AI_API_FORMAT | anthropic | anthropic / openai |
| AI_CONFIG_PATH | ~/.claw/ai-config.json | 页面配置优先于环境变量，原子保存，文件权限 0600 |
| AI_CODEX_EXECUTABLE | codex | 优先服务端 PATH；macOS 默认命令缺失时回退到 ChatGPT.app 内置 CLI；显式配置不会被回退覆盖，不接受浏览器指定命令 |
| AI_CODEX_HOME | ~/.claw/codex-news | 此服务专属的授权缓存，目录 0700，不使用个人 ~/.codex |
| AI_CODEX_MODEL | 空 | 账号默认模型 |
| AI_CODEX_REASONING_EFFORT | 空 | 模型默认思考强度；页面配置 oauth_reasoning_effort 优先 |

配置文件中包含 API Key，应视为凭据保管，不提交仓库、不上传工单。
凭据损坏时停用 AI 而不是使行情/交易应用启动失败；页面重新保存可恢复。
官方 CLI 管理账号凭据与续期，页面和 API 状态响应不含访问令牌、刷新令牌、API Key。
后端日志也不输出 AI 错误响应正文。

配置、测试、授权和断开接口仅允许本机客户端、loopback Host 与受信工作台 Origin
（同源或 localhost/127.0.0.1:5173）。这不替代整个应用的身份认证；不要将无认证的
Claw 服务直接暴露到公网。远程访问必须先按现有部署增加认证并显式设计配置管理权限。

## OpenAI 账号授权

先确认**后端运行环境**能找到官方 Codex CLI。默认先查 PATH 中的 `codex`；
macOS 找不到时检查 `/Applications/ChatGPT.app/Contents/Resources/codex`。
仅复用该可执行文件，不读取 ChatGPT 的凭据或配置。其他安装位置可显式设置
`AI_CODEX_EXECUTABLE`；无有效安装时再按官方文档安装。然后在配置抽屉：

1. 选择「OpenAI 账号授权」，刷新状态。
2. 点击「连接 OpenAI 账号」生成链接，再点击同一操作区的蓝色「打开 OpenAI 授权页面」主按钮。
   这是明确的两步操作，不是自动跳转；等待状态仍保留可点击的继续授权入口，而不是只显示灰色连接按钮。
3. 用户在运行 Claw 的本机浏览器完成登录，回到页面等待状态刷新。
   CLI 在本机监听回调（当前版本为 localhost:1455）；浏览器须能访问该地址。
   页面重载后若链接丢失，会提示返回原发起标签页或手动取消后重试，不自动取消用户已有授权。
4. 确认授权及分析通道状态，保存配置，再点击测试。

登录不自动切换当前接入；取消/失败也不修改原 API Key。
账号分析只向独立的临时会话发送文本。启动前核验生效配置，禁用 shell、图片读取、
联网检索、MCP、插件和子代理；要求服务端确认仅空目录可读、无平台默认读取、工具无网络、
禁止审批升级的受限权限配置。版本不能回传该安全契约时拒绝发送新闻。
账号已登录不等于分析调用可用；页面会另外显示分析通道能力/限制，失败保留规则降级，
不偷偷切回另一付费服务。依赖缺失、版本不兼容、授权未完成均不能显示为已连通。
2026-09-14 排障确认：已安装官方 CLI 0.153.4 已移除 `readOnly.access`，并不是必须升级 CLI。
`thread/start.sandbox` 是有损兼容投影，不能独立证明读取范围。当前实现启用实验权限协议，并同时核验：

1. `config/read` 中命名配置 `claw_news_isolated` 仅包含唯一临时空目录的 `read`；
   禁止继承、额外/平台路径、工作区展开和网络配置，只允许明确已知的空元数据字段。
2. `permissionProfile/list` 必须声明该配置唯一且 `allowed=true`，防止只信任未知配置键的回显。
3. `thread/start` 显式传入 `permissions` 和空 `runtimeWorkspaceRoots`，检查返回的
   `activePermissionProfile.id`/无继承、空工作区根、无外部指令、临时会话、禁止审批、只读/无工具网络。
4. `turn/start` 继续使用同一 `permissions`，**不能再传旧 `sandboxPolicy.access`**。
   每次新请求都重新验证；工具调用/审批请求仍拒绝，完成或超时后清理会话。

账号服务访问 OpenAI 需要网络，这与工具沙箱联网是两回事。子进程现在仅额外继承标准代理设置，
在 macOS 通过 `urllib.request.getproxies()` 使用环境变量或系统已配置的代理；不硬编码代理地址，
不继承 API 密钥/个人 Codex 配置，不输出可能带凭据的代理 URL。直连失败而浏览器登录正常时应检查代理。
CLI 声明 `willRetry=true` 的传输错误允许在总超时内恢复，不误判成已完成失败。

已通过本机真实 CLI 的无登录隔离测试，并用现有授权的 `gpt-5.6-sol` / `low` 完成短连接测试（返回 OK）。
另选取一条已入库新闻，只读输入、调用真实 `NewsProcessor`，情感和事件均返回 `method=ai`，
产生摘要、事件和要点；未写回生产数据库、未批量重跑新闻或变更交易配置。

官方说明：

- [Authentication](https://developers.openai.com/codex/auth/)
- [Codex App Server](https://developers.openai.com/codex/app-server/)
- [Codex CLI](https://developers.openai.com/codex/cli/)

ChatGPT/Codex 账号权益与 Platform API 计费不是同一条通道；自动化使用与额度取决于
实际账号权限，不保证某一订阅必然可用。

## 模型选择与保存反馈

- 授权后通过本机只读接口 `GET /api/v1/ai/oauth/models` 调用 Codex `model/list`，
  分页读取可见且支持文本输入的目录。选择器使用模型的 `model` 值，而非可能不同的目录条目 ID。
  不内置猜测的模型清单，不自动选择第一项；目录不保证账号额度或调用权限。
- 支持账号默认模型、目录选择和手动输入完整模型 ID。目录失败、返回空列表或旧后端 404 时，
  给出明确提示，仍能保存默认/手动模型，不把目录错误误报成未授权。
- 思考强度来自 `model/list.supportedReasoningEfforts` / `defaultReasoningEffort`，接口输出
  `reasoning_efforts` / `default_reasoning_effort`。页面只显示模型实际声明且本新闻桥支持的普通强度；
  不猜测未知模型、旧目录或未来代理模式的能力。空值表示模型默认，旧保存文件无需迁移。
- 强度保存为 `oauth_reasoning_effort`。显式强度调用前重新核对目录，并通过
  `thread/start.config.model_reasoning_effort` 的生效回显和 `turn/start.effort` 传递；
  模型/强度不匹配时不发送新闻，也不静默换模型。目录默认模型配合显式强度时会固定本次已核验的模型。
- 保存 OAuth 配置时保留服务端已有的 API 地址、模型、参数与密钥，不提交另一页隐藏的未保存草稿。
  模型选择和保存不会发起模型请求；不会绕过新闻隔离检查。
- 保存结果固定显示在抽屉底部按钮旁，区分“配置已保存但分析未就绪”和“配置未保存”。
  页面同时标示已保存模型与尚未保存的选择。保存成功的 HTTP 200 不意味着模型连接测试成功。
- 模型目录新增接口需安排后端重启后才会生效；不可为加载下拉框擅自中断盘中调度。

## 授权按钮灰色 / Not Found 排障

1. 检查 `GET /api/v1/ai/status` 是否包含 `auth_mode`、`api_format` 和 `oauth.available`。
   只有 enabled/model/base_url/has_key 是旧后端契约，不代表机器没有 CLI。
2. 查看现有后端 `/openapi.json` 是否已注册 `/api/v1/ai/config`、`/test`、`/oauth/start`。
   若缺失，需安排重启**现有后端进程**；重载网页或重新构建前端不能更新 Python 内存路由。
   重启影响采集/调度，先确认时机，不并行启动替代服务。
3. 页面区分后端版本不匹配、接口错误、明确缺少 CLI、授权进行中、已授权，并展示禁用原因。
   配置读取失败或旧契约时禁用保存/测试，避免继续请求不存在的接口造成 Not Found。
4. CLI 存在但启动失败时不能显示“未安装”。真实测试发现 `-c` 的 dotted key 不解析
   带引号路径键；权限目录必须作为完整 TOML 值传入。回归测试覆盖实际可执行文件，避免仅 mock 通过。
5. 用户仍需自行完成官方授权；分析通道未通过安全校验、模型权限/额度或连接测试时，不应标成可用。

## 清洗统计与阅读口径

- 保留 `analyzed_count` 原有含义：已处理（包括 AI 参与、规则降级、兼容历史记录）。
- 新增 `ai_analyzed_count`、`fallback_count`、`legacy_analyzed_count`、`failed_count`。
  `analyzed` 是 AI **参与**：情感或事件至少一项明确返回 `method=ai`，不保证两项都成功。
  纯 `keyword`/`fallback` 必须标记规则降级，页面投影与追加式分析证据保持一致，不重写历史记录。
- 非对象/错误结构的模型 JSON 安全降级；有效 `events=[]` 是 AI 无事件结论，不再强行生成关键词事件。
- 失败数是待处理的子集；过期的 analyzing 仍按已有逻辑归回待处理。
- 处理覆盖率不是 AI 成功率；不是交易时段的日期窗口继续沿用原实现。
- 无 `since` 时总数是全部历史入库，不是当天数据。来源筛选影响统计；
  情感、层级和重大筛选主要影响返回样本。
- 解读只依据当前返回样本，清洗状态筛选只筛本页（最多 50 条）。
- 手动分析上限仍是原先的 100 / 300 条；不再标成“全量”。同一任务内记录已尝试 ID，
  不会在每个小批次反复重试同一条最新失败新闻，避免较早新闻长期得不到处理；新任务仍可重试。
- 规则降级保留原评分归因，不为了视觉展示更改既有决策规则。
- 清洗中断、失败、取消、任务过期单独显示。连续三次进度读取失败后锁定重复提交，
  可恢复读取原任务进度。离开页面停止轮询，不取消服务端任务。
- 解读依据与新闻正文摘录可展开；正文仍是后端摘录，完整内容通过安全原文链接查看。
  模型推断与直接个股证据继续区分。

## 验证

在 backend 运行：

```sh
python3 -m pytest tests/test_ai_configuration.py tests/test_codex_bridge.py \
  tests/test_news_ai_pipeline.py tests/test_news_cleaning_status.py \
  tests/test_news_api_logic.py tests/test_news_fetch_latency.py -q
```

在 frontend 运行（沿用已有 5173 服务，可用 CLAW_WEB_URL 指定）：

```sh
npx playwright test e2e/news.ai-config.spec.cjs --reporter=line --workers=1
npm run build
```

浏览器测试拦截全部 API，只验证契约、空态、异常、OAuth 入口、密钥留空语义、
任务终态/恢复及 390px 移动布局；不应将其当作真实 OAuth 连通或真实行情验证。

本次后端专项回归为 115 passed / 3 skipped，另显式运行真实 CLI 无登录隔离测试 2 passed。
前端 `npm run build` 成功；新闻配置交互回归 23 passed / 1 skipped，覆盖强度保存、切模型、
未知能力、旧后端、授权异常及移动布局。原 5173 页面另以真实状态/目录验证了已保存模型和四档强度，
无旧隔离警告；部署后的 `/api/v1/ai/test` 返回 `ok=true`。自动交易仍关闭，调度器运行正常。
扩展运行 `test_news_catalyst.py` 时另有 3 个既有 fixture 兼容错误（旧 `FakeSession` 返回对象缺少
PIT 新闻版本的 `id`）；失败位于未修改的证据读取路径，不为本次接入修复改写交易归因逻辑。

可显式运行真实环境冒烟测试（不需真实凭据）：

```sh
# backend：新建临时 CODEX_HOME，启动官方授权流程后立即取消，不打开浏览器、不发送模型请求
CLAW_TEST_CODEX=codex python3 -m pytest tests/test_codex_bridge.py -q
# frontend：仅 AI 状态透传到现有后端；其他请求仍隔离，禁止真实配置修改及分析调用
CLAW_TEST_LIVE_AI=1 npx playwright test e2e/news.ai-config.spec.cjs --reporter=line --workers=1
```

本轮只读状态检查已确认用户完成账号授权，保存的通道为 `openai_oauth`；
分析能力检查仍未通过，尚未进行真实模型连接测试或发送新闻。
模型目录协议另在无凭据的临时 CODEX_HOME 中用已安装 CLI 验证，返回 6 条目录记录；
这不代表所有目录模型均可由当前账号调用，也不替代部署后新增接口的验收。
