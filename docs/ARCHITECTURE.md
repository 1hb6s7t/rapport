# 架构与扩展点（v0.4）

> 读者：想改代码的人。目标：改一处新功能时，知道该碰哪些文件、不该碰哪些。

## 1. 分层与依赖方向

```
api/*  ──►  state/*  ──►  db/*          （HTTP 层只做校验与翻译，不含业务规则）
  │            │
  │            └──►  llm/audit（AuditedProvider）──►  llm/openai_compat / llm/fake
  │
  └──►  jobs/runner ──► jobs/pipeline ──► features / llm / scoring / report / knowledge
                                              ▲
                              schemas/*  ◄────┘   （纯数据合同，谁都可以依赖，它不依赖任何人）
```

硬规则：

- `scoring/`、`features/`、`report/`、`knowledge/` **不得**导入 `state`、`db`、`api`。它们是纯函数层，同输入同输出，可离线测试。
- `jobs/status.py` 是分析状态机的唯一定义。任何地方写状态字符串都要从它取。
- `state/` 按职责拆成 mixin：`provider`（供应商与本地/云守卫）、`imports`（原始材料与证据账本）、`history`（快照/画像版本/阶段）、`diary`、`relationships`、`audit`（调用账本与反馈）、`maintenance`。新增职责就新增一个文件，不要往现有文件里塞。
- `db/migrate.ensure_schema` 每次启动都跑：只加表、只加列（带默认值），永不改名或删列。破坏性变更必须写显式 Alembic 版本。

## 2. 关系档案（Relationship）是所有派生数据的作用域

`imports / analyses / reports / events / snapshots / persona_versions / llm_calls / feedback` 都带 `relationship_id`，默认值 `"default"`（首次启动自动创建）。任何**读历史**的函数都必须带作用域参数：`ledger_n_eff(rid)`、`trend_series(rid)`、`last_stage(rid)`……省略参数意味着"默认对象"，**绝不**意味着"全部"。

- 未知 id 直接 404，不静默新建。
- 删除对象级联删除其全部派生行与加密 blob，并把它从日记授权范围里摘掉；默认对象只能清空不能删。
- 非默认对象的事件指纹会掺入 `relationship_id`，所以两个人各说一句无时间戳的"早"不会合并成一条账本行；默认对象的指纹与 v0.3 完全一致，老库无需重算。

## 3. 每次模型调用都有账（`llm_calls`）

`state.make_provider()` 返回的永远是 `AuditedProvider`。它按系统提示第一行 `TASK: xxx` 识别用途，记录 token、耗时、预算、状态（ok / truncated / bad_json / error）、请求哈希；**不记录任何提示词或回复正文**。账本坏了不会影响分析（sink 异常被吞）。

用途：`GET /api/analyses/{id}/calls` 看一次分析的真实调用；`GET /api/stats` 看 30 天汇总；截断率 >5% 时设置页会提示换模型。这也是 4/60 截断缺陷从"评测脚本里的一行"变成"产品里可观测的数字"的方式。

## 4. 反馈闭环（`feedback`）

报告里每条可判断的主张（逐句语义、观察、行动、雷区、阶段、边界、特质、总分）都有 👍/👎/？。反馈**只记录、不回写**报告；逐句语义的 👎 可以附上修正标签，`GET /api/feedback/export` 把它们导出成评测用例（`adjudicated: false`）。进入 `docs/EVAL.md` 的金标签前必须经人工裁定，这是把"用户用得越多，评测集越像真实分布"做成流程而不是承诺。

## 4b. 出网闸门（`llm/outbound.py`）

任何把用户内容发往模型端点的路径都走同一个三步：**preview（返回将发送的全部内容 + 费用 + 票据）→ 用户确认 → confirm（携票据与哈希，服务端重算哈希比对后才调用）**。分析链的实现在 `api/analyses.py`（历史原因仍用 `Analysis` 行存哈希），代回复链用 `OutboundGate` 的内存票据（15 分钟、单次）。日记的 revision 参与哈希：预览后改了日记，确认会被拒。新增任何“会出网”的功能时，先接闸门，再写业务。

## 4c. 错误契约（`errors.py`）

所有用户可见错误继承 `RapportError(code, msg, hint, status)`；`main.py` 一个处理器渲染成 `{error:{code,msg,hint}}`。新模块的错误类直接继承它，不要在 API 层 try/except 翻译。

## 5. 证据质量 `q_i`

`scoring/evidence.event_quality(kind, ts_precision)`：分钟/秒级文本 1.0，日级 0.9，无时间 0.8，**未读媒体占位 0.0（v0.6）**。此前媒体按 0.5 计，真实材料里几千张没打开过的图片把 `N_eff` 顶高、先验 α 压到 0——"证据够了"的理由是一堆没读的照片。已读的媒体（OCR 截图、微信转写语音）到账本时已经是 text，不走这一支。`evidence_breakdown()` 把已确认事件分成 text / media_unobserved / other，随报告 `prior_breakdown.evidence_counts` 展示。这是 v2 参考代码里 `q_i`（归属正确、上下文完整、独立、已确认）里"上下文完整"那一维的最小实现；归属与确认在上游强制。全部是产品假设，每 episode 上限 5 仍然生效。

## 6. 报告 Schema 演进

`schemas/migrate.upgrade_report_body` 在读取时把旧版本 body 升级到当前版本。今天只有 `report-v1`，注册表为空；将来改字段时写一个纯函数并 `@register_upgrade("report-v1", "report-v2")`，旧行不用重写。未知版本 409，不半解析。

