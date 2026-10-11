# 心迹 Rapport · 再规划 v0.5：把四个核心做到"好用"

> **状态（2026-09-19）**：阶段 0–5 已全部落地（提交见 CHANGELOG v0.5/v0.6/v0.7）；阶段 6 的工具 `scripts/outcome_direction.py` 就绪，等回访数据积累。各阶段实测结果在 `docs/EVAL.md` §15。

> 定稿 2026-09-16。路径相对 `backend/rapport/`，脚本相对仓库根 `scripts/`。优先级按用户决定：好感度 → 攻略 → 语义 → 侧写。评测预算不设限，但每次花钱运行前先 `--limit 1` 与 `--limit 1 --repeat 2` 两条路径 dry-run（EVAL §13）。用户材料、反馈导出、回访标签永不进仓库（写 `~/.rapport/eval/`，脚本层守卫）。

## Context

v0.4.1 框架已完整，但四个核心的效果证据分别是：语义留出集 macro-F1 0.345（门槛 0.70）；好感度权重与锚点零校准、总分稳定性未测；侧写无校准且"先验被聊天推翻"不可见；攻略无用户实验。用户要求核心不只是能用，要好用。"好用"的验收口径：观察可追溯到原句且用户自己看不出来；建议具体到能照做且知道何时停；判断错了能被看出并被纠正；数字同材料稳定、跨材料方向与用户事后感受一致。

## 已核实的缺陷（直接进阶段 0）

- `features/lexicon.py` `FUTURE_RE` 与 `ALT_TIME_RE` 词面重叠："改天吧"既算未来指向正向又算带替代时间的拒绝。
- `prompts/extract.md` 的 invitation/accept/decline/plan 信号从不进入好感度（`assemble()` 无 signals 参数）。
- `prompts/synthesize.md` stage 枚举缺 `stable`；`action_templates.fallback_playbook` invite 分支未把 stable 归入 warm。
- `scripts/adjudicate.py compare` 的 kappa 把"完全一致"序列与"有重叠"序列当两个评分者，统计实现错误；两份裁定 CSV 0 行已填。
- `scripts/intent_eval.py` 单次与 repeat 两条路径重复实现，repeat 不产逐条表；`evaluate_case` 不导出 candidates/tag_basis/counter_evidence。
- `RUBRIC_VERSION`/`FEATURES_VERSION` 手写常量：改锚点或词表不 bump 会静默连线。
- `report/persona_versions.diff_persona` 按 label 文本比较，模型换措辞就记"修正"。
- `knowledge/priors.MBTI_PRIORS` 是纯字符串，无法与六项倾向对照。
- intent 单次采样 temperature 0.2，置信度只靠模型自评。

## 1. 好感度（优先级 1）

验收：主分/五维/每个趋势点可点开看证据与输入；规则层同材料两次逐字节相同；随机去掉 10% 会话段重算 total 变化 ≤5；含"改天吧"的 cooling fixture future 规则分 ≤50、有邀约被接受的 warm fixture ≥65；同材料 `run_byok` 3 次 total 极差 ≤5（花钱）；用户对自己 ≥3 段关系 × 2 时间窗五维打"偏高/合适/偏低"，合适 ≥70%、反向 ≤1/5。

改动：
- A 词表拆分 + 邀约事件链：`FUTURE_RE` 拆为 `PROPOSE_RE`/`COMMIT_RE`，`ALT_TIME_RE` 不再计正向；新建 `features/invitations.py`（`InvitationChain`：propose → response(accept|alt_time|decline|deflect|none) → commit，来源 rule ∪ LLM signal）；`rubric.rule_dims` future 维改为 提议频率 0.35 / 接受率 0.30 / 改期率反向 0.15 / 兑现率 0.20，旧词频只在无链时兜底；`assemble()`/`build_affinity()` 加 `signals` 参数；`Affinity.future_chains` 合同字段（带默认，不升 v2）；前端 future 卡片列链。
- B 趋势下钻：`TrendPoint` 加 `evidence_ids / dim_evidence / dim_inputs / llm_override`；`trend.build_trend` 保存每维证据与输入；`pipeline._map_overrides_to_buckets` 同时返回每桶 signal id；前端趋势点可点，抽屉显示五维 rule/LLM 与证据 chip。
- C 阶段动态权重作影子分：`rubric.STAGE_BETA` + `RELIABILITY`，`w_j = WEIGHTS·β_stage·coverage_j·reliability_j`；`Affinity.alt_totals{static, stage_weighted}` 与 `weights_used`；主界面仍显示静态，等阶段 6 回访方向表再决定。
- D 版本派生：`rubric.rubric_digest()`、`lexicon.lexicon_digest()`，`RUBRIC_VERSION`/`FEATURES_VERSION` 改为 `xxx-v1+<hash>`（做法同 `llm/prompts.prompts_version`）；改锚点或词表快照自动断线。

