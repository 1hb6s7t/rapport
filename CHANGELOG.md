## v1.0.0 (2026-10-11) — 首个正式版本

把此前分散的研究层、本地模型与可选的微信数据库读取整合进主线，并把仓库整理成可发布状态。

**新增**
- **离线情绪研究层**（`backend/rapport/grounded`、`embed`、`emotion`）：规则模式的逐句情绪可改用本地多信号融合（BGE 近邻检索 + 类别质心 + 温度标定的小模型 + 礼貌标记），每条读数带各信号投票与最相似例句的标签。完全离线、不联网、不进客观分数。默认关闭。详见 [docs/EMOTION-LAYER.md](docs/EMOTION-LAYER.md)。
- **伤心类的类别先验修正**：kNN 投票按类别锚点数做先验修正（`anchors.PRIOR_ALPHA`），在弱标注 validation 与独立参考标注上同时把伤心 F1 从 0 提到可读。
- **参考标注工作流**：工作台新增标签页（`state/gold.py`、`api/gold.py`、`gold_labels` 表），分层抽样、标注前不显示 AI 标签、数字键快捷标注；`tools/evaluate_grounded.py --gold` 用参考标注重评。
- **可选的微信数据库读取**（Windows，默认关闭）：结构化读取本机已登录账号的聊天记录，增量同步、加密游标、可选本机语音转文字。只在本机运行、不发送任何消息。详见 [docs/WECHAT-DATABASE.md](docs/WECHAT-DATABASE.md)。
- **本地模型常驻与选择**（`llm/resident.py`、`llm/selected_local.py`）。
- 新增研究脚本：`tools/build_anchors.py`、`fit_calibration.py`、`evaluate_grounded.py`、`evaluate_local_llm.py`、`sweep_public_fusion.py`、`check_transfer.py`、`experiment_gated_hybrid.py` 等。

**变更**
- 版本号 0.8.0a0 → 1.0.0。
- CI 在安装 `wechat-database` 与 `research-signals` extra 之后再跑测试；工作分支收敛到 `main`。
- 许可由 MIT 改为 **Apache-2.0**（`LICENSE`、新增 `NOTICE`、`pyproject.toml`、徽章与文档同步）。
- README 品牌标识更新为当前版本，与首页水彩标识一致。

**测试**：后端 1000 通过 / 23 跳过（另有 1 项在缺少 `psutil` 的环境下失败，装上 `wechat-database` extra 后通过）。

**能力边界（未变，且必须随发行一起读）**：情绪识别仍只能作候选信号，不能单独定情绪。详见 [docs/LIMITATIONS.md](docs/LIMITATIONS.md) 与 [docs/EMOTION-LAYER.md](docs/EMOTION-LAYER.md)。

## 未发布（2026-09-20）— 对外发布前的去品牌化

**目标**：仓库里不出现第三方工具名与个人私有配置，避免发布后不必要的麻烦。功能行为不变，后端 406 测试通过，前端 tsc / vitest / build 全绿。

- 解析器文件与类改名：`parsers/wx_session_json.py` / `WxSessionJsonParser`，解析器 id 改为 `wx_session_json`，代码里不再出现任何工具名。**sniff 改为结构判定**：只看顶层是否同时有 `session` 与 `messages`，不匹配任何工具名；同一批真实脱敏导出重跑 `private_import_check.py`：9/9 段解析成功、分数与边界与改名前一致。
- 内部约定跟着改：会话元信息键由旧的工具名键改为 `export`、消息级类型键改为 `export_type`；导出头部元信息（version / exportedAt）改为从"第一个带这两个键的顶层对象"读取，不再依赖固定键名。
- `parsers/wechat_export.py` 的解析器标签与错误文案改为"第三方工具"表述；README、README.en、前端导入页同步。
- 文档与脚本里的第三方工具名、私有网关域名、个人目录示例（`D:/exports`）改为中性表述；测试里的示例端点改用 `api.example.invalid`，测试占位 key 改用 `test-key`。
- 合成 fixture 改名：`fixtures/synthetic/wechat_session_export.json`、`fixtures/synthetic/wechat_export.csv`（原文件名含工具名）。
- 已发布的 git 历史也已重写，旧名称不再出现在任何提交里（提交哈希因此全部改变）。

**对老数据的影响**：数据库里历史导入行保存的 `parser_id` 仍是旧字符串（导入记录是事实，不追溯改写），评审页按字符串原样显示，不影响解析与报告。用旧 id 显式指定格式不再被接受，需改用 `auto` 或 `wx_session_json`。

## v0.8.0-alpha (2026-09-19) — 规则透明化、去评分门禁、本地模型

**规则透明化**：`docs/RULES.md` 逐项写出规则模式的特征、锚点、权重、阶段与边界判定，以及诚实边界。

