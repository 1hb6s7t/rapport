"""Serialize local inference against optional training and OCR workers."""
from rapport.learning.gpu import GPUBusy, GPULease
from rapport.llm.provider import LLMError


class GPUGuardedProvider:
    def __init__(self, inner, data_dir, before_call=None):
        self.inner, self.data_dir = inner, data_dir
        self.before_call = before_call

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def complete_json(self, *args, **kwargs):
        try:
            if self.before_call:
                self.before_call()
            with GPULease(self.data_dir, "local-inference"):
                return self.inner.complete_json(*args, **kwargs)
        except GPUBusy as exc:
            raise LLMError("gpu_busy", "本地显卡正被训练或识别任务使用，请稍后生成。") from exc

    def ping(self):
        try:
            if self.before_call:
                self.before_call()
            with GPULease(self.data_dir, "local-model-check"):
                return self.inner.ping()
        except GPUBusy:
            return False, "本地显卡正被其他任务使用"
        except LLMError as exc:
            return False, exc.msg
