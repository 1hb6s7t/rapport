"""Application state, split by responsibility.

`AppState` is composed from mixins so that each concern lives in one file and can be read on
its own, while every existing call site (`state.save_import(...)`, `state.trend_series()` ...)
keeps working unchanged:

- `provider`      — provider config, key storage, local/cloud guard, audited provider factory
- `imports`       — raw material blobs, canonical events and the evidence ledger (N_eff)
- `history`       — snapshots, portrait versions, stage history, last report
- `diary`         — diary CRUD, memory / pattern switches, retrieve-then-authorise search
- `relationships` — the 关系档案 entity that scopes everything above
- `audit`         — model-call ledger and user feedback
- `actions`       — playbook execution log (我做了) and weeks-later outcome check-ins
- `preferences`   — the user's own weighting knobs (label prior policy)
- `emotion`       — the offline small-model switch for rules mode
- `maintenance`   — startup reconcile, retention purge, wipe-all

Persisted chat text uses the encrypted blob store. Desktop previews/history jobs also
hold bounded, temporary plaintext in memory until import, revocation or expiry.
"""

from __future__ import annotations

import json
import uuid

from rapport.config import Settings
from rapport.db import Database
from rapport.desktop.dispatch import DispatchStore
from rapport.desktop.dsh_source import DshService
from rapport.desktop.dsh_worker import DesktopWorker
from rapport.desktop.stickers import StickerSearchStore
from rapport.desktop.wechat import CaptureTickets
from rapport.desktop.wechat_jobs import HistoryJobs
from rapport.learning import LearningService
from rapport.llm.outbound import OutboundGate
from rapport.llm.provider import LLMProvider
from rapport.security import Auth, BlobStore, Crypto, RateLimiter, SecretStore
from rapport.state.actions import ActionsMixin
from rapport.state.audit import AuditMixin
from rapport.state.diary import DiaryMixin
from rapport.state.drive import DriveMixin
from rapport.state.emotion import EmotionMixin
from rapport.state.gold import GoldMixin
from rapport.state.history import HistoryMixin
from rapport.state.imports import ImportsMixin
from rapport.state.maintenance import MaintenanceMixin
from rapport.state.preferences import PreferencesMixin
from rapport.state.provider import ProviderMixin
from rapport.state.relationships import RelationshipsMixin


def new_id() -> str:
    return uuid.uuid4().hex[:16]


class AppState(
    ProviderMixin,
    ImportsMixin,
    HistoryMixin,
    DiaryMixin,
    RelationshipsMixin,
    AuditMixin,
    ActionsMixin,
    DriveMixin,
    PreferencesMixin,
    EmotionMixin,
    GoldMixin,
    MaintenanceMixin,
):
    def __init__(self, settings: Settings, provider_override: LLMProvider | None = None):
        self.settings = settings
        settings.ensure_dirs()
        self.db = Database(settings.db_path)
        self.crypto = Crypto(settings.key_file)
        self.secrets = SecretStore(self.crypto, settings.secrets_file)
        self.blobs = BlobStore(self.crypto, settings.blob_dir)
        self.auth = Auth.create(settings.pairing_code, auto_local=settings.local_auto_session)
        self.pair_limiter = RateLimiter(limit=20, per_s=60)
        self.provider_override = provider_override
        self.outbound = OutboundGate()
        self.wechat_captures = CaptureTickets()
        self.wechat_history = HistoryJobs()
        self.dsh_jobs = HistoryJobs()
        self.dsh = DshService(self)
        self.desktop = DesktopWorker()
        self.dispatch = DispatchStore(self)
        self.sticker_search = StickerSearchStore()
        self.learning = LearningService(settings.data_dir)
        self.runner = None  # set by jobs.runner
        self.ensure_default_relationship()

    def cleanup_personalization(self, relationship_id: str | None = None):
        if relationship_id is None:
            from rapport.research.lifecycle import safe_wipe

            safe_wipe(self.settings.data_dir)
        learning = getattr(self, "learning", None)
        if learning:
            if relationship_id is None:
                learning.wipe_all()
            else:
                learning.revoke_relationship(relationship_id)
        self.dispatch.clear(relationship_id)
        self.dsh_jobs.clear(relationship_id)
        self.dsh.clear(relationship_id)
        self.sticker_search.clear(relationship_id)
        try:
            from rapport.archives import get_service

            archive = get_service(self)
            if relationship_id is None:
                archive.wipe_all()
            else:
                archive.delete_relationship(relationship_id)
        except ImportError:
            pass
        if relationship_id is None:
            from rapport.datasets import DatasetStore

            datasets = DatasetStore(self.settings.data_dir / "datasets", self.crypto)
            if hasattr(datasets, "wipe_all"):
                datasets.wipe_all()


def dumps(o) -> str:
    return json.dumps(o, ensure_ascii=False, default=str)


__all__ = ["AppState", "dumps", "new_id"]
