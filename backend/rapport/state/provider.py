"""Provider configuration, key storage and the local/cloud guard."""

from __future__ import annotations

from rapport.config import (
    DEFAULT_PROVIDER,
    PROVIDER_PRESETS,
    STYLE_REPLY_PRESET,
    ConnectionMode,
    is_loopback_url,
)
from rapport.db import Setting
from rapport.llm.audit import AuditedProvider
from rapport.llm.openai_compat import OpenAICompatProvider
from rapport.llm.provider import LLMError, LLMProvider


class ProviderMixin:
    def task_provider_config(self, task: str) -> dict:
        """Resolve the exact task route before the outbound preview is signed."""
        cfg = dict(self.provider_config())
        if task == "reply" and self.connection_mode() == ConnectionMode.local and self.settings.reply_adapter_manifest:
            from rapport.llm.selected_local import ENDPOINT, PRESET, selection

            chosen = selection(self.settings)
            cfg.update(preset=PRESET, model=chosen["id"], base_url=ENDPOINT, has_key=True, ready=True,
                       native="selected", endpoint_region="本机", processing_scope="本机")
            return cfg
        personal = self.learning.active_model("reply") if task == "reply" and self.connection_mode() == ConnectionMode.local else None
        if personal:
            cfg.update(preset="personal", model=personal["id"], base_url="local://personal", has_key=True, ready=True,
                       native="personal", endpoint_region="本机", processing_scope="本机")
            return cfg
        preset = PROVIDER_PRESETS[cfg["preset"]]
        if preset.native == "ollama" and cfg.get("model") == preset.default_model:
            cfg["model"] = (preset.task_models or {}).get(task) or cfg["model"]
        if task == "reply" and self.connection_mode() == ConnectionMode.local:
            # No enabled personal adapter: local drafts imitate our own earlier replies to this friend
            # on the local Ollama. A saved cloud preset cannot run in local mode anyway.
            style = PROVIDER_PRESETS[STYLE_REPLY_PRESET]
            ollama = preset.native == "ollama"
            cfg.update(preset=STYLE_REPLY_PRESET, model=cfg["model"] if ollama else style.default_model,
                       base_url=cfg["base_url"] if ollama else style.base_url, ready=True, native="ollama",
                       endpoint_region=style.endpoint_region, processing_scope=style.processing_scope)
        return cfg

    def provider_config(self) -> dict:
        with self.db.session() as s:
            row = s.get(Setting, "provider")
            cfg = dict(row.value) if row else {}
        cfg.setdefault("preset", DEFAULT_PROVIDER)
        p = PROVIDER_PRESETS.get(cfg["preset"], PROVIDER_PRESETS[DEFAULT_PROVIDER])
        cfg.setdefault("model", p.default_model)
        cfg.setdefault("base_url", p.base_url)
        cfg.setdefault("cost_warn_cny", self.settings.cost_warn_cny)
        cfg["has_key"] = bool(self.get_api_key(cfg["preset"]))
        cfg["key_source"] = "env" if self.settings.api_key else ("stored" if cfg["has_key"] else None)
        cfg["endpoint_region"] = p.endpoint_region
        cfg["processing_scope"] = p.processing_scope
        cfg["native"] = p.native
        # Local runtimes (Ollama on loopback, the personal adapter) need no API key to be usable.
        cfg["ready"] = cfg["has_key"] or p.native in {"ollama", "personal"}
        cfg["task_models"] = p.task_models
        cfg["price"] = {
            "input_per_m": p.price.input_per_m,
            "output_per_m": p.price.output_per_m,
            "currency": p.price.currency,
            "verified_on": p.price.verified_on,
            "note": p.price.note,
        }
        return cfg

    def save_provider_config(
        self, preset: str, model: str | None, base_url: str | None, api_key: str | None, cost_warn_cny: float | None
    ) -> None:
        if preset not in PROVIDER_PRESETS:
            raise ValueError("unknown preset")
        with self.db.session() as s:
            row = s.get(Setting, "provider") or Setting(key="provider", value={})
            val = dict(row.value)
            val["preset"] = preset
            val["model"] = model or PROVIDER_PRESETS[preset].default_model
            val["base_url"] = base_url or PROVIDER_PRESETS[preset].base_url
            if cost_warn_cny is not None:
                val["cost_warn_cny"] = cost_warn_cny
            row.value = val
            s.merge(row)
        if api_key is not None:
            sec = self.secrets.load()
            if api_key == "":
                sec.pop(f"key:{preset}", None)
            else:
                sec[f"key:{preset}"] = api_key
            self.secrets.save(sec)

    def get_api_key(self, preset: str) -> str | None:
        if self.settings.api_key:
            return self.settings.api_key
        return self.secrets.load().get(f"key:{preset}")

    def connection_mode(self) -> ConnectionMode:
        """Persisted mode wins; falls back to the env/default setting."""
        with self.db.session() as s:
            row = s.get(Setting, "connection_mode")
            if row and isinstance(row.value, dict) and row.value.get("mode"):
                try:
                    return ConnectionMode(row.value["mode"])
                except ValueError:
                    pass
        return self.settings.connection_mode

    def set_connection_mode(self, mode: ConnectionMode) -> None:
        with self.db.session() as s:
            row = s.get(Setting, "connection_mode") or Setting(key="connection_mode", value={})
            row.value = {"mode": mode.value}
            s.merge(row)

    def make_provider(
        self,
        cfg: dict | None = None,
        *,
        analysis_id: str | None = None,
        relationship_id: str | None = None,
        task: str = "intent",
    ) -> LLMProvider:
        """Build the configured provider, wrapped so every call lands in the audit ledger.

        In local mode a non-loopback endpoint is refused outright: there is no silent fallback
        to a cloud provider (v2 CHG-02).
        """
        cfg = cfg or self.task_provider_config(task)
        if self.provider_override is not None:
            inner: LLMProvider = self.provider_override
        elif (cfg or {}).get("preset") == "selected-local":
            from rapport.llm.selected_local import SelectedLocalProvider

            if task != "reply" or self.connection_mode() != ConnectionMode.local:
                raise LLMError("selected_task_mismatch", "已选权重仅用于本地回复起草。")
            inner = SelectedLocalProvider(self.settings, cfg.get("model"))
        elif (cfg or {}).get("preset") == "personal":
            from rapport.llm.personal import PersonalReplyProvider

            if task != "reply" or self.connection_mode() != ConnectionMode.local:
                raise LLMError("personal_task_mismatch", "个人表达模型仅用于本地回复生成。")
            current = self.learning.active_model("reply", relationship_id)
            if not current or current["id"] != cfg.get("model"):
                raise LLMError("personal_model_changed", "个人模型版本已变化，请重新预览。")
            inner = PersonalReplyProvider(self.learning, current["id"])
        else:
            cfg = cfg or self.task_provider_config(task)
            preset = cfg["preset"]
            base_url = cfg.get("base_url")
            mode = self.connection_mode()
            if mode == ConnectionMode.local and not is_loopback_url(base_url):
                raise LLMError(
                    "mode_violation",
                    "当前是本地模式，但配置的模型端点不在本机（%s）。"
                    "本地模式不会向云端发送任何内容；如需使用云 API，请在「设置与数据」里显式切换到云模式。"
                    % (base_url or "未设置"),
                )
            p = PROVIDER_PRESETS.get(preset)
            if p is not None and p.native == "ollama":
                from rapport.llm.ollama import OllamaProvider

                model = (p.task_models or {}).get(task) or cfg.get("model") or p.default_model
                # a user-overridden model applies to every task only when it differs from the preset default
                if cfg.get("model") and cfg.get("model") != p.default_model:
                    model = cfg["model"]
                inner = OllamaProvider(preset, model, base_url, task=task)
            else:
                key = self.get_api_key(preset)
                if not key:
                    raise LLMError("no_key", "尚未配置 API Key")
                inner = OpenAICompatProvider(preset, key, cfg.get("model"), base_url, allow_private=self.settings.allow_private_endpoints)
        if self.provider_override is None and is_loopback_url((cfg or {}).get("base_url")):
            from rapport.llm.gpu_guard import GPUGuardedProvider

            before_call = None
            if self.settings.reply_adapter_manifest and (cfg or {}).get("preset") != "selected-local":
                from rapport.llm.selected_local import SelectedLocalProvider, selection

                selected = SelectedLocalProvider(self.settings, selection(self.settings)["id"])
                def before_call():
                    return selected._call("POST", "/unload", {})
            inner = GPUGuardedProvider(inner, self.settings.data_dir, before_call=before_call)
        return AuditedProvider(inner, sink=self.record_llm_call, analysis_id=analysis_id, relationship_id=relationship_id)
