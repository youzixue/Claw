# DSH 提示词优化插件

适用于 DSH 0.2.x。插件独立于 Claw 的交易、数据采集、风控和前端工作台。

## 使用

- 当前模型选择器右侧、麦克风左侧的魔棒按钮：优化输入框文字。
- 操作与状态收进一个固定 48×28 的组合按钮：主区优化，加载时同一位置显示居中的细环与停止符号，点击即可取消；右侧小箭头展开操作菜单，不增加独立 loading/取消/撤回图标。
- 空白输入置灰。成功显示短暂勾选动画，撤回显示回转动画，再平滑回到魔棒；按钮与麦克风保持居中对齐，状态切换不挤动工具栏。适配减少动态效果设置，不自动发送消息。
- 不弹出优化成功、撤回、取消或错误状态提示；说明与错误详情仅在图标悬停时显示。仅覆盖手动编辑的撤回操作保留确认。
- 使用当前输入框选中的 provider/model；每次都建立隔离调用，不读取聊天历史、文件内容或附件，也不向聊天会话写入消息。
- 优化成功后可再次点击主区优化。右侧菜单中的“撤回上一次优化”逐次恢复上一版，显示剩余次数，最多保留最近 30 次；关闭页面或插件会清空内存历史。菜单支持方向键、Escape 与点击外部关闭。
- 优化后手动编辑再撤回，需要确认覆盖编辑。优化期间输入发生变化，不覆盖新输入。
- 清空或发送输入后清除撤回记录，防止旧消息回到下一条输入。
- 附件保持不变。包含结构化文件/技能引用芯片或处于命令模式时禁用，避免把引用退化成纯文本；普通文本路径不受影响。
- 输入与可回填的优化结果上限均为 16000 字符；模型响应读取上限为 32000 字符，超出可回填上限的结果不截断、不覆盖原文，避免下一次按钮被禁用。请求超时 90 秒。

## 连续点击优化修复（2026-10-01）

- 复现了“第一次改写成功，第二次返回相同文本但界面像没执行”的状态错误：控制器把无改动结果返回为 false，客户端不播放完成动画；悬停说明又只显示操作文案，隐藏实际完成说明。旧测试的模型每次必定追加文字，漏掉了这一分支。
- 现在每次空闲点击都基于当前草稿、当前选中模型发起独立请求。成功完成与文本改动分离：相同结果也显示完成勾选与“优化完成，模型未作进一步修改”；不重复替换、不增加虚假的撤回步骤，不为制造变化要求模型无意义扩写。
- 正常成功/未改动/取消/过期结果等说明显示在原有悬停提示中，不增加状态弹窗。已有成功动画不阻止继续点击；忙碌时点击仍是取消，不能同时重复提交。
- 回填前检查 16000 字符上限；超长结果保留原文，按钮仍可再次使用，不直接截断必要约束。
- 新增“改写→两次未改动→再次改写”的状态单测；浏览器检查每次点击真实产生请求、使用最新文本、未改动仍反馈成功且撤回数不增加；原生 DeepSeek 适配器串联 HTTP、客户端和控制器验证连续三次调用均是独立 session ID。
- 验证：57 项单元/接口测试、Playwright 隔离浏览器连续点击/撤回/取消测试、原生 DeepSeek 连续调用协议测试、原生任务模型锁集成测试均通过。当前用户 GUI 的直接点击验收未完成，隔离测试不冒充实际页面结果。
- 本次修复仅修改客户端状态/反馈和本地构建产物，没有更改 Host 请求逻辑、聊天模型或交易模块。构建后刷新现有 DSH 页面加载；无需为这次连续点击修复重启 DSH，不承诺无刷新热更新。

## 运行任务与待选模型隔离（2026-10-01 修复）

DSH 原生选择器把选择记录为“下一次请求”的模型，原生执行循环会在同一任务的下一步读取它；因此仅切换输入框模型、没有发送消息，也可能切换正在运行的任务。优化接口自身始终使用独立 session ID，未写入聊天模型。

插件现在通过原生 scoped waterfall 给普通会话保留任务级路由快照，不替换选择器、不修改应用 asar：

- 当前任务的模型及推理选择固定；工具返回、请求重试、自动 goal 续跑、问答回复和同轮补充消息不消费待选模型。
- 新选择仍在原生模型选择器显示，提示词优化立即使用它；下一条新用户任务开始时才消费新选择。
- 同时保持系统提示词的模型变量与实际路由一致，移除本次组装中不真实的模型切换通知，不改写已有历史。
- 加载修复时从已有请求头恢复当前任务路由；会话独立，子代理和后代 scope 按精确 Agent 身份隔离；卸载移除监听器。
- 原生新会话默认模型保存行为不变。本修复防止未发送选择造成的任务中途切换，不修复 Codex 适配器中已存在的失效轮次恢复状态；已经进入该错误状态的会话仍建议另开会话继续。

