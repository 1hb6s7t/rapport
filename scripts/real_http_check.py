"""Real HTTP end-to-end check through the API, using the key file only in-process.

    uv run python ../scripts/real_http_check.py "C:/path/API KEY.txt" --preset mimo --model mimo-v2.5-pro
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient  # noqa: E402
from real_byok_check import load_key  # noqa: E402

from rapport.config import reset_settings  # noqa: E402
from rapport.main import create_app  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("key_file")
    ap.add_argument("--preset", default="mimo")
    ap.add_argument("--model", default="mimo-v2.5-pro")
    ap.add_argument("--fixture", default="paste_ab_warm.txt")
    args = ap.parse_args()

    key, file_url = load_key(Path(args.key_file))
    data_dir = Path(tempfile.mkdtemp())
    settings = reset_settings(data_dir=data_dir, pairing_code="123456", api_key=key)
    app = create_app(settings, start_runner=False)
    c = TestClient(app, base_url="http://127.0.0.1")
    print(f"data_dir={data_dir}")

    r = c.post("/api/pair", json={"code": "123456"})
    c.headers["x-rapport-csrf"] = r.json()["csrf"]
    print("pair:", r.status_code)

    r = c.put("/api/settings", json={"preset": args.preset, "model": args.model, "api_key": None})
    print("settings:", r.status_code, r.json()["provider"]["preset"], r.json()["provider"]["model"], "has_key:", r.json()["provider"]["has_key"])
    r = c.post("/api/settings/test-provider")
    print("test-provider:", r.json())

    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic" / args.fixture
    imp = c.post("/api/imports", json={"text": fixture.read_text(encoding="utf-8")}).json()
    print(f"import: {imp['parser_id']} {imp['n_messages']} messages")
    sel = c.post(f"/api/imports/{imp['id']}/confirm", json={}).json()["selected"]
    print(f"confirm range: {sel['n_events']} events, me {sel['n_me']} / other {sel['n_other']}")

    a = c.post("/api/analyses", json={"import_id": imp["id"], "mode": "byok", "profile": {"goal": "invite", "other": {"mbti": "ENTJ"}}}).json()
    print("analysis:", a["id"], a["status"], "needs_confirmation:", a["needs_confirmation"])
    pv = c.get(f"/api/analyses/{a['id']}/preview").json()
    print(f"preview: windows={pv['n_windows']} messages={pv['n_messages']} est_cost={pv['cost']['cny']} price_known={pv['cost']['price_known']} over_threshold={pv['cost']['over_threshold']}")
    print(f"  destination: {pv['destination']['label']} / {pv['destination']['model']} / region={pv['destination']['endpoint_region']}")
    print(f"  redaction: names={pv['redaction']['names_replaced']} counts={pv['redaction']['counts']}")
    joined = "\n".join(ln for w in pv["windows"] for ln in w)
    print(f"  leak check: 'ENTJ' in payload = {'ENTJ' in joined}; '小' names in payload = {any(n in joined for n in ('小明', '小红'))}")

    r = c.post(f"/api/analyses/{a['id']}/confirm", json={"payload_hash": pv["payload_hash"]})
    print("confirm without accept_cost (price unknown -> should be 409):", r.status_code, r.json().get("error", {}).get("code"))
    r = c.post(f"/api/analyses/{a['id']}/confirm", json={"payload_hash": pv["payload_hash"], "accept_cost": True})
    print("confirm with accept_cost:", r.status_code, r.json()["status"])

    n = app.state.rapport.runner.run_pending_once()
    print(f"runner executed {n} job(s)")
    a = c.get(f"/api/analyses/{a['id']}").json()
    print("analysis final:", a["status"], "report:", a["report_id"], "error:", a["error"])

    rep = c.get(f"/api/reports/{a['report_id']}").json()
    print(f"report: mode={rep['mode']} total={rep['affinity']['total']} ai_status={rep['ai_status']} coverage={rep['affinity']['coverage']}")
    print("  dims: " + ", ".join(f"{k}={v['rule']}/{v['llm']}->{v['score']}" for k, v in rep["affinity"]["dims"].items()))
    print(f"  stage={rep['stage']['value']} boundary={rep['boundary']['value']} obs={len(rep['observations'])} traits={len(rep['persona']['traits'])} actions={len(rep['playbook']['actions'])}")
    print(f"  mbti comparison: {rep['persona']['mbti_comparison'] and rep['persona']['mbti_comparison']['verdict']} inferred={rep['persona']['mbti_comparison'] and rep['persona']['mbti_comparison']['inferred']}")
    print(f"  trend: bucket={rep['trend']['bucket']} momentum={rep['trend']['momentum']} points={[p['score'] for p in rep['trend']['points']]}")
    print(f"  usage: {rep['cost']['actual_usage']} cny={rep['cost']['actual_cny']}")
    md = c.get(f"/api/reports/{a['report_id']}/export?format=md").text
    print(f"  markdown {len(md)} chars; contains key? {'sk-' in md}")
    r = c.patch(f"/api/reports/{a['report_id']}/profile", json={"profile": {"other": {"mbti": "ISFJ"}, "goal": "invite"}})
    rep2 = r.json()
    print(f"relabel keeps core: total {rep['affinity']['total']} -> {rep2['affinity']['total']}, stage {rep['stage']['value']} -> {rep2['stage']['value']}, mbti {rep2['persona']['mbti_comparison']['declared']}")
    print("delete:", c.delete(f"/api/reports/{a['report_id']}").status_code, "then get:", c.get(f"/api/reports/{a['report_id']}").status_code)
    print("wipe:", c.delete("/api/data").json()["cleared"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
