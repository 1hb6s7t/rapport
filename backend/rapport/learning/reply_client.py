"""One bounded private inference worker; the model stays loaded until the worker idles out."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import uuid
from pathlib import Path

from rapport.llm.provider import LLMError

# A cold worker loads the base model and adapter before answering; later requests only generate.
WARM_TIMEOUT_S = 600
REQUEST_TIMEOUT_S = 120


class ReplyWorker:
    def __init__(self, service, model_id):
        self.service, self.model_id = service, model_id
        self.proc = None
        self.responses = queue.Queue(maxsize=2)
        self.lock = threading.Lock()

    def _start(self):
        if self.proc is not None and self.proc.poll() is None:
            return False
        self.responses = queue.Queue(maxsize=2)
        env = dict(os.environ, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", TOKENIZERS_PARALLELISM="false", PYTHONIOENCODING="utf-8", OMP_NUM_THREADS="2")
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
        python = env.get("RAPPORT_TRAINING_PYTHON", sys.executable)
        self.proc = subprocess.Popen([python, "-m", "rapport.learning.reply_batch", "--data-dir", str(self.service.data_dir), "--model", self.model_id],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1, env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        process, responses = self.proc, self.responses

        def reader():
            try:
                for line in process.stdout:
                    try:
                        responses.put(json.loads(line), timeout=2)
                    except (ValueError, queue.Full):
                        break
            finally:
                try:
                    responses.put({"error": "worker_exited"}, timeout=2)
                except queue.Full:
                    pass

        threading.Thread(target=reader, daemon=True).start()
        return True

    def request(self, request=None, *, warm=False):
        with self.lock:
            try:
                started = self._start()
                ident = uuid.uuid4().hex
                packet = {"id": ident, "operation": "warm" if warm else "generate", "request": request}
                self.proc.stdin.write(json.dumps(packet, ensure_ascii=False) + "\n")
                self.proc.stdin.flush()
                result = self.responses.get(timeout=WARM_TIMEOUT_S if warm or started else REQUEST_TIMEOUT_S)
                if result.get("error"):
                    raise LLMError("personal_generation_failed", "本地个人模型暂不可用，未切换到云端。")
                if result.get("id") != ident:
                    raise LLMError("personal_protocol_mismatch", "个人回复请求已失效，请重试。")
                return result
            except queue.Empty as exc:
                self.close()
                raise LLMError("personal_timeout", "本地个人模型超时，已停止本次请求。") from exc
            except LLMError:
                self.close()
                raise
            except (OSError, ValueError) as exc:
                self.close()
                raise LLMError("personal_runtime_failed", "本地个人模型进程无法运行，请检查学习中心。") from exc

    def close(self):
        process, self.proc = self.proc, None
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            for stream in (process.stdin, process.stdout):
                if stream:
                    stream.close()


class ReplyWorkerPool:
    def __init__(self, service):
        self.service = service
        self.worker = None
        self.lock = threading.RLock()
        self.status = {"status": "unloaded", "model_id": None}
        self._generation = 0

    def get(self, model_id):
        with self.lock:
            if self.worker is not None and self.worker.model_id != model_id:
                self.worker.close()
                self.worker = None
            if self.worker is None:
                self.worker = ReplyWorker(self.service, model_id)
            return self.worker

    def warm(self, model_id):
        with self.lock:
            generation = self._generation
            self.status = {"status": "loading", "model_id": model_id}
        try:
            result = self.get(model_id).request(warm=True)
            with self.lock:
                if generation == self._generation:
                    self.status = {"status": "ready", "model_id": model_id, "load_seconds": result.get("load_seconds"),
                                   "load_breakdown": result.get("load_breakdown")}
        except LLMError as exc:
            with self.lock:
                if generation == self._generation:
                    self.status = {"status": "error", "model_id": model_id, "error": exc.code}

    def complete(self, model_id, request):
        generation = self._generation
        result = self.get(model_id).request(request)
        with self.lock:
            if generation != self._generation:
                raise LLMError("personal_request_cancelled", "个人模型已切换或停止，本次回复已丢弃。")
            self.status = {"status": "ready", "model_id": model_id}
        return result

    def close(self):
        with self.lock:
            self._generation += 1
            if self.worker:
                self.worker.close()
                self.worker = None
            self.status = {"status": "unloaded", "model_id": None}
