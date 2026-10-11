/** Types aligned with backend schemas (message-v1, report-v1, API views). */

export type Sender = "me" | "other" | "system" | "unknown";
export type MsgType = "text" | "image" | "voice" | "video" | "sticker" | "file" | "call" | "transfer" | "link" | "system" | "unknown";

export interface Message {
  id: string;
  seq: number;
  sender: Sender;
  ts: string | null;
  ts_precision: "second" | "minute" | "day" | "label" | "none";
  type: MsgType;
  text: string | null;
  quoted_text: string | null;
  meta: Record<string, unknown>;
}

export interface ImportView {
  id: string;
  relationship_id: string;
  status: string;
  source: string;
  parser_id: string;
  filename: string | null;
  participants: { me: string; other: string };
  timezone: string;
  n_messages: number;
  n_events: number;
  n_unknown: number;
  has_reliable_time: boolean;
  warnings: { code: string; msg: string; msg_ids: string[] }[];
  range_start: number | null;
  range_end: number | null;
  content_hash: string | null;
  expires_at: string | null;
  messages: Message[];
  selected?: {
    n_messages: number;
    n_events: number;
    n_me: number;
    n_other: number;
    first_ts: string | null;
    last_ts: string | null;
    untimed_ratio: number;
    truncated: boolean;
    ai_limit_note: string | null;
  };
}

export type Goal = "understand" | "invite" | "clarify" | "repair" | "decide";
export interface BirthDate {
  year?: number | null;
  month?: number | null;
  day?: number | null;
  calendar: "gregorian" | "lunar";
  precision: "unknown" | "year" | "year_month" | "month_day" | "day";
  is_leap_month?: boolean | null;
}
export interface Person {
  alias: string;
  mbti?: string | null;
  zodiac?: string | null;
  chinese_zodiac?: string | null;
  birthday?: BirthDate | null;
  source?: "user" | "imported" | "unknown";
  confirmed?: boolean;
  revision?: number;
}
export interface Profile {
  me: Person;
  other: Person;
  goal: Goal;
  goal_text: string;
  stage_confirmed?: string | null;
}

export interface AnalysisView {
  id: string;
  import_id: string;
  mode: "rules" | "byok";
  status: string;
  stage_done: number;
  stage_total: number;
  message: string;
  error: { code: string; msg: string } | null;
  report_id: string | null;
  provider: string | null;
  model: string | null;
  est_cost: number | null;
  needs_confirmation: boolean;
  reused?: boolean;
  relationship_id: string;
}

export interface PreviewView {
  analysis_id: string;
  payload_hash: string;
  destination: { preset: string; label: string; base_url: string; model: string; endpoint_region: string; processing_scope: string; has_key: boolean; ready?: boolean };
  windows: string[][];
  n_windows: number;
  n_intent_calls?: number;
  n_calls?: number;
  large_run?: boolean;
  est_minutes?: number;
  large_run_note?: string;
  n_messages: number;
  redaction: { counts: Record<string, number>; names_replaced: number };
  cost: { tokens_in: number; tokens_out: number; cny: number | null; currency: string; price_known: boolean; price_date: string; over_threshold: boolean; note: string };
  fields_sent: string[];
  fields_not_sent: string[];
  prompts_version: string;
}

export type Confidence = "low" | "medium" | "high";
/** One offer and what came of it — the evidence behind the future dimension. */
export interface InvitationChain {
  propose_id: string;
  propose_text: string;
  response: "accept" | "alt_time" | "decline" | "deflect" | "none";
  response_id: string | null;
  commit_ids: string[];
}
export interface DimScore {
  score: number | null;
  rule: number | null;
  llm: number | null;
  confidence: Confidence;
  evidence_ids: string[];
  note: string;
  inputs: Record<string, unknown>;
}
/** One exploratory change point in the session-feature series (ruptures PELT); never a score input. */
export interface ChangePoint {
  index: number;
  date: string | null;
  stability: number;
  evidence_ids: string[];
  note: string;
}

