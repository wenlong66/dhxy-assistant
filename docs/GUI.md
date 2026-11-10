# Windows GUI 使用说明（v0.3.0）

这个界面是真正的 PySide6 Windows 桌面程序，入口是 `gui_app.py`，不是静态设计图。
它调用现有的 `maa_adapter.py`、`quiz_vision.py`、`quiz_core.py`，没有重写 OCR 或题库。
**没有完成全部日常任务自动化**：目前只实现大理寺普通知识题的单题识别、查询和显式单次提交；
其余日常任务仅显示为灰色“未接入”，不可勾选或启动。

## 快速开始

1. Windows x64 电脑先安装 [Python 3.11](https://www.python.org/downloads/) 与 Git；
   [配置 Git](https://git-scm.com/) 后获取项目 `git clone https://github.com/wenlong66/dhxy-assistant.git`。
2. 在仓库根目录双击 **`setup_gui.bat`**。脚本创建 `.venv`、安装 `requirements-gui.txt`，
   然后下载和校验中文 OCR 模型。需要能访问 Python 软件包源及 GitHub 模型文件。
   安装模型的具体版本和校验信息见 `setup_ocr.py`。
3. 启动 Android 模拟器，安装与你的账号渠道相符的《大话西游》手游并**手动登录**。
   建议固定横屏 1280×720，打开 ADB 本地调试；不同模拟器端口不同。
4. 双击 **`start_gui.bat`**，点击「扫描设备」→选择实例→「连接」。
   自动发现失败时，到「连接设置」填写 ADB 可执行文件路径和真实实例地址，保存后连接。
5. 手动进入游戏里的**大理寺答题**题目界面，点击「OCR 识别当前题」。
   核对题目、OCR 和区域框图（`data/maa/inspection/overlay.png`），确认无漏字、串行和区域错位。
6. 点击「开始 / 预览本题答案」：默认只查本地题库，不花费 OpenAI 额度，也不会点击游戏。
   如果需要 AI，可勾选允许查询、填写你有权限的模型 ID 与 API Key。
7. 首次提交前必须已完成有效的 OCR 检查，勾选已核对布局。
   允许未验证 AI 候选则另外勾选「允许试答」。点击「提交本题一次」并再次确认。
8. 看到实际结果以后，在「本地题库」填写 `attempt_id`、实际答对/答错/不明确及依据。
   仅确认正确的记录进入已验证题库；点击成功不等于答对。

也可以用 PowerShell 代替批处理：

```powershell
py -3.11 -m venv .venv
$PY = ".\.venv\Scripts\python.exe"
& $PY -m pip install -r requirements-gui.txt
& $PY setup_ocr.py
& $PY setup_ocr.py --verify
& $PY gui_app.py
```

## 窗口功能

| 窗口 | 已接入 | 说明 |
| --- | --- | --- |
| 模拟器扫描/连接 | 是（待实机验证） | Maa 的 ADB Toolkit，可手动配置地址；单设备连接 |
| 实时画面 | 是（待实机验证） | 连接成功后约两秒只读截图一次，不自动点击 |
| 保存截图/ROI | 是 | 本地输出 PNG 和区域框图，供调布局 |
| OCR 调试 | 是（待实机验证） | Maa 本地中文 OCR，严格校验标题、题号、四个选项 |
| AI 答案预览 | 是（API 仍需你的账号实测） | 题库优先、AI 可选；密钥留在内存 |
| 单题执行 | 是（待实机验证） | 必须显式授权，最多点击一次；调用后重新确认场景 |
| 本地题库 | 是 | 搜索和查看最近 200 条，录入实际反馈 |
| 师门/帮派/宝图/五环 | **否** | UI 中禁用，不存在假任务按钮 |
| 自动登录／十题循环／自动反馈／每日调度 | **否** | 仍需要开发和真实画面样本 |

## 停止、暂停与费用

- 「暂停画面预览」只停止周期性截图，**不会中断正在进行的 AI 请求或原生操作**。
- 「断开 / 停止」将设置取消标志；还没执行的点击会检查该标志。
  已经发往设备的点击无法撤回，已发出的 OpenAI API 请求也可能产生费用。
  操作线程要等当前请求返回后才会释放连接，不会强制终止正在运行的原生调用。
- 每次点击「开始 / 预览本题答案」只处理**一道题**；不会连续点十题。
- API 请求默认关闭；未明确启用时不调用 OpenAI。启用后仍设置每天最多 30 次请求（可调整）；
  这只是次数限制，不保证账单金额上限。回答成功也不代表游戏内正确。
- OpenAI Key 仅存在当前进程内存中，不会写入 `config/local.json`、SQLite、截图或日志；
  也不会上传整个游戏画面。密钥不应提交 Git。
- OCR 模型和游戏截图仅放在本地 `.gitignore` 所排除的目录内。

## 限制和安全

**没有在你的 Windows 电脑、模拟器账号、Maa 原生库和 OpenAI 账号上做过真实端到端验证。**
本次测试为离线业务逻辑测试与 Python 编译；Qt 控件的现场运行还需在安装 PySide6 的电脑验收。
首先验证 `扫描→连接→截图→OCR`，不建议直接开启自动点击。

若检测到异常验证、登录页、不认识的弹窗、OCR 低置信度或画面比例变化，工具应暂停。
不包含游戏内存修改、注入或绕过反作弊技术；使用自动化操作仍可能违反游戏条款并带来账号处罚风险。

## 源码结构

- `gui_app.py`：真实 PySide6 窗口、设备后台串行队列及信号、按钮、日志。
- `gui_backend.py`：与 GUI 解耦的设备控制、截图、OCR 调用、答题、题库查询与反馈。
- `maa_adapter.py`：固定 Maa 5.15.0 的 Android ADB 适配器。
- `quiz_vision.py`：严格页面校验、ROI 与单题安全提交；添加取消检查。
- `quiz_core.py`：SQLite／OpenAI，API Key 可仅通过内存参数注入。
- `requirements-gui.txt`：PySide6＋原设备依赖。
- `setup_gui.bat`、`start_gui.bat`：Windows 初次安装与启动。

最新完整模拟器说明见 [EMULATOR.md](EMULATOR.md)，离线测试范围见 [VALIDATION.md](VALIDATION.md)。