验证：`tests/test_features.py` 词表回归；新 `tests/test_invitations.py`；`tests/test_scoring.py` cooling/warm 方向；新 `scripts/affinity_stability.py --mode rules --resample 20`（0 费）与 `--mode byok --repeat 3 --models …`（≈324 次）；`tests/test_snapshots.py` 改锚点后 comparable=False。

退出：三模型 total 极差都 >5 → `fuse` 改 0.7/0.3 再测，仍不过则 LLM 维不进 total；影子分与静态分中位差 >8 → 动态权重只留代码不展示；重采样极差 >5 → 先收紧 `MIN_SESSIONS/COVERAGE_MIN`。

## 2. 攻略（优先级 2）

验收：每个选项有 `trigger_ids`（校验保证）与 ≥2 条 `expected_reactions` + `stop_when`；规则模式每目标 ≥2 情境 × ≥2 选项；用户标"我做了 + 对方反应"后，下次报告 `change_notes` 引用且不重复推荐被拒绝的同目标推进选项；三模型选项校验存活率 ≥85%；用户对选项打"会照做/不会/看不懂"，前两者 ≥80%（n≥15）。

改动：`schemas/report_v1` 新增 `Reaction / Option(id=sha1(goal+action)[:10], trigger_ids, expected_reactions, stop_when, risk) / Situation`，`Playbook.situations` 与 `done_review`，`actions` 由每情境首选项派生保持兼容；`prompts/synthesize.md` playbook 段改为 situations 1–3 × options 2–3，新增"上次行动回顾"输入段；`llm/validate._validate_situations`（无 trigger 丢弃、reactions<2 丢弃、边界下 `ADVANCE_WORDS` + 新 `MANIPULATION_WORDS`）；`knowledge/action_templates` 改为 `SITUATION_TEMPLATES: list[(predicate(Features)->trigger_ids, Situation)]` ≥6 个情境；新表 `ActionLog` + `state/actions.py` mixin + `POST /reports/{id}/actions/{option_id}/done` + `GET /relationships/{rid}/actions`，级联进删除与 wipe；`playbook_change.diff_playbook` 加入执行回顾；前端情境→选项手风琴 + "我做了"五选一。

验证：新 `tests/test_playbook_situations.py`；`llm/fake._synthesize` 输出 situations；新 `scripts/synth_compliance.py`（9 次综合调用统计 dropped 与存活率）。退出：存活率 <60% → `trigger_ids` 改为程序按 basis_ids 反查。

## 3. 语义情绪（优先级 3）

验收（诚实口径，"少说错、说了能核"）：B 批 60 条 context `--repeat 3` macro-F1 均值 ≥0.45 且区间下沿高于基线上沿；高置信误判 ≤1/60；极性错误（负向判成正向或反之）≤5%；真实材料非弃答判断 👍 ≥70%、极性反了的 👎 ≤5%（n≥40）；双人裁定 kappa ≥0.6 才允许把数字写进 LIMITATIONS 作能力描述。

改动：
- A 零费结构线索：新建 `features/cues.py`（目标是否回答了问题、谁开的话题、前 6 回合 A 重复请求次数、A 是否让步/道歉、前文笑标记数、目标前时间差、目标长度/中位、拒绝与改期词命中），`build_intent_input(..., cues=True)` 注入"结构线索（程序算的事实）"段，`ByokContext.use_cues`，`intent_eval --cues`。只写事实不写标签暗示。LLM 抽线索的两阶段仅在 A 有效且错误仍集中在结构时做。
- B 采样一致性替代自评：`llm/sampling.sample_json`；`intent.run_intent_sampled(k=3, temperature=0.7)` 合并规则 出现 ≥2/3 保留、1 次进候选；`IntentReading.consistency / samples`；负向门槛在 consistency<1 且片段<2 时降级；前端显示"3 次判断中 N 次一致"；`estimate_intent_tokens` 乘 samples。
- C 私有真实材料评测：`intent_eval --cases <feedback 导出 json>`，disagree+修正标签作金、agree 作弱金；`--out` 在仓库内则拒绝运行；`feedback_export_eval` 补 `episode_lines`。
- D 双人裁定：修 kappa（逐标签二分类 kappa 或 Krippendorff α）；找不到第二人则作者间隔 ≥2 周盲标作 reviewer_B，EVAL 写明是自我复测。

验证：新 `scripts/model_compare.py`（复用 intent_eval 的 `_run_condition/score/render`，`--models` 多模型）三模型 B 批基线 ≈540 次；`--cues` 一轮；`--samples 3` 两轮；分别决策不合并声称。退出：cues 与 sampling 都 <0.05 → 标题标签降为候选解释，不再投提示词工作，成本回投好感度与攻略。

## 4. 性格侧写（优先级 4）

