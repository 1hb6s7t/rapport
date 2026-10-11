# 本地中文 OCR 配置与验证

当前默认采用 **PP-OCRv6 small 文字检测 + PP-OCRv5 server 中文识别**，通过 RapidOCR 3.9.2 调用 ONNX Runtime。微信截图与聊天正文全部在本机处理。

Windows 显卡版本安装：

```powershell
uv sync --extra desktop --extra ocr-gpu
```

CPU 版本：`uv sync --extra desktop --extra ocr`。两组运行时共享同一个 `onnxruntime` 模块，不能同时安装；项目已声明 extras 互斥。DirectML 版本固定为 1.24.4，模型配置固定，不随上游默认模型改变。

## 本机验证

测试设备：i9-13900HX、RTX 4060 Laptop 8 GB、Windows；DirectML 默认设备 0 经 DXGI 核对为该显卡。三个小样本分别为中文 18px、中文 24px、用户提供的聊天截图，各检查五个预期文字片段。下表为已预热的单张推理中位数，不能代表所有图片或完整滚动读取的端到端耗时。

| 配置 | 中文 18px | 中文 24px | 聊天截图 |
|---|---:|---:|---:|
| 原默认 small 模型 / CPU | 4/5 · 1637ms | 5/5 · 1801ms | 5/5 · 1857ms |
| v6 medium 识别 / CPU | 4/5 · 4154ms | 4/5 · 4252ms | 5/5 · 3982ms |
| v5 server 识别 / CPU | 5/5 · 7347ms | 5/5 · 7913ms | 5/5 · 9826ms |
| **v5 server 识别 / DirectML** | **5/5 · 141ms** | **5/5 · 245ms** | **5/5 · 209ms** |

交接机复测（2026-09-28，RTX 4060 Ti 16 GB，`onnxruntime-directml` 1.24.4）：一张 1400×751 的真实微信聊天裁剪图，DirectML 首次 3.19 秒（含建会话），预热后中位数 **269 ms**；同一张图纯 CPU 预热后中位数 **24.0 秒**。两者识别结果一致（34 个文字框，内容摘要相同）。此前在 CPU 版上实读 69 段画面用时 1502 秒，平均每段约 22 秒，主要耗在识别上。

候选对比关闭了正向截图不需要的角度分类；应用保留角度分类以兼容截图导入，实际封装接口在该聊天截图上预热后测得约 146ms。第一次调用包含模型加载和显卡准备，应用测试约 3 秒；不同设备首次编译可能更久。复用模型实例，避免每一屏重新加载。

这只是小样本选择依据，并非总体准确率保证。额外的孤立小字号姓名样本仍出现近形字误识别，所以好友身份优先由无障碍提供的标题名称和输入框收件人名称双重精确核对，正文仍需用户校对。

## 实现

- 检测与识别仍是可替换的本地 ONNX 模型，RapidOCR 是执行封装。
- 模型缓存在应用数据目录 `ocr-models/`；首次获取只下载公开模型，不上传图片。
- 正式安装前校验了公开模型 SHA256；官方模型内嵌对应字符字典，不混用旧字典。
- `run()` 直接传入 PIL RGB 图像，避免 ndarray 被 RapidOCR 按 BGR 解释造成颜色颠倒。
- 对共享实例的初始化和调用加锁，避免同时读取微信与导入截图时互相影响。
- 限定 CPU 线程数量；安装 DirectML 后启用显卡，未安装时使用 CPU。界面显示识别 session **实际使用**的设备，不把“支持 GPU”当作“正在用 GPU”。
- 只有真正在 DirectML 上识别时才占用本地显卡锁（`learning/gpu.lock`）。本地训练或生成占着显卡时，识别改用同一套模型的 CPU 会话，慢但不失败，也不争显存。CPU 版或已回退到 CPU 的会话不碰显卡锁。微信读取的进度和结果里会写明每段画面实际用的设备。
- DirectML 保持默认 provider 选项；RapidOCR 3.9.2 的非空 `dml_ep_cfg` 会成为 DictConfig，实测可能造成 ORT 回退 CPU。
- 数组输出和空结果均经过回归验证，不再使用 NumPy 数组的隐式真值判断。

验证文件：`backend/tests/test_ocr_engine.py`，涵盖颜色、坐标、置信度、空结果、旧输出格式、并发串行化以及显卡实际状态与 CPU 回退。

参考：[RapidOCR 官方模型列表](https://rapidai.github.io/RapidOCRDocs/main/model_list/)、[官方模型地址与校验值](https://github.com/RapidAI/RapidOCR/blob/main/python/rapidocr/default_models.yaml)、[DirectML 运行时要求](https://onnxruntime.ai/docs/execution-providers/DirectML-ExecutionProvider.html)。
