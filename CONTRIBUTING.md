# Contributing

感谢你的兴趣。几条原则：

1. **永远不要提交真实聊天记录、截图、昵称或 API Key。** 测试与 Issue 一律使用 `fixtures/synthetic/` 或自己编造的对话。
2. **默认零外发不能被破坏。** 任何新的网络路径（新供应商、遥测、更新检查）都需要先开 Issue 讨论并写一份简短的 ADR（`docs/adr/`）。
3. **数字要有分母。** 新指标必须带 `n` 与 `unavailable_reason`，缺失不补 0。评分权重 / 锚点 / 门槛的改动要说明理由并更新 `RUBRIC_VERSION`。
4. **边界优先。** 任何建议类功能都必须在 `boundary != none` 时退化为“尊重边界”类行动。
5. **原创。** 提示词、词表、行动模板、彩蛋文案必须是原创，不复制商业产品或研究限定数据集的内容。

## 流程

- Fork → 分支 → PR。PR 描述请写明：影响的模块、是否新增网络请求、是否改变分数、测试怎么跑的。
- 提交请签署 DCO（`git commit -s`）。这不是版权转让。
- CI 必须绿：`ruff`、`pytest`、`tsc`、`vitest`、前端构建。

## 本地开发

见 README「开发」一节。新增解析器：在 `backend/rapport/parsers/` 实现 `Parser` 子类（`sniff` + `parse`），注册到 `registry.py`，并在 `tests/test_parsers.py` 添加 ≥2 个合成 fixture。
