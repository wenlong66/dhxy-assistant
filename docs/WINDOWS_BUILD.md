# Windows 64 位免安装版

## 使用成品

1. 将 ZIP **完整解压**到可写目录，例如 `D:\Tools\DHXY-Assistant`。不要在压缩包内直接运行，也不要放在 `Program Files` 等受保护目录。
2. 双击 `DHXY-Assistant.exe`。使用者不需要安装 Python，不要将 EXE 与 `_internal`、`assets`、`config` 分开。
3. 单独安装并启动 Android 模拟器，开启本地 ADB 调试，手动登录游戏。
4. 点击「扫描设备」→选择实例→「连接」。自动发现失败时，在「连接设置」填写模拟器自带的 ADB 路径和真实实例地址。
5. 先验证截图和 OCR，再使用自动任务；任务实机校准与限制不会因为打包而改变。详细操作见 [GUI 使用说明](GUI.md)。

配置保存在 EXE 旁的 `config/local.json`，题库、截图及日志保存在 `data/`。
AI 默认关闭；如需启用，将 `config/ai.example.json` 复制为 `config/ai.local.json` 并在本机填写，不要分享密钥。

升级时把新版解压到**新文件夹**，关闭旧版后，按需复制旧版的 `data/`、`config/local.json` 和 `config/ai.local.json`。
自定义识别布局也需自行保留；不要直接用新版文件覆盖正在使用的目录。删除整个应用目录会同时删除其中的题库和配置。

成品没有数字签名，Windows 可能提示未知发布者。不要为运行本程序关闭系统安全防护；先确认文件来源。
自动化操作可能违反游戏条款并带来账号处罚风险。

## 一键重新打包（源码维护者）

需要 Windows x64、64 位 Python 3.11 和构建依赖下载网络；构建时不要运行待打包的 EXE。
在项目根目录双击 **`build_windows.bat`**：

1. 复用 `.venv`，不存在时用 `py -3.11` 创建；拒绝 32 位或 ARM64 Python。
2. 安装 `requirements-build.txt`，校验 OCR 模型；缺失或损坏时调用现有 `setup_ocr.py` 下载并校验。
3. 运行全部离线测试；失败即停止，不生成 ZIP。
4. 用 PyInstaller 构建无控制台的目录版，携带 Maa 原生 DLL、Android 辅助工具及 OCR 模型，并检查所需 VC 运行库 DLL 已随包携带。
5. 从不同工作目录启动成品自检：模型完整性、布局、Maa 原生资源加载、临时配置与 SQLite 读写、GUI 打开与正常退出。**不连接设备、不发送点击、不调用 AI。**
6. 在 `dist/DHXY-Assistant-win64-时间戳/` 生成应用目录，同时生成同名 ZIP；每次构建使用新目录，不删除或覆盖旧成品。

构建记录、自检 JSON 和 PyInstaller 警告保存在 `build/windows/时间戳/`。
已安装依赖、模型均已准备好时，也可运行：

```powershell
& .\.venv\Scripts\python.exe -X utf8 build_windows.py
```

打包采用明确的资源白名单，不复制本机 `config/local.json`、`config/ai.local.json`、环境变量文件、`data/`、截图或私人题库。
依赖版本记录见成品中的 `dependency-versions.json`，随包许可证见 `licenses/`。
MaaFramework、MaaAgentBinary、Qt 等各自的许可证及再分发义务需分别遵守；保留许可证不等于完成全部再分发义务，公开发布前需核对实际版本条款。

构建自检不代表已在未安装 Python/VC 运行库的全新电脑上验证，也不代替模拟器连接、真实 OCR 推理或游戏任务实机验收。
