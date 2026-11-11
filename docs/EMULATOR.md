# Windows + Android 模拟器接入（v0.2.0）

## 这次集成的是什么

是 **Android 模拟器**，不是 Windows 虚拟机，也不是 iOS 模拟器。
本机 Python 助手通过 ADB 连接 Android 实例，MaaFramework 截取该实例的游戏画面、做本地 OCR，
仅在显式授权时点击答案。OpenAI 只收到题干和选项文字，不直接连接模拟器、不返回坐标。
你手动安装游戏、登录账号、打开大理寺题目；本版不代替这些步骤。

```text
Windows Python 程序
  ├─ MaaFramework AdbController → ADB → Android 模拟器里的《大话西游》手游
  ├─ MaaFramework OCR（本地 PP-OCRv5）→ 题干与四个选项
  ├─ SQLite 命中 → 不调用 OpenAI
  └─ 未命中且 --ai → OpenAI → 本地校验 → 再次截图 → 可选单次点击
```

可先测试你已经安装的 MuMu、雷电或蓝叠；不需要同时安装多个。
设备发现走 MaaFramework 官方 Toolkit，手动回退走普通 ADB，未针对每个品牌做实机验证。
不要求 root、不修改游戏、不读取密码。普通 Android 真机通过 USB 调试也可作为后续测试目标，
但本版布局来自横屏游戏截图，不保证所有设备布局都适配。
Windows 手游桌面版不是 Android ADB 目标；本版尚未接入 Win32Controller。

## 1. 安装 Python 依赖与 OCR 模型

在仓库根目录使用 PowerShell：

```powershell
git pull --ff-only
py -3.11 -m venv .venv
$PY = ".\.venv\Scripts\python.exe"
& $PY -m pip install -r requirements-device.txt
& $PY setup_ocr.py
& $PY setup_ocr.py --verify
& $PY device_app.py init-config
```

推荐先使用 Windows x64 + Python 3.11。依赖固定 `maafw==5.15.0`；包名不是 `maa`。
不依赖 PowerShell 激活脚本，直接调用虚拟环境的 Python，避免执行策略问题。
`setup_ocr.py` 从 MaaCommonAssets 的固定提交下载约 21.3 MB 的中文 PP-OCRv5 模型和说明，
按精确长度和 Git blob SHA 校验。模型文件不提交进本仓库，不自动执行下载内容。
GitHub 原始文件域名下载失败时，会自动改用 GitHub 官方 API 下载同一固定版本，每个文件最多尝试 3 次。
两种官方地址均不可访问时会明确失败，不会改用不明镜像。

公司网络需按网络管理要求配置访问；不要禁用 TLS 校验。
已完整安装时 `--verify` 不联网。不需要安装 CUDA；本地 OCR 不产生 OpenAI API 费用。
首次模型加载及实际识别耗时取决于设备；这里没有实测性能数据。

## 2. 启动模拟器并确认原角色

在模拟器安装与你现有账号/渠道匹配的《大话西游》手游，手动登录并确认服务器和角色。
不要为了接入工具清空原游戏数据，也不要把安卓包与 iOS/渠道账号兼容性当作已验证。
建议测试配置：横屏 **1280×720**，固定分辨率和界面缩放。这里只要求近似 16:9；
改变布局、出现黑边/导航条后必须重新检查 ROI，不能只认为窗口看起来差不多。

到模拟器设置中开启 ADB/本地调试，仅允许本机连接。不同品牌、版本和实例端口可能不同；
从当前实例的设置、诊断信息或多开管理器读取，不把某个端口当作所有人的默认值。
**ADB 调试端口不是 ADB 服务端的 5037 端口。不要把调试端口开放到公网。**

## 3. 配置设备连接

先尝试自动发现：

```powershell
& $PY device_app.py devices
```

如果只有一个实例，可以保留 `config/local.json` 的 `adb_path` 和 `address` 为空。
多个实例时必须明确指定；程序不会默认选列表里的第一个。

手动连接：使用模拟器自带的 ADB 或 Android SDK Platform-Tools 的 `adb.exe`。
以下路径/端口仅是占位示例，请换成你实际读取的值：

```powershell
$ADB = "C:\Android\platform-tools\adb.exe"
$SERIAL = "127.0.0.1:你的实例端口"
& $ADB connect $SERIAL
& $ADB devices -l
```

应显示 `device`，不是 `offline` 或 `unauthorized`。真机授权弹窗由你自己确认。
随后编辑 `config/local.json` 的对应项，例如：

```json
{
  "adb_path": "C:/Android/platform-tools/adb.exe",
  "address": "127.0.0.1:你的实例端口"
}
```

文件允许只写需要覆盖的键；其他项继承 `connection.example.json`。JSON 中不能写注释。
相对资源、数据库和布局路径按仓库根目录解析，而不是按当前终端工作目录解析。
`account_scope` 是你自行选择的题库隔离标识，不要填写账号密码。
发现失败且手动连接成功时，明确配置上述两项即可尝试普通 ADB 回退。

## 4. 截图、检查区域、识别题目

手动进入大理寺的一道普通题目，保持游戏画面稳定：

```powershell
& $PY device_app.py capture
& $PY device_app.py guard
& $PY device_app.py inspect
```

`capture` 仅截图，不需要 OCR 模型；产生 `data/capture.png` 和 `data/capture-overlay.png`。
`guard` 校验题号、题干及实际选项，并运行 Maa `DaliSceneGuard` 流水线检查题号，动作是 `DoNothing`。
`inspect` 读取同一帧的题号/题干和二至四个选项及字母标签，不读取会被公告遮挡的顶部标题；结果写在
`data/maa/inspection/ocr.json`，同目录有截图和区域框图。不调用 OpenAI，也不点击。

