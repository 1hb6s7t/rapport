"""User-selected local draft adapter; independent of research gate results."""
from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

from rapport.llm.provider import CallUsage, LLMError, ProviderInfo

ENDPOINT = 'http://127.0.0.1:' + str(int(os.getenv('RAPPORT_SELECTED_PORT', '17861')))
PRESET = 'selected-local'


def selection(settings):
    if not settings.reply_adapter_manifest:
        return None
    path = Path(settings.reply_adapter_manifest).resolve()
    try:
        record = json.loads(path.read_text('utf-8'))
        if (record['scope'] != 'local_reply_drafts' or not record['training_complete']
                or record['id'] != 'r7-epoch-8-' + record['artifact_hash'][:12]
                or record['selection_basis'] != 'explicit_user_choice_2026-10-02'):
            raise ValueError('Invalid selection')
    except (OSError, KeyError, ValueError) as exc:
        raise LLMError('selected_adapter_invalid', '所选回复权重信息缺失或已变化。') from exc
    return record


class SelectedLocalProvider:
    reasoning = False

    def __init__(self, settings, expected_model):
        record = selection(settings)
        if not record or record['id'] != expected_model:
            raise LLMError('selected_adapter_changed', '回复权重已变化，请重新预览。')
        self.record = record
        root = Path(settings.reply_adapter_manifest).resolve().parent.parent
        self.token = (root / '.runtime-token').read_text('utf-8').strip()
        self.info = ProviderInfo(PRESET, ENDPOINT, record['id'], '本机', '本机', 'n/a',
                                 {'scope': 'local_reply_drafts', 'quality_gate_passed': False})

    def _call(self, method, path, body=None):
        try:
            # Loading the selected 8B weights plus generation can exceed three
            # minutes on a busy 16 GB GPU. Keep connection failures fast while
            # allowing a bounded wait for the local inference result.
            with httpx.Client(base_url=ENDPOINT, timeout=httpx.Timeout(600, connect=10, write=30, pool=30),
                              trust_env=False, follow_redirects=False) as client:
                response = client.request(method, path, json=body, headers={'Authorization': 'Bearer ' + self.token})
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise LLMError('selected_adapter_unavailable', '本地回复服务尚未就绪，请运行启动脚本。') from exc
        if response.status_code != 200:
            code = data.get('error', 'selected_adapter_failed')
            messages = {'gpu_busy': '显卡或本地模型正被其他任务使用，请稍后重试。',
                        'prompt_too_long': '当前上下文超过模型容量，请缩小回复范围。',
                        'truncated': '这次回复未完整生成，请调整输入后重新预览。'}
            raise LLMError(code, messages.get(code, '本地回复服务未完成请求，请查看运行状态。'))
        if data.get('model') != self.record['id'] or data.get('artifact_hash') != self.record['artifact_hash']:
            raise LLMError('selected_adapter_changed', '服务中的权重与预览不一致，请重新启动服务。')
        return data

    def ping(self):
        try:
            self._call('GET', '/health')
            return True, '第 8 轮回复权重已选定'
        except LLMError as exc:
            return False, exc.msg

    def complete_json(self, system, user, max_tokens=400, temperature=.8):
        from rapport.agent.skill import MARKER
        body = {'system': system, 'user': user, 'max_tokens': max_tokens, 'temperature': temperature, 'model': self.record['id']}
        if MARKER in system:
            body['max_input_tokens'] = 8192
        result = self._call('POST', '/complete', body)
        return result['data'], CallUsage(**result['usage'])
