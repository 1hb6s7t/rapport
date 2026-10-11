"""Settings, data directories, provider presets and price table."""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

APP_VERSION = "0.8.0a0"
PARSER_VERSION = "parsers-v1"
FEATURES_VERSION = "features-v1"  # overwritten below by the lexicon content hash
RUBRIC_VERSION = "affinity-rubric-v0.2"  # overwritten below by the rubric parameter hash
PROMPTS_VERSION = "prompts-v1"  # overwritten below by the content hash of llm/prompts/*.md
MESSAGE_SCHEMA_VERSION = "message-v1"
REPORT_SCHEMA_VERSION = "report-v1"

AFFINITY_DISCLAIMER = (
    "好感度指数基于聊天中可观察的互动行为（回应、主动、投入、温度、未来指向）计算，"
    "是对本段材料的行为描述，不是对方真实心理的概率，也不预测关系结果。"
    "材料不完整、语境缺失、平台差异都会影响数值。请把它当作复盘的参考，而不是决定。"
)


class ConnectionMode(StrEnum):
    """Local-first by default; cloud is an explicit opt-in (v2 requirement, CHG-02).

    The guard lives in `state.make_provider`: in local mode a non-loopback endpoint is
    refused outright, so there is no silent fallback to a cloud provider.
    """

    local = "local"
    cloud = "cloud"


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def is_loopback_url(url: str | None) -> bool:
    from urllib.parse import urlparse

    if not url:
        return False
    try:
        host = urlparse(url).hostname
    except ValueError:
        return False
    return host in LOOPBACK_HOSTS


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RAPPORT_", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 7860
    data_dir: Path = Path(os.environ.get("RAPPORT_DATA_DIR", str(Path.home() / ".rapport")))
    api_key: str | None = None  # RAPPORT_API_KEY overrides the stored key
    raw_retention_hours: int = 24
    session_gap_hours: int = 6
    max_text_bytes: int = 300 * 1024 * 1024  # a multi-year chat export with quoted messages can be tens of MB
    max_messages: int = 50000  # hard reject above this (a full multi-year WeChat export is ~25k)
    large_import_messages: int = 5000  # above this: warn, keep going; AI mode still needs a window
    max_ai_messages: int = 50000  # hard cap for one AI run (same as the import cap); the pipeline windows 120 msgs per call
    ai_warn_messages: int = 2000  # above this the preview says how many calls / how long / how much, and asks for an explicit accept
    max_images: int = 20
    max_image_bytes: int = 10 * 1024 * 1024
    max_image_pixels: int = 20_000_000
    cost_warn_cny: float = 0.5
    allowed_hosts: list[str] = ["127.0.0.1", "localhost"]
    dev_cors_origin: str | None = None  # e.g. http://localhost:5173 for the vite dev server
    local_auto_session: bool = True  # local browser opens directly, without a pairing code
    pairing_code: str | None = None  # explicit legacy/test clients only
    open_browser: bool = False
    reply_adapter_manifest: str | None = None
    # Pragmatic-pattern reference layer. Measured on the held-out subset (docs/EVAL.md §14):
    # macro-F1 0.345 with it off vs 0.31 with it on, i.e. no transferable gain and a negative
    # direction — well short of the ≥0.05 that would count as an improvement. Default stays off.
    # The layer is kept (and can be switched on in settings) because it is the executable record
    # of the "structure, not wording" correction, and a negative result is only reproducible
    # while the apparatus still exists.
    use_intent_patterns: bool = False
    connection_mode: ConnectionMode = ConnectionMode.local
    log_level: str = "INFO"
    # Custom endpoints: refuse cloud-metadata and private-network targets unless explicitly allowed.
    allow_private_endpoints: bool = False

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def blob_dir(self) -> Path:
        return self.data_dir / "blobs"

    @property
    def key_file(self) -> Path:
        return self.data_dir / ".key"

    @property
    def secrets_file(self) -> Path:
        return self.data_dir / "secrets.enc"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.blob_dir.mkdir(parents=True, exist_ok=True)


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings(**overrides) -> Settings:
    global _settings
    _settings = Settings(**overrides)
    return _settings


