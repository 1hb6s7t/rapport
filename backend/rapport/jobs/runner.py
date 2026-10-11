"""In-process job runner: a daemon thread polls the analyses table and executes serially.

The analyses row is the source of truth. The queue never holds raw chat or keys. Status
changes go through `jobs.status` so an illegal transition is a bug report, not silent drift.
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
from datetime import datetime

from rapport.db import DEFAULT_RELATIONSHIP_ID, Analysis, Import, ReportRow
from rapport.jobs.pipeline import ByokContext, Cancelled, PipelineHooks, Progress, run_byok, run_rules
from rapport.jobs.status import AnalysisStatus, IllegalTransition, check_transition
from rapport.llm.extract import build_payload
from rapport.llm.provider import LLMError
from rapport.report.playbook_change import diff_playbook
from rapport.schemas.profile import Profile
from rapport.state import AppState, new_id  # noqa: E402  (state is fully loaded before the runner is)

log = logging.getLogger("rapport.jobs")
S = AnalysisStatus


class JobRunner:
    def __init__(self, state: AppState, poll_s: float = 0.5):
        self.state = state
        self.poll_s = poll_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._wake = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="rapport-jobs", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)

    def wake(self) -> None:
        self._wake.set()

    def run_pending_once(self) -> int:
        """Synchronously run all queued jobs (tests / CLI)."""
        n = 0
        while True:
            aid = self._claim()
            if not aid:
                return n
            self._execute(aid)
            n += 1

    def _loop(self) -> None:
        last_purge = 0.0
        last_drive = 0.0
        while not self._stop.is_set():
            try:
                aid = self._claim()
                if aid:
                    self._execute(aid)
                    continue
                if time.time() - last_purge > 300:
                    self.state.purge_expired()
                    last_purge = time.time()
                if time.time() - last_drive > 60:
                    # The drive layer's clock: once a minute here, once an hour inside (idempotent).
                    self.state.drive_tick(datetime.now())
                    last_drive = time.time()
            except Exception:  # noqa: BLE001
                log.error("runner loop error: %s", traceback.format_exc())
            self._wake.wait(self.poll_s)
            self._wake.clear()

    def _claim(self) -> str | None:
        with self.state.db.session() as s:
            a = (
                s.query(Analysis)
                .filter(Analysis.status == S.queued.value, Analysis.tombstone.is_(False))
                .order_by(Analysis.created_at)
                .first()
            )
            if not a:
                return None
            a.status = (S.extracting if a.mode == "byok" else S.assembling).value
            a.message = "开始处理"
            return a.id

    def _set(self, aid: str, **fields) -> None:
        with self.state.db.session() as s:
            a = s.get(Analysis, aid)
            if not a:
                return
            if "status" in fields:
                check_transition(a.status, str(fields["status"]))
            for k, v in fields.items():
                setattr(a, k, str(v) if k == "status" else v)
            a.updated_at = datetime.now()

    def _cancelled(self, aid: str) -> bool:
        with self.state.db.session() as s:
            a = s.get(Analysis, aid)
            return bool(a is None or a.cancel_requested or a.tombstone)

    def _offline_readings(self, conv, start: int, end: int):
        """Offline per-message readings for rules mode; None unless the user enabled the layer.

        Two engines: "grounded" (BGE retrieval + calibrated emotion model + Chinese markers) and
        "structbert" (the model alone). The layer is an enhancement, never a dependency: any
        failure here must leave the analysis running exactly as before the feature existed.
        """
        try:
            config = self.state.emotion_config()
            if not config.get("enabled"):
                return None
            if config.get("engine") == "grounded":
                from rapport.grounded.classify import readings_from_conversation

                return readings_from_conversation(conv, start, end, config) or None
            from rapport.emotion import readings_from_conversation

            return readings_from_conversation(conv, start, end, max_messages=config["max_messages"]) or None
        except Exception:  # noqa: BLE001
            log.warning("offline reading layer skipped: %s", traceback.format_exc())
            return None

    def _change_points(self, conv, start: int, end: int):
        """Exploratory trend change points; None unless enabled and enough sessions exist."""
        try:
            config = self.state.emotion_config()
            if not config.get("enabled") or not config.get("use_changepoints"):
                return None
            from rapport.scoring.changepoints import detect_change_points, to_schema

            payload = detect_change_points(conv, start, end)
            points = to_schema(payload)
            return (points, payload.get("note") or "") if points else None
        except Exception:  # noqa: BLE001
            log.warning("change-point layer skipped: %s", traceback.format_exc())
            return None

    def _execute(self, aid: str) -> None:
        with self.state.db.session() as s:
            a = s.get(Analysis, aid)
            if not a:
                return
            imp = s.get(Import, a.import_id)
            mode, profile_d, import_id = a.mode, dict(a.profile), a.import_id
            confirmed_hash = a.content_hash
            edited = {int(k): v for k, v in (a.edited_lines or {}).items()}
            checkpoints = dict(a.checkpoints or {})
            provider_preset = a.provider
            relationship_id = (imp.relationship_id if imp else None) or a.relationship_id or DEFAULT_RELATIONSHIP_ID
        if not imp or imp.deleted:
            self._set(aid, status=S.failed, error_code="import_deleted", error_msg="原始材料已删除")
            return
        if imp.status != "confirmed" or not imp.content_hash or imp.content_hash != confirmed_hash:
            # The material was edited after this analysis was created (and, for BYOK, after the
            # outbound preview was confirmed). Sending it would break "only what you saw leaves".
            self._set(aid, status=S.failed, error_code="import_changed", error_msg="材料在确认后被修改，请重新确认区间并重新预览")
            return
        conv = self.state.load_conversation(imp)
        if conv is None:
            self._set(aid, status=S.failed, error_code="blob_missing", error_msg="原始材料不可读（可能已到期清理）")
            return
        start, end = imp.range_start or 0, imp.range_end if imp.range_end is not None else len(conv.messages) - 1
        events = [m for m in conv.messages[start : end + 1] if m.is_event]
        profile = Profile.model_validate(profile_d)
        report_id = new_id()
        try:
            # Everything historical is read for THIS relationship only.
            n_eff = self.state.ledger_n_eff(relationship_id)
            evidence_counts = self.state.ledger_breakdown(relationship_id)
            prev_report_id, _ = self.state.last_report(relationship_id)
            last_actions = self.state.actions_since(relationship_id, prev_report_id)
            previous_stage = self.state.last_stage(relationship_id)
            prior_policy = self.state.prior_policy()
            if mode == "rules":
                changes = self._change_points(conv, start, end)
                report = run_rules(
                    conv, events, profile, self.state.settings.session_gap_hours,
                    report_id=report_id, n_eff=n_eff, previous_stage=previous_stage, evidence_counts=evidence_counts, last_actions=last_actions,
                    prior_policy=prior_policy,
                    intent_readings=self._offline_readings(conv, start, end),
                    change_points=changes[0] if changes else None,
                    change_note=changes[1] if changes else "",
                )
            else:
                cfg = self.state.provider_config()
                provider = self.state.make_provider(cfg, analysis_id=aid, relationship_id=relationship_id, task="intent")
                from rapport.config import PROVIDER_PRESETS as _PRESETS
                from rapport.llm.limits import TASKS as _TASKS

                _p = _PRESETS.get(cfg["preset"])
                providers = (
                    {t: self.state.make_provider(cfg, analysis_id=aid, relationship_id=relationship_id, task=t) for t in _TASKS}
                    if _p is not None and _p.task_models
                    else {}
                )
                payload = build_payload(conv, events, reasoning=bool(getattr(provider, "reasoning", False)))

                def progress(p: Progress) -> None:
                    self._set(aid, status=p.stage, stage_done=p.done, stage_total=p.total, message=p.message)

                def checkpoint(k: str, v) -> None:
                    checkpoints[k] = v
                    self._set(aid, checkpoints=dict(checkpoints))

                hooks = PipelineHooks(
                    progress=progress,
                    is_cancelled=lambda: self._cancelled(aid),
                    checkpoint=checkpoint,
                    load_checkpoint=lambda k: checkpoints.get(k),
                )
                ctx = ByokContext(
                    provider=provider,
                    providers=providers,
                    payload=payload,
                    edited_lines=edited,
                    versions_extra={
                        "provider": provider.info.preset,
                        "model": provider.info.model,
                        "price_date": provider.info.price_date,
                    },
                    preset=provider_preset or cfg["preset"],
                    use_patterns=self.state.patterns_enabled(),
                    use_cues=self.state.cues_enabled(),
                    intent_samples=self.state.intent_samples(),
                    last_actions=last_actions,
                )
                report = run_byok(
                    conv, events, profile, ctx, hooks, self.state.settings.session_gap_hours,
                    report_id=report_id, n_eff=n_eff, previous_stage=previous_stage, evidence_counts=evidence_counts,
                    prior_policy=prior_policy,
                )
        except Cancelled:
            self._set(aid, status=S.cancelled, message="已取消（在途调用可能已计费）")
            return
        except LLMError as e:
            self._set(aid, status=S.failed, error_code=e.code, error_msg=e.msg, message="模型调用失败")
            return
        except IllegalTransition as e:
            # The row was moved under us (cancelled / tombstoned from the API). Leave it alone.
            log.warning("analysis %s: %s", aid, e)
            return
        except Exception as e:  # noqa: BLE001
            log.error("analysis %s failed: %s", aid, traceback.format_exc())
            self._set(aid, status=S.failed, error_code="internal", error_msg=f"{type(e).__name__}", message="内部错误")
            return
        # tombstone check before writing the report
        if self._cancelled(aid):
            self._set(aid, status=S.cancelled, message="已取消，结果未保存")
            return
        try:
            self._persist(aid, report, imp, import_id, mode, profile_d, n_eff, relationship_id)
        except IllegalTransition as e:
            log.warning("analysis %s: %s", aid, e)
        except Exception as e:  # noqa: BLE001
            log.error("analysis %s persist failed: %s", aid, traceback.format_exc())
            self._set(aid, status=S.failed, error_code="persist_error", error_msg=f"{type(e).__name__}", message="结果保存失败")

    def _persist(self, aid: str, report, imp, import_id: str, mode: str, profile_d: dict, n_eff: float, relationship_id: str) -> None:
        from rapport.analysis_context import build_context

        conversation = self.state.load_conversation(imp)
        if conversation is not None:
            report.analysis_context = build_context(self.state, relationship_id, imp, conversation, purpose="review", report=report)
        prev_id, prev_body = self.state.last_report(relationship_id)
        report.playbook.changed_from_report = prev_id
        report.playbook.change_notes = diff_playbook(prev_body, report)
        with self.state.db.session() as s:
            a = s.get(Analysis, aid)
            if not a or a.tombstone or a.cancel_requested:
                return
            s.add(
                ReportRow(
                    id=report.id,
                    relationship_id=relationship_id,
                    analysis_id=aid,
                    import_id=import_id,
                    mode=mode,
                    total=report.affinity.total,
                    body=report.model_dump(mode="json"),
                    profile=profile_d,
                )
            )
            if imp.content_hash:
                self.state.save_snapshot(report, import_id, imp.content_hash, n_eff, relationship_id=relationship_id)
            version = self.state.save_persona_version(report, relationship_id=relationship_id)
            if version:
                row = s.get(ReportRow, report.id)
                if row is not None:
                    body = dict(row.body)
                    body["persona"] = report.persona.model_dump(mode="json")
                    row.body = body
            a.report_id = report.id
            final = (
                S.insufficient_data
                if (report.affinity.total is None and report.affinity.insufficient_reason and mode == "rules" and not report.observations)
                else S.completed
            )
            check_transition(a.status, final.value)
            a.status = final.value
            a.message = "完成"
            a.stage_done, a.stage_total = 1, 1