**去评分门禁**（用户决定）：
- `scoring/affinity.build_affinity`：总分永远给出；`Affinity.caveats[]` 列出证据薄弱与边界提示，`Affinity.confidence` 按证据量给 low/medium/high；`alt_totals`/`provisional_total` 同样永远给出。
- `llm/intent`：负向门槛移除（`apply_negative_gate` 为空操作，原逻辑保留为 `_legacy_negative_gate`）；`independent_units`/`consistency` 只作展示。前端负面判断默认展开，每条标「N 个片段支持」。
- `llm/validate`：边界下推进类行动/选项/直接话术不再丢弃，带 `caution`（`BOUNDARY_CAUTION`）；操控话术过滤保留。
- `knowledge/situations.rule_situations`：边界不再清空情境，`why` 前置 ⚠ 并给推进类选项加 `caution`；`fallback_playbook` 边界骨架放第一条，目标行动保留带提示。
- `scoring/stage`：边界不再阻止「稳定」。报告顶部新增边界横幅（附证据，注明不改分数）；导出同步。
- 真实 9 段规则层：9/9 出分。测试改写 9 处，396 通过。

**本地模型（零出网，用户要求：聊天记录不上云）**：
- `llm/ollama.py` `OllamaProvider`：Ollama 原生 `/api/chat`，`format: "json"`、`think: false`、按任务 `num_ctx`（`llm/limits.LOCAL_CTX`）、`keep_alive 30m`；`done_reason == "length"` → `truncated`；只接受回环端点。
- 预设 `ollama`（solo：qwen3:8b 包办）、`ollama-split`（4b 提取 + 8b 综合/语义/代回复）、`ollama-lite`（4b 包办）；`ProviderPreset.native / task_models / profile_label`。
- `state.make_provider(task=)` 按任务选模型；`ByokContext.providers` + `provider_for(task)`，云端行为不变；代回复走 `task="reply"`。
- `rapport models doctor | pull --profile | local-on --profile`（`models_cli.py`）；`GET /models/doctor`、`POST/GET /models/pull`（后台拉取 + 轮询进度）、`POST /models/local-on`。设置页新增「本地模型（零出网）」卡片：硬件检测、三档选择、分工展示、拉取进度条、一键切换；权重目录显示 `OLLAMA_MODELS`。
- 评测脚本 `intent_eval / affinity_stability / synth_compliance` 支持 `--preset ollama|ollama-split|ollama-lite`（免 key，`scripts/_local.py`）。
- 测试 `tests/test_local_models.py`（10 条，MockTransport，不需要真 Ollama）。

## v0.7.0-alpha (2026-09-19) — 词表 v2

**界面**：新配色（暖纸底 + 深墨 + 紫→玫红品牌对，正文对比度全部 ≥4.5:1）、心迹 logo（SVG，头部与首页与 favicon）、导航胶囊、五维各自一色（图表与卡片一致）、卡片阴影与渐变主按钮。

**粘贴到聊天窗口（Windows，可选，默认关）**：`rapport/desktop/paste.py` 用 `uiautomation` 读取当前聚焦控件，是可输入控件且不在心迹自己的窗口里时，经剪贴板 Ctrl+V 粘入草稿。`GET/PATCH /desktop/config`、`GET /desktop/focus`、`POST /desktop/paste`、`GET /desktop/log`。回车需要 `allow_send` 第二开关 + 每次请求显式 `send=true`；关闭主开关会一并关掉 `allow_send`。日志不含文字。代回复页与收件箱 `draft` 提醒上各一个按钮，3 秒倒计时给用户把光标放进输入框。`uv sync --extra desktop`。

**上传上限 10 MiB → 300 MiB**，文件按 4 MiB 分块读取。

**AI 模式取消 300 条上限**
- `max_ai_messages` 300 → 50000（与导入硬上限一致）；新增 `ai_warn_messages = 2000`。预览页返回 `n_calls / est_minutes / large_run`，超过 2000 条时必须勾选确认（`large_run_needs_accept`），价格已知且很小也一样——长材料是数百次调用与数分钟，不只是钱。
- 提取阶段逐窗重试（`rate_limit / timeout / connection / status` 退避 5/20/60 s），一次 429 不再丢掉已完成的窗口；检查点续跑不变。
- 综合阶段本来就只读各窗口的信号摘要（上限 80 条），长材料不会把全文塞进一次请求。校正页与导入警告文案同步。
- v2 真实基线 3 轮终值见 `docs/EVAL.md` §15.8。

依据 v0.6 的真实材料映射（26.7% 对方消息在固定词表里没有类、51% 情绪是「无明显情绪」，`docs/LABEL-V2-PROPOSAL.md`）。

- `schemas/report_v1`：语用 8 → 13（+分享陈述、询问、观点评价、抱怨、同意接受），情绪 7 → 10（+无明显情绪、无奈、惊讶困惑）；`VOCAB_VERSION = "labels-v2"`；`NEUTRAL_PRAGMATIC` 集合。**负向集合不变**，新增类不进门槛。报告体不升版本（新增枚举值向后兼容）。
- `prompts/intent.md`：词表两行 + 每类一句判据（真生气必须指向 A，对第三方的不满是抱怨；无明显情绪是日常消息的正常答案）。提示词哈希随之变化，快照按设计断线。
- `scripts/intent_eval.py`：`G` 加 5 键；120 条合成题金标签按 v2 补标 31 条。**v1 与 v2 的合成题数字不可比。**
- `docs/label_map_v2.json` + `label_map.py` 默认 v2：真实标注重映射后语用无类 4.6%、情绪 1.4%。
- `private_annotate.py`、`docs/ANNOTATION-PROMPT.md`、前端 `Intent.tsx` 词表同步。

