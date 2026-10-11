"""Private adapter requests stay in a local subprocess and use the ordinary reply validator."""
import json

from rapport.llm.provider import CallUsage, LLMError, ProviderInfo


class PersonalReplyProvider:
    reasoning = False

    def __init__(self, service, model_id):
        self.service, self.model_id = service, model_id
        self.info = ProviderInfo("personal", "local://personal", model_id, "本机", "本机", "n/a")

    def ping(self):
        model = self.service.model(self.model_id)
        return model["state"] == "active", "个人模型已启用" if model["state"] == "active" else "个人模型未启用"

    def complete_json(self, system, user, max_tokens=512, temperature=.2):
        from rapport.llm.personal_request import SYSTEM

        try:
            if system != SYSTEM:
                raise LLMError("personal_protocol_mismatch", "本地回复协议不匹配，请重新预览。")
            request = json.loads(user)
            value = self.service.reply_workers().complete(self.model_id, request)
            return value["data"], CallUsage(value["prompt_tokens"], value["completion_tokens"], 1)
        except (OSError, ValueError, KeyError) as exc:
            raise LLMError("personal_runtime_failed", "个人模型运行失败或超时，请检查学习中心的环境状态。") from exc
