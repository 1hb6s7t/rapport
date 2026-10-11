"""Private inference lifecycle tests: mocks only, no CUDA or private dataset access."""

from __future__ import annotations

import io
import json
import queue
import sys
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from rapport.learning.reply_batch import BatchRuntime
from rapport.learning.reply_client import ReplyWorker, ReplyWorkerPool
from rapport.learning.service import LearningService
from rapport.llm.provider import LLMError


class Process:
    def __init__(self):
        self.alive = True
        self.stdin = io.StringIO()
        self.stdout = io.StringIO()
        self.terminate = Mock(side_effect=self.stop)
        self.kill = Mock(side_effect=self.stop)
        self.wait = Mock(return_value=0)

    def stop(self):
        self.alive = False

    def poll(self):
        return None if self.alive else 0


def ready_worker(tmp_path, monkeypatch, response="ok"):
    worker = ReplyWorker(SimpleNamespace(data_dir=tmp_path), "candidate-1")
    process = Process()
    worker.proc = process
    monkeypatch.setattr(worker, "_start", lambda: None)

    def get(timeout):
        if response == "timeout":
            raise queue.Empty
        if response == "exit":
            process.alive = False
            return {"error": "worker_exited"}
        packet = json.loads(process.stdin.getvalue().splitlines()[-1])
        if response == "mismatch":
            return {"id": "wrong-request", "data": {}}
        if response == "error":
            return {"error": "ValueError", "message": "synthetic"}
        return {"id": packet["id"], "data": {"drafts": []}, "prompt_tokens": 2, "completion_tokens": 0}

    monkeypatch.setattr(worker.responses, "get", get)
    return worker, process


def test_two_requests_reuse_process_and_distinct_request_ids(tmp_path, monkeypatch):
    from rapport.learning import reply_client

    worker, process = ready_worker(tmp_path, monkeypatch)
    # Exercise real start's alive-process guard: it must not spawn between requests.
    monkeypatch.setattr(worker, "_start", lambda: ReplyWorker._start(worker))
    spawn = Mock(side_effect=AssertionError("existing worker should be reused"))
    monkeypatch.setattr(reply_client.subprocess, "Popen", spawn)
    worker.request({"one": 1})
    worker.request({"two": 2})
    packets = [json.loads(line) for line in process.stdin.getvalue().splitlines()]
    assert len(packets) == 2 and packets[0]["id"] != packets[1]["id"]
    assert worker.proc is process and spawn.call_count == 0
    worker.close()
    assert process.stdin.closed and process.stdout.closed


@pytest.mark.parametrize("response,code", [("timeout", "personal_timeout"), ("exit", "personal_generation_failed"),
                                          ("error", "personal_generation_failed"), ("mismatch", "personal_protocol_mismatch")])
def test_failed_worker_request_closes_process_and_streams(tmp_path, monkeypatch, response, code):
    worker, process = ready_worker(tmp_path, monkeypatch, response)
    with pytest.raises(LLMError) as caught:
        worker.request({"synthetic": True})
    assert caught.value.code == code
    assert worker.proc is None
    assert process.stdin.closed and process.stdout.closed
    assert not process.alive


def test_worker_pool_closes_previous_model_before_switch(tmp_path):
    pool = ReplyWorkerPool(SimpleNamespace(data_dir=tmp_path))
    first = pool.get("candidate-a")
    first.close = Mock()
    assert pool.get("candidate-a") is first
    second = pool.get("candidate-b")
    assert second is not first and second.model_id == "candidate-b"
    first.close.assert_called_once()
    second.close = Mock()
    pool.close()
    second.close.assert_called_once()
    assert pool.worker is None and pool.status["status"] == "unloaded"


def test_obsolete_warm_response_cannot_restore_closed_model_status(tmp_path, monkeypatch):
    import threading

    pool = ReplyWorkerPool(SimpleNamespace(data_dir=tmp_path))
    entered, finish = threading.Event(), threading.Event()

    def delayed_request(**kwargs):
        entered.set()
        assert finish.wait(2)
        return {"load_seconds": 1}

    worker = SimpleNamespace(model_id="candidate-a", request=delayed_request, close=Mock())
    monkeypatch.setattr(pool, "get", lambda ident: worker)
    thread = threading.Thread(target=pool.warm, args=("candidate-a",))
    thread.start()
    assert entered.wait(2)
    pool.close()
    finish.set()
    thread.join(2)
    assert not thread.is_alive()
    assert pool.status == {"status": "unloaded", "model_id": None}