**未变**：门槛逻辑、采样一致性、cues 均按 v1 结构继续工作。v2 真实基线跑完记入 `docs/EVAL.md` §15.7。

## v0.6.0-alpha (2026-09-18) — 真实数据进系统

**回访方向表（v0.5 计划阶段 6 的工具）**：`scripts/outcome_direction.py` 读取本机数据库里已回答的回访与快照，按维度输出 Δ 与「同向/反向/不能判」计数，并写明样本门（≥3 段 × 2 次）与切换主分的决策规则；只出表，不拟合。真实数据要等回访积累（≥14 天后才会被问）。

**侧写：先验卡与极性（v0.5 计划阶段 5）**
- `knowledge/prior_check.py`：六个倾向各两极（`TENDENCY_POLES` + 关键词），16 型各 3–6 张先验卡（极性 + 反例提示），`check_priors` 逐卡给出 supported / contradicted / untested 并带倾向证据 id；星座属相不产卡。
- `Tendency.pole`（模型可直接给，否则从措辞推断）；`Report.prior_checks`；`Persona.pole_stability`（最近 3 版极性一致率）。
- `diff_persona` 先比极性：同极换措辞不记修正。`synthesize.md` 倾向加 `pole` 字段。`scripts/prior_cards_lint.py` 0 费校验。前端先验卡下方三态徽标 + 反例提示，倾向卡显示一致率。测试 +7。

**语义：结构线索与采样一致性（v0.5 计划阶段 4A/4B）**
- `features/cues.py`：目标句是否在回答 A 的提问、谁开启对话、A 连续提问/提议次数、A 是否让步道歉、前 6 句笑标记数、距上一条间隔、长度对比、拒绝/改期词命中——全是程序事实，不含任何标签词。`build_intent_input(cues=True)` 注入；产品开关 `PUT /diary/cues/state`（默认关）；`intent_eval --cues`。
- `llm/sampling.py`：`run_intent_sampled(k=3, temperature=0.7)`，多数合并（≥2/3 保留，1 次进候选），`IntentReading.consistency`（各次与合并集的 Jaccard 均值）与 `samples`。**负向门槛第二把钥匙**：k 次全一致的负向判断可不受独立片段门槛限制；单次调用行为不变。费用估算 ×k；`PUT /diary/intent-samples/state?k=`（默认 1）；`intent_eval --samples k`；前端徽标「k 次判断中 n 次一致」。
- 真实材料上的负向门槛诊断与两轮实验见 `docs/EVAL.md` §15。

**攻略存活率（阶段 3 门）**：Gemini-3.8-Flash 三 fixture × 3 轮，选项存活 100%（门 ≥85%），校验器丢掉 1 条操控话术；`~/.rapport/eval/synth_compliance-gemini-2026-09-19.md`。

**攻略：情境 → 选项（v0.5 计划阶段 3）**
- `schemas/report_v1`：`Reaction / Option / Situation / DoneReview`，`Playbook.situations` 与 `done_review`；`actions` 保留兼容。
- `llm/validate._validate_situations`：无真实 `trigger_ids` 丢、`stop_when` 缺丢、`expected_reactions` <2 丢、含操控话术（`MANIPULATION_WORDS`）无条件丢、边界下推进话术丢；全部计入 `dropped`。
- `prompts/synthesize.md`：playbook 改为 situations 1–3 × options 2–3；输入新增「上次行动回顾」；对方拒绝过的同目标不再推荐。
- `knowledge/situations.py`：7 个规则情境模板（明确拒绝、争执未和解、提议未回应、改期未定、答应未落实、对方常提问、对方常主动），只在特征层能指出消息时触发；`stop_contact/safety` 下不产任何推进情境。
- `report/assemble.build_done_review` + runner 把 `ActionLog.actions_since` 送进综合与装配；被拒绝的目标从情境与 actions 里剔除。
- 前端：情境卡 + 选项 tab、预期反应徽标、风险、「我做了，对方：」五选一（写 `ActionLog`）；导出 Markdown 含回顾与情境。
- `synth_compliance.py` 统计 situations/options 存活与丢弃。测试 +11（`tests/test_playbook_situations.py`）。

**评测脚本**：`--enrich N` 每固定标签保底 N 条支持；repeat 模式每轮落盘；报告表头区分真实材料与合成题；供应商错误带原因短句。真实基线与门槛诊断见 `docs/EVAL.md` §15。

