# Third-party notices

Rapport 自身代码为 MIT 许可。运行时与构建依赖保留其各自许可（以锁文件中的实际版本为准，发布前请用 `uv pip licenses` / `npx license-checker` 复核）：

| 组件 | 许可 | 用途 |
|---|---|---|
| FastAPI, Starlette, uvicorn | MIT / BSD-3 | HTTP 服务 |
| Pydantic, pydantic-settings | MIT | 数据合同 |
| SQLAlchemy, Alembic | MIT | 本地存储 |
| cryptography | Apache-2.0 OR BSD-3 | 本地加密 |
| openai (Python SDK) | Apache-2.0 | OpenAI 兼容客户端（请求发往用户配置的端点，不一定是 OpenAI） |
| httpx, typer | BSD-3 / MIT | 网络 / CLI |
| rapidocr, onnxruntime, Pillow, numpy（可选） | Apache-2.0 / MIT / MIT-CMU / BSD | 本地截图识别；RapidOCR 内置的 PaddleOCR 系列模型权重许可以其仓库声明为准 |
| React, react-dom, react-router | MIT | 前端 |
| @tanstack/react-query | MIT | 数据获取 |
| Apache ECharts | Apache-2.0 | 图表 |
| Tailwind CSS, Vite, TypeScript, vitest | MIT / Apache-2.0 | 构建与测试 |

本项目不打包任何研究限定数据集（如 ESConv、CPED）或其派生内容；提示词、词表与模板均为原创。

## 离线情绪研究层与本地模型（可选组件）

两者都默认关闭、按需开启、完全离线。模型权重**不随仓库分发**，由 `tools/` 下的脚本按需下载并导出。

| 组件 | 许可 | 用途 |
|---|---|---|
| BAAI/bge-small-zh-v1.5 | MIT | 句向量编码（kNN 检索与类别质心）。ONNX 导出与完整性清单由 `tools/bge_model.py` 生成，运行时代码在 `backend/rapport/embed`。来源：https://huggingface.co/BAAI/bge-small-zh-v1.5 |
| iic/nlp_structbert_emotion-classification_chinese-tiny | Apache-2.0 | 单句 7 分类情绪小模型，作融合层的次要信号。导出与清单由 `tools/emotion_model.py` 生成，运行时代码在 `backend/rapport/emotion`。来源：https://www.modelscope.cn/models/iic/nlp_structbert_emotion-classification_chinese-tiny |
| ConvoKit（中文礼貌策略） | MIT | 可观察语用线索。vendored 于 `backend/rapport/research/vendor/convokit_zh`，由 `backend/rapport/grounded/markers.py` 使用。来源：https://github.com/CornellNLP/ConvoKit |
| jieba 0.42.1 | MIT | 中文分词（礼貌标记）。`research-signals` extra。来源：https://github.com/fxsjy/jieba |
| ruptures 1.1.10 | BSD-2-Clause | 变化点检测（PELT），用于趋势卡片上的探索性标记。`research-signals` extra；连带 numpy（BSD-3）与 scipy（BSD-3）。来源：https://github.com/deepcharles/ruptures |

## 微信数据库读取（可选组件）

默认关闭、按需开启、只在本机运行、不上传。详见 [docs/WECHAT-DATABASE.md](docs/WECHAT-DATABASE.md)。

| 组件 | 许可 | 用途 |
|---|---|---|
| wechatauto-replica 1.2.4.3 | Apache-2.0 | 数据库引擎（**仅引擎**）。改动说明与完整许可证见 `backend/rapport/vendor/wechatauto_db`。来源：https://github.com/fanyuantaier/wechatauto-replica |
| dsh-wechat-bot（commit `42d672ea8aa49925637973fe11194f768bc92af6`） | MIT | 消息源、输入名与桌面工作线程适配的派生来源，逐模块在文件头部标注。完整许可证：`backend/rapport/desktop/LICENSE-dsh-wechat-bot.txt`。来源：https://github.com/huaqian695-sudo/dsh-wechat-bot |
| pywechat / pyweixin | LGPL-2.1 | 既有的窗口导航适配 `backend/rapport/desktop/pyweixin_navigation.py`。许可证：`backend/rapport/desktop/LICENSE-pyweixin.txt`。来源：https://github.com/Hello-Mr-Crab/pywechat |
| zstandard 0.25.0 | BSD-3 | 数据库解压。`wechat-database` extra。 |
| psutil | BSD-3 | 按微信进程打开的数据库文件识别已登录账号（不按修改时间）。`wechat-database` extra。 |

## 实际锁定的版本（2026-09-15，取自 uv.lock 与 package.json）

后端（Python）：fastapi 0.141.1 · pydantic 2.13.5 · sqlalchemy 2.0.52 · alembic 1.20.0 ·
cryptography 50.0.1 · openai 3.13.0 · uvicorn 0.52.4 · typer 0.27.2 · httpx 0.28.1 ·
rapidocr 3.9.2 · onnxruntime 1.30.0 · pillow 12.3.0

前端（Node）：react / react-dom 19 · react-router-dom 7 · @tanstack/react-query 5 · echarts 5 ·
tailwindcss 4 · vite 6 · typescript 5.7 · vitest 2.1

**这些版本已锁定并实际安装运行**；上面的表格给出的是许可类别，尚未做完整 SBOM 与传递依赖审计。