@dataclass(frozen=True)
class Price:
    """Price per 1M tokens in CNY. None means unknown and is never displayed as 0."""

    input_per_m: float | None
    output_per_m: float | None
    currency: str = "CNY"
    verified_on: str = "unverified"
    note: str = ""


@dataclass(frozen=True)
class ProviderPreset:
    id: str
    label: str
    base_url: str | None
    default_model: str
    endpoint_region: str
    processing_scope: str
    price: Price
    supports_json_mode: bool = True
    reasoning: bool = False  # spends completion tokens on hidden reasoning before the answer
    native: str | None = None  # "ollama" → use the native client instead of the OpenAI-compatible layer
    task_models: dict[str, str] | None = None  # extract / synthesize / intent / reply → model tag (None = default_model for all)
    profile_label: str = ""  # shown in the local-model picker


# Prices are hand-maintained "as of" values and must be re-verified before each release.
STYLE_REPLY_PRESET = "style-local"

PROVIDER_PRESETS: dict[str, ProviderPreset] = {
    "personal": ProviderPreset(
        id="personal", label="本地 · 已启用的个人回复模型", base_url="local://personal", default_model="active",
        endpoint_region="本机", processing_scope="本机", price=Price(0.0, 0.0, "CNY", "n/a", "本地推理无按量费用"), native="personal",
    ),
    "deepseek": ProviderPreset(
        id="deepseek",
        label="DeepSeek",
        base_url="https://api.deepseek.com",
        default_model="deepseek-chat",
        endpoint_region="中国大陆",
        processing_scope="以供应商公开条款为准",
        price=Price(2.0, 8.0, "CNY", "2026-09-14", "deepseek-chat 标准价（缓存未命中输入价）"),
    ),
    "qwen": ProviderPreset(
        id="qwen",
        label="通义千问（阿里云百炼）",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        default_model="qwen-plus",
        endpoint_region="中国大陆（北京）",
        processing_scope="以供应商公开条款为准",
        price=Price(0.8, 2.0, "CNY", "2026-09-14", "qwen-plus 非思考模式"),
    ),
    "moonshot": ProviderPreset(
        id="moonshot",
        label="Moonshot (Kimi)",
        base_url="https://api.moonshot.cn/v1",
        default_model="kimi-k2-0905-preview",
        endpoint_region="中国大陆",
        processing_scope="以供应商公开条款为准",
        price=Price(4.0, 16.0, "CNY", "2026-09-14", ""),
    ),
    "openai": ProviderPreset(
        id="openai",
        label="OpenAI",
        base_url="https://api.openai.com/v1",
        default_model="gpt-4.1-mini",
        endpoint_region="全球",
        processing_scope="全球",
        price=Price(2.9, 11.6, "CNY", "2026-09-14", "按 0.40/1.60 USD 折算，汇率 7.25"),
    ),
    # Local profiles (v0.8). All three share one Ollama on loopback; they differ in which model
    # answers which task. Qwen3 is used with thinking switched off (see llm/ollama.py).
    "ollama": ProviderPreset(
        id="ollama",
        label="本地 · solo（qwen3:8b 包办）",
        base_url="http://127.0.0.1:11434",
        default_model="qwen3:8b",
        endpoint_region="本机",
        processing_scope="本机",
        price=Price(0.0, 0.0, "CNY", "n/a", "本地推理无按量费用"),
        native="ollama",
        task_models={"extract": "qwen3:8b", "synthesize": "qwen3:8b", "intent": "qwen3:8b", "reply": "qwen3:8b"},
        profile_label="solo：一个 8B 模型包办四类任务，不换模型；8 GB 显存",
    ),
    "ollama-split": ProviderPreset(
        id="ollama-split",
        label="本地 · split（4b 提取 + 8b 综合/语义）",
        base_url="http://127.0.0.1:11434",
        default_model="qwen3:8b",
        endpoint_region="本机",
        processing_scope="本机",
        price=Price(0.0, 0.0, "CNY", "n/a", "本地推理无按量费用"),
        native="ollama",
        task_models={"extract": "qwen3:4b", "synthesize": "qwen3:8b", "intent": "qwen3:8b", "reply": "qwen3:8b"},
        profile_label="split：长材料提取用 4B 更快，综合与语义用 8B；两模型交替常驻",
    ),
    "ollama-lite": ProviderPreset(
        id="ollama-lite",
        label="本地 · lite（qwen3:4b 包办）",
        base_url="http://127.0.0.1:11434",
        default_model="qwen3:4b",
        endpoint_region="本机",
        processing_scope="本机",
        price=Price(0.0, 0.0, "CNY", "n/a", "本地推理无按量费用"),
        native="ollama",
        task_models={"extract": "qwen3:4b", "synthesize": "qwen3:4b", "intent": "qwen3:4b", "reply": "qwen3:4b"},
        profile_label="lite：4B 包办，无独显或 4 GB 显存也能跑；语义能力按评测如实标注",
    ),
    # Local replies when no personal adapter is enabled: the same local Ollama, shown our own earlier
    # replies to this friend as style examples (learning/style_rag.py). Nothing leaves the machine.
    "selected-local": ProviderPreset(
        id="selected-local",
        label="本地 · 第 8 轮个性化草稿",
        base_url="http://127.0.0.1:" + str(int(os.getenv("RAPPORT_SELECTED_PORT", "17861"))),
        default_model="r7-epoch-8",
        endpoint_region="本机",
        processing_scope="本机",
        price=Price(0.0, 0.0, "CNY", "n/a", "本地推理无按量费用"),
        native="selected",
        profile_label="使用已选定的最佳权重起草，发送前请核对内容",
    ),
    "style-local": ProviderPreset(
        id="style-local",
        label="本地 · 风格检索回复（qwen3:8b）",
        base_url="http://127.0.0.1:11434",
        default_model="qwen3:8b",
        endpoint_region="本机",
        processing_scope="本机",
        price=Price(0.0, 0.0, "CNY", "n/a", "本地推理无按量费用"),
        native="ollama",
        profile_label="回复专用：本机 8B 模型参照你以前回复这位好友的原话起草；未启用个人模型时本地模式的默认回复方式",
    ),
    "mimo": ProviderPreset(
        id="mimo",
        label="小米 MiMo（token-plan）",
        base_url="https://token-plan-cn.xiaomimimo.com/v1",
        default_model="mimo-v2.5-pro",
        endpoint_region="中国大陆",
        processing_scope="以供应商公开条款为准",
        price=Price(None, None, "CNY", "unknown", "该端点未公开按量计费单价；费用以你的 token-plan 套餐为准。"),
        reasoning=True,
    ),
    "custom": ProviderPreset(
        id="custom",
        label="自定义 OpenAI 兼容端点",
        base_url=None,
        default_model="",
        endpoint_region="未知",
        processing_scope="未知",
        price=Price(None, None, "CNY", "unknown", "价格未知，需用户确认"),
    ),
}
DEFAULT_PROVIDER = "deepseek"


def _bind_prompts_version() -> None:
    """PROMPTS_VERSION tracks the prompt files: editing one changes every downstream key."""
    global PROMPTS_VERSION
    try:
        from rapport.llm.prompts import prompts_version

        PROMPTS_VERSION = prompts_version()
    except Exception:  # pragma: no cover - a missing prompt dir keeps the family label
        pass


_bind_prompts_version()


def _bind_scoring_versions() -> None:
    """RUBRIC_VERSION / FEATURES_VERSION track their parameters: editing an anchor, a weight or a
    lexicon pattern changes the snapshot comparability key, so the trend line breaks instead of
    silently connecting two different rulers."""
    global RUBRIC_VERSION, FEATURES_VERSION
    try:
        from rapport.features.lexicon import lexicon_digest
        from rapport.scoring.rubric import rubric_digest

        FEATURES_VERSION = f"{FEATURES_VERSION.split('+')[0]}+{lexicon_digest()}"
        RUBRIC_VERSION = f"{RUBRIC_VERSION.split('+')[0]}+{rubric_digest()}"
    except Exception:  # pragma: no cover
        pass


_bind_scoring_versions()