**主动提醒：驱动层（阶段 D，用户提出的"激素系统"）**
- `drive/params.py`（参数一处，`drive-v1+<hash>` 版本）、`drive/engine.py`（纯函数：`drive/inhibition/novelty` 三量按半衰期衰减，分泌源可解释，`p = drive×(1−inhibition)×(0.5+novelty)`，按 `(对象, 日期, 小时)` 种子掷骰，可复现、按小时幂等）、`drive/nudges.py`（五种模板，**必须绑定报告证据 id，绑不上就沉默**）、`drive/actuators.py`（`Actuator` 协议；只有 `InboxActuator`，「代发给对方」只留接口）。
- `state/drive.py`：状态存 `settings` KV，`drive_tick(now)` 每小时一次；新表 `nudges`（收件箱），级联进删对象与清空。`JobRunner` 空闲时每 60 秒调一次 tick。
- 硬约束：**默认关**；每天 ≤1（0–3 可调）；静默 23:00–08:00；同类 48 h 冷却；`stop_contact/safety_concern` 钉死抑制；**趋势下降不分泌冲动、只允许「先照顾自己」且不报数字**。忽略 +0.3、👎 +0.4 抑制。
- API：`GET/PATCH /drive/config`、`GET /drive/inbox`、`POST /nudges/{id}/{read|dismissed|acted}`、`PUT /nudges/{id}/verdict`、`GET /relationships/{rid}/drive`（解释三量与"为什么没有"）。
- 前端：顶栏铃铛（30 s 轮询未读）、`pages/Inbox.tsx`（每条可展开"为什么是现在"）、设置页开关/上限/静默时段、历史页三条状态条。
- 文档 `docs/DRIVE.md`；16 项测试（衰减、钉死、下降只 self_care、上限与静默、种子复现、幂等、反馈回灌、无证据不生成、默认关不写、级联、scoring 不引用 drive）。

**标注数据变成尺子（阶段 C1/C2）**
- `docs/label_map_v1.json` + `scripts/label_map.py`：开放词表（3,867 语用 / 94 情绪）→ 固定词表；`none` = 固定词表无此类（不是弃答），`unmapped` = 未裁定。输出 `~/.rapport/eval/gold/cases-v1.json`（22,410 gold + 6,182 weak）与覆盖报告。口径 `docs/LABEL-MAP.md`。
- `intent_eval.py --cases/--stratum/--sample/--seed`：真实用例替代合成题；新增 `极性错误率` 与 `none 误报率`；`none` 不进 macro-F1 与全标签命中；`--out` 拒绝仓库内路径。假供应商与真实端点各跑通一次（`docs/EVAL.md` §15）。
- `docs/LABEL-V2-PROPOSAL.md`：映射结果显示 26.7% 语用 / 64.3% 情绪没有类 → 词表 v2 提案（+5 中性语用、+3 情绪），实施排 v0.7。
- 供应商错误状态现在带网关的原因短句（此前 403 只显示状态码，把"账号耗尽"看成了认证问题）。

**微信单聊导出 JSON 解析器**（`parsers/wx_session_json.py`）
- 识别 `{session, messages}` 结构，sniff 0.99（高于粘贴解析器对单行 JSON 的 0.6）。中文类型标签映射到 `MsgType`；引用消息拆正文与被引内容；**微信自带转写的语音按文本处理**（`meta.from_voice`），未转写的语音仍是占位；图片/视频/表情/文件只存相对路径，内容不读。群聊导出拒绝；未知类型计数不猜。
- 9 条测试；合成样例 `fixtures/synthetic/wechat_session_export.json`。

**大材料**
- `max_messages` 5000 → 50000 为硬拒；超过 `large_import_messages`（5000）只加 `large_import` 警告。AI 模式上限 300 条不变，但 422 提示改为给出"最近 300 条互动消息覆盖哪个日期区间"。

**未读媒体不再是证据**（`scoring/evidence.py`）
- 媒体占位的证据质量 0.5 → 0.0。作者真实材料里 7,712 条未读图片/表情此前把 `N_eff` 顶高、先验 α 压到 0。`evidence_breakdown()` + `prior_breakdown.evidence_counts` 在报告里写明"N 条媒体未读，不计入证据"。

**边界是当前状态，不是永久记录**（`scoring/boundary.py`、`features/compute.py`）
- 只有材料末尾 30 天内的命中才设边界；更早的命中写进边界说明并保留证据 id，不再隐藏总分。真实材料上此前 7/9 段因一年前的一句话没有总分。
- 安全词表排除「想死你了」「累得想死」等口语。

**私有导入冒烟** `scripts/private_import_check.py`：脱敏导出 → parse → 规则报告，只写计数与分数到 `~/.rapport/eval/`，不写聊天内容。9 段全部导入成功。

**测试**：331 → 346。

## v0.5.0-alpha (2026-09-16) — 尺子、好感度结构、三模型基线

目标从"框架完整"转为四个核心"好用"（计划：`docs/PLAN-v0.5.md`）。本版只做 0 费的结构与评测基建，并跑出第一批带区间的基线。

