"""OCR adapter regressions with synthetic pixels and a fake inference engine."""

from __future__ import annotations

import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import ModuleType, SimpleNamespace

import pytest

from rapport import config
from rapport.ocr import engine

np = pytest.importorskip("numpy")
Image = pytest.importorskip("PIL.Image")


def test_modern_numpy_output_preserves_text_geometry_and_confidence(monkeypatch):
    result = SimpleNamespace(
        boxes=np.array([
            [[12.5, 10.0], [55.0, 9.5], [56.5, 32.0], [11.0, 33.5]],
            [[100.0, 40.0], [140.0, 40.0], [140.0, 58.0], [100.0, 58.0]],
        ]),
        txts=np.array(["劳娈淇", "今天 12:30"]),
        scores=np.array([0.973, 0.812]),
    )
    monkeypatch.setattr(engine, "_engine", lambda: lambda _img: result)

    boxes = engine.run(Image.new("RGB", (160, 80), "white"))

    assert [box.text for box in boxes] == ["劳娈淇", "今天 12:30"]
    assert [(box.x0, box.y0, box.x1, box.y1) for box in boxes] == [
        (11.0, 9.5, 56.5, 33.5),
        (100.0, 40.0, 140.0, 58.0),
    ]
    assert [box.score for box in boxes] == pytest.approx([0.973, 0.812])


@pytest.mark.parametrize(
    "result",
    [
        None,
        (None, 0.01),
        ([], 0.01),
        SimpleNamespace(boxes=None, txts=None, scores=None),
        SimpleNamespace(boxes=[], txts=[], scores=[]),
        SimpleNamespace(
            boxes=np.empty((0, 4, 2)),
            txts=np.array([], dtype=str),
            scores=np.array([], dtype=float),
        ),
    ],
    ids=["none", "legacy-none", "legacy-empty", "modern-none", "modern-empty", "numpy-empty"],
)
def test_no_recognized_text_returns_empty_list(monkeypatch, result):
    monkeypatch.setattr(engine, "_engine", lambda: lambda _img: result)

    assert engine.run(Image.new("RGB", (20, 20), "white")) == []


def test_legacy_result_still_converts(monkeypatch):
    result = (
        [
            [[[2, 4], [42, 3], [44, 19], [1, 20]], "稍后见", 0.94],
            [[[80, 40], [110, 40], [110, 60], [80, 60]], "好的", 0.89],
        ],
        [0.01, 0.02, 0.03],
    )
    monkeypatch.setattr(engine, "_engine", lambda: lambda _img: result)

    assert engine.run(Image.new("RGB", (120, 80), "white")) == [
        engine.OcrBox(1, 3, 44, 20, "稍后见", 0.94),
        engine.OcrBox(80, 40, 110, 60, "好的", 0.89),
    ]


def test_rgb_image_reaches_inference_as_pil_without_channel_swap(monkeypatch):
    image = Image.new("RGB", (2, 1))
    image.putpixel((0, 0), (255, 0, 0))
    image.putpixel((1, 0), (0, 0, 255))
    received = []

    def infer(value):
        received.append(value)
        return None

    monkeypatch.setattr(engine, "_engine", lambda: infer)

    assert engine.run(image) == []
    assert len(received) == 1
    # RapidOCR treats ndarray inputs as BGR; retaining PIL avoids reinterpreting
    # this screenshot's red and blue channels at its input boundary.
    assert isinstance(received[0], Image.Image)
    assert received[0].mode == "RGB"
    assert received[0].getpixel((0, 0)) == (255, 0, 0)
    assert received[0].getpixel((1, 0)) == (0, 0, 255)


def test_shared_engine_inference_is_serialized(monkeypatch):
    workers = 6
    start = threading.Barrier(workers)
    counter_lock = threading.Lock()
    active = 0
    peak_active = 0
    calls = 0

    def infer(_image):
        nonlocal active, peak_active, calls
        with counter_lock:
            active += 1
            peak_active = max(peak_active, active)
            calls += 1
        try:
            # Release the GIL while this fake mutable engine is busy, giving
            # all callers a chance to expose an unguarded shared invocation.
            time.sleep(0.04)
            return None
        finally:
            with counter_lock:
                active -= 1

    monkeypatch.setattr(engine, "_engine", lambda: infer)

    def read_one(_index):
        start.wait(timeout=5)
        return engine.run(Image.new("RGB", (20, 20), "white"))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(read_one, range(workers)))

    assert results == [[] for _ in range(workers)]
    assert calls == workers
    assert active == 0
    assert peak_active == 1


def test_identical_chat_pixels_skip_repeat_ocr_but_changed_pixels_do_not(monkeypatch):
    calls = []
    first = Image.new("RGB", (80, 50), "white")
    changed = first.copy()
    changed.putpixel((10, 10), (255, 0, 0))
    monkeypatch.setattr(engine, "run", lambda img: calls.append(img.getpixel((10, 10))) or
                        [engine.OcrBox(1, 2, 30, 20, "你好", .9)])
    engine._CHAT_CACHE.clear()
    try:
        before = engine.run_chat_cached(first)
        before[0].text = "caller mutation"
        assert engine.run_chat_cached(first)[0].text == "你好"
        assert engine.run_chat_cached(changed)[0].text == "你好"
        assert calls == [(255, 255, 255), (255, 0, 0)]
    finally:
        engine._CHAT_CACHE.clear()


