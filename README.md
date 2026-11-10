# DHXY Assistant — 大话西游手游个人辅助

当前版本：**v0.3.0 — Windows GUI（PySide6）+ MaaFramework / Android ADB / OCR**。

新增 **Windows 可操作 GUI**：`gui_app.py`，提供模拟器扫描连接、约两秒刷新一次的画面预览、
OCR 调试、OpenAI 配置（密钥仅内存）、本地题库和人工反馈，支持显式单次提交。
**双击安装与启动：`setup_gui.bat` → `start_gui.bat`。**
详细步骤：[docs/GUI.md](docs/GUI.md)。

已有真实设备适配代码：设备发现与连接、截图、区域预览、Maa OCR、题库/AI 衔接、
提交前画面复核和显式单题点击。**不是已经实机验收的完整日常助手，也不是打包好的 Windows 安装程序。**
你仍需手动登录游戏、进入大理寺题目；本版不自动答完十题，也不自动识别答对/答错。

**Windows 接入步骤：[docs/EMULATOR.md](docs/EMULATOR.md)**  
**测试范围：[docs/VALIDATION.md](docs/VALIDATION.md)**（离线测试；没有真实 Windows GUI、模拟器或 OCR 推理测试）。

```powershell
py -3.11 -m venv .venv
$PY = ".\.venv\Scripts\python.exe"
& $PY -m pip install -r requirements-device.txt
& $PY setup_ocr.py
& $PY device_app.py init-config
& $PY device_app.py devices
# 手动登录并打开大理寺题目后：
& $PY device_app.py capture
& $PY device_app.py inspect
& $PY device_app.py answer
```

默认不点击、不调用 OpenAI。设备选项见 `config/connection.example.json`；
真实本机配置、截图、题库和下载模型均已加入 `.gitignore`。
初始 ROI 来自用户提供的参考图，需在自己的设备上检查，不能把它当作已验证通用模板。

原 `app.py` 仍是完全不操作游戏的独立答题核心，以下为其使用方式。

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

**尚未包含**：自动反馈识别、完整十题循环、日常任务导航、游戏实测、在线搜索题库、自动模型升级。独立核心输入 JSON 的 `scene` 标签不代表真实画面已通过检测；设备入口另做标题/标签/题号校验。

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
3. 为本次题目展示赋予唯一 `instance_id`，换题时更新；v0.2 使用账号 scope/设备/游戏日/题号生成保守稳定标识，避免重启或重进同一题造成重复点击。
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

本次已做93项离线单元测试与设备接口模拟测试，未使用真实 API Key，未发生真实 OpenAI 调用，
未连接你的电脑、模拟器或账号；未验证 Windows 游戏内任务执行。
当前测试范围见 `docs/VALIDATION.md`；`TEST_RESULTS.txt` 与 `demo_output.json` 是旧版核心历史记录。

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

此项目并非网易官方工具，不承诺游戏账号安全。仅适配普通知识答题与其所需设备接口；
不会处理登录验证、安全挑战、游戏内存修改或交易。设备控制代码已接入，但未在真实模拟器上执行验证。
本版本未指定软件许可证；公开可见性与后续许可证选择分别处理。