**阶段 0 尺子与基建**
- 版本派生：`RUBRIC_VERSION` / `FEATURES_VERSION` 改为内容哈希（`rubric_digest()` / `lexicon_digest()`），改锚点或词表快照自动断线（`tests/test_snapshots.py` 锁定）。
- 私有评测目录守卫 `scripts/_private.py`：真实材料的评测输出拒绝仓库内路径，且在任何模型调用之前拒绝。
- 四个评测脚本骨架 `affinity_stability.py` / `model_compare.py` / `synth_compliance.py` / `private_rating_sheet.py`，共享 `_probe.py` 进度探针（flush + `--progress` + 结尾 DONE）。
- `intent_eval.py` 单次与多轮统一走 `_run_condition`，多轮也产逐条表；导出 candidates / tag_basis / counter_evidence_ids / evidence_strength；`--fake` 离线 dry-run。
- `ActionLog`（"我做了 + 对方反应"）与 `Outcome`（两周后回访）表、`state/actions.py`、`api/actions.py`；前端历史页回访卡。
- `adjudicate.py` 的 kappa 改为逐标签二分类 Cohen's κ；`synthesize.md` stage 枚举补 `stable`。

**阶段 1 好感度结构**
- 词表拆分：`FUTURE_RE` 拆为 `PROPOSE_RE` / `ACCEPT_RE` / `COMMIT_RE`，`ALT_TIME_RE`（"改天吧"）不再计入未来指向正向。
- 新建 `features/invitations.py`：提议 → 应答（答应/改期/拒绝/绕开/未答）→ 落定 的事件链；future 维改读链（提议 0.35 / 接受 0.30 / 改期反向 0.15 / 落定 0.20），无链时退回词表兜底；`Affinity.future_chains` 进合同，前端 future 卡片列链。
- 趋势下钻：`TrendPoint` 带 `evidence_ids / dim_evidence / dim_inputs / llm_override`，前端点趋势点开抽屉。
- 阶段动态权重影子分：`Affinity.alt_totals{static, stage_weighted}`，主分不变。
- 三个真实缺陷：合成 fixture 原为随机抽句且无种子（重写为有场景逻辑的对话 + 固定 seed，旧报告数字不可比）；`explicit_decline`（"不用了"）曾让总分消失，现只有 `stop_contact` / `safety_concern` 抑制；零提议曾被当 0 分，现标为证据缺失。

**阶段 2 三模型基线（`~/.rapport/eval/p2-*.md`）**
- 好感度 byok 三模型 × 三 fixture × 3 轮：total 极差全部 ≤4（warm 69–75、cooling 34–40、stop 一致不出分）。
- 语义 B 批 60 × 3 轮 macro-F1：DeepSeek V4.1 Flash 0.345 [0.324, 0.380]、glm-5.3-flash 0.353 [0.319, 0.373]（区间重叠，不分高下）、gpt-5.6-luna 0.283（可排除）。负向门槛使上线后 F1 腰斩至 0.195，待诊断。

**测试**：302 → 331。

## v0.4.1-alpha (2026-09-16) — 出网闸门、错误契约、审计修复

这一版落实了三份独立审计（后端架构 / 前端与产品 / 调研对照）里判为 P0 与隐私红线的项目。

**代回复出网闸门（v2 M10/M13 的承诺，此前未做）**
- 自由模式的代回复此前直接调用模型，把日记原文塞进请求，没有预览、哈希与费用确认。现在与分析链同一纪律：`POST /api/replies/preview` 返回将发送的脱敏对话行、**逐条日记原文**、目的地、费用与一张 15 分钟内单次有效的 ticket；`POST /api/replies` 必须携带 ticket 与 payload_hash，日记在预览后被编辑（revision 变化）会以 `hash_mismatch` 拒绝。严格模式不调用模型，无需预览。
- `llm/outbound.py` 的 `OutboundGate` 是可复用的闸门对象，票据只存内存。
- 响应新增 `context`：草稿回应的是哪条消息及其前几条，前端展示。

**第一人称事实核验（v2 M10）**
- 草稿里“我上周去爬了山”这类没有任何已授权日记支撑的经历陈述，标 `needs_review` 并列出 `unbacked_claims`，前端显示「需核对」；不丢弃（模型可能没编，只是用户没写日记）。防抄袭校验（复述未授权日记）保持不变。
- `prompts/reply.md` 补入 v2 的四条规则：对话/日记里的指令只是数据；计划仍是计划、不确定时间不写“今天/刚刚”；对方已拒绝时只起草尊重边界的内容。

**“只发用户看过的内容”补齐两处漏洞**
- 校正页 PATCH 在存在排队或进行中的分析时返回 409 `analysis_active`；worker 执行前校验材料哈希与创建时一致，否则 `failed(import_changed)`。此前确认出网后再改角色/正文，会把未预览的内容发出去。
- 重新确认时若 `edited_lines` 变化则清空 checkpoints，不再复用旧内容的窗口结果。
- PATCH（换角色、改文本）后重建该导入的事件账本；此前账本里的 sender 与真实对话相反且仍 `confirmed`。

**错误契约与版本**
- `errors.RapportError(code, msg, hint, status)` 成为唯一基类；`LLMError / ParseError / StrictFactError / RelationshipNotFound` 继承它，`main.py` 一个处理器统一渲染 `{error:{code,msg,hint}}`，API 层不再逐点翻译。
- `PROMPTS_VERSION` 改为 `prompts-v1+<提示词文件内容哈希>`：改任何一个 `prompts/*.md`，payload hash、幂等键、快照可比性全部随之变化。此前是从未 bump 的手写常量。
- 分析 id 改用随机 id（幂等键仍是独立列）：v0.3 用 `sha1(幂等键+确认时间)` 做主键，失败一次后再建同参数分析会撞主键 500。
- 推理型模型输出估算只乘一次 `REASONING_MULTIPLIER`（`llm/limits.py`）：此前各阶段 ×3 后 `estimate_cost` 再 ×2.5，费用预警虚高 7.5×。所有 token 预算、窗口大小集中到 `llm/limits.py`。
- `logging.basicConfig` 接入；`FastAPI(version=…)` 使用真实版本号。