def test_obsolete_completion_cannot_restore_closed_model_status(tmp_path, monkeypatch):
    import threading

    pool = ReplyWorkerPool(SimpleNamespace(data_dir=tmp_path))
    entered, finish = threading.Event(), threading.Event()
    outcomes = []

    def delayed_request(request):
        entered.set()
        assert finish.wait(2)
        return {"data": {"drafts": []}}

    worker = SimpleNamespace(model_id="candidate-a", request=delayed_request, close=Mock())
    monkeypatch.setattr(pool, "get", lambda ident: worker)

    def complete():
        try:
            outcomes.append(pool.complete("candidate-a", {}))
        except LLMError as error:
            outcomes.append(error)

    thread = threading.Thread(target=complete)
    thread.start()
    assert entered.wait(2)
    pool.close()
    finish.set()
    thread.join(2)
    assert not thread.is_alive()
    assert len(outcomes) == 1
    assert isinstance(outcomes[0], LLMError) and outcomes[0].code == "personal_request_cancelled"
    assert pool.status == {"status": "unloaded", "model_id": None}


def test_resident_runtime_is_reused_and_gpu_lease_released_on_failure(tmp_path, monkeypatch):
    from rapport.learning import reply_batch

    events = []
    model = SimpleNamespace(to=lambda device: events.append(("move", device)), eval=lambda: events.append("eval"))

    @contextmanager
    def lease(*args):
        events.append("acquire")
        try:
            yield
        finally:
            events.append("release")

    monkeypatch.setattr(reply_batch, "GPULease", lease)
    runtime = BatchRuntime(SimpleNamespace(data_dir=tmp_path), {"id": "model"})
    runtime.model, runtime.tokenizer, runtime.device = model, object(), "cuda"
    runtime._loaded = MagicMock()
    with runtime.on_device():
        events.append("first")
    with pytest.raises(ValueError, match="synthetic"):
        with runtime.on_device():
            raise ValueError("synthetic")
    assert runtime.model is model  # Weights stay loaded on the device, not reloaded.
    assert events.count("acquire") == events.count("release") == 2
    assert not [event for event in events if isinstance(event, tuple)]  # No CPU<->GPU copies per request.
    assert events[-1] == "release"
    loaded = runtime._loaded
    runtime.close()
    loaded.__exit__.assert_called_once()
    assert runtime.model is None and runtime.tokenizer is None


