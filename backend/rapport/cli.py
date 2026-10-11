"""CLI: serve / analyze / demo / doctor / models."""

from __future__ import annotations

import json
import sys
import webbrowser
from pathlib import Path

import typer

from rapport import __version__
from rapport.config import get_settings

app = typer.Typer(help="Rapport（心迹）— 自托管 AI 关系复盘工具", no_args_is_help=True, add_completion=False)

from rapport.learning_cli import learning_app  # noqa: E402
from rapport.models_cli import models_app  # noqa: E402

app.add_typer(models_app, name="models")
app.add_typer(learning_app, name="learning")


@app.command()
def serve(host: str = typer.Option(None), port: int = typer.Option(None), open_browser: bool = typer.Option(False, "--open")):
    """启动本地服务（默认只监听 127.0.0.1:7860）。"""
    import uvicorn

    from rapport.main import create_app

    s = get_settings()
    if host:
        s.host = host
    if port:
        s.port = port
    if s.host not in ("127.0.0.1", "localhost", "::1"):
        typer.secho("当前自动连接入口仅供本机访问。", fg="yellow")
    application = create_app(s)
    typer.secho(f"\nRapport v{__version__}  http://{s.host}:{s.port}", fg="green", bold=True)
    typer.secho(f"直接打开本机页面即可使用。数据目录：{s.data_dir}\n", fg="cyan")
    if open_browser:
        webbrowser.open(f"http://{s.host}:{s.port}")
    uvicorn.run(application, host=s.host, port=s.port, log_level="info", access_log=False)


@app.command()
def analyze(
    path: Path = typer.Argument(..., exists=True, readable=True),
    fmt: str = typer.Option("auto", "--format", "-f"),
    out: Path | None = typer.Option(None, "--out", "-o", help="输出 Markdown 文件"),
    as_json: bool = typer.Option(False, "--json"),
    date_order: str | None = typer.Option(None),
):
    """离线规则模式分析一个文件，不需要 Key。"""
    from rapport.jobs.pipeline import run_rules
    from rapport.parsers import ParseError, ParseOptions, parse
    from rapport.report import to_markdown
    from rapport.schemas.profile import Profile

    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        conv, pid = parse(text, "cli", fmt, path.name, ParseOptions(date_order=date_order))
    except ParseError as e:
        typer.secho(f"解析失败 [{e.code}]：{e.msg} {e.hint}", fg="red")
        raise typer.Exit(2) from e
    rep = run_rules(conv, conv.events(), Profile())
    if as_json:
        s = json.dumps(rep.model_dump(mode="json"), ensure_ascii=False, indent=2)
    else:
        s = to_markdown(rep)
    if out:
        out.write_text(s, encoding="utf-8")
        typer.echo(f"已写入 {out}（解析器 {pid}，{len(conv.messages)} 条）")
    else:
        sys.stdout.reconfigure(encoding="utf-8") if hasattr(sys.stdout, "reconfigure") else None
        typer.echo(s)


@app.command()
def demo(out: Path | None = typer.Option(None, "--out", "-o")):
    """输出合成演示报告（Markdown）。"""
    from rapport.demo import demo_report
    from rapport.report import to_markdown

    s = to_markdown(demo_report())
    if out:
        out.write_text(s, encoding="utf-8")
        typer.echo(f"已写入 {out}")
    else:
        sys.stdout.reconfigure(encoding="utf-8") if hasattr(sys.stdout, "reconfigure") else None
        typer.echo(s)


@app.command()
def doctor():
    """检查环境：Python、数据目录、OCR、前端构建、Key 来源。"""
    s = get_settings()
    typer.echo(f"Rapport {__version__}  Python {sys.version.split()[0]}")
    typer.echo(f"数据目录：{s.data_dir}  {'可写' if _writable(s.data_dir) else '不可写'}")
    try:
        import rapidocr  # noqa: F401

        typer.echo("OCR：已安装（rapidocr）")
    except Exception:  # noqa: BLE001
        typer.echo("OCR：未安装（可选：uv sync --extra ocr）")
    static = Path(__file__).parent / "static" / "index.html"
    typer.echo(f"前端构建：{'存在' if static.exists() else '缺失（cd frontend && npm run build）'}")
    typer.echo(f"API Key 环境变量：{'已设置' if s.api_key else '未设置（可在设置页填写，将加密保存）'}")
    typer.echo(f"监听：{s.host}:{s.port}")


def _writable(p: Path) -> bool:
    try:
        p.mkdir(parents=True, exist_ok=True)
        t = p / ".w"
        t.write_text("x")
        t.unlink()
        return True
    except OSError:
        return False


if __name__ == "__main__":
    app()