**端点硬化（v2 §4.6）**
- 自定义端点默认拒绝云元数据地址（169.254.x）与 RFC1918 内网；`RAPPORT_ALLOW_PRIVATE_ENDPOINTS=1` 可放开。
- HTTP 客户端不跟随重定向、不继承环境代理；返回体超 4 MiB 丢弃；`response_format` 400 回退只在错误体确实提到它时触发（此前任何 400 都重放一次，计费两次）。

**前端 P0（四个）与可解释性**
- 趋势图 `ScatterChart` 未注册导致“样本不足”空心点从不渲染——已注册；参考线文案改为“中性参考线（不是及格线）”。
- 阶段枚举缺 `stable` 且文案与后端漂移——对齐为 陌生/初识/暧昧/确定关系/稳定，并加回退。
- 日记页“严格模式话术”按错误响应形状读取导致白屏——修正；新增日记编辑与批准短句输入、全局记忆开关，严格模式在 UI 上首次可用。
- 设置页此前没有本地/云连接模式开关，BYOK 主路径无法从界面走通——补上，并显示当前链路。
- 代回复页：预览→确认两步；展示“回应的是哪条消息”；`import_id` 默认取当前流程；「已核对 / 需核对」徽章。
- 逐句语义卡显示每个标签各自的证据（`tag_basis`）与「事后解释（含后文）」标记（`IntentReading.post_hoc`，v2 M03 要求）。
- `ErrorBoundary` + `*` 路由：渲染异常变成可读卡片而不是白屏；报告切换时清空旧状态；导出检查 `res.ok`；错误 Alert 用 `role="alert"`。

**测试**：284 → 302（出网闸门 6、第一人称核验 2、端点硬化 3、材料守卫 4、错误契约与版本 3）。

## v0.4.0-alpha (2026-09-16) — 结构：作用域、账本、反馈闭环

这一版不加新的“分析能力”，而是把 v0.3 的框架补成能长期演进的形状。四个结构性变化：

**关系档案（Relationship）**
- 新表 `relationships`；`imports / analyses / reports / events / snapshots / persona_versions` 全部带 `relationship_id`，默认 `default`（首次启动自动创建）。此前所有历史查询都是全库范围，分析第二个人会把两个人的证据、曲线、阶段与画像揉在一起——这是 v0.3 最大的结构缺口。
- `N_eff`、趋势、阶段历史、画像版本、上一份报告、代回复的“最近一次导入”与雷区提示，全部按对象读取。
- 非默认对象的事件指纹掺入对象 id，两个人各一句无时间戳的“早”不再合并；默认对象指纹与 v0.3 一致。
- 接口：`GET/POST/PATCH/DELETE /api/relationships`，`GET /api/relationships/{id}/overview`；导入、OCR、代回复接受 `relationship_id`；`/trend`、`/stage/history`、`/persona/history`、`/reports` 支持按对象过滤。删除对象级联删除其全部派生行与加密 blob，并把它从日记授权范围摘除；默认对象只能清空。
- 前端：导入 / 日记 / 代回复页有对象选择器（可就地新建）；「历史」页按对象展示并可维护名称、备注与长期标签；「标签与目标」页自动带入对象的长期标签，文案改为“起始假设、随证据衰减、不进分数”。

**模型调用账本（`llm_calls`）**
- `state.make_provider()` 现在返回 `AuditedProvider`：每次调用记录用途（从 `TASK:` 行识别）、preset/model、token、`max_tokens`、耗时、状态（ok / truncated / bad_json / error）、请求哈希；**不记录提示词或回复**。账本写入失败不影响分析。
- `GET /api/analyses/{id}/calls`、`GET /api/stats?days=30`；报告页可展开本次分析的调用明细；设置页显示 30 天汇总，截断率 >5% 提示换模型。评测里 4/60 的截断缺陷因此在产品中可观测。

**反馈闭环（`feedback`）**
- 报告里的好感度总分、观察、特质、阶段、边界、行动、雷区、逐句语义都可点 👍/👎/？；同一主张再点一次即替换，删报告级联删反馈。反馈不改动报告。
- 逐句语义的 👎 可附修正标签；`GET /api/feedback/export` 导出为评测用例（含聊天片段，仅本机；`adjudicated: false`，进 `docs/EVAL.md` 前须人工裁定）。反馈备注不进导出与诊断。