def test_each_style_keeps_its_best_judged_distinct_draft_and_releases_device(tmp_path, monkeypatch):
    from rapport.learning import reply_batch, self_judge
    from rapport.llm.personal_request import DISTINCT_RETRIES, SAMPLES_PER_STYLE, compile_request
    from rapport.llm.reply import ReplyInput

    adapter = {"on": True}

    @contextmanager
    def disable_adapter():
        adapter["on"] = False
        try:
            yield
        finally:
            adapter["on"] = True

    rounds = [
        # casual, sincere, direct: sincere's best duplicates casual's pick, direct has nothing usable yet.
        [[("好好休息呀", True), ("早点睡", True), ("", True)],
         [("好好 休息呀", True), ("辛苦啦", True), ("我在学校呢", False)],
         [("", True), ("", True), ("", True)]],
        [[("那早点睡哈", True), ("嗯嗯", True), ("", True)]],  # only direct is sampled again
    ]
    calls, judged, leases = [], [], []

    def sample(model, tokenizer, prompts, k, device, *, seed):
        calls.append({"styles": len(prompts), "k": k, "seed": seed, "adapter": adapter["on"]})
        groups = rounds[len(calls) - 1]
        assert len(groups) == len(prompts)
        return groups, 10 * len(prompts), 4 * k * len(prompts)

    scores = {"好好休息呀": 2.0, "好好 休息呀": 3.0, "早点睡": 1.0, "辛苦啦": .5, "我在学校呢": 4.0, "那早点睡哈": -6.0, "嗯嗯": -7.0}

    def score(model, tokenizer, lines, drafts, device):
        judged.append((adapter["on"], list(lines), list(drafts)))
        return [scores[d] for d in drafts]

    @contextmanager
    def lease(*args):
        leases.append("acquire")
        try:
            yield
        finally:
            leases.append("release")

    monkeypatch.setattr(reply_batch, "GPULease", lease)
    monkeypatch.setattr(reply_batch, "sample_shared", sample)
    monkeypatch.setattr(self_judge, "score_drafts", score)
    runtime = BatchRuntime(SimpleNamespace(data_dir=tmp_path), {"id": "model"})
    runtime.model = SimpleNamespace(disable_adapter=disable_adapter)
    runtime.tokenizer = object()
    packet = compile_request(ReplyInput(lines=["m000012 09-26 14:32 B: 累啦"], styles=["casual", "sincere", "direct"], memories=[], goal="回应"))
    result = runtime.complete(packet)
    drafts = {draft["style"]: draft for draft in result["data"]["drafts"]}
    # sincere's best, "好好 休息呀", is casual's pick again once spaces are ignored, so sincere takes its next best.
    assert drafts["casual"]["text"] == "好好休息呀"
    assert drafts["sincere"]["text"] == "我在学校呢"  # the best distinct draft; truncated, so flagged
    assert drafts["sincere"]["risk_notes"] == ["回复到达长度上限，请核对句意是否完整。"] and result["truncated_styles"] == ["sincere"]
    assert drafts["direct"]["text"] == "那早点睡哈" and "本地自评" in drafts["direct"]["risk_notes"][0]  # low judged score is flagged
    assert [c["styles"] for c in calls] == [3, 1] and all(c["k"] == SAMPLES_PER_STYLE and c["adapter"] for c in calls)
    assert len({c["seed"] for c in calls}) == 2 and len(calls) <= DISTINCT_RETRIES + 1
    assert all(not on for on, _, _ in judged) and judged[0][1] == ["B: 累啦"]  # judged with the adapter off, on plain lines
    assert result["resampled_styles"] == 1 and not result["invalid_styles"]
    assert leases == ["acquire", "release"]


def test_a_style_that_never_gets_a_distinct_draft_is_reported_invalid(tmp_path, monkeypatch):
    from rapport.learning import reply_batch, self_judge
    from rapport.llm.personal_request import DISTINCT_RETRIES, compile_request
    from rapport.llm.reply import ReplyInput

    @contextmanager
    def lease(*args):
        yield

    monkeypatch.setattr(reply_batch, "GPULease", lease)
    monkeypatch.setattr(reply_batch, "sample_shared", lambda m, t, prompts, k, d, *, seed: ([[("好的", True)] * k for _ in prompts], 1, 1))
    monkeypatch.setattr(self_judge, "score_drafts", lambda m, t, lines, drafts, d: [0.] * len(drafts))
    runtime = BatchRuntime(SimpleNamespace(data_dir=tmp_path), {"id": "model"})
    runtime.model = SimpleNamespace(disable_adapter=nullcontext)
    runtime.tokenizer = object()
    result = runtime.complete(compile_request(ReplyInput(lines=["B: 行吗"], styles=["casual", "direct"], memories=[], goal="回应")))
    assert [d["style"] for d in result["data"]["drafts"]] == ["casual"]
    assert result["invalid_styles"] == ["direct"] and result["resampled_styles"] == DISTINCT_RETRIES


def test_worker_main_rechecks_revoked_model_before_loading_or_generating(tmp_path, monkeypatch, capsys):
    from rapport.learning import reply_batch

    record = {"id": "candidate", "artifact_hash": "same", "state": "active"}
    service = SimpleNamespace(model=Mock(side_effect=[record, record | {"state": "revoked"}]))
    monkeypatch.setattr("rapport.learning.service.LearningService", lambda *a, **kw: service)
    runtime = SimpleNamespace(load=Mock(), complete=Mock(), close=Mock())
    monkeypatch.setattr(reply_batch, "BatchRuntime", lambda *args: runtime)
    monkeypatch.setattr(sys, "argv", ["reply_batch", "--data-dir", str(tmp_path), "--model", "candidate"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"id": "request", "operation": "warm"}) + "\n"))
    reply_batch.main()
    output = json.loads(capsys.readouterr().out)
    assert output["error"] == "ValueError"
    assert runtime.load.call_count == runtime.complete.call_count == 0
    runtime.close.assert_called_once()