export interface TrendPoint {
  label: string;
  start: string | null;
  end: string | null;
  score: number | null;
  n_msgs: number;
  n_other_text: number;
  insufficient: boolean;
  dims: Record<string, number | null>;
  /** Drill-down: what produced this segment's number (empty on reports written before v0.5). */
  evidence_ids?: string[];
  dim_evidence?: Record<string, string[]>;
  dim_inputs?: Record<string, Record<string, { value: number | null; n: number; unavailable_reason?: string | null }>>;
  llm_override?: Record<string, number>;
}
export interface Observation {
  id: string;
  claim: string;
  evidence_ids: string[];
  counter_evidence_ids: string[];
  alternatives: string[];
  limitations: string;
}
export interface Reaction { if_they: string; then: string; signal: "good" | "neutral" | "stop" }
export interface Option {
  id: string;
  goal: string;
  action: string;
  trigger_ids: string[];
  suggested_text: string;
  expected_reactions: Reaction[];
  stop_when: string;
  risk: string;
  caution?: string;
  personalized: boolean;
}
export interface Situation { id: string; title: string; trigger_ids: string[]; why: string; options: Option[] }
export interface DoneReview { option_id: string; goal: string; observed: string; note: string; verdict: string }

export interface Action {
  goal: string;
  action: string;
  basis_ids: string[];
  suggested_text: string;
  observe: string;
  stop_when: string;
  alternatives: string[];
  personalized: boolean;
  caution?: string;
}

export interface IntentCandidate {
  reading: string;
  likelihood: Confidence;
  basis_ids: string[];
}

export interface IntentReading {
  msg_id: string;
  /** "local_model" when produced offline by the bundled small model. */
  source?: string | null;
  post_hoc?: boolean;
  tag_basis?: Record<string, string[]>;
  episode_id: string;
  speaker: string;
  surface_text: string;
  literal_affect: string;
  pragmatic_tags: string[];
  emotions: string[];
  boundary_status: string;
  candidates: IntentCandidate[];
  evidence_strength: Confidence;
  model_confidence_raw: number | null;
  evidence_ids: string[];
  counter_evidence_ids: string[];
  reference_example_ids: string[];
  ambiguous: boolean;
  abstain_reason: string | null;
  suppressed_reason: string | null;
  independent_units: number;
  consistency?: number | null;
  samples?: number;
  /** v0.8 offline research layer: what each local signal voted, and the nearest labelled examples' labels. */
  method_votes?: Record<string, string>;
  similar_examples?: { label: string; score: number }[];
}


export interface PriorHypothesis {
  label: string;
  value: string;
  weight: number;
  hypothesis: string;
  source_type: string;
  basis: string;
  entertainment: boolean;
}

export interface PriorCheck {
  label: string;
  value: string;
  tendency_key: string;
  lean: string;
  lean_label: string;
  text: string;
  counterexample_prompt: string;
  status: "supported" | "contradicted" | "untested";
  observed_pole: string;
  observed_label: string;
  evidence_ids: string[];
}

export interface PriorBreakdown {
  alpha: number;
  weights: Record<string, number>;
  unknown: number;
  present: string[];
  n_eff: number;
  evidence_counts?: Record<string, number>;
  /** The budget this report was built under (user-adjustable in 设置 → 个性化权重). */
  policy_version?: string;
  alpha0?: number;
  shares?: Record<string, number>;
}

export interface DiaryRecord {
  id: string;
  revision: number;
  text: string;
  kind: "fact" | "plan" | "feeling";
  enabled: boolean;
  recorded_at: string;
  occurred_at: string | null;
  allowed_relationships: string[];
  approved_clause: string | null;
}

export interface MemoryRef {
  id: string;
  revision: number;
  kind: string;
}

export interface ReplyDraft {
  style: string;
  text: string;
  items?: { kind: "text" | "sticker" | "image"; text?: string; asset_id?: string }[];
  ranking?: { evidence_fit: number; auxiliary_fit: number | null; auxiliary_weight: number; score: number; source: string };
  memory_refs: MemoryRef[];
  why: string;
  risk_notes: string[];
  verified: boolean;
  verification_note: string;
  mode: string;
  needs_review: boolean;
  unbacked_claims: string[];
}

