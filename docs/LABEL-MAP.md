# 开放词表 → 固定词表：映射口径（label-map-v2；v1 见 git 历史）

> 读者：要用真实材料标注做评测的人。映射表在 `docs/label_map_v2.json`（v0.7 起默认；v1 保留作对照），脚本 `scripts/label_map.py`，输出 `~/.rapport/eval/gold/cases-v2.json`。

**v2 结果（2026-09-19）**：语用只落 `none` 4.6%（v1 26.7%），情绪 1.4%（v1 64.3%）。分布：分享陈述 11,430 · 不确定 4,755 · 询问 3,718 · 观点评价 2,812 · 同意接受 2,408 · 抱怨 1,824 · 调侃 1,331 · 信息确认 1,240 · 委婉拒绝 627 · 寻求安慰 231 · 真生气 102 · 反讽 54 · 撒娇假生气 21。情绪：无明显情绪 14,667 · 无奈 2,226 · 开心 1,852 · 惊讶困惑 1,585 · 亲近 1,041。

## 为什么要映射

外部标注（context-v4）用的是**开放词表**：语用标签 3,867 种、情绪 94 种（"分享代码""催促赴约""无明显情绪"……）。产品固定词表只有 8 个语用标签和 7 种情绪。不做映射，这 28,592 条对方消息一条也进不了 `intent_eval`。

映射不是"把长尾都塞进 8 类"。每个开放标签只能落到三种结果之一：

| 结果 | 含义 | 在评测里怎么算 |
|---|---|---|
| 固定标签 | 语义上确实是这一类（「延期回应」→ 委婉拒绝） | 正常金标签 |
| `none` | **固定词表没有这一类**（「观点评价」「抱怨」「笑声回应」） | 不是弃答。金为 `none` 的行只算 **none 误报率**：模型硬给出负向标签才算错，不计入 macro-F1 与全标签命中 |
| `unmapped` | 映射表尚未裁定（<10 次的长尾且前缀规则没接住） | 该消息进 weak 层，不进 gold |

## 规则

- ≥10 次的标签逐个人工定（`pragmatic` / `emotion` 表）；更长尾走 `pragmatic_prefix_rules`（前缀或包含匹配，顺序即优先级）。
- 「明确拒绝 / 拒绝提议 / 拒绝邀约」→ 委婉拒绝，并把 `boundary` 抬到 `explicit_decline`（`boundary_override`）。固定词表里没有"直接拒绝"这一类，这是已知的表达力缺口。
- 情绪：正向（开心/亲近）与负向（不满/伤心/担忧）同现 → `混合`。
- 一条消息若 `media_unknown`、`needs_review`、含 `unmapped`、或标注 text 与原文对不上 → **weak** 层；其余 → **gold** 层。两层都是 **AI 弱金**，不是人工金标准。

## 2026-09-18 首次映射结果（真实材料 9 段）

| 项 | 数 | 占比 |
|---|---:|---:|
| 对方消息 | 28,592 | |
| gold / weak | 22,410 / 6,182 | 78% / 22% |
| 语用只落到 `none` | 7,642 | **26.7%** |
| 语用含 `unmapped` | 1,013 | 3.5% |
| 情绪只落到 `none` | 18,373 | **64.3%** |
| 标注 text 与原文不一致 | 403 | 1.4% |

映射后固定语用标签分布：信息确认 13,637 · 不确定 4,755 · 调侃 1,331 · 委婉拒绝 627 · 寻求安慰 231 · 真生气 102 · 反讽 54 · 撒娇假生气 21。

**结论**：四分之一的对方消息在固定词表里没有类，情绪上是三分之二。这不是标注质量问题，是产品词表设计成"只抓风险与暧昧信号"的直接后果。它成为 `docs/LABEL-V2-PROPOSAL.md` 的依据。

## 怎么跑

```
cd backend
uv run python ../scripts/label_map.py --annotations "~/.rapport/eval/annotations" --lines "~/.rapport/eval/redacted" --report
uv run python ../scripts/intent_eval.py --fake --cases "~/.rapport/eval/gold/cases-v1.json" --stratum gold --sample 12 --conditions context
uv run python ../scripts/intent_eval.py "<key file>" --model "DeepSeek V4.1 Flash" --cases ... --stratum gold --sample 300 --repeat 3 --conditions context --out "~/.rapport/eval/gold/real-baseline-<date>.md"
```

`--out` 必须在仓库外；`--sample` 按段比例、固定 seed，可复现。报告表头会写明用例文件、映射表版本与内容哈希，以及分层与抽样参数。