def test_encrypted_model_registry_roundtrip_and_legacy_metadata_migration(tmp_path):
    service = LearningService(tmp_path, spawn_jobs=False)
    record = {"id": "candidate-1", "created_at": 1, "task": "reply", "state": "evaluated", "metrics": {"test": {"note": "私人评测内容"}},
              "artifact_format": "encrypted_lora_zip", "artifact_hash": "abc", "source_ids": ["source-1"]}
    service.register(record)
    with service.db() as connection:
        raw = connection.execute("SELECT body FROM models WHERE id=?", (record["id"],)).fetchone()[0]
    assert set(json.loads(raw)) == {"encrypted_record"}
    assert "私人评测内容" not in raw
    assert service.model(record["id"]) == record
    assert service.models() == [record | {"personal_trial_available": False}]
    legacy = record | {"id": "legacy", "created_at": 0}
    with service.db() as connection:
        connection.execute("INSERT INTO models(id,body) VALUES(?,?)", ("legacy", json.dumps(legacy)))
    assert service.model("legacy") == legacy
    service.register(legacy | {"state": "revoked"})
    with service.db() as connection:
        assert set(json.loads(connection.execute("SELECT body FROM models WHERE id='legacy'").fetchone()[0])) == {"encrypted_record"}
    assert service.model("legacy")["state"] == "revoked"


def test_source_revocation_stops_cached_reply_worker(tmp_path):
    service = LearningService(tmp_path, spawn_jobs=False)
    record = {"id": "candidate-1", "created_at": 1, "task": "reply", "state": "active", "metrics": {},
              "artifact_hash": "abc", "source_ids": ["source-1"], "gate": {"passed": True}}
    service.register(record)
    pool = service.reply_workers()
    worker = pool.get(record["id"])
    worker.close = Mock()
    service.invalidate_sources(["source-1"])
    assert service.model(record["id"])["state"] == "revoked"
    worker.close.assert_called_once()


def test_review_rejects_stale_evaluation_hash_even_when_sample_ids_unchanged(tmp_path):
    from rapport.errors import RapportError

    service = LearningService(tmp_path, spawn_jobs=False)
    record = {"id": "candidate-1", "created_at": 1, "task": "reply", "state": "evaluated", "metrics": {},
              "artifact_hash": "abc", "source_ids": [], "gate": {"passed": False, "reasons": ["personal_generation_review_required"]}}
    service.register(record)
    folder = service.root / "models" / record["id"]
    folder.mkdir()
    samples = [{"id": f"test-{index}", "candidate_reply": "旧回复"} for index in range(10)]
    (folder / "evaluation.bin").write_bytes(service.crypto.encrypt(json.dumps(samples).encode()))
    old = service.evaluation_bundle(record["id"])
    assert len(old["items"]) == 10 and len(old["evaluation_hash"]) == 64
    changed = [sample | {"candidate_reply": "新回复"} for sample in samples]
    (folder / "evaluation.bin").write_bytes(service.crypto.encrypt(json.dumps(changed).encode()))
    fresh = service.evaluation_bundle(record["id"])
    assert fresh["evaluation_hash"] != old["evaluation_hash"]
    decisions = {sample["id"]: True for sample in samples}
    with pytest.raises(RapportError) as caught:
        service.review_replies(record["id"], decisions, evaluation_hash=old["evaluation_hash"])
    assert caught.value.status == 409
    assert not service.model(record["id"])["gate"]["passed"]
    accepted = service.review_replies(record["id"], decisions, evaluation_hash=fresh["evaluation_hash"])
    assert accepted["gate"]["passed"]