部署注意：本次只修改本地插件源码及构建产物，没有重启或中断当前任务。Host 模块没有已验证的源码热重载，刷新网页不足以重新载入 Host 修复；请等现有任务结束后重启 DSH。客户端 UI 代码未改。

验证：40 项单元/接口测试、原生 DSH 的 Cordis scope + 模型选择 + prompt assembly + request/notice waterfall 离线集成测试，以及原有 Playwright 输入框交互测试全部通过。没有发起真实模型调用或向用户会话发送测试任务。浏览器扩展未连接，当前 GUI 直接点击验收仍未完成。

原生运行时集成测试：

```sh
ELECTRON_RUN_AS_NODE=1 '/Applications/DeepSeek Harness.app/Contents/MacOS/DeepSeek Harness' tests/model-lock.runtime.mjs
```

## 性能改进（2026-10-01）

- 编辑指令从 356 字符缩短到 257 字符，强调轻量消歧与去重、不新增教程/方案/模板，清晰原文可不改动。完整保留事实、路径、数字、语气、全部约束和否定条件，不通过删除必要内容换速度。
- 优化器仍使用输入框选中的模型和隔离调用；不自动改用其他快模型，不改变聊天路由或推理设置。已支持的轻推理档选择保持不变。实测 GPT 准备阶段约 0 毫秒，所以没有增加无收益的能力缓存。
- 客户端通过原有认证接口请求 NDJSON 进度，不再一直等完整 JSON。按钮原位显示准备/等待正文/生成阶段；正文到达后圆环更亮，悬停可查看已收到的真实字符数。不是百分比，也不增加弹窗。
- 进度只传字符数，不泄露推理或回填半成品。必须收到完整成功结果并确认流结束，才原子替换一次输入并增加一次撤回记录；取消、截断、错误、新编辑和切换会话仍保护原文。支持旧 Host 的 JSON 响应兼容。
- 进度约每 120 毫秒发送一次；输出长度检查改为增量计数，避免每个 chunk 重算全部文本块。超时、断连取消和并发上限保留。
- 同一短样例、同一 GPT-6.1-Sol/low、能力预热后按旧/新/新/旧交替测试：旧版完成 6.432/6.716 秒，新版 5.395/5.246 秒，均值约从 6.57 降至 5.32 秒（本样本约 19%）。旧版扩写为 86–105 字符，新版 54–55 字符。首正文仍约需 4.2–5.0 秒，流式进度本身不加快模型推理。只有四次调用/一个样例，不保证其他模型或长文也有同样提升。
- 测速原始记录：[performance-1790822957856.json](<.dsh/plugins/prompt-optimizer/artifacts/performance-1790822957856.json>)。测试只使用固定无敏感信息的样例，不发送当前草稿、调用工具或修改用户聊天。
- 52 项单元/接口测试、原生模型锁运行时集成测试、Playwright 隔离交互测试通过；新增真实流式进度、UTF-8 分片、协议/长度边界、取消/超时、失败保留原文和一次性撤回验证。实际 GUI 直接点击验收尚未完成。
- **代码及客户端构建已完成；当前进程没有已验证的 Host 热重载，也没有重启。请等任务结束后重启 DSH 并刷新页面生效。** 仅刷新页面不能加载 Host 性能改进。影响范围仅为本地优化插件，不涉及 Claw 交易模块。

可选性能对照测试（四次真实模型调用）：

```sh
ELECTRON_RUN_AS_NODE=1 '/Applications/DeepSeek Harness.app/Contents/MacOS/DeepSeek Harness' tests/live-performance.mjs
```

## DeepSeek 页面失败排查（2026-10-01）

