# DHXY Assistant — 大话西游手游个人辅助

当前版本：答题核心 v0.1.1；设备与 MaaFramework 适配尚未完成。

本包是可运行、可离线测试的 Python 答题核心，**不是完整日常助手，也不是 Windows 安装包**。
它不登录游戏、不读密码、不截图、不做 OCR、不连接模拟器、不点击游戏。
游戏入口、截图识别、答案提交和结果反馈需要后续 MaaFramework 适配器。

## 本次实现

- SQLite 本地题库：先查缓存，未命中且显式允许 AI 才请求 OpenAI。
- 使用 OpenAI Responses API + 严格 JSON Schema；不赋予模型工具或鼠标权限。
- 记录 AI 候选答案、已验证答案、弃答、错误隔离、结果未知和请求中状态。
- 缓存答案文字而不是 A/B/C/D；普通选项调换顺序后重新映射选项字母。
- 精确题目指纹；只清理空白、全半角与明确的 `第n/m题：` 前缀，不删除等级、数值、否定词。
- 含选项字母或位置引用的题目保留顺序，避免不安全地复用答案。检测是保守规则，不是完整自然语言解析器。
- scope 区分手游、任务包/版本；可在游戏规则变更后更换 scope，重新验证答案。
- 调用次数上限、单题并发请求占位、失败后不自动重试、token 使用量记录。
- 过期结果/题目实例变化/选项顺序变化保护；提交前一次性 claim；实际点击由外部完成。
- 可明确选择上传一张用户已裁好的题目图片；默认只传题干和选项文本。
- API Key 从本机进程环境或本机终端隐藏输入读取，不写进数据库或源码。

**不包含**：OCR 引擎、MaaFramework 插件/任务包、ADB 控制、自动反馈识别、GUI、游戏实测、在线搜索题库、自动模型升级。不能把输入 JSON 的 `scene` 标签当成真实画面已通过检测。

## 运行

要求 Python 3.11 或以上，核心仅使用标准库，无第三方依赖需要安装。
Windows 进入解压目录后运行；其他平台可将 `py` 换成 `python`。

```powershell
py app.py demo
py -m unittest discover -s tests -v
```

离线演示用的是自造常识题和假模型，不会请求 OpenAI，不会污染持久化题库。
演示结果包括：第一次得到候选答案 → 模拟明确正确反馈 → 选项换位后缓存命中并映射到新字母。

### 读取真实题目输入

`examples/dali_question.json` 是根据用户截图人工转写的输入样例，**未预置或验证正确答案**。
每次进入新的一题，适配器必须生成新的 `instance_id`；同一道题在同一次展示期间保持不变。

只查本地题库，不访问网络：

```powershell
py app.py solve --question examples/dali_question.json
```

显式启用 OpenAI，且仅在未命中题库时调用：

```powershell
py app.py solve --question examples/dali_question.json --ai --model YOUR_MODEL_ID --prompt-key
```

密钥由使用者从自己的 OpenAI 开发者账号获取，留在本机使用，不要发到聊天或提交到仓库。
`--prompt-key` 只在本机终端隐藏输入，不保存到文件；已存在 `OPENAI_API_KEY` 时使用该进程环境。
此代码不会替你修改系统环境变量。

不预设未经核实的模型 ID。请把命令中的 `YOUR_MODEL_ID` 替换为你的账号可用、支持 Responses API 和 JSON Schema 输出的模型；也可以设置本机 `OPENAI_MODEL` 环境变量。
模型兼容性与游戏题目准确率尚未在线验证，真实调用可能产生 API 费用。

只打印结果、不操作游戏是本 CLI 的固定行为。`--allow-trial` 仅改变供外部执行器参考的资格标记：

```powershell
py app.py solve --question examples/dali_question.json --ai --model YOUR_MODEL_ID --prompt-key --allow-trial
```

需要图片上下文时，先在本机裁掉聊天、账号、余额、二维码等无关信息，再显式提供图片路径：

```powershell
py app.py solve --question examples/dali_question.json --ai --model YOUR_MODEL_ID --prompt-key --image cropped-quiz.png
```

这个参数不会代替 OCR，也不会从图片自动生成输入 JSON。缓存命中时不上传图片。

### 反馈

读取命令输出中的真实 `attempt_id`。只有已经实际答题且看到明确反馈后才登记：

```powershell
py app.py feedback YOUR_ATTEMPT_ID correct --evidence "实际看到明确的正确反馈"
py app.py feedback YOUR_ATTEMPT_ID wrong --evidence "实际看到明确的错误反馈"
py app.py feedback YOUR_ATTEMPT_ID unknown --evidence "只有下一题出现，未看到正确或错误提示"
```