@pytest.mark.parametrize("policy", [None, "previous-allow-all-policy"])
def test_activation_rejects_missing_or_old_personal_fact_policy(tmp_path, monkeypatch, policy):
    from rapport.errors import RapportError
    from rapport.llm.personal_request import VERSION

    service = LearningService(tmp_path, spawn_jobs=False)
    record = {"id": "candidate-1", "created_at": 1, "task": "reply", "state": "evaluated", "runtime_supported": True,
              "metrics": {"test": {"generation_protocol": VERSION, "validation_policy": policy}}, "gate": {"passed": True},
              "artifact_hash": "same"}
    service.register(record)
    read_artifact = Mock(side_effect=AssertionError("outdated policy must not load model weights"))
    monkeypatch.setattr(service, "load_artifact", read_artifact)
    with pytest.raises(RapportError) as caught:
        service.activate(record["id"], confirm=True)
    assert caught.value.code == "learning_runtime_changed"
    assert caught.value.status == 409
    assert service.model(record["id"])["state"] == "evaluated"
    read_artifact.assert_not_called()


def test_production_path_evaluation_reports_filtered_raw_fact_risk(tmp_path, monkeypatch):
    from rapport.learning import reply_evaluation
    from rapport.llm.personal_request import FACT_POLICY

    service = LearningService(tmp_path, spawn_jobs=False)
    record = {"id": "candidate-1", "created_at": 1, "task": "reply", "state": "candidate", "metrics": {},
              "artifact_hash": "same", "training_protocol": "personal-instruction-natural-v1"}
    service.register(record)
    (service.root / "models" / record["id"]).mkdir()

    class FakeModel:
        base = False

        @contextmanager
        def disable_adapter(self):
            self.base = True
            try:
                yield
            finally:
                self.base = False

    class FakeRuntime:
        device = "cpu"
        load_seconds = .1

        def __init__(self):
            self.model = FakeModel()
            self.tokenizer = SimpleNamespace(apply_chat_template=lambda *a, **kw: [1, 2], encode=lambda *a, **kw: [3], eos_token_id=9)
            self.close = Mock()

        def load(self):
            pass

        def on_device(self):
            return nullcontext()

        def complete(self, request, *, base=False):
            drafts = [{"style": "casual", "text": "我上周买了那本书。"},
                      {"style": "sincere", "text": "那你好好休息呀。"}, {"style": "direct", "text": "今晚先放松一下吧。"}]
            return {"data": {"drafts": drafts}, "elapsed_s": .2, "prompt_tokens": 10, "completion_tokens": 15,
                    "invalid_styles": [], "truncated_styles": []}

    runtime = FakeRuntime()
    monkeypatch.setattr(reply_evaluation, "BatchRuntime", lambda *args: runtime)
    # Held-out loss: the adapter scores 1.0, the base (adapter disabled) 2.0.
    monkeypatch.setattr(reply_evaluation, "answer_loss",
                        lambda model, prefix, answer, device: SimpleNamespace(cpu=lambda: 2.0 if model.base else 1.0))
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(inference_mode=nullcontext))
    rows = [{"id": f"test-{index}", "split": "test", "training_eligible": True, "response": "早点休息呀",
             "context": [{"sender": "other", "text": "今天有点累"}]} for index in range(30)]
    result = reply_evaluation.evaluate(service, {"model_id": record["id"], "max_eval_samples": 30}, rows, lambda *args: None)
    metrics = result["metrics"]
    assert metrics["validation_policy"] == FACT_POLICY
    assert metrics["validation_counts"]["fact_review_groups"] == 30
    assert metrics["validation_counts"]["withheld_fact_drafts"] == 30
    assert metrics["validation_counts"]["insufficient_safe_candidates"] == 0
    assert metrics["structured_failures"] == 0  # Structure is correct; raw factual risk is not zero.
    assert not result["gate"]["passed"]
    assert "personal_generation_review_required" in result["gate"]["reasons"]
    samples = service.evaluation_samples(record["id"])
    assert all(len(sample["withheld_drafts"]) == 1 and len(sample["structured_drafts"]) == 2 for sample in samples)
    assert all("买了那本书" not in draft["text"] for sample in samples for draft in sample["structured_drafts"])
    runtime.close.assert_called_once()