## 7. 新增一个 X 要改哪些文件

| 想加 | 改这些 | 不用改 |
|---|---|---|
| 解析器 | `parsers/<new>.py` + `parsers/registry.py` 注册 + fixture + 测试 | 其它一切 |
| LLM 供应商预设 | `config.PROVIDER_PRESETS` 一条 | Provider 类（OpenAI 兼容即可） |
| 非 OpenAI 协议的供应商 | 实现 `LLMProvider` Protocol 的类 + `state/provider.make_provider` 分支 | pipeline（它只认 Protocol） |
| 好感度维度 | `schemas/report_v1.DIMENSIONS/DIM_LABELS` + `scoring/rubric.WEIGHTS/rule_dims` + `llm/prompts/synthesize.md` + `validate.py` 的维度循环 + 前端 `i18n/labels` | 快照/趋势（按 dict 迭代） |
| 报告分区 | `schemas/report_v1` 字段 + `report/assemble` 装配 + `frontend/api/types.ts` + `Report.tsx` 一个 Card + `report/export_md` | 数据库（body 是 JSON） |
| 派生表 | `db/models.py` 加类（带 `relationship_id`）+ `state/relationships.relationship_delete` 级联列表 + `state/maintenance.wipe_all` 列表 + `tests/test_deletion.py` | 迁移脚本（`ensure_schema` 自动建表） |
| 会出网的新功能 | 先在 `api/<x>.py` 加 `/preview` 用 `st.outbound.issue(...)`，再在正式端点 `st.outbound.redeem(ticket, hash)` 并重算哈希比对 | 闸门本身 |
| 后台任务阶段 | `jobs/status.AnalysisStatus` + `RUNNING` + `Progress(stage=...)` + 前端 `STATUS_LABELS`/`Progress.tsx` `STAGES` | runner（按集合判断） |
| 一种前端语言 | `i18n/labels.ts` 平行文件 + 后端文案（目前散在各处，是已知债务） | — |
| 一种攻略情境（规则模式） | `knowledge/situations.py` 加一对 `(predicate(Features)->trigger_ids, builder)` 进 `SITUATION_TEMPLATES`；选项必须 ≥2 个 `expected_reactions`（含一个 stop）、`stop_when`、`risk` | 校验器、装配、前端（按合同渲染） |
| 一张先验卡（MBTI 某型某倾向的极性假设） | `knowledge/prior_check._T` 表里填极性 + `_COUNTER` 反例提示；`scripts/prior_cards_lint.py` 校验每型 ≥3 卡 | 校验、装配、前端（按 `prior_checks` 渲染） |
| 一种主动提醒 | `drive/nudges.py` 加模板（必须绑证据 id）+ `drive/params.kinds` + `engine.pick_kind` 顺序 + 前端 `NUDGE_KIND_LABELS` | 引擎数学、收件箱、API（按 kind 字符串走） |
| 一种执行器（代发） | 实现 `drive/actuators.Actuator`（`audience="other"` 必须逐条人工确认、边界钉死时不可选）+ `REGISTRY` | 引擎、模板；见 `docs/DRIVE.md` §3 |

## 7b. 攻略合同（v0.6，v0.5 计划 §2）

`Playbook.situations[]` 是主结构：情境（`trigger_ids` 指向真实消息）→ 1–3 个 `Option`（`id = sha1(goal|action)[:10]`、`trigger_ids`、`expected_reactions ≥2`、`stop_when`、`risk`）。`actions[]` 保留给旧客户端：模型没给情境时由规则情境首选项派生。`llm/validate._validate_situations` 的丢弃计数（`situations_no_trigger / options_no_stop / options_few_reactions / options_manipulation / options_advance_under_boundary`）进 `Validated.dropped`，`scripts/synth_compliance.py` 据此报存活率。`MANIPULATION_WORDS` 无论边界都丢。`done_review` 来自 `ActionLog`（`actions_since`）：对方拒绝过的目标，本次不再推荐同目标选项（`assemble.build_done_review`）。

## 8. 已知债务（按优先级）

0. （v0.6 新）主动提醒的 LLM 起草开关 `llm_drafts` 已预留、未实现；实现时必须走 `OutboundGate`。驱动层参数全是假设，验收见 `docs/DRIVE.md` §6。
0. （v0.6 新）边界只看最近 30 天（`features.compute.BOUNDARY_RECENT_DAYS`）；更早的命中进 note 与 basis_ids 但不闸分。30 天是产品假设，未校准；无时间戳材料退回全量命中。
1. 后端用户可见文案散落在各模块字符串里，没有集中的文案表；做多语言前要先归拢。
2. 参考例与日记检索都是字符 bigram 词法检索；`Retriever` 接口留了位，向量检索（本地 embedding）未做。
3. 前端页面各自 `useState` 拉数据，没有统一的查询缓存；`@tanstack/react-query` 已装未用。
4. `Report.tsx` 约 450 行，分区应拆成独立组件文件。
5. 推理型模型截断（评测集 4/60）现在可观测但未消除；分档重试策略待做。
6. 前后端各有一份枚举文案表（阶段、维度、边界、倾向）；v0.4.1 对齐了阶段表，根治要做 `GET /labels` 单一来源。
7. `Report.tsx` 仍是一个大文件；分区拆成独立组件是下一步。