@pytest.fixture
def clean_engine_factory():
    engine._engine.cache_clear()
    engine._ACTIVE_DEVICE = "待初始化"
    try:
        yield
    finally:
        engine._engine.cache_clear()
        engine._ACTIVE_DEVICE = "待初始化"


@pytest.mark.parametrize(
    ("available_providers", "session_providers", "request_dml", "device"),
    [
        (
            ["DmlExecutionProvider", "CPUExecutionProvider"],
            ["DmlExecutionProvider", "CPUExecutionProvider"],
            True,
            "GPU · DirectML",
        ),
        (
            ["DmlExecutionProvider", "CPUExecutionProvider"],
            ["CPUExecutionProvider"],
            True,
            "CPU",
        ),
        (
            ["CPUExecutionProvider"],
            ["CPUExecutionProvider"],
            False,
            "CPU",
        ),
    ],
    ids=["directml-active", "directml-falls-back-to-cpu", "cpu-only"],
)
def test_factory_requests_acceleration_but_reports_actual_recognition_device(
    monkeypatch,
    tmp_path,
    clean_engine_factory,
    available_providers,
    session_providers,
    request_dml,
    device,
):
    requested_params = []
    session = SimpleNamespace(get_providers=lambda: session_providers)
    recognition = SimpleNamespace(session=SimpleNamespace(session=session))
    fake_engine = SimpleNamespace(text_rec=recognition)

    def create_engine(*, params):
        requested_params.append(params)
        return fake_engine

    runtime = ModuleType("onnxruntime")
    runtime.get_available_providers = lambda: available_providers
    rapidocr = ModuleType("rapidocr")
    rapidocr.ModelType = SimpleNamespace(SMALL="small", SERVER="server")
    rapidocr.OCRVersion = SimpleNamespace(PPOCRV5="PP-OCRv5", PPOCRV6="PP-OCRv6")
    rapidocr.RapidOCR = create_engine
    monkeypatch.setitem(sys.modules, "onnxruntime", runtime)
    monkeypatch.setitem(sys.modules, "rapidocr", rapidocr)
    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(data_dir=tmp_path))

    assert engine.info()["device"] == "待初始化"
    assert engine._engine() is fake_engine
    assert engine._engine() is fake_engine
    assert len(requested_params) == 1
    assert requested_params[0]["EngineConfig.onnxruntime.use_dml"] is request_dml
    assert engine.info()["device"] == device
    assert engine.info()["local"] is True


@pytest.fixture
def lease_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(data_dir=tmp_path))
    monkeypatch.setattr(engine, "_LAST_DEVICE", "待初始化")
    return tmp_path


def session(label):
    box = [[0, 0], [10, 0], [10, 10], [0, 10]]
    return lambda: (lambda _img: SimpleNamespace(boxes=[box], txts=[label], scores=[.9]))


def test_busy_gpu_sends_recognition_to_the_cpu_instead_of_failing(monkeypatch, lease_dir):
    from rapport.learning.gpu import GPULease

    monkeypatch.setattr(engine, "_directml_requested", lambda: True)
    monkeypatch.setattr(engine, "_ACTIVE_DEVICE", engine.GPU_DEVICE)
    monkeypatch.setattr(engine, "_engine", session("gpu"))
    monkeypatch.setattr(engine, "_cpu_engine", session("cpu"))
    with GPULease(lease_dir, "lora:training"):  # a training job holds the GPU for hours
        assert [box.text for box in engine.run(Image.new("RGB", (20, 20)))] == ["cpu"]
        assert engine.last_device() == engine.CPU_FALLBACK_DEVICE
    assert [box.text for box in engine.run(Image.new("RGB", (20, 20)))] == ["gpu"]
    assert engine.last_device() == engine.GPU_DEVICE
    assert engine.info()["last_device"] == engine.GPU_DEVICE


def test_directml_recognition_holds_the_gpu_lease_while_it_runs(monkeypatch, lease_dir):
    owners = []

    def infer(_img):
        owners.append(json.loads((lease_dir / "learning" / "gpu-owner.json").read_text(encoding="utf-8"))["owner"])
        return None

    monkeypatch.setattr(engine, "_directml_requested", lambda: True)
    monkeypatch.setattr(engine, "_ACTIVE_DEVICE", engine.GPU_DEVICE)
    monkeypatch.setattr(engine, "_engine", lambda: infer)
    assert engine.run(Image.new("RGB", (20, 20))) == []
    assert owners == ["ocr"]


@pytest.mark.parametrize("requested", [False, True], ids=["cpu-runtime", "directml-session-fell-back"])
def test_cpu_recognition_never_waits_for_or_takes_the_gpu_lease(monkeypatch, lease_dir, requested):
    from rapport.learning.gpu import GPULease

    monkeypatch.setattr(engine, "_directml_requested", lambda: requested)
    monkeypatch.setattr(engine, "_ACTIVE_DEVICE", "CPU")
    monkeypatch.setattr(engine, "_engine", session("cpu session"))
    monkeypatch.setattr(engine, "_cpu_engine", lambda: pytest.fail("no second CPU session is needed"))
    with GPULease(lease_dir, "lora:training"):
        assert [box.text for box in engine.run(Image.new("RGB", (20, 20)))] == ["cpu session"]
    assert engine.last_device() == "CPU"