export interface ReplyPreview {
  ticket_id: string;
  purpose: string;
  payload_hash: string;
  destination: { preset: string; label: string; base_url: string | null; model: string; endpoint_region: string; processing_scope: string; has_key: boolean; ready?: boolean; price_date: string; connection_mode: string };
  lines: string[];
  n_lines: number;
  diary_refs: { id: string; revision: number; kind: string; chars: number }[];
  n_diary_chars: number;
  diary_lines: string[];
  cost: { tokens_in: number; tokens_out: number; cny: number | null; currency: string; price_known: boolean; price_date: string; over_threshold: boolean; note: string };
  prompts_version: string;
  expires_in_s: number;
  goal: string;
  styles: string[];
  fields_sent: string[];
  fields_not_sent: string[];
}

export interface ReplyPack {
  drafts: ReplyDraft[];
  memory_enabled: boolean;
  memories_used: MemoryRef[];
  dropped_memories: Record<string, number>;
  style_note: string;
  send_note: string;
  fabricated_note: string;
  abstain_reason: string | null;
  context?: { import_id: string | null; target_msg_id: string | null; messages: { id: string; sender: string; ts: string | null; text: string }[] };
}

export interface StageHistoryPoint {
  report_id: string;
  created_at: string;
  value: string | null;
  change: { before: string; after: string; direction: string; reason: string; basis_ids: string[] } | null;
}

export interface PersonaVersionEntry {
  version: number;
  report_id: string;
  created_at: string;
  active: boolean;
  superseded_by: string | null;
  tendencies: Record<string, string>;
  traits: string[];
  change_notes: { field: string; kind: string; before: string; after: string; reason: string }[];
}

export interface TrendSeries {
  points: { id: string; created_at: string; report_id: string; total: number | null; coverage: number; n_eff: number; dims: Record<string, number | null>; comparable: boolean; note: string }[];
  comparable_with_latest: number;
  note: string;
}

export interface Report {
  id: string;
  created_at: string;
  mode: "demo" | "rules" | "byok";
  synthetic: boolean;
  versions: { app: string; parser: string; features: string; rubric: string; prompts: string | null; provider: string | null; model: string | null; price_date: string | null };
  scope: {
    msg_total: number;
    msg_analyzed: number;
    text_other: number;
    text_me: number;
    sessions: number;
    range_start: string | null;
    range_end: string | null;
    has_reliable_time: boolean;
    coverage: number;
    quality_flags: string[];
  };
  affinity: {
    total: number | null;
    caveats?: string[];
    confidence?: string;
    raw_total: number | null;
    coverage: number;
    suppressed_reason: string | null;
    insufficient_reason: string | null;
    min_evidence_reason: string | null;
    provisional_total: number | null;
    rho: number;
    provisional_note: string;
    independent_units: number;
    independent_unit_kind: string;
    dims: Record<string, DimScore>;
    /** Empty when the material had no offer at all, which is when the dimension falls back to words. */
    future_chains: InvitationChain[];
    disclaimer: string;
  };
  trend: { bucket: string; points: TrendPoint[]; momentum: "rising" | "flat" | "falling" | "unknown"; note: string; change_points?: ChangePoint[]; change_note?: string };
  observations: Observation[];
  priors: PriorHypothesis[];
  prior_checks: PriorCheck[];
  prior_breakdown: PriorBreakdown;
  intent_readings: IntentReading[];
  persona: {
    available: boolean;
    limitations: string;
    tendencies: Record<string, { label: string; evidence_ids: string[]; note: string; pole?: string }>;
    pole_stability?: Record<string, number>;
    traits: { statement: string; evidence_ids: string[]; confidence: Confidence }[];
    inferred_axes: Record<string, { lean: string; confidence: Confidence; note: string }>;
    mbti_comparison: { declared: string; inferred: string; verdict: string; notes: string[] } | null;
  };
  stage: { value: string; basis_ids: string[]; user_confirmed: boolean; note: string; change: { before: string; after: string; direction: string; reason: string } | null };
  boundary: { value: string; basis_ids: string[]; note: string };
  playbook: {
    changed_from_report: string | null;
    change_notes: string[];
    actions: Action[];
    situations: Situation[];
    done_review: DoneReview[];
    landmines: { topic: string; why: string; evidence_ids: string[] }[];
    scripts: Record<string, { text: string; when_to_use: string; caution?: string }>;
    note: string;
  };
  fun: { zodiac_pair: { title: string; text: string } | null; chinese_zodiac_pair: { title: string; text: string } | null; disclaimer: string };
  evidence: Record<string, { sender: string; ts: string | null; text: string }>;
  cost: { est_tokens_in: number; est_tokens_out: number; est_cny: number | null; actual_usage: { prompt_tokens: number; completion_tokens: number; calls: number } | null; actual_cny: number | null };
  notes: string[];
  ai_status: string;
  ai_error: string | null;
}