这些是互斥结果示例，不应对同一次回答依次运行三个命令。
CLI 反馈来源为 `manual`；未来自动检测器使用 `game_ui`，且必须先成功 claim 提交。

- 明确正确：升级为 `verified`，之后本地命中可进入自动提交候选。
- 明确错误：改为 `invalid`，隔离此答案；不会把剩余选项任意一个当成正确答案。
- 结果未知：改为 `needs_review`，不重复提交、不自动重新请求 AI。
- AI 弃答：缓存 `abstain`，不在每帧重新问一遍。
- 仅出现下一题、任务次数增加、获得普通奖励：都不能单独证明答对。

明确要求重新求解未验证/错误/弃答记录时：

```powershell
py app.py solve --question examples/dali_question.json --ai --model YOUR_MODEL_ID --prompt-key --refresh
```

刷新时会把已有的明确错误选项传给模型。已验证答案不能被直接覆盖；先执行 `invalidate` 并给出原因。
若同一候选出现互相矛盾的正确/错误反馈，保留为 `needs_review`，需人工核对证据或更新版本 scope。

```powershell
py app.py invalidate --question examples/dali_question.json --reason "游戏规则更新，需要重新验证"
py app.py stats
```

请求期间进程崩溃时会保留占位，以防重启后重复计费。确认所有其他进程停止后才执行：

```powershell
py app.py recover --all-workers-stopped
```

然后对确实需要重试的题目使用 `--refresh`。超时可能仍已产生服务端费用，因此没有透明自动重试。

## 与 MaaFramework 适配的边界

1. 本地识别确实进入普通“大理寺答题”，排除登录、安全挑战和其他弹窗。
2. 对题干、四个选项做局部 OCR；两次稳定读数一致后组装 `Question`。
3. 为本次题目展示赋予唯一 `instance_id`，并在换题、退出/重进时更新。
4. 工作线程调用 `store.resolve(...)`，不要阻塞桌面 UI。不要持续逐帧调用它。
5. 模型返回后重新截图、识别，确认题目实例、文字、顺序和页面都没改变。
6. 只有策略允许的答案才能提交；首次 AI 候选需要显式的 `allow_trial=True`。
7. 使用当前画面里的答案按钮定位，不复用旧坐标；调用 `claim_submission` 一次。
8. claim 成功后由适配器点击一次；若点击失败或结果不明确，登记 unknown 并暂停。
9. 从实际正确/错误反馈确认后调用 `feedback`；没有真实反馈素材时不要伪造检测器。

模型不返回坐标，也不调用控制器。`can_submit=True` 不是“已点击”或“已答对”。

## 运行限制与隐私

默认每个游戏日最多 30 次 AI 请求**尝试**；占位、失败、超时也计数。这不是金额硬上限。
查询已验证缓存不受这个 AI 次数上限影响。日界线按中国服务器 profile 的 UTC+8 05:00，
不是主机时区；活动面板显示的刷新时间来自用户截图。服务器时区仍需目标服确认。

默认请求超时 25 秒，输出 token 上限 1536。无自动联网搜索、无后台无限重试。
OpenAI 请求设 `store=false`，不传历史聊天；这不等于承诺服务端零保留。
数据库未加密，含题目、答案、反馈和用量，不含 API Key 或完整游戏截图。
不要把数据目录、截图、密钥提交到 Git。

## 验证范围

本次已做离线单元测试和模拟响应解析测试，未使用真实 API Key，未发生真实 OpenAI 调用，
未连接你的电脑、模拟器或账号；未验证 Windows 游戏内任务执行。
具体测试结果见 `TEST_RESULTS.txt`，示例输出见 `demo_output.json`。

## 官方参考文档

- OpenAI Structured Outputs: https://developers.openai.com/api/docs/guides/structured-outputs
- OpenAI image input: https://developers.openai.com/api/docs/guides/images-vision
- OpenAI Responses API: https://developers.openai.com/api/reference/resources/responses/methods/create
- Data controls: https://developers.openai.com/api/docs/guides/your-data

## 发布到 GitHub

阅读 [PUBLISH.md](PUBLISH.md)，或在此目录运行 `py publish_github.py`。
脚本仅针对新公开仓库，默认目标为 `wenlong66/dhxy-assistant`；不会覆盖同名现有仓库。
源码包不包含截图、游戏账号、运行时题库或任何真实密钥。

## 当前边界

此项目并非网易官方工具，不承诺游戏账号安全。仅保留普通知识答题核心；
不会处理登录验证、安全挑战、游戏内存修改或交易。设备控制尚未实现。
本版本未指定软件许可证；公开可见性与后续许可证选择分别处理。
