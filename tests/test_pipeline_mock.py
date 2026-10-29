import json

import httpx


def collect(base: str, url: str) -> list[dict]:
    r = httpx.post(f"{base}/api/runs", json={"url": url}, timeout=10)
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    events = []
    with httpx.stream("GET", f"{base}/api/runs/{run_id}/events", timeout=180) as s:
        kind = None
        for line in s.iter_lines():
            if line.startswith("event: "):
                kind = line[7:]
            elif line.startswith("data: ") and kind not in (None, "end"):
                events.append(json.loads(line[6:]))
            if kind == "end":
                break
    return events, run_id


def test_full_pipeline_on_demo(server_url):
    events, run_id = collect(server_url, f"{server_url}/demo/sunny-crumb.html")
    errors = [e for e in events if e["type"] == "error"]
    assert not errors, errors
    result = next(e for e in events if e["type"] == "result")

    assert result["issues_before"] >= 15
    assert result["score_before"] <= 40
    assert result["score_after"] > result["score_before"]
    assert result["score_after"] >= 90

    fixable = result["issues_before"] - len(result["needs_human"])
    verified = [p for p in result["patches"] if p["verified"]]
    assert len(verified) >= 0.8 * result["issues_before"]
    assert len(verified) >= fixable - 1

    # the write -> run -> test -> retry loop fired and recovered
    steps = {e["id"] for e in events if e["type"] == "step"}
    assert "retry-1" in steps
    assert any(p["attempt"] == 2 and p["verified"] for p in verified)

    tiers = {c["tier"] for c in result["model_calls"]}
    assert tiers == {"nano", "super", "ultra"}
    assert any(p["sources"] for p in verified)
    assert result["pages"][0]["screenshot_before"] and result["pages"][0]["screenshot_after"]

    patch = httpx.get(f"{server_url}/api/runs/{run_id}/download?type=patch").json()
    assert len(patch["patches"]) == len(result["patches"])
    report = httpx.get(f"{server_url}/api/runs/{run_id}/download?type=report").text
    assert "guarantee" in report.lower()


def test_ssrf_guard_rejects_private_targets(server_url):
    for bad in ["http://127.0.0.1:22/", "http://169.254.169.254/latest/meta-data/", "file:///etc/passwd",
                "http://10.0.0.5/", "http://[::1]:8000/", "ftp://example.com/"]:
        r = httpx.post(f"{server_url}/api/runs", json={"url": bad}, timeout=10)
        assert r.status_code == 400, (bad, r.text)
