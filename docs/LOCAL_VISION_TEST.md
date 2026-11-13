# 局部识别优化：真机／模拟器测试

## 本次范围

保留 MaaFramework 5.15.0 和原有 GUI，不引入 KeymouseGo。
本次改动集中在日常任务链路：主界面 → 活动 → 金扫帚列表 → 领取 → 等待 → 核对完成。

- `DailyRunner.read()` 默认只获取一次新画面；读取某个区域时才运行局部 OCR。
- 同一帧内，任务表识别一次，卡片／次数／按钮从这次结果筛选，不逐卡重新 OCR。
- 相邻帧同一 ROI 的像素完全一致、OCR 置信度达标且未超过 1 秒，才复用文字结果。
- 缓存最多 32 个区域；命中不延长过期时间；空结果、低置信度或纯色画面不跨帧缓存。
- 点击（包括失败、结果未知）、画面尺寸改变都会清除缓存。已经用于点击的画面不能再次点击。
- 固定入口仍采用已校准坐标，点击前必须通过页面条件；按钮颜色检查仍保留为 OCR 漏识别保护，不能仅凭颜色消耗道具。
- 菜单等待默认改为识别结束后再等待 0.2 秒，不是承诺每秒 5 次识别。
- 长任务每 3 秒只读检查追踪区；追踪消失后再读结束提示。两个新帧出现明确完成提示才提前检查列表，追踪消失本身不算完成。
- 五环等没有明确结束文字的页面保留原来的定时核对，不冒险猜测完成。
- 普通宝图保留固定等待 5 分钟后首次核对，不启用提前检查；出现文牒通关选项时优先使用并直接核对完成状态。
- GUI 进度回调复用日常流程已有截图，不再为了每条日志多截一次图。
- `data/maa/daily/performance.json` 记录截图、OCR、像素比较、点击、进度回调等耗时与调用量。

仍然获取设备整帧；本次减少的是识别范围、重复 OCR 和重复取图，不是 Android 端原生局部截图。
雷电连接会自动查询所选 ADB 路径旁的 `ldconsole.exe`，用 `get-serialno` 核对实例后启用 `LDPlayerExtras` 无损快速取图候选。
不保存实例 PID；每次连接重新查询。Maa 在初始化时实测快速通道和原有 ADB 通道并选择较快的可用方式；缺少 DLL、控制台失败或实例不匹配时保留原连接。
运行中取图失败仍按原逻辑暂停，重新连接后重新检测通道，不自动重试游戏点击。无需新增设置或依赖；保持原始截图分辨率，局部 OCR 和坐标校验不变。
2026-11-13 本机 1600×900 雷电复测：正常入口自动启用后 20 次截图中位数约 57ms，普通 ADB 对照约 742ms；模拟快速 DLL 加载失败后仍可通过 ADB 截图。
画面方向、稳定区域像素及本地 OCR 已核对，204 项离线测试在 UTF-8 模式下通过；整轮任务和长期稳定性尚未验收。
未改动答题模块的四次读取／题目一致性／提交去重保护；也未默认启用未经实机标定的颜色或模板来替代文字页面校验。
取点取色 GUI 编辑器和答题 OCR 优化不包含在这批提交中。

## 启动

在当前项目目录，先保存自己的未提交修改，再切到测试分支：

```powershell
git fetch origin
git switch --track origin/perf/local-vision
python gui_app.py
```

如果本地已有该分支，使用 `git switch perf/local-vision`，再 `git pull --ff-only`。
现有依赖已安装时无需重新下载 OCR 模型，本次没有新增运行依赖。

## 真机连接和布局限制

使用现有 ADB 连接入口。Android 手机需开启 USB 调试，并在手机上确认对电脑的授权；选择对应设备序列号，不能误选其他设备。
连接后先检查截图内容、方向和尺寸，再开始任务。

当前日常坐标仍基于 `834 × 470` 的约 16:9 横屏游戏内容区域。
自动检查宽高比偏差，超出约 2.5% 会停止，不会硬把 20:9/19.5:9 手机画面拉伸到 16:9。
系统状态栏、导航栏、黑边、游戏 UI 布局变化均可能使原标定失效。
**不符合已标定布局的手机不要直接执行；先提供诊断画面重新标定。**

## 第一轮：只检查，不点击

手动打开游戏“活动 → 日常 → 金扫帚任务列表”，点击 GUI 的“检查日常列表”。
这是只读操作。核对每张卡片名称、次数、前往／立即完成按钮是否正确，尤其检查已完成任务和卡片排序变化。

检查本地文件：