**工程**
- `state.py`（673 行）拆为 `state/` 包的七个 mixin；`jobs/status.py` 成为分析状态机唯一定义，`_set` 校验非法迁移。修掉一个真实缺陷：`intent` 阶段不在旧的 `ACTIVE` 元组里，服务重启时该阶段的任务会卡在 `intent` 而不是 `interrupted`。
- `db/migrate.ensure_schema`：每次启动只加表、只加列（带默认值），v0.3 数据库无需手工迁移；Alembic `0002` 复用同一函数。设置页显示本次启动补齐了哪些字段。
- `schemas/migrate.upgrade_report_body`：报告 body 读取时按版本升级，未知版本 409 而非半解析。
- `scoring/evidence.event_quality`：证据质量随时间精度与消息类型取值（1.0 / 0.9 / 0.8 / 0.5），`Event` 表新增 `ts_precision`。
- 版本号统一到 `config.APP_VERSION`（此前 `__init__` 还写着 0.1.0）；前端进度页补上 `intent` 阶段。
- 新增 `docs/ARCHITECTURE.md`。测试 242 → 284（关系隔离 8、账本与反馈 12、状态机/迁移/证据质量 22）。

## v0.3.0-alpha (2026-09-15) — Phase 2 / 3 / 4 与前端补齐

**Phase 2 · 标签先验、快照与画像版本（M04/M05）**
- 标签先验引擎：`α(N)=0.70·e^(−N/40)` 为标签总预算，MBTI/星座/属相按 0.40/0.20/0.10 衰减；缺标签的份额归「未知」而不放大其他标签。
- `N_eff` 只统计**已确认**事件：确认一次导入才会增长；重复导入同一聊天不增加（FR-002）。
- 收缩分 `S_prov = ρ·50 + (1−ρ)·S_obs`，ρ 只由证据量决定；主界面仍只显示观测分。
- 真实快照：同材料同版本只出一点，规则/模型变更标记为不可比并在界面说明（FR-010）；`GET /api/trend`。
- 画像版本：每次 AI 报告产生一个可查版本，旧版本保留内容并标记失效，变更带原因与证据；`GET /api/persona/history`。

**Phase 3 · 日记记忆与代回复（M09/M10）**
- 日记：`revision`、类型（事实/计划/感受）、启用开关、关系授权范围、已批准短句；时间戳必须带时区，存储统一 UTC。
- 检索后回查授权：已删除/已关闭/旧版本/越权/来自未来的条目都会被丢弃并给出原因；空授权范围什么都检索不到；**关闭开关时检索器根本不被调用**。
- 严格模式：只用你逐字批准过的日记短句 + 固定模板；改写一律拒绝。
- 代回复：2–3 条多风格草稿，每条标注所用日记的 id 与版本、风险提示与校验说明；**防编造校验**会丢弃任何复述未授权日记内容的草稿；本工具不代发任何消息。
- 界面新增：日记页、代回复页、历史页（阶段变化 / 画像版本 / 可比快照）。

**Phase 4 · 阶段状态机与攻略演进（M08）**
- 阶段阶梯改为需求里的说法：陌生 → 初识 → 暧昧 → 确定关系 → 稳定；**不允许跳级**（跳级会被降为下一级并说明）。
- 「稳定」需要：关系已确认 + ≥8 段会话 + ≥21 天跨度 + 无边界信号。
- **回退被允许且被明确记录**，不隐藏；`GET /api/stage/history`。
- 攻略变更说明：阶段/边界/画像版本/主行动/新增雷区的变化都会写成一句可读的原因。

**评测（`docs/EVAL.md` §8）**
- 两次完整运行都保留：基线 `EVAL-RESULTS-2026-09-15-baseline.md` 与改动后 `EVAL-RESULTS.md`。
- 结论：要求"每个标签单独给证据"改善了多数弱标签精确率与无上下文时的弃答，但**没有**解决准确率（context macro-F1 0.535→0.547，仍低于 0.70 门槛），并且**损害了无参考例的情况**（0.439→0.409）。

# Changelog

## v0.2.0-alpha (2026-09-15) — Phase 0 + Phase 1

**Phase 0 · 基建对齐**
- 好感度显示门槛：覆盖度 0.70 → **0.60**，并新增「≥2 个独立上下文片段」门（有时间戳按会话段计，无时间戳按消息顺序两半各自需有足够对方文本）。
- 新增证据账本 `scoring/evidence.py`（移植自 v2 调研参考实现）：有效证据量 `N_eff = Σ_episode min(5, Σq_i)`；**AI 草稿与公共参考例子不计入**；同一 canonical 事件不得静默换 episode。
- 新增 canonical 事件表 `events` 与内容指纹：重复导入同一聊天不再产生新证据行；同一分钟内两条真实相同的消息仍保持独立。
- 新增 `ConnectionMode(local|cloud)`，默认 **local**：本地模式下配置云端点直接被拒（`mode_violation`），不存在静默回退；设置页可显式切换，切换后云调用仍走原有的脱敏预览 + 内容哈希确认。

