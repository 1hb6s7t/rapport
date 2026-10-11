"""Persistent jobs and model registry; no automatic promotion or arbitrary commands."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from rapport.errors import RapportError
from rapport.learning.catalog import research_catalog
from rapport.security.crypto import Crypto

FINAL = {"completed", "failed", "cancelled", "interrupted"}
KINDS = {"dataset_build", "archive_prepare", "train", "evaluate"}
DEFAULT_SCHEDULE = {"enabled": False, "dataset_id": None, "weekly_day": 0, "min_new_samples": 200,
                    "pool": "offline_review", "architecture": "shared", "last_inspected_at": None,
                    "last_trained_at": None, "last_sample_count": 0, "base_model_id": None}


def _id(value: str):
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}", value):
        raise RapportError("learning_invalid_id", "无效的任务、数据集或模型标识", status=400)
    return value


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def worker_env() -> dict:
    """Environment for isolated training/evaluation workers: offline, no tokenizer threads."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    env["HF_HUB_OFFLINE"] = "1"
    env["TRANSFORMERS_OFFLINE"] = "1"
    env["TOKENIZERS_PARALLELISM"] = "false"
    env["OMP_NUM_THREADS"] = "2"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        import ctypes

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.restype = ctypes.c_void_p
        k.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        k.CloseHandle.argtypes = [ctypes.c_void_p]
        k.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        h = k.OpenProcess(0x1000, False, pid)
        if h:
            code = ctypes.c_ulong()
            running = bool(k.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259
            k.CloseHandle(h)
            return running
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class LearningService:
    def __init__(self, data_dir: Path, *, spawn_jobs: bool = True):
        self.data_dir = Path(data_dir).resolve()
        self.root = self.data_dir / "learning"
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "models").mkdir(exist_ok=True)
        (self.root / "base_models").mkdir(exist_ok=True)
        self.crypto = Crypto(self.data_dir / ".key")
        self.spawn_jobs = spawn_jobs
        self._children = {}
        self._stop = threading.Event()
        self._thread = None
        with self.db() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS models (id TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS settings (id TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, at REAL, action TEXT, object_id TEXT);
            """)

    @contextmanager
    def db(self):
        c = sqlite3.connect(self.root / "learning.db", timeout=30)
        c.execute("PRAGMA journal_mode=WAL")
        try:
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def _get(self, c, table, ident):
        row = c.execute(f"SELECT body FROM {table} WHERE id=?", (ident,)).fetchone()
        return self._decode_record(row[0]) if row else None

    def _decode_record(self, raw):
        record = json.loads(raw)
        if set(record) == {"encrypted_record"}:
            return json.loads(self.crypto.decrypt(record["encrypted_record"].encode()))
        return record  # Existing non-private registry metadata is migrated on next update.

    def _put(self, c, table, record):
        value = json.dumps(record, ensure_ascii=False)
        if table == "models":
            value = json.dumps({"encrypted_record": self.crypto.encrypt(value.encode()).decode()})
        c.execute(f"INSERT OR REPLACE INTO {table}(id,body) VALUES(?,?)", (record["id"], value))

    def _event(self, c, action, ident):
        c.execute("INSERT INTO events(at,action,object_id) VALUES(?,?,?)", (time.time(), action, ident))

    def datasets(self):
        from rapport.datasets import DatasetStore

        return DatasetStore(self.data_dir / "datasets", self.crypto)

    def capabilities(self):
        optional = ("torch", "transformers", "peft", "accelerate")
        missing = [name for name in optional if importlib.util.find_spec(name) is None]
        python = os.environ.get("RAPPORT_TRAINING_PYTHON")
        if python and Path(python).is_file():
            if not hasattr(self, "_runtime_probe"):
                try:
                    result = subprocess.run([python, "-c", "import importlib.util,json; print(json.dumps([m for m in ['torch','transformers','peft','accelerate'] if importlib.util.find_spec(m) is None]))"],
                                            capture_output=True, timeout=20, text=True,
                                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                    self._runtime_probe = json.loads(result.stdout) if result.returncode == 0 else list(optional)
                except (OSError, subprocess.TimeoutExpired, ValueError):
                    self._runtime_probe = list(optional)
            missing = self._runtime_probe
        bases = []
        for p in (self.root / "base_models").iterdir():
            if p.is_dir() and not p.is_symlink():
                ready = (p / "config.json").is_file() and bool(list(p.glob("*.safetensors")) or list(p.glob("pytorch_model*.bin")))
                bases.append({"id": p.name, "ready": ready})
        return {"cpu_baseline": True, "lora_dependencies": not missing, "missing_dependencies": missing,
                "cloud_training": False, "local_base_models": bases, "paper_reproductions": False,
                "reply_adapter_runtime": not missing}

    def overview(self):
        from rapport.learning.research_view import interaction_summaries, summaries

        return {"capabilities": self.capabilities(), "papers": research_catalog(), "schedules": self.schedule(),
                "jobs": self.jobs(), "models": self.models(), "personal_runtime": self.reply_workers().status,
                "research_runs": summaries(self.data_dir), "interaction_studies": interaction_summaries(self.data_dir)}

    def reply_workers(self):
        from rapport.learning.reply_client import ReplyWorkerPool

        if not hasattr(self, "_reply_pool"):
            self._reply_pool = ReplyWorkerPool(self)
        return self._reply_pool

    def jobs(self):
        self.reconcile()
        with self.db() as c:
            return sorted([json.loads(x[0]) for x in c.execute("SELECT body FROM jobs")], key=lambda x: x["created_at"], reverse=True)

    def job(self, ident):
        with self.db() as c:
            record = self._get(c, "jobs", _id(ident))
        if not record:
            raise RapportError("learning_job_missing", "任务不存在", status=404)
        return record

    def create_job(self, kind: str, *, dataset_id=None, model_id=None, pool="offline_review", architecture="shared",
                   max_features=12000, epochs=2, base_model_id=None, retry_of=None,
                   max_train_samples=None, max_eval_samples=64, eval_offset=0, source_mode="dataset", relationship_ids=None,
                   scheduled=False, schedule_revision=None, parent_model_id=None):
        from rapport.learning.incremental import sample_keys, seal, validate_scope

        if kind not in KINDS or pool not in {"offline_review", "realtime_review", "personal_reply"} or architecture not in {"shared", "separate", "reply_lora"}:
            raise RapportError("learning_invalid_job", "不支持的训练任务配置", status=400)
        if not 100 <= max_features <= 50000 or not 1 <= epochs <= 10:
            raise RapportError("learning_invalid_budget", "训练预算超出本地任务限制", status=400)
        if (max_train_samples is not None and not 20 <= max_train_samples <= 50000) or not 5 <= max_eval_samples <= 2000:
            raise RapportError("learning_invalid_budget", "样本预算超出限制", status=400)
        if kind == "evaluate":
            model = self.model(_id(model_id or ""))
            if model["state"] == "revoked":
                raise RapportError("learning_model_revoked", "来源已删除，模型不可重新评测启用", status=409)
            dataset_id, pool, architecture = model["dataset_id"], model["pool"], model["architecture"]
        if parent_model_id is not None:
            # A preference round continues a personal adapter on its own dataset and base.
            parent = self.model(_id(parent_model_id))
            if kind != "train" or parent.get("task") != "reply" or parent["state"] == "revoked":
                raise RapportError("learning_invalid_parent", "偏好训练只能接在未撤销的个人回复模型之后", status=400)
            dataset_id, pool, architecture, base_model_id = parent["dataset_id"], "personal_reply", "reply_lora", parent["base_model_id"]
        if source_mode not in {"dataset", "archives"}:
            raise RapportError("learning_invalid_source", "未知的训练数据来源。", status=422)
        scope = []
        if kind == "archive_prepare" or source_mode == "archives":
            scope = validate_scope(self, relationship_ids)
            source_mode = "archives"
        if kind not in {"dataset_build", "archive_prepare"} or dataset_id is not None:
            selected_dataset = self.datasets().get(_id(dataset_id or ""))
            if architecture == "reply_lora":
                from rapport.datasets.corpus import VERSION as DATASET_VERSION

                if selected_dataset.get("version") != DATASET_VERSION:
                    raise RapportError("dataset_policy_stale", "个人回复模型的数据集使用过期保护规则，请重新构建。", status=409)
        if architecture == "reply_lora" and kind == "train":
            if pool != "personal_reply":
                raise RapportError("learning_reply_pool", "个人回复训练只能使用本人表达数据集", status=400)
            _id(base_model_id or "")
            base = self.root / "base_models" / base_model_id
            if base.is_symlink() or not (base / "config.json").is_file():
                raise RapportError("learning_base_missing", "尚未注册可训练的本地 Hugging Face 基座", status=400)
            if not self.capabilities()["lora_dependencies"]:
                raise RapportError("learning_dependencies_missing", "尚未安装本地 LoRA 训练依赖，请查看学习中心的运行状态", status=409)
        elif kind != "archive_prepare" and architecture != "reply_lora" and pool == "personal_reply":
            raise RapportError("learning_task_mismatch", "文字回复样本不能用于情绪分类器", status=400)
        ident = uuid.uuid4().hex
        record = {"id": ident, "kind": kind, "dataset_id": dataset_id, "model_id": model_id, "pool": pool,
                  "architecture": architecture, "max_features": max_features, "epochs": epochs, "base_model_id": base_model_id,
                  "max_train_samples": max_train_samples, "max_eval_samples": max_eval_samples, "eval_offset": int(eval_offset or 0),
                  "status": "queued", "progress": 0.0, "stage": "等待独立训练进程", "created_at": time.time(),
                  "updated_at": time.time(), "pid": None, "cancel_requested": False, "result": None, "error": None,
                  "retry_of": retry_of, "checkpoint": None, "source_mode": source_mode,
                  "archive_scope": seal(self, scope), "scheduled": bool(scheduled), "schedule_revision": schedule_revision,
                  "parent_model_id": parent_model_id}
        if kind == "train":
            train_rows = self.datasets().records(dataset_id, pool)
            if architecture == "reply_lora" and max_train_samples:
                import random

                from rapport.datasets.corpus import reply_split

                train_rows = [row for row in train_rows if reply_split(row) == "train" and row.get("training_eligible") and row.get("response")]
                random.Random(20260922).shuffle(train_rows)
                train_rows = train_rows[:max_train_samples]
            record["training_sample_keys"] = seal(self, sorted(sample_keys(train_rows)))
        with self.db() as c:
            self._put(c, "jobs", record)
            self._event(c, "job_created", ident)
        if self.spawn_jobs:
            self._spawn(ident)
        return self.job(ident)

    def _spawn(self, ident):
        env = worker_env()
        kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
        try:
            job = self.job(ident)
            python = os.environ.get("RAPPORT_TRAINING_PYTHON", sys.executable) if job["architecture"] == "reply_lora" and job["kind"] != "archive_prepare" else sys.executable
            proc = subprocess.Popen([python, "-m", "rapport.learning.worker", "--data-dir", str(self.data_dir), "--job", ident],
                                    env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
            self._children[ident] = proc
            with self.db() as c:
                c.execute("BEGIN IMMEDIATE")
                record = self._get(c, "jobs", ident)
                if record["status"] == "queued":
                    record["pid"] = proc.pid
                    self._put(c, "jobs", record)
        except OSError:
            self.update_job(ident, status="failed", error="独立训练进程无法启动", stage="启动失败")

    def claim(self, ident):
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            record = self._get(c, "jobs", ident)
            if not record or record["status"] != "queued" or record["cancel_requested"]:
                return False
            record.update(status="running", pid=os.getpid(), updated_at=time.time(), stage="载入数据")
            self._put(c, "jobs", record)
        return True

    def update_job(self, ident, **changes):
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            record = self._get(c, "jobs", ident)
            if not record:
                return
            record.update(changes, updated_at=time.time())
            self._put(c, "jobs", record)

    def cancel(self, ident):
        record = self.job(ident)
        if record["status"] in FINAL:
            return record
        self.update_job(ident, cancel_requested=True, stage="取消请求已记录",
                        **({"status": "cancelled"} if record["status"] == "queued" else {}))
        return self.job(ident)

    def retry(self, ident):
        from rapport.learning.incremental import job_scope

        job = self.job(ident)
        if job["status"] not in {"cancelled", "failed", "interrupted"}:
            raise RapportError("learning_retry_state", "只有取消、失败或中断的任务可以重试", status=409)
        args = {k: job[k] for k in ("dataset_id", "model_id", "pool", "architecture", "max_features", "epochs", "base_model_id")}
        args.update(max_train_samples=job.get("max_train_samples"), max_eval_samples=job.get("max_eval_samples", 64))
        args.update(source_mode=job.get("source_mode", "dataset"), relationship_ids=job_scope(self, job))
        return self.create_job(job["kind"], retry_of=ident, **args)

    def reconcile(self):
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            for row in c.execute("SELECT body FROM jobs").fetchall():
                job = json.loads(row[0])
                if job["status"] in {"running", "queued"} and time.time() - job["updated_at"] > 30 and not _alive(job.get("pid")):
                    job.update(status="interrupted", stage="训练进程已退出，可从已完成阶段重试", updated_at=time.time())
                    self._put(c, "jobs", job)

    def save_artifact(self, model_id, weights):
        p = self.root / "models" / _id(model_id)
        p.mkdir(parents=True, exist_ok=True)
        data = json.dumps(weights, ensure_ascii=False, sort_keys=True).encode()
        tmp = p / "weights.tmp"
        tmp.write_bytes(self.crypto.encrypt(data))
        tmp.replace(p / "weights.bin")
        return hashlib.sha256(data).hexdigest()

    def load_artifact(self, model):
        p = self.root / "models" / _id(model["id"]) / "weights.bin"
        if not p.exists():
            raise RapportError("learning_artifact_missing", "模型权重文件缺失", status=409)
        raw = self.crypto.decrypt(p.read_bytes())
        if hashlib.sha256(raw).hexdigest() != model["artifact_hash"]:
            raise RapportError("learning_artifact_changed", "模型权重校验失败", status=409)
        return raw if model.get("artifact_format") == "encrypted_lora_zip" else json.loads(raw)

    def register(self, record):
        with self.db() as c:
            self._put(c, "models", record)
            self._event(c, "model_registered", record["id"])
        return record

    def models(self):
        with self.db() as c:
            records = sorted([self._decode_record(x[0]) for x in c.execute("SELECT body FROM models")], key=lambda x: x["created_at"], reverse=True)
        for model in records:
            model["personal_trial_available"] = self._personal_trial_available(model)
        return records

    def _personal_trial_available(self, model):
        from rapport.datasets.corpus import VERSION as DATASET_VERSION
        from rapport.llm.personal_request import FACT_POLICY, VERSION

        gate, metrics = model.get("gate", {}), model.get("metrics", {}).get("test", {})
        try:
            current_dataset = self.datasets().get(model["dataset_id"]).get("version") == DATASET_VERSION
        except (RapportError, KeyError):
            current_dataset = False
        return bool(model.get("task") == "reply" and model.get("state") != "revoked" and model.get("runtime_supported")
                    and current_dataset
                    and gate.get("automatic_passed") and set(gate.get("reasons", [])) <= {"personal_generation_review_required"}
                    and metrics.get("generation_protocol") == VERSION and metrics.get("validation_policy") == FACT_POLICY)

    def model(self, ident):
        with self.db() as c:
            record = self._get(c, "models", _id(ident))
        if not record:
            raise RapportError("learning_model_missing", "模型版本不存在", status=404)
        return record

    def evaluated(self, ident, metrics, gate):
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            model = self._get(c, "models", ident)
            if model["state"] == "revoked":
                raise RapportError("learning_model_revoked", "数据来源已删除，评测结果不再可启用", status=409)
            if model["metrics"].get("test"):
                model.setdefault("evaluation_history", []).append({"metrics": model["metrics"]["test"], "gate": model.get("gate"), "at": model.get("evaluated_at")})
                model["evaluation_history"] = model["evaluation_history"][-20:]
            model["metrics"]["test"] = metrics
            model["evaluated_at"] = time.time()
            model.pop("reply_review", None)
            model["gate"] = gate
            model["state"] = "evaluated"
            self._put(c, "models", model)
            self._event(c, "model_evaluated", ident)
        return model

    def activate(self, ident, *, confirm: bool, rollback=False, personal_trial=False):
        if not confirm:
            raise RapportError("learning_confirmation_required", "需要确认启用此模型版本", status=400)
        model = self.model(ident)
        trial_allowed = personal_trial and self._personal_trial_available(model)
        if model["state"] == "revoked" or (not model.get("gate", {}).get("passed") and not trial_allowed) or not model.get("runtime_supported"):
            raise RapportError("learning_gate_failed", "模型尚未通过独立评测或本地运行条件，无法启用", status=409)
        if model["task"] == "reply":
            from rapport.llm.personal_request import FACT_POLICY, VERSION

            if model.get("metrics", {}).get("test", {}).get("generation_protocol") != VERSION:
                raise RapportError("learning_runtime_changed", "个人回复运行方式已更新，请重新评测此版本。", status=409)
            if model.get("metrics", {}).get("test", {}).get("validation_policy") != FACT_POLICY:
                raise RapportError("learning_runtime_changed", "本人事实校验策略已更新，请重新评测此版本。", status=409)
            if not trial_allowed and model.get("reply_review", {}).get("evaluation_hash") != self.evaluation_bundle(ident)["evaluation_hash"]:
                raise RapportError("learning_review_stale", "请复核最新回复样例后再启用。", status=409)
        dataset = self.datasets().get(model["dataset_id"])
        if model["task"] == "reply":
            from rapport.datasets.corpus import VERSION as DATASET_VERSION

            if dataset.get("version") != DATASET_VERSION:
                raise RapportError("dataset_policy_stale", "该候选使用过期的数据保护规则，不能启用。", status=409)
        self.load_artifact(model)
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            current = self._get(c, "models", ident)
            if current["state"] == "revoked":
                raise RapportError("learning_model_revoked", "数据来源已删除", status=409)
            for row in c.execute("SELECT body FROM models").fetchall():
                other = self._decode_record(row[0])
                if other["task"] == model["task"] and other["state"] == "active" and other.get("scope") == model.get("scope"):
                    other["state"] = "evaluated"
                    self._put(c, "models", other)
            if current.get("evaluated_at") != model.get("evaluated_at") or current.get("gate") != model.get("gate"):
                raise RapportError("learning_model_changed", "模型评测状态已变化，请刷新后再确认。", status=409)
            current.update(state="active", activated_at=time.time(), activation_scope="personal_trial" if trial_allowed else "reviewed")
            self._put(c, "models", current)
            self._event(c, "model_rollback" if rollback else "model_activated", ident)
        if current["task"] == "reply":
            self.reply_workers().close()
            threading.Thread(target=self.reply_workers().warm, args=(ident,), daemon=True).start()
        return current

    def deactivate(self, ident):
        with self.db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record = self._get(connection, "models", _id(ident))
            if not record:
                raise RapportError("learning_model_missing", "模型不存在", status=404)
            if record["state"] == "active":
                record["state"] = "evaluated"
                self._put(connection, "models", record)
                self._event(connection, "model_deactivated", ident)
        pool = getattr(self, "_reply_pool", None)
        if pool and pool.worker and pool.worker.model_id == ident:
            pool.close()
        return record

    def active_model(self, task="analysis", relationship_id=None):
        # Global personal model uses anonymised multi-friend examples; never a friend identity predictor.
        return next((m for m in self.models() if m["state"] == "active" and m["task"] == task), None)

    def evaluation_samples(self, ident):
        return self.evaluation_bundle(ident)["items"]

    def evaluation_bundle(self, ident):
        model = self.model(ident)
        path = self.root / "models" / model["id"] / "evaluation.bin"
        if model["state"] == "revoked" or not path.is_file():
            raise RapportError("learning_evaluation_missing", "尚无可复核的回复评测样例", status=409)
        raw = self.crypto.decrypt(path.read_bytes())
        return {"items": json.loads(raw), "evaluation_hash": hashlib.sha256(raw).hexdigest()}

    def review_replies(self, ident, decisions, evaluation_hash=None, *, reviewer="user"):
        if reviewer not in {"user", "delegated_assistant"}:
            raise RapportError("learning_review_invalid", "复核来源无效", status=400)
        model = self.model(ident)
        bundle = self.evaluation_bundle(ident)
        if evaluation_hash is not None and evaluation_hash != bundle["evaluation_hash"]:
            raise RapportError("learning_review_stale", "评测样例已更新，请重新打开并复核最新回复。", status=409)
        samples = bundle["items"]
        expected = {sample["id"] for sample in samples}
        if set(decisions) != expected or len(expected) < 10:
            raise RapportError("learning_review_incomplete", "请逐条复核本轮全部回复样例（至少10条）", status=409)
        if any(not isinstance(value, bool) for value in decisions.values()):
            raise RapportError("learning_review_invalid", "复核值无效", status=400)
        accepted = sum(decisions.values())
        gate = dict(model["gate"])
        reasons = [r for r in gate["reasons"] if r not in {"personal_generation_review_required", "personal_generation_review_failed"}]
        if accepted / len(expected) < .9:
            reasons.append("personal_generation_review_failed")
        gate.update(passed=not reasons, reasons=reasons, reviewed_samples=len(expected), accepted_samples=accepted,
                    quality_review_source=reviewer)
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            current = self._get(c, "models", ident)
            if current["state"] == "revoked":
                raise RapportError("learning_model_revoked", "来源已撤销", status=409)
            if current.get("evaluated_at") != model.get("evaluated_at") or self.evaluation_bundle(ident)["evaluation_hash"] != bundle["evaluation_hash"]:
                raise RapportError("learning_review_stale", "评测样例已更新，请重新复核。", status=409)
            current.update(gate=gate, reply_review={"at": time.time(), "decisions": decisions,
                                                    "evaluation_hash": bundle["evaluation_hash"], "reviewer": reviewer})
            self._put(c, "models", current)
        return current

    def predict_analysis(self, texts: list[str], relationship_id=None):
        from rapport.learning.baseline import predict

        model = self.active_model("analysis", relationship_id)
        if not model:
            return {"available": False, "predictions": []}
        if model["pool"] != "realtime_review":
            return {"available": False, "reason": "offline_only", "predictions": []}
        return {"available": True, "model_id": model["id"], "uncalibrated": True,
                "predictions": predict(self.load_artifact(model), texts)}

    def invalidate_sources(self, source_ids: list[str]):
        targets = set(source_ids)
        revoked = []
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            for row in c.execute("SELECT body FROM models").fetchall():
                model = self._decode_record(row[0])
                if targets.intersection(model.get("source_ids", [])):
                    model.update(state="revoked", revoked_reason="source_deleted", gate={"passed": False, "reasons": ["source_deleted"]})
                    self._put(c, "models", model)
                    self._event(c, "model_revoked", model["id"])
                    revoked.append(model["id"])
        pool = getattr(self, "_reply_pool", None)
        if pool and pool.worker and pool.worker.model_id in revoked:
            pool.close()

    def revoke_dataset(self, dataset_id):
        for model in self.models():
            if model["dataset_id"] == dataset_id:
                self.invalidate_sources(model.get("source_ids", []) + [f"dataset:{dataset_id}"])
        for job in self.jobs():
            if job.get("dataset_id") == dataset_id and job["status"] not in FINAL:
                self.cancel(job["id"])

    def revoke_relationship(self, relationship_id):
        self.invalidate_sources([f"relationship:{relationship_id}"])
        with self.db() as c:
            for row in c.execute("SELECT body FROM models").fetchall():
                model = self._decode_record(row[0])
                if model.get("relationship_id") == relationship_id:
                    model.update(state="revoked", gate={"passed": False, "reasons": ["relationship_deleted"]})
                    self._put(c, "models", model)

    def wipe_all(self):
        """Cancel workers first; never leave a running worker able to recreate private weights."""
        self.reply_workers().close()
        self.set_schedule({"enabled": False})
        pending = [j for j in self.jobs() if j["status"] not in FINAL]
        for job in pending:
            self.cancel(job["id"])
        deadline = time.monotonic() + 8
        while any(_alive(j.get("pid")) for j in pending) and time.monotonic() < deadline:
            time.sleep(.1)
        if any(_alive(j.get("pid")) for j in pending):
            raise RapportError("learning_stop_pending", "训练正在停止，请稍后再次清空资料。", status=409)
        transient = [*self.root.glob("adapter-*"), *self.root.glob("rapport-lora-*")]
        for folder in transient:
            for path in [folder, *folder.rglob("*")]:
                if path.is_symlink() or path.is_junction() or not path.resolve().is_relative_to(self.root.resolve()):
                    raise RapportError("learning_cleanup_path", "临时模型目录包含非托管路径，未清理。", status=409)
        root = (self.root / "models").resolve()
        paths, folders = [], []
        for folder in root.iterdir():
            if folder.is_symlink() or folder.is_junction() or not folder.is_dir() or not folder.resolve().is_relative_to(root):
                raise RapportError("learning_cleanup_path", "模型目录包含非托管路径，未清理。", status=409)
            for p in folder.iterdir():
                if p.is_symlink() or not p.is_file() or p.name not in {"weights.bin", "weights.tmp", "evaluation.bin", "evaluation.tmp"}:
                    raise RapportError("learning_cleanup_path", "模型目录包含未知内容，未清理。", status=409)
                paths.append(p)
            folders.append(folder)
        for p in paths:
            p.unlink(missing_ok=True)
        for folder in folders:
            folder.rmdir()
        for folder in transient:
            shutil.rmtree(folder)
        with self.db() as c:
            for table in ("jobs", "models", "settings", "events"):
                c.execute(f"DELETE FROM {table}")
        return {"models": len(folders)}

    def schedule(self):
        from rapport.learning.incremental import get_schedule

        return get_schedule(self)

    def set_schedule(self, values):
        from rapport.learning.incremental import set_schedule

        return set_schedule(self, values)

    def tick(self, now=None):
        from rapport.learning.incremental import tick

        return tick(self, now)

    def start_scheduler(self):
        if self._thread and self._thread.is_alive():
            return

        def loop():
            while not self._stop.wait(30):
                try:
                    self.tick()
                except Exception:  # no data or model content logged by background scheduler
                    pass

        self._thread = threading.Thread(target=loop, name="learning-schedule", daemon=True)
        self._thread.start()

    def close(self):
        self.reply_workers().close()
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