export interface Relationship {
  id: string;
  name: string;
  person?: Person;
  profile_revision?: number;
  note: string;
  labels: { mbti: string | null; zodiac: string | null; chinese_zodiac: string | null };
  archived: boolean;
  is_default: boolean;
  created_at: string | null;
  updated_at: string | null;
  counts: { imports?: number; reports?: number; snapshots?: number; events_confirmed?: number; feedback?: number };
}

export type FeedbackKind = "intent" | "observation" | "action" | "landmine" | "stage" | "trait" | "affinity" | "boundary";
export type Verdict = "agree" | "disagree" | "unsure";
export interface FeedbackItem {
  id: string;
  created_at: string;
  relationship_id: string;
  report_id: string;
  target_kind: FeedbackKind;
  target_key: string;
  verdict: Verdict;
  note: string;
  correction: Record<string, unknown>;
}

export interface LLMTotals {
  calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  cost_cny: number;
  truncated: number;
  bad_json: number;
  errors: number;
  avg_latency_ms: number;
  truncation_rate: number;
  failure_rate: number;
}
export interface StatsView {
  llm: { days: number; since: string; ledger_since: string | null; totals: LLMTotals; by_purpose: Record<string, LLMTotals>; note: string };
  feedback: { by_kind: Record<string, Record<Verdict, number>>; total: number };
  relationships: number;
}

export interface CallRow {
  id: string;
  created_at: string;
  purpose: string;
  preset: string;
  model: string;
  prompt_tokens: number;
  completion_tokens: number;
  usage_known: boolean;
  max_tokens: number;
  latency_ms: number;
  status: string;
  error_code: string | null;
  cost_cny: number | null;
}
export interface CallsView {
  analysis_id: string;
  calls: CallRow[];
  summary: { calls: number; prompt_tokens: number; completion_tokens: number; truncated: number; failed: number; cost_cny: number | null };
}

export interface RelationshipOverview {
  relationship: Relationship;
  n_eff: number;
  trend: TrendSeries;
  stage_history: { points: StageHistoryPoint[]; note: string };
  persona_history: { versions: PersonaVersionEntry[]; note: string };
  feedback: StatsView["feedback"];
  llm: LLMTotals;
}

export interface SettingsView {
  connection_mode: "local" | "cloud";
  effective_chain: string;
  provider: {
    preset: string;
    model: string;
    base_url: string | null;
    cost_warn_cny: number;
    has_key: boolean;
    ready?: boolean;
    key_source: string | null;
    endpoint_region: string;
    processing_scope: string;
    price: { input_per_m: number | null; output_per_m: number | null; currency: string; verified_on: string; note: string };
  };
  presets: { id: string; label: string; base_url: string | null; default_model: string; endpoint_region: string; processing_scope: string; price: { input_per_m: number | null; output_per_m: number | null; verified_on: string }; native?: string | null; task_models?: Record<string, string> | null; profile_label?: string }[];
  limits: { max_text_bytes: number; max_messages: number; large_import_messages?: number; max_ai_messages: number; ai_warn_messages?: number; max_images: number; max_image_bytes: number; raw_retention_hours: number };
  formats: { id: string; label: string; files: string[] }[];
  ocr_available: boolean;
  data_dir: string;
  schema_changes?: Record<string, string[]>;
}

/** v0.8 local models: `GET /models/doctor`. Facts about the machine and Ollama, never about material. */
export interface LocalDoctor {
  ollama_installed: boolean;
  ollama_path: string | null;
  ollama_running: boolean;
  ollama_version: string | null;
  error: string | null;
  gpu: string | null;
  vram_gb: number | null;
  ram_gb: number | null;
  models: { name: string; size_gb: number; family: string }[];
  models_dir?: string;
  recommended_profile: string;
  profiles: Record<string, { preset: string; label: string; models: string[]; missing: string[]; ready: boolean }>;
  presets_task_models?: Record<string, Record<string, string>>;
  notes: string[];
}