- `data/maa/daily/screen.png`：本次画面。
- `data/maa/daily/ocr.json`：只包含本次实际识别的区域，`partial_ocr: true` 是正常状态，不代表丢失了一次完整识别。
- `data/maa/daily/performance.json`：识别方式、调用量、耗时、缓存命中数。

默认 `capture_scope` 是 `full_device_frame`，`recognition_scope` 是 `on_demand_roi`。
初次 OCR 可能含模型加载耗时。先检查一次预热，再重复检查用于比较。
GUI 的每次“检查日常列表”使用一个新的 runner，因此不会跨按钮操作保留识别缓存；缓存收益主要发生在一个日常运行内部。

## 第二轮：一个任务

只勾选一项日常任务再启动。已有金扫帚时仍会沿用原逻辑消耗金扫帚，这不是本次新增行为；不希望消耗时不要运行该操作。

检查活动入口和扫帚入口是否正常，任务卡片是否识别正确；领取后不能重复点领取，等待期间不能操作战斗。
出现完成提示时可提前核对列表；没有明确提示时按原定时策略核对。
测试“停止 / 断开”，确认停止后没有后续点击。停止助手不会停止游戏内已经启动的自动任务。

宝图支持普通领取、文牒优先及已有金扫帚路径；普通竞技场尚未接入，仅支持已有金扫帚路径。

## A/B 比较与回退

模式通过当前进程的环境变量读取，关闭 GUI 后从同一个终端重新启动。

默认局部模式：

```powershell
$env:DHXY_VISION_MODE = "roi"
python gui_app.py
```

全图 OCR 对照模式（保留新的点击保护，不是撤销全部改动）：

```powershell
$env:DHXY_VISION_MODE = "full"
$env:DHXY_EARLY_PROBE = "0"
python gui_app.py
```

在同一页面预热后重复“检查日常列表”，每次把 `performance.json` 另存到本地不同文件再比较。
文件会覆盖为最近一次运行结果。不要直接用不同任务的总完成时间推算提速倍数。

其他可调参数：

| 环境变量 | 默认值 | 范围／含义 |
|---|---:|---|
| `DHXY_POLL_SECONDS` | `0.20` | 0.05～2 秒，菜单轮询额外等待 |
| `DHXY_CACHE_SECONDS` | `1.0` | 0～2 秒；0 禁用跨帧缓存 |
| `DHXY_PROBE_SECONDS` | `3.0` | 1～30 秒，长任务只读检查间隔 |
| `DHXY_EARLY_PROBE` | `1` | 0 禁用提前检查，1 启用 |

恢复默认：

```powershell
Remove-Item Env:DHXY_VISION_MODE -ErrorAction SilentlyContinue
Remove-Item Env:DHXY_EARLY_PROBE -ErrorAction SilentlyContinue
Remove-Item Env:DHXY_POLL_SECONDS -ErrorAction SilentlyContinue
Remove-Item Env:DHXY_CACHE_SECONDS -ErrorAction SilentlyContinue
Remove-Item Env:DHXY_PROBE_SECONDS -ErrorAction SilentlyContinue
```

## 如何判断瓶颈

`timings.capture`：取图、传输及复制的合计；不能仅靠缩小 OCR 区域解决。
`timings.ocr`：真正执行 OCR 的合计；`counters.ocr_pixels` 为输入区域像素累计，不是实际模型运算量。
`counters.ocr_cache_hits`：新帧区域完全相同时省下的 OCR 次数。
`counters.same_frame_region_hits`：一帧内复用已识别区域的次数。
`timings.progress_callback`：包含 GUI 编码现有预览画面的时间，不含额外设备截图。
均值为全运行统计，P50/P95 仅使用最近 512 次样本。没有发生的阶段可能不出现。

缓存命中少不一定是错误：动画、字体闪烁、画面压缩和背景变化都会导致像素不完全相同。
本版宁可重新 OCR，也不把大致相似的画面当作同一题／同一按钮。

## 反馈材料

提供运行阶段、手机／模拟器型号、截图分辨率、是否有黑边、相关 GUI 日志、`performance.json`；识别异常时再附 `ocr.json` 和截图。
截图和 OCR 可能包含角色名、聊天等个人信息，发送前自行裁剪或遮盖；不要上传账号密码、API Key、数据库、完整 `data` 目录。
这些运行文件在 `.gitignore` 中排除，不应提交到公开仓库。

## 自动化验证

```powershell
python -m unittest discover -s tests -v
```

测试覆盖原有日常回放、局部识别、ROI 坐标、缓存超时／失效、单像素变化、停止、失败点击、提前完成提示、全图对照、指标输出。
测试使用模拟帧和既有 OCR 回放，不替代真机 OCR 准确率、ADB 延迟和实际 UI 布局验证。
