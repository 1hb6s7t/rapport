# 本地情绪小模型（EMOTION-MODEL）

> 2026-10-10 起，规则模式的默认离线引擎是「离线研究层」（BGE 近邻检索 + 本模型温度标定后的次要信号 + 礼貌标记），
> 见 docs/EMOTION-LAYER.md；本文件只描述这个单句模型本身。它在本项目弱标注 test 切分上的 macro-F1 只有 0.056
> （没有「无明显情绪」类），单独使用时会把链接、公告也贴上情绪——设置里仍可选「只用情绪小模型」，但不推荐。

规则模式下可选的离线小模型层：把对方最近的消息逐句交给一个公开预训练的小模型，给出情绪标签。
**默认关闭、按需开启、完全离线、不进入客观分数。** 开关在「设置 → 分析与个性化」。

## 它是什么 / 不是什么

- 是什么：ModelScope `iic/nlp_structbert_emotion-classification_chinese-tiny`（Apache-2.0）的
  ONNX 导出。单句 7 分类：恐惧 / 愤怒 / 厌恶 / 喜好 / 悲伤 / 高兴 / 惊讶；映射到本产品的固定
  情绪集（高兴→开心、喜好→亲近、悲伤→伤心、愤怒/厌恶→不满、恐惧→担忧、惊讶→惊讶困惑）。
- 不是什么：不是 AI 分析模式的替代品，也不参与客观关系分数、画像假设或任何打分逻辑。
  它产出的 `IntentReading` 一律带 `source="local_model"`，UI 上逐条标注「本地小模型」。
  低把握（<0.5）的句子标为「材料不足」；负面标签一律附「单句模型容易高估」的提醒
  （本项目实测：`我好想你` 被判为「悲伤」0.93，句子级模型有负面偏置——这正是要逐条标注来源的原因）。
- 运行时依赖只有 onnxruntime（语音转文字 ASR 已在用），不需要 torch。分词器是纯 Python 复刻
  （`backend/rapport/emotion/tokenizer.py`），安装时与参考实现逐样本比对通过才会生效。

## 安装 / 重建（三步，各自在能跑它的解释器里）

```bash
# 1) 下载原始权重（任意 venv；会校验字节数并记录 SHA256）
python tools/emotion_model.py download --out models/emotion-zh-tiny --skip-existing

# 2) 导出 ONNX（需要 torch 的训练 venv）
<train-venv>/Scripts/python.exe tools/emotion_model.py export --dir models/emotion-zh-tiny

# 3) 校验并写 manifest（后端 venv，有 onnxruntime）
backend/.venv/Scripts/python.exe -I tools/emotion_model.py verify --dir models/emotion-zh-tiny
```

`verify` 只在两项都通过时写 `manifest.json`：纯 Python 分词器与参考分词器 16 个样本 id 全等；
ONNX 与 torch logits 最大差 < 1e-4（本次导出实测 4.1e-06）。运行时只信 manifest。

模型目录解析顺序：`RAPPORT_EMOTION_MODEL_DIR` 环境变量 → `<应用根>/models/emotion-zh-tiny`
→ `<数据目录>/models/emotion-zh-tiny`。删除目录并关掉开关即可完全移除该能力。

## 产物清单（models/emotion-zh-tiny/）

| 文件 | 说明 |
| --- | --- |
| `model.onnx` | 35.2 MB，SHA256 记录在 manifest.json |
| `vocab.txt` | 参考词表，SHA256 记录在 manifest.json |
| `manifest.json` | 标签表、映射、上限 128 token、来源、许可证、校验结果 |
| `parity.json` | 16 样本 + 1 批次的参考 id/logits（校验凭据） |
| `export-info.json` | torch / transformers 版本、opset、缺失/多余权重键（本次均为空） |
| `SOURCE-README.md` | 上游模型卡原文 |

## 行为参数

- 每次分析最多判断的消息条数：10–500，默认 120（只取对方最近发出的文字消息）。
- 阈值：max-prob < 0.5 → `ambiguous`；≥ 0.8 → 证据强度 medium，其余 low。
- `model_confidence_raw` 为未校准的原始概率 ×100，UI 明示「未经校准」。
- 读取结果可进入回复草稿上下文（`post_hoc=false` / `context_mode="reply"`，证据只含该条消息本身），
  与 AI 模式读取走同一可见性契约。

## 设置与接口

- `GET/PATCH /api/preferences/emotion`（`config.enabled`、`config.max_messages`、`model` 状态）。
- 设置页「分析与个性化 → 本地情绪小模型」卡片；报告「语义与情绪（逐句）」逐条显示来源徽章。
- 规则模式报告附注：「逐句情绪由本机小模型离线判断（StructBERT-tiny，Apache-2.0），未经人工校准，
  只作参考；不参与客观关系分数。」