/** `GET /models/pull`: progress of the background weight download. */
export interface LocalPullStatus {
  running: boolean;
  profile: string | null;
  items: { model: string; status: string; completed: number; total: number; done: boolean }[];
  error: string | null;
}

/** The check-in asked on the history page once a report is older than the ask window. */
export interface OutcomePrompt {
  report_id: string;
  report_created_at: string;
  days_since: number;
  question: string;
  options: string[];
}

export interface OutcomeEntry {
  id: string;
  relationship_id: string;
  report_id: string | null;
  answered_at: string | null;
  outcome: string;
  met_since: boolean;
  note: string;
}

export interface DriveConfig {
  enabled: boolean;
  llm_drafts: boolean;
  daily_cap: number;
  quiet_start_h: number;
  quiet_end_h: number;
  disabled_relationships: string[];
  version: string;
}

export type PriorLabelKey = "mbti" | "zodiac" | "chinese_zodiac";

export interface PriorPolicyNumbers {
  alpha0: number;
  scale: number;
  ratio: Record<PriorLabelKey, number>;
  shares: Record<PriorLabelKey, number>;
  version: string;
}

/** GET/PATCH /preferences/prior — how much MBTI / 星座 / 属相 may weigh before chat evidence takes over. */
export interface PriorPolicyView extends PriorPolicyNumbers {
  is_default: boolean;
  default: PriorPolicyNumbers;
}

export interface EmotionModelStatus {
  id: string;
  available: boolean;
  dir: string;
  license: string;
  source: string;
  labels: string[];
  size_bytes: number | null;
  sha256: string | null;
  max_len: number;
  note: string;
  reason: string | null;
}

export interface EmotionConfig {
  enabled: boolean;
  engine: "grounded" | "structbert";
  max_messages: number;
  use_markers: boolean;
  use_changepoints: boolean;
}

export interface OfflineComponentStatus {
  available: boolean;
  reason?: string | null;
  note?: string;
  id?: string;
  license?: string;
  source?: unknown;
  size_bytes?: number | null;
  sha256?: string | null;
  count?: number;
  labels?: string[];
  quality?: string | null;
  temperature?: number;
  ece_before?: number | null;
  ece_after?: number | null;
  fitted_on?: { dataset_id?: string; split?: string; n?: number } | null;
  markers?: string[];
}

/** GET/PATCH /preferences/emotion — the offline analysis layer switch for rules mode. */
export interface EmotionConfigView {
  config: EmotionConfig;
  default: EmotionConfig;
  engines: string[];
  model: {
    emotion_model: EmotionModelStatus;
    bge?: OfflineComponentStatus;
    anchors?: OfflineComponentStatus;
    calibration?: OfflineComponentStatus;
    markers?: OfflineComponentStatus;
  };
}

export interface DriveExplain {
  relationship_id: string;
  enabled: boolean;
  version: string;
  drive: number;
  inhibition: number;
  novelty: number;
  p_last_hour: number;
  roll_last_hour: number | null;
  last_tick: string | null;
  last_nudge_at: string | null;
  today_count: number;
  daily_cap: number;
  boundary_pinned: boolean;
  sources: Record<"drive" | "inhibition" | "novelty", Record<string, number>>;
  blocked_by: string | null;
  why_not: string | null;
  note: string;
}

export interface NudgeItem {
  id: string;
  relationship_id: string;
  created_at: string;
  updated_at: string | null;
  kind: "recall" | "timing" | "draft" | "checkin" | "self_care";
  text: string;
  evidence_ids: string[];
  why: string;
  drive_snapshot: { drive?: number; inhibition?: number; novelty?: number; p?: number; sources?: Record<string, Record<string, number>> };
  status: "unread" | "read" | "dismissed" | "acted";
  verdict: "agree" | "disagree" | "unsure" | null;
  actuator: string;
  version: string;
}

export interface InboxView {
  items: NudgeItem[];
  unread: number;
  note: string;
}

export interface OutcomesView {
  relationship_id: string;
  items: OutcomeEntry[];
  prompt: OutcomePrompt | null;
  note: string;
}
