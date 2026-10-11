"""`rapport learning`: build the personal dataset, train, evaluate, review and switch models locally.

Everything runs against the configured private data directory (``RAPPORT_DATA_DIR``). Nothing is
uploaded. Commands print counts, ids, metrics and progress only, never chat text; the one exception
is ``samples``, which writes the encrypted evaluation examples to a local file for the reviewer.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import typer

from rapport.config import get_settings

learning_app = typer.Typer(help="个人回复模型：数据集、训练、评测、复核与启用（全部在本机）", no_args_is_help=True,
                           add_completion=False)


def _service():
    from rapport.learning import LearningService

    return LearningService(get_settings().data_dir, spawn_jobs=False)


def _echo(value) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _model_summary(model: dict) -> dict:
    test = model.get("metrics", {}).get("test", {})
    return {"id": model["id"], "state": model["state"], "base": model.get("base_model_id"), "dataset": model.get("dataset_id"),
            "protocol": model.get("training_protocol"), "trained_samples": model.get("trained_samples"),
            "gate": {k: model.get("gate", {}).get(k) for k in ("passed", "automatic_passed", "reasons", "reviewed_samples",
                                                                "accepted_samples", "quality_review_source")},
            "test": {k: test.get(k) for k in ("samples", "reply_loss", "base_reply_loss", "model_load_s", "reply_group_warm_p95_s",
                                              "peak_reserved_vram_bytes", "safe_candidate_group_coverage",
                                              "distinct_candidate_group_coverage", "generation_protocol")}}


@learning_app.command()
def status():
    """运行条件、数据集、模型与最近的任务。"""
    service = _service()
    datasets = [{k: d.get(k) for k in ("id", "status", "version", "created_at", "pools", "time_splits", "eligible")}
                for d in service.datasets().list()]
    jobs = [{k: j.get(k) for k in ("id", "kind", "status", "stage", "progress", "error", "result")} for j in service.jobs()[:8]]
    _echo({"data_dir": str(service.data_dir), "capabilities": service.capabilities(), "datasets": datasets,
           "models": [_model_summary(m) for m in service.models()], "jobs": jobs})


@learning_app.command("register-base")
def register_base(source: Path = typer.Option(..., exists=True, file_okay=False, help="Hugging Face 格式的本地基座目录"),
                  model_id: str = typer.Option(..., "--id", help="注册名，例如 qwen3-1.7b")):
    """把公开基座权重复制（不是链接）到数据目录的 learning/base_models/<id>。"""
    from rapport.learning.lora import BASE_PATTERNS, TOKENIZER_PATTERNS, verified_hash
    from rapport.learning.service import _id

    service = _service()
    target = service.root / "base_models" / _id(model_id)
    if target.exists():
        raise typer.BadParameter(f"{target} 已存在；如需替换请先手动移走")
    if not (source / "config.json").is_file() or not list(source.glob("*.safetensors")):
        raise typer.BadParameter("来源目录需要 config.json 和 *.safetensors")
    partial = target.with_name(target.name + ".partial")
    shutil.copytree(source, partial)
    partial.replace(target)
    _echo({"base_model_id": model_id, "path": str(target),
           "base_hash": verified_hash(target, BASE_PATTERNS), "tokenizer_hash": verified_hash(target, TOKENIZER_PATTERNS)})


@learning_app.command()
def dataset(source: Path = typer.Option(..., exists=True, file_okay=False, help="只读的脱敏 eval 目录（含 redacted/ 与 annotations/）")):
    """审核并构建当前规则版本的个人数据集快照。来源目录只读。"""
    service = _service()
    store = service.datasets()
    manifest = store.build(source)
    protocol = manifest.get("split_policy", {}).get("personal_reply_protocol", {})
    _echo({k: manifest.get(k) for k in ("id", "version", "pools", "time_splits", "eligible")} | {"personal_reply_protocol": protocol})


def _run_job(service, job: dict, label: str) -> dict:
    from rapport.learning.service import worker_env

    logs = service.data_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    probe = logs / f"{label}-{job['id'][:8]}.progress"
    python = os.environ.get("RAPPORT_TRAINING_PYTHON", sys.executable)
    command = [python, "-m", "rapport.learning.worker", "--data-dir", str(service.data_dir), "--job", job["id"]]
    typer.echo(f"job {job['id']} started; progress probe: {probe}")
    process = subprocess.Popen(command, env=worker_env(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    last = None
    started = time.monotonic()
    with probe.open("a", encoding="utf-8") as sink:
        while True:
            finished = process.poll() is not None
            record = service.job(job["id"])
            line = f"{time.monotonic() - started:7.0f}s {record['status']:>9} {record.get('progress', 0) * 100:5.1f}% {record.get('stage')}"
            if line.split(" ", 1)[1] != last:
                typer.echo(line)
                sink.write(line + "\n")
                sink.flush()
                last = line.split(" ", 1)[1]
            if finished:
                break
            time.sleep(5)
        tail = (process.stderr.read() or "")[-2000:]
        record = service.job(job["id"])
        sink.write(("DONE " if record["status"] == "completed" else "FAILED ") + json.dumps(
            {k: record.get(k) for k in ("status", "error", "result")}, ensure_ascii=False, default=str) + "\n")
    if record["status"] != "completed":
        typer.secho(f"job {record['status']}: {record.get('error')}", fg="red")
        if tail.strip():
            typer.echo(tail)  # Worker stderr: tracebacks and library warnings, no sample text is printed by our code.
        raise typer.Exit(1)
    return record


@learning_app.command()
def train(dataset_id: str = typer.Option(..., "--dataset"), base_model_id: str = typer.Option(..., "--base"),
          epochs: int = typer.Option(2), max_samples: int | None = typer.Option(None, help="默认使用全部合格训练样本"),
          max_eval_samples: int = typer.Option(64)):
    """训练一个个人回复 LoRA 候选（前台等待，进度写入 logs/*.progress）。"""
    service = _service()
    job = service.create_job("train", dataset_id=dataset_id, pool="personal_reply", architecture="reply_lora",
                             base_model_id=base_model_id, epochs=epochs, max_train_samples=max_samples,
                             max_eval_samples=max_eval_samples)
    record = _run_job(service, job, "train")
    _echo(_model_summary(service.model(record["result"]["model_id"])))


@learning_app.command()
def prefer(model_id: str = typer.Option(..., "--model", help="已训练的个人回复模型（父模型）"),
           epochs: int = typer.Option(1), max_samples: int | None = typer.Option(None, help="默认使用全部合格训练样本"),
           max_eval_samples: int = typer.Option(64)):
    """在个人回复模型之上做一轮偏好训练（父模型自己起草、基座自评，拒绝明显不合格的草稿）。"""
    service = _service()
    job = service.create_job("train", parent_model_id=model_id, epochs=epochs, max_train_samples=max_samples,
                             max_eval_samples=max_eval_samples)
    record = _run_job(service, job, "prefer")
    _echo(_model_summary(service.model(record["result"]["model_id"])))


@learning_app.command()
def evaluate(model_id: str = typer.Option(..., "--model"), samples: int = typer.Option(64),
             offset: int = typer.Option(0, help="跳过打乱后的前 N 组；0 为以往候选复核的同一批组，64 为另一批新组")):
    """用生产同路径对候选做独立测试集评测（不启用）。复核与启用以最近一次评测为准。"""
    service = _service()
    job = service.create_job("evaluate", model_id=model_id, max_eval_samples=samples, eval_offset=offset)
    _run_job(service, job, "evaluate")
    _echo(_model_summary(service.model(model_id)))


@learning_app.command("evaluate-style")
def evaluate_style(dataset_id: str = typer.Option(..., "--dataset"), model: str = typer.Option("qwen3:8b", help="本机 Ollama 模型"),
                   samples: int = typer.Option(64), offset: int = typer.Option(0, help="跳过打乱后的前 N 组（0 与 LoRA 评测同组）"),
                   out: Path | None = typer.Option(None)):
    """本地风格检索（不训练）：同一批独立测试组、同样的严格事实校验；样例写到本机文件供复核，勿外传。"""
    from rapport.learning.style_rag import VERSION, evaluate_style_retrieval

    service = _service()
    tag = f"{VERSION}-{model.replace(':', '-')}-{dataset_id[:8]}" + (f"-from{offset}" if offset else "")
    logs = service.data_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    probe = logs / f"{tag}.progress"
    typer.echo(f"progress probe: {probe}")
    with probe.open("a", encoding="utf-8") as sink:
        def progress(line: str) -> None:
            typer.echo(line)
            sink.write(line + "\n")
            sink.flush()

        result = evaluate_style_retrieval(service, dataset_id, model=model, samples=samples, progress=progress, offset=offset)
        sink.write("DONE\n")
    target = out or service.data_dir / "review" / f"{tag}.samples.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    metrics = {k: v for k, v in result["metrics"].items() if k != "test_ids"}
    _echo(metrics | {"path": str(target)})


@learning_app.command()
def samples(model_id: str = typer.Option(..., "--model"), out: Path | None = typer.Option(None)):
    """把加密的评测样例解密写到本机文件，供逐组复核。文件含私人聊天，勿外传。"""
    service = _service()
    bundle = service.evaluation_bundle(model_id)
    target = out or service.data_dir / "review" / f"{model_id}.samples.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(bundle, ensure_ascii=False, indent=1), encoding="utf-8")
    _echo({"model_id": model_id, "samples": len(bundle["items"]), "evaluation_hash": bundle["evaluation_hash"], "path": str(target)})


@learning_app.command()
def review(model_id: str = typer.Option(..., "--model"), decisions: Path = typer.Option(..., exists=True, dir_okay=False),
           reviewer: str = typer.Option("delegated_assistant", help="user 或 delegated_assistant")):
    """登记逐组复核结果。文件格式：{"evaluation_hash": ..., "decisions": {样例id: true/false}}。"""
    body = json.loads(decisions.read_text(encoding="utf-8"))
    service = _service()
    record = service.review_replies(model_id, body["decisions"], body.get("evaluation_hash"), reviewer=reviewer)
    _echo(_model_summary(record))


@learning_app.command()
def activate(model_id: str = typer.Option(..., "--model"), trial: bool = typer.Option(False, help="仅自动门槛通过时的本机试用")):
    """启用已通过门槛的候选（会替换同范围内当前启用的版本）。"""
    service = _service()
    record = service.activate(model_id, confirm=True, personal_trial=trial)
    _echo(_model_summary(record))


@learning_app.command()
def rollback(model_id: str = typer.Option(..., "--model")):
    """回滚到一个此前已通过门槛的版本。"""
    service = _service()
    _echo(_model_summary(service.activate(model_id, confirm=True, rollback=True)))


@learning_app.command()
def deactivate(model_id: str = typer.Option(..., "--model")):
    """停用个人模型（回复回到其他已配置的模式）。"""
    _echo(_model_summary(_service().deactivate(model_id)))
