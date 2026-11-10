# v0.2.0 验证记录

## 已执行

在 Linux / Python 3.13.5 的当前构建环境执行：

```text
python -m unittest discover -s tests -v
Ran 93 tests
OK
```

其中 38 项是原答题核心回归；55 项是新加入的布局、OCR 结果解析、设备合同和提交保护测试。
`maa_adapter.py`、`quiz_vision.py`、`device_app.py`、`setup_ocr.py` 通过 compileall。
`device_app.py --help` 可在未安装 MaaFramework 时执行；旧核心离线演示仍可独立运行。

## 覆盖范围

- 参考分辨率/720p/双倍缩放；竖屏、超宽屏、区域越界、缺失区域拒绝。
- 多行文字合并、同一行片段排序、数字与否定词保留、低分/NaN/缺失文字拒绝。
- 大理寺标题、四个字母标签、四个不同选项与题号范围校验。
- 同日同设备题目实例稳定，设备隔离，画面/尺寸/选项顺序变化与过期拒绝。
- 默认不点击、不配置 AI 时不请求；新候选须显式许可，执行须确认布局。
- API 结果回来后的重新识别、一次点击、重复提交阻断、点击失败未知状态。
- 落盘失败时不提交；缓存命中不重复调用；正确性不会因输入成功被自动确认。
- 多设备不得自动选第一个，按地址选择，同地址去重，手动 ADB 回退。
- 对 Maa Toolkit/AdbController/Tasker/JOCR 接口的 mock 合同测试。
- 下载资源的精确长度/Git blob 哈希校验，缺失文件失败；进程锁互斥与释放。

## 未执行，不代表通过

MaaFramework 原生 DLL/共享库加载、OCR 模型真实推理、真实模型下载、Windows 环境安装、
Android 模拟器/真机截图点击、游戏内答题反馈识别、真实 OpenAI 请求。
本轮测试使用模拟对象和人工合成文字块，**不是图片识别准确率测试，更不是游戏端到端测试**。
模型来源和 Python API 已与固定上游提交/标签核对，不能替代目标机器上的集成验收。

原 `TEST_RESULTS.txt` 和 `demo_output.json` 保留为 v0.1.1 的历史记录。
目标设备验收顺序：devices → capture → guard → inspect → answer（预览）→ 单题显式执行。