当前 ROI 是从已有 834×470 参考图人工标注的 **初始配置**，不是已经实测通过的模板。
程序按原始截图尺寸换算 ROI，且控制器强制使用原始截图尺寸，避免重复缩放点击坐标。
比例超过容差时拒绝操作。相同比例并不能保证 UI 相同：需要你检查框图和文本。

查看 `ocr.json` 中 `valid` 是否为 `true`，同时人工核对完整题干、等级/数字、否定词和四个选项。
检测到的任一文本块低于阈值会拒绝整题，不把低分数字或“不”字悄悄删掉。
**OCR 置信度不是正确率保证，连续两帧一致也不是事实准确保证。**

需要调 ROI 时，复制 `config/dali.layout.json` 为 `config/dali.local.json` 并在 local 配置中
把 `layout` 改为 `config/dali.local.json`。矩形是 `[x,y,width,height]`，基于 `reference_size`。
题目区不要包含头像和选项；选项文字区不要包含图标和字母标签。换行道具名在各自选项内合并。
不要通过随意降低阈值来掩盖错误区域或画面分辨率问题。

## 5. 先预览答案，再显式试答一题

只查题库：

```powershell
& $PY device_app.py answer
```

允许未命中时询问 OpenAI，但仍不点击：

```powershell
& $PY device_app.py answer --ai --model YOUR_MODEL_ID --prompt-key
```

`YOUR_MODEL_ID` 必须替换为你账号可用、支持 Responses API/JSON Schema 的模型。
沿用已有 `OPENAI_MODEL`、`OPENAI_API_KEY` 也可以。隐藏输入只用于本机终端；不要把密钥写入
JSON、README、命令历史或 Git。此设备 CLI 仅传题目文字，不上传完整屏幕。

检查截图区域和识别文本后，显式允许一次点击与未验证答案试答：

```powershell
& $PY device_app.py answer --ai --model YOUR_MODEL_ID --prompt-key --allow-trial --execute --layout-confirmed
```

三个开关含义不同：`--ai` 允许 API 费用；`--allow-trial` 允许使用未验证候选；
`--execute --layout-confirmed` 允许已校准界面的实际输入。没有 `--execute` 绝不发点击。
已验证缓存不需要 `--allow-trial`。API 返回后重新识别两次；题目、选项顺序、尺寸变化或
过期均拒绝提交。每次命令最多点击一个答案，不循环答十题，不自动关闭弹窗。

操作回执在 `data/maa/receipts/`。`click_sent=true` 仅指输入作业成功，**不是答对**。
目前还没有从真实答对/答错/完成界面建立反馈规则，所以必须按实际观察手工回写：

```powershell
& $PY app.py feedback 实际attempt_id correct --evidence "实际看到明确答对提示"
```

答错用 `wrong`，没看清用 `unknown`；三者只选实际发生的一种，不要顺序全执行。
自定义数据库时，反馈命令也要使用同一个文件：`app.py --db 路径 feedback ...`。
结果未决会阻止对同题再点击；仅出现下一题、奖励或进度变化不能作为正确证据。

## 故障定位与停止

| 现象 | 排查顺序 |
| --- | --- |
| 找不到设备 | 确认模拟器已启动、开启本机 ADB、实例端口正确；使用同一份 adb.exe 检查 devices |
| 多个设备 | 在 local.json 显式指定 address，不关闭/删除其他实例的数据 |
| 模型缺失 | 运行 setup_ocr.py，再运行 --verify；保留失败信息，不用未知模型包替换 |
| 题号/选项标签未匹配 | 手动进入大理寺题目，检查 overlay、黑边、横屏比例和字体识别结果 |
| 低分/漏字 | 修正 ROI、提高画面清晰度，再核对 inspect；不要强行点击 |
| API 失败/弃答 | 原核心会保留状态，避免自动重复收费；检查模型权限和题目，不循环重试 |
| 重复提交被拒 | 查看回执并按实际反馈回写，不删除数据库来绕过保护 |
| 存在 .device.lock | 先确认所有助手进程已停止；崩溃遗留时才手动删除该锁文件 |

终端 Ctrl+C 请求停止。已经发给设备的点击无法撤回；原生驱动作业退出延迟可能受驱动影响。
不要同时手工操作题目或运行其他点击脚本；本版不支持多账号并发。
脚本不重启设备、不杀 ADB 服务、不自动重新登录、不处理验证码或安全挑战。
公开仓库不包含游戏截图、私人题库、日志、密钥或模型权重。只用视觉和模拟输入仍可能违反游戏规则，
本项目不承诺官方许可或账号安全。

## 核验与参考

代码合同核对至 MaaFramework **v5.15.0**；离线测试覆盖见 [VALIDATION.md](VALIDATION.md)。
本环境没有 Maa 原生运行库、Windows/Android 模拟器或真实游戏连接，所以没有实际 OCR 准确率、
点击成功率或任务完成率报告。

- Maa Python 官方示例：https://github.com/MaaXYZ/MaaFramework/blob/v5.15.0/sample/python/demo1.py
- 识别接口与 JOCR：https://github.com/MaaXYZ/MaaFramework/blob/v5.15.0/source/binding/Python/maa/pipeline.py
- 控制方式：https://maafw.com/docs/2.4-ControlMethods/
- OCR 资源：https://github.com/MaaXYZ/MaaCommonAssets/tree/dabcd4681ac990dc4361de26416d986abd80e4aa/OCR/ppocr_v5/zh_cn
- Android ADB：https://developer.android.com/tools/adb
- Windows 模拟器说明：https://docs.maa.plus/zh-cn/manual/device/windows.html