**Phase 1 · 语义情绪模块与评测集**
- 新增 `IntentReading` 合同：表层 / 语用多标签 / 情绪多标签 / 边界 / 候选解释 / 证据强度 / 模型自评 / 证据与反证 id / `ambiguous`。**禁止强制互斥三选一**。
- 新增 episode 构造（`features/episodes.py`）：按会话段 + 引用 + 长静默切分；`reply` 模式只看到目标之前的内容，`review` 模式含后文并标记 `post_hoc`。
- 新增上下文预算规划（`llm/budget.py`）：16K 分档，超预算按「参考例 → 摘要 → 相关历史」削减，**task / episode / 输出预算永不裁剪**（边界与拒绝证据在 episode 内）。
- 新增 M03 意图模块（`llm/intent.py` + `prompts/intent.md`）与校验：非法证据 id 剔除、无证据降级为 `ambiguous`、候选解释上限 3 条。
- **负向门槛**：`真生气 / 反讽 / 委婉拒绝 / 不满 / 伤心` 类判断在独立片段少于 2 个时**降级为候选解释**，不上浮为结论（实测该模型系统性高估冷淡）。
- 新增参考例库（`knowledge/reference.py` + 42 组原创种子，覆盖全部语用标签）：仅加载 `license_review_status = approved_for_local_reference` 的条目，`eval:` 前缀条目一律拒绝；检索为字符 bigram 词法检索（非语义检索）。
- 新增评测集与工具：`scripts/intent_eval.py`（60 组 = 15 表层表达 × 4 上下文，三条件对照），指标含 macro-F1（原始 / 上线后）、分标签 P/R/F1、弃答率、门槛触发率、高置信度误判率。规范见 `docs/EVAL.md`，结果见 `docs/EVAL-RESULTS.md`。
- 报告新增「语义与情绪（逐句）」区块；前端负面判断**默认折叠**，展开时提示需更多证据。
- **出网预览费用修正**：语义判断最多会多发 8 次调用，预览现在计入这些调用（返回 `n_intent_calls`），费用确认不再低估。
- **编辑一致性修正**：你在预览页编辑并确认的脱敏正文，现在同样用于语义阶段；确认保存的哈希按**编辑后**的内容计算，保证「确认的内容 = 实际发送的内容」。

## v0.1.0-alpha (2026-09-14)

首个可运行版本。

- 导入：A/B 粘贴、WhatsApp TXT（iOS/Android，日月歧义需选择）、QQ 消息管理器 TXT、微信第三方导出 CSV/JSON、Telegram 单聊 JSON、message-v1 JSON、聊天截图（可选本地 RapidOCR）。
- 校正页：逐条改角色 / 文本 / 类型、一键互换、区间选择、未知发送者阻断确认、改动使旧确认失效。
- 规则引擎：会话切分、双口径回复时间、发起 / 结束、提问 / 关心 / 计划 / 亲昵词表、双发率、深夜率；`affinity-rubric-v0.1` 五维评分与半分进位；按天 / 周 / 月分桶的趋势与动量；边界（拒绝 / 停止联系 / 安全）与阶段判定。
- BYOK：OpenAI 兼容 Provider（DeepSeek 默认，Qwen / Moonshot / OpenAI / Ollama / 自定义），脱敏预览、估价、内容哈希确认、分窗提取 → 综合 → 本地校验（证据 id、stop_when、边界一致性）→ 一次修复重试；失败回退规则报告。
- 报告：总分 / 五维 / 趋势图、观察与反证、沟通侧写与四轴倾向、MBTI 事后对照、阶段与边界、行动 / 雷区 / 话术、星座属相彩蛋（权重 0）；Markdown / JSON 导出默认不含原文。
- 安全：回环监听、配对码、会话 Cookie、CSRF、Host/Origin 白名单、加密 blob 与 secrets、24h 原始材料清理、诊断信息不含敏感内容。
- 工程：67 个离线后端测试、前端 tsc/vitest/构建、GitHub Actions、Dockerfile + compose（仅回环映射）。

### 已用真实模型验证（2026-09-14）

在小米 MiMo `token-plan` 端点（`https://token-plan-cn.xiaomimimo.com/v1`，模型 `mimo-v2.5-pro`）上跑通了真实 BYOK 链路：

- 报告：五维（规则 / AI / 最终）、趋势、阶段、边界、观察（带证据 id）、特质、行动（含 stop_when）全部生成且通过本地校验。
- 边界材料（含“别再联系我了”）：总分抑制、阶段 unknown、只输出尊重边界类行动，模型正确引用边界证据 id。
- 改 MBTI 标签：核心分数、阶段、行动不变，只更新对照区。
- 导出 Markdown / JSON 不含 Key 与原始正文；删除报告后接口 404；清空数据可用。
- 脱敏预览不含 MBTI、昵称与手机号；`mimo` 未公开单价 → 估价显示“未知”，且必须显式勾选接受费用才能继续。

该端点是**推理型**模型（返回 `reasoning_content`），思考 token 占用输出预算。为此新增：截断识别（`finish_reason == "length"` → `LLMError("truncated")`）后自动以双倍预算重试一次；`ProviderPreset.reasoning` 标记，估算输出 token 时乘 2.5；端点拒绝 `response_format` 时自动降级重试。

已知限制：评分锚点未经过盲测验证；OCR 只支持标准左右气泡；WhatsApp 格式白名单以合成 fixture 为准；Alembic 迁移在 v0.1 仅做初始建表。