- 用户反馈 DeepSeek 能聊天但优化失败。当前独立真实调用未复现：相同合成短文本、原生 DeepSeek Messages 适配器及已配置 API key，Flash 1.892 秒、Pro 3.183 秒均返回正文；HTTP 200，实际档位 low，没有改模型、调用工具或写聊天。
- 记录：[Flash](<.dsh/plugins/prompt-optimizer/artifacts/deepseek-smoke-1790826448722-ce85557b.json>)、[Pro](<.dsh/plugins/prompt-optimizer/artifacts/deepseek-smoke-1790826495152-e1912cb4.json>)。这不是当前 GUI 的端到端成功证明，也不代表长文本时延；独立测试未挂载当前进程的请求扩展贡献者，页面失败根因仍待定位，不能宣称已修复。
- 补充 AUTH、INVALID_REQUEST、UNSUPPORTED_REASONING_EFFORT、REQUEST_EXTENSION、STREAM_CLOSED 等安全分类说明；未知提供方代码归为 MODEL_ERROR，不泄露原始后端错误、URL 或凭据。
- 客户端现有悬停说明显示 `[错误码] 中文说明`，同时兼容旧 Host 的 JSON/NDJSON 错误。刷新现有页面即可尝试加载错误码显示；新的 Host 中文分类说明仍需等任务结束后重启 DSH，未执行重启或确认热加载。
- 新增原生 DeepSeek 适配器 → 优化 HTTP → 客户端解析的离线测试，覆盖 Flash/Pro 路由、推理不外泄、文本不重复、截断/拒绝错误码。55 项单元/接口测试、原生 DeepSeek 协议集成、原生模型锁隔离集成及 Playwright 隔离交互测试均通过。浏览器实际页面尚未验收。
- 未更改模型路由、推理档、聊天配置或交易代码。失败时仍保留输入与撤回记录。

离线原生协议回归：
```sh
ELECTRON_RUN_AS_NODE=1 '/Applications/DeepSeek Harness.app/Contents/MacOS/DeepSeek Harness' tests/deepseek.runtime.mjs
```

可选真实 DeepSeek 冒烟（一次合成文本模型调用，会消耗额度；只读现有凭据，不打印或持久化密钥）：
```sh
DSH_LIVE_DEEPSEEK=1 ELECTRON_RUN_AS_NODE=1 '/Applications/DeepSeek Harness.app/Contents/MacOS/DeepSeek Harness' tests/live-deepseek-smoke.mjs
# 可加 DSH_TEST_MODEL=deepseek-v4-pro；默认使用配置中的第一个模型。
```

## 视觉风格

插件只使用 DSH 主题别名令牌，不自定义调色板（测试会断言样式表内没有硬编码十六进制色值），因此浅色与深色主题都由 DSH 本身驱动。几何与材质直接对齐 DSH 原生组件：

- 触发器沿用 `PermissionSelect` 的选择器轮廓：28px 高、`--dsw-radius-sm`、无边框、悬停 `--dsw-alias-interactive-bg-hover`、箭头 `--dsw-alias-label-caption`、禁用 `--dsw-alias-label-dimmed`、焦点环 `--dsw-focus-ring-*`。
- 菜单沿用 `Menu.module.css`：4px 内边距、`--dsw-radius-lg`、`--dsw-menu-surface-fill` + `--dsw-menu-backdrop-filter` 半透明材质、`--dsw-elevation-prominent` 阴影、行高 34px / 行圆角 `--dsw-radius-md` / 13px 字号 / 14px 图标 `--dsw-alias-menu-icon`、计数 11px `--dsw-alias-label-caption`，并带 `data-menu-material` 以套用 DSH 的深色描边覆盖。
- 撤回确认卡沿用 DSH 对话框表面（`--dsw-alias-bg-overlay` + 突出阴影），按钮为紧凑 ghost 样式，破坏性操作使用 `--dsw-alias-state-error-primary` 与其悬停填充。
- 状态色也取自语义令牌：加载/撤回 `--dsw-alias-state-business-primary`、成功 `--dsw-alias-state-success-primary`、错误 `--dsw-alias-state-error-primary`。

## 模型兼容与安全

统一复用 DSH 的 `llm.prepareCall` 与流式 chunk 协议；凭据和协议转换完全由现有适配器负责。不开启工具，不硬编码厂商 HTTP API，不把一个模型的推理等级强加到另一模型。

不主动指定 `temperature`、`maxTokens` 或 `stop`，尤其 Codex OAuth 的 app-server 不支持这三者。优化仅做必要语言整理，不分析素材任务的解决方案。通过 `llm.resolveModelInfo` 查询当前 provider/model 的真实能力，仅在明确支持时选用 `none`、`minimal`、`low` 中最轻的推理档；没有兼容轻档则保持适配器默认值。不改变聊天模型或聊天推理设置，不硬塞厂商参数。能力查询与 prepareCall 之间若发生 HMR 能力变化，仅重新准备同一路由的默认调用，不重复发起推理。
无文本/截断/失败/中断/工具请求的输出不回填，只保留原文。推理内容不回填，文本 delta 与最终 block 不重复拼接。