验收：每条先验卡逐项显示 supported/contradicted/untested 带证据；16 型各 ≥3 卡覆盖 ≥3 个倾向；换措辞不记变化；每倾向显示最近 3 版极性一致率；用户对 contradicted 判定"合理" ≥80%（n≥10）。

改动：`report_v1.TENDENCY_POLES` 极性词表，`Tendency.pole`，`PriorCard{tendency_key, lean, counterexample_prompt, text}`，`PriorCheck{status, evidence_ids}`，`Persona.pole_stability`；`knowledge/priors.MBTI_PRIORS` 改为每型 3–4 张原创卡（原文保留为 text，星座属相不产 checks）；新建 `knowledge/prior_check.check_priors(cards, persona)`；`synthesize.md` tendencies 加 `pole`，`validate` 校验取值；`diff_persona` 先比 pole；`save_persona_version` 存 pole 并算 `pole_stability`；前端三态徽标。

验证：新 `tests/test_prior_check.py`；`test_persona_versions` 加"换措辞不记变化"；新 `scripts/prior_cards_lint.py`；极性一致率从 `affinity_stability --mode byok` 同批数据顺带统计。退出：三模型 pole 一致率普遍 <0.5 → 只显示 untested。

## 5. 公共基建

1. 私有评测目录 `Settings.eval_dir = data_dir/"eval"`；`scripts/_private.py ensure_private_out()` 拒绝仓库内路径。
2. 结果回访：新表 `Outcome{relationship_id, report_id, asked_at, answered_at, outcome: better|same|worse|ended|unknown, met_since, note}`，`state/outcomes.py`，`POST /relationships/{rid}/outcomes`，历史页在最近报告 ≥14 天后回问一行；测试固定 scoring 无任何读取 outcome 的路径；`scripts/outcome_direction.py` 只输出各维 delta 与结果同向率的方向表，由人决定 `STAGE_BETA`，不拟合。
3. `scripts/model_compare.py`、`scripts/affinity_stability.py`、`scripts/synth_compliance.py`、`scripts/private_rating_sheet.py` 全部遵守：flush + `--progress` + 结尾 DONE；失败行保留；报告写切片与实际条数。
4. `intent_eval.py` 单次路径改为调用 `_run_condition`，repeat 模式也产逐条表；`evaluate_case` 导出 candidates/tag_basis/counter_evidence/evidence_strength。

## 6. 排期与门

| 阶段 | 内容 | 类型 | 验收门 | 退出 |
|---|---|---|---|---|
| 0 尺子与基建（~2 天） | 版本派生；私有目录守卫；四个脚本骨架（假供应商跑通）；Outcome/ActionLog 表与 API；修 kappa、stable 枚举、eval 路径统一 | 直接做，0 费 | pytest 全绿；脚本 `--limit 1` 假供应商通过 | 无 |
| 1 好感度结构（~3 天） | 词表拆分、邀约链、future 新公式、signals 进 assemble、趋势下钻、动态权重影子 | 直接做，0 费 | cooling ≤50 / warm ≥65；重采样 ≤5；确定性 | 重采样 >5 → 先收门槛 |
| 2 三模型基线（~1 天机器） | affinity_stability byok ×3 模型 ×3 fixture；model_compare B 批 ×3；synth_compliance 基线；用户私有材料一次 + 五维评分表 | 先测，花钱 | 拿到每模型 total 极差、macro-F1 区间、截断率，选主力模型 | 三模型 total 极差都 >10 → LLM 维退为参考 |
| 3 攻略（~3 天） | situations 合同/prompt/校验/模板/前端/"我做了"/done_review | 直接做 + 9 次 | 纯层 100%；存活率 ≥85% | <60% → trigger 程序反查 |
| 4 语义两轮（~2 天） | cues → sampling → 组合，B 批 repeat 3；私有用例 ≥40 后跑 `--cases`；并行裁定或间隔盲标 | 先测，花钱 | §3 三条同时满足才改默认 | 都 <0.05 → 标签降候选 |
| 5 侧写对照（~2 天） | 极性词表、48 卡、prior_check��按极性 diff、pole_stability、前端三态 | 直接做，复用阶段 2 数据 | lint 全过；换措辞不记变化；一致率报告 | 一致率 <0.5 → 只显示 untested |
| 6 回访方向（≥2–4 周后） | 用户回答回访；方向表；人工决定 STAGE_BETA 与是否切主分 | 等待 | ≥3 段关系 × 2 次回访；≥3 维同向才切主分 | 样本不足 → 静态保持主分 |

## 验证总则

后端 `cd backend && uv run ruff check rapport tests && uv run pytest -q`；前端 `npx tsc --noEmit && npx vitest run && npm run build`；每次花钱运行先两条路径 dry-run，`--repeat 3` 报区间，区间重叠不得声称改进；所有真实材料结果写 `~/.rapport/eval/`，不进 git。
