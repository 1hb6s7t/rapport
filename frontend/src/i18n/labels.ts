/** Display labels for enums. Chinese first; `en` is a scaffold for future i18n. */

export const DIM_LABELS: Record<string, string> = {
  responsiveness: "回应性",
  initiative: "主动性",
  investment: "投入度",
  warmth: "情感温度",
  future: "未来指向",
};
export const DIM_ORDER = ["responsiveness", "initiative", "investment", "warmth", "future"];
export const DIM_HINT: Record<string, string> = {
  responsiveness: "对方回复的速度与是否认真",
  initiative: "对方主动开启/推进话题的程度",
  investment: "对方投入的篮幅与自我表露",
  warmth: "关心、幽默、亲昵的强度",
  future: "对方提及见面、计划、未来的意愿",
};
// Mirrors backend schemas/report_v1.STAGE_LABELS — keep in sync (v0.4.1: added `stable`, aligned wording).
export const STAGE_LABELS: Record<string, string> = {
  unknown: "无法判断",
  initial_contact: "陌生",
  getting_to_know: "初识",
  affectionate_signals: "暧昧",
  mutually_confirmed: "确定关系",
  stable: "稳定",
  repair: "修复期",
};
export const BOUNDARY_LABELS: Record<string, string> = {
  none: "未发现明确边界信号",
  explicit_decline: "对方明确拒绝过某项提议",
  stop_contact: "对方表达了不想继续联系",
  safety_concern: "出现安全相关信号",
};
export const TENDENCY_LABELS: Record<string, string> = {
  directness: "表达直接性",
  emotional_expression: "情绪表达",
  detail_preference: "细节偏好",
  need_expression: "需求表达",
  disagreement_handling: "分歧处理",
  rhythm: "互动节奏",
};
export const AXIS_LABELS: Record<string, string> = {
  EI: "外向 E / 内向 I",
  SN: "具体 S / 抽象 N",
  TF: "逻辑 T / 情感 F",
  JP: "计划 J / 随性 P",
};
export const MODE_LABELS: Record<string, string> = { demo: "演示（合成数据）", rules: "规则模式", byok: "AI 分析" };
export const MOMENTUM_LABELS: Record<string, string> = { rising: "上升", flat: "平稳", falling: "下降", unknown: "样本不足" };
export const GOAL_LABELS: Record<string, string> = {
  understand: "理解对方当前的互动方式",
  invite: "判断是否 / 如何提出邀约",
  clarify: "澄清一个误会或疑问",
  repair: "复盘一次冲突并修复",
  decide: "决定是否继续投入",
};
export const CONF_LABELS: Record<string, string> = { low: "低", medium: "中", high: "高" };
export const SCRIPT_LABELS: Record<string, string> = { casual: "轻松版", sincere: "真诚版", direct: "直接版" };
export const VERDICT_LABELS: Record<string, string> = { similar: "与自述相近", context_differs: "情境有差异", insufficient: "信息不足" };
export const QUALITY_FLAGS: Record<string, string> = {
  no_reliable_time: "缺少可靠时间",
  few_other_texts: "对方文本较少",
  unknown_senders: "存在未确认发送者",
  range_truncated: "只分析了部分区间",
};
export const DIRECTION_LABELS: Record<string, string> = { advance: "推进", regress: "回退", same: "未变", unknown: "未知" };

/** How the other person answered an offer (v0.5 invitation chains). */
export const CHAIN_LABELS: Record<string, string> = {
  accept: "答应了",
  alt_time: "改期 / 推脱",
  decline: "拒绝了",
  deflect: "绕开了这个话题",
  none: "没有回应",
};
export const CHAIN_TONES: Record<string, "neutral" | "good" | "warn" | "info" | "accent"> = {
  accept: "good",
  alt_time: "warn",
  decline: "warn",
  deflect: "neutral",
  none: "neutral",
};
export const PRIOR_CHECK_LABELS: Record<string, string> = { supported: "被聊天支持", contradicted: "被聊天推翻", untested: "尚未检验" };
export const PRIOR_CHECK_TONES: Record<string, "good" | "warn" | "neutral"> = { supported: "good", contradicted: "warn", untested: "neutral" };

export const OBSERVED_LABELS: Record<string, string> = { accepted: "接受了", alt_time: "改了时间", declined: "拒绝了", no_reply: "没回应", other: "其它" };
export const REACTION_TONES: Record<string, "good" | "neutral" | "warn"> = { good: "good", neutral: "neutral", stop: "warn" };

export const NUDGE_KIND_LABELS: Record<string, string> = {
  recall: "想起 TA 说过的话",
  timing: "回复节奏",
  draft: "可用话术",
  checkin: "两周回访",
  self_care: "先照顾自己",
};
export const DRIVE_VAR_LABELS: Record<string, string> = { drive: "冲动", inhibition: "抑制", novelty: "刺激" };
export const DRIVE_SOURCE_LABELS: Record<string, string> = {
  affinity: "好感度",
  rising: "趋势上升",
  silence: "好久没看",
  diary: "新日记",
  outcome_better: "回访：变好了",
  boundary: "边界信号",
  explicit_decline: "明确拒绝",
  dismissed: "你忽略过",
  thumbs_down: "你点过 👎",
  pulse: "随机脉冲",
  new_report: "新报告",
  new_action: "你记录了行动",
};

export const STATUS_LABELS: Record<string, string> = {
  created: "等待确认",
  queued: "排队中",
  extracting: "提取信号",
  synthesizing: "综合分析",
  validating: "校验证据",
  intent: "逐句语义",
  assembling: "生成报告",
  completed: "完成",
  insufficient_data: "材料不足",
  failed: "失败",
  interrupted: "已中断",
  cancelled: "已取消",
};
export const ZODIAC: [string, string][] = [
  ["aries", "白羊"], ["taurus", "金牛"], ["gemini", "双子"], ["cancer", "巨蟹"], ["leo", "狮子"], ["virgo", "处女"],
  ["libra", "天秤"], ["scorpio", "天蝎"], ["sagittarius", "射手"], ["capricorn", "摩羯"], ["aquarius", "水瓶"], ["pisces", "双鱼"],
];
export const CHINESE_ZODIAC: [string, string][] = [
  ["rat", "鼠"], ["ox", "牛"], ["tiger", "虎"], ["rabbit", "兔"], ["dragon", "龙"], ["snake", "蛇"],
  ["horse", "马"], ["goat", "羊"], ["monkey", "猴"], ["rooster", "鸡"], ["dog", "狗"], ["pig", "猪"],
];
export const MBTI_TYPES = ["INTJ", "INTP", "ENTJ", "ENTP", "INFJ", "INFP", "ENFJ", "ENFP", "ISTJ", "ISFJ", "ESTJ", "ESFJ", "ISTP", "ISFP", "ESTP", "ESFP"];