[OpenAI 官方推理说明](<https://developers.openai.com/api/docs/guides/reasoning>)指出较低推理档优先速度，支持档位依模型而异；本项目始终以运行时适配器返回的能力为准。

接口在 DSH `Connection.fetch` 下注册，复用现有登录 Cookie、Host/Origin 检查与断连取消。未登录返回 401。响应不缓存，提供方错误信息不原样回传，避免泄露凭据。

## 开发与验证

源码目录：[src](<.dsh/plugins/prompt-optimizer/src>)；构建脚本：[build.mjs](<.dsh/plugins/prompt-optimizer/scripts/build.mjs>)。无需修改应用 asar 或启动新 Web 服务。

在插件目录运行：

```sh
npm run build
npm test
node tests/client.e2e.mjs
```

浏览器隔离测试使用已有 Playwright 和 React 18 UMD，可通过 `PLAYWRIGHT_MODULE`、`TEST_REACT_ROOT` 指定测试依赖。测试页面注入的是从 DSH 主题包解析出的真实令牌值（浅色与深色各一套），因此预览反映真实观感，而不是插件私有的配色；模型响应仍是隔离 mock，不冒充真实模型效果。

真实模型冒烟测试需显式运行，使用当前 DSH 安装包中的运行时及已配置 Codex OAuth 适配器：

```sh
ELECTRON_RUN_AS_NODE=1 '/Applications/DeepSeek Harness.app/Contents/MacOS/DeepSeek Harness' tests/live-model-smoke.mjs
```

该测试只发送固定无敏感信息的样例，默认使用 `gpt-6.1-sol`，会消耗一次模型调用；不会发送当前聊天输入或改动会话。其他模型的兼容性通过适配器契约及单元测试验证，不宣称逐个线上模型实测。

## 本次验证记录

- 30 项单元/接口测试全部通过，新增轻量推理能力选择、不支持档位、能力变化、取消与错误脱敏。
- Playwright 隔离浏览器交互通过：固定尺寸/工具栏及 SVG 中心对齐、加载原位取消、成功/撤回动画、操作菜单与键盘、连续 3 次优化/逐次撤回、手动编辑确认、过期结果保护、失败/取消保留历史、模型/会话隔离、清空、引用/命令保护、深色、390px 移动端及减少动态效果。
- 视觉回归断言 DSH 令牌生效：触发器圆角 8px、箭头 12px/`--dsw-alias-label-caption`、空输入 `--dsw-alias-label-dimmed`、菜单圆角 16px/内边距 4px/行高 34px/行圆角 12px/13px 字号/图标 14px/计数 11px、浅色与深色菜单材质分别为 `#f8f9fa94` 与 `#43454a73` 且带背景模糊、深色下文字与图标切换到深色令牌、样式表内无硬编码色值。
- 2026-10-01 速度对照：同一短文本、隔离无工具调用、预热能力查询后交替执行默认与快速档各 2 次。当前 GPT-6.1-Sol 适配器默认已经是 low，两组实际均为 low：默认 6.632/6.614 秒，快速 6.280/7.266 秒。**本样本未显示明确提速，不能把模型波动宣传为性能提升**；轻量策略主要避免其他模型继承更昂贵的默认推理档。结果见 [live-speed.json](<.dsh/plugins/prompt-optimizer/artifacts/live-speed.json>)，不保证线上时延。
- 真实 GPT-6.1-Sol 通过 DSH 0.2.x 的模型调用链返回优化正文；未发送聊天消息，未提供工具。
- 现有 GUI 的 Client 插槽与 Host 插件都处于 active；未登录接口实测为 HTTP 401。
- Browser Skill 尚未连接，因此当前用户 GUI 的直接点击/截图验收未完成；刷新页面后可试用。隔离截图不代表当前页面截图。

## 安装和更新

构建后，通过 DSH 插件管理安装当前插件的绝对目录。本次已通过当前 DSH 的原生插件管理器作为本地 link 安装到 desktop profile，Host 和 Client 插槽均已核验激活。无需再次安装。全局旧版 CLI 与桌面版运行时版本可能不同，后续安装/管理优先使用当前 GUI 的插件管理器。

修改客户端源码后必须重新构建，并刷新现有 DSH 页面。未启动源码 checkout 的 `dev:web` watcher，不承诺无刷新更新。卸载/关闭请在 DSH 插件管理中操作 `dsh-local-prompt-optimizer`，不要改动应用包。
