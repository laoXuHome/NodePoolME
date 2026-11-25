from datetime import datetime
from zoneinfo import ZoneInfo

from backend.database import db
from backend.services import speed_queue
from backend.subscriptions import builder
from backend.testers import speedtest


def _node(name: str, airport: str = "测速机场") -> tuple[int, int]:
    airport_id = db.execute(
        "INSERT INTO airports (name, url, enabled, created_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (airport, "https://example.com/sub", 1,
         "2026-11-25 09:00:00", "2026-11-25 09:00:00"),
    )
    node_id = db.execute(
        "INSERT INTO nodes (airport_id, raw_name, protocol, server, port, fingerprint, params, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (airport_id, name, "ss", "1.2.3.4", 443, f"fp-{name}", "{}",
         "2026-11-25 09:00:00", "2026-11-25 09:00:00"),
    )
    return airport_id, node_id


def _valid_run(node_id: int, mb_per_s: float, finished_at: str = "2026-11-25 10:00:00") -> int:
    return db.execute(
        "INSERT INTO speed_runs (node_id, started_at, finished_at, status, confidence, "
        "mb_per_s, multi_mbps, sources_total, sources_ok) VALUES (?,?,?,?,?,?,?,?,?)",
        (node_id, finished_at, finished_at, "ok", "normal", mb_per_s, mb_per_s * 8, 3, 3),
    )


def test_grade_boundaries_use_mb_per_s():
    assert speedtest.grade_for(3.0)["key"] == "excellent"
    assert speedtest.grade_for(2.99)["key"] == "good"
    assert speedtest.grade_for(1.5)["key"] == "good"
    assert speedtest.grade_for(1.49)["key"] == "poor"
    assert speedtest.grade_for(0.5)["key"] == "poor"
    assert speedtest.grade_for(0.499)["key"] == "danger"
    assert speedtest.grade_for(0.01)["key"] == "danger"   # real slow speed is a grade, not an error
    assert speedtest.grade_for(None) == {}


def test_speed_grade_visual_standard_is_global():
    assert [(g["label"], g["emoji"], g["color"]) for g in speedtest.GRADE_DEFS.values()] == [
        ("优", "🟢", "#10B981"),
        ("良", "🔵", "#4682B4"),
        ("差", "🟡", "#E9C46A"),
        ("劣", "🔴", "#EF4444"),
    ]


def test_speed_tag_prefix_is_idempotent_and_exclusion_wins():
    airport_id, node_id = _node("香港01")
    assert speedtest.speed_map()[node_id]["status"] == "pending"
    name = speedtest.apply_speed_prefix("测速机场·香港01")
    assert name == "待测速测速机场·香港01"
    assert speedtest.strip_speed_prefix(name) == "测速机场·香港01"
    _valid_run(node_id, 2.4)
    state = speedtest.speed_map()[node_id]
    assert state["status"] == "ok" and state["grade"] == "good"
    tagged = speedtest.apply_speed_prefix("🟣测速机场·香港01", state["marker"])
    assert tagged == "🔵测速机场·香港01"
    db.execute("UPDATE airports SET speed_excluded=1 WHERE id=?", (airport_id,))
    state = speedtest.speed_map()[node_id]
    assert state["status"] == "excluded" and state["marker"] == "⚪"
    assert state["color"] == "#CBD5E1"
    assert speedtest.apply_speed_prefix("🟢测速机场·香港01", state["marker"]) == "⚪测速机场·香港01"
    assert speedtest.strip_speed_prefix("🟠测速机场·香港01") == "测速机场·香港01"
    assert speedtest.strip_speed_prefix("🟡测速机场·香港01") == "测速机场·香港01"


def test_failure_never_overwrites_last_valid_label():
    _, node_id = _node("香港02")
    _valid_run(node_id, 6.6)
    db.execute("INSERT INTO speed_runs (node_id, started_at, finished_at, status, message) "
               "VALUES (?,?,?,?,?)",
               (node_id, "2026-11-25 11:00:00", "2026-11-25 11:00:00", "source_unavailable",
                "所有测速源均失败"))
    state = speedtest.speed_map()[node_id]
    assert state["status"] == "ok" and state["grade"] == "excellent", state
    assert state["last_run_status"] == "source_unavailable"


def test_no_valid_result_and_all_sources_failed_are_distinct():
    _, node_id = _node("香港03")
    db.execute("INSERT INTO speed_runs (node_id, started_at, finished_at, status) VALUES (?,?,?,?)",
               (node_id, "2026-11-25 11:00:00", "2026-11-25 11:00:00", "source_unavailable"))
    assert speedtest.speed_map()[node_id]["status"] == "source_failed"
    _, node2 = _node("香港04")
    db.execute("INSERT INTO speed_runs (node_id, started_at, finished_at, status) VALUES (?,?,?,?)",
               (node2, "2026-11-25 11:00:00", "2026-11-25 11:00:00", "engine_error"))
    assert speedtest.speed_map()[node2]["status"] == "error"


def test_traffic_limit_default_and_schedule_window():
    db.set_setting("speed_traffic_limit_mb", 50)
    settings = speedtest._settings()
    assert settings["traffic_limit_bytes"] == 50 * 1024 * 1024
    assert settings["sample"] == 10
    assert settings["conn_timeout"] == 8
    current, start, end = speed_queue.window_bounds(
        datetime(2026, 11, 25, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai")))
    assert start.hour == 18 and end.hour == 23 and end.minute == 30
    current, start, end = speed_queue.window_bounds(
        datetime(2026, 11, 25, 23, 40, tzinfo=ZoneInfo("Asia/Shanghai")))
    assert not (start <= current < end)


def test_overnight_window_after_midnight_belongs_to_previous_day():
    db.set_setting("speed_window_start", "20:00")
    db.set_setting("speed_window_end", "02:00")
    current, start, end = speed_queue.window_bounds(
        datetime(2026, 11, 26, 0, 30, tzinfo=ZoneInfo("Asia/Shanghai")))
    assert start.date().isoformat() == "2026-11-25"
    assert end.date().isoformat() == "2026-11-26"
    assert start <= current < end
    current, start, end = speed_queue.window_bounds(
        datetime(2026, 11, 26, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai")))
    assert not (start <= current < end)


def test_speed_filter_accepts_keys_and_chinese_labels():
    assert builder.parse_speed_levels(None) is None
    assert builder.parse_speed_levels("excellent,good") == {"excellent", "good"}
    assert builder.parse_speed_levels("优，良") == {"excellent", "good"}
    assert builder.parse_speed_levels("🟣,🔵") == {"excellent", "good"}


def test_eligible_nodes_respect_airport_exclusion():
    airport_id, node_id = _node("香港05")
    assert speedtest.eligible_node_ids() == [node_id]
    assert speedtest.eligible_node_ids(new_only=True) == [node_id]
    _valid_run(node_id, 1.5)
    assert speedtest.eligible_node_ids(new_only=True) == []
    db.execute("UPDATE airports SET speed_excluded=1 WHERE id=?", (airport_id,))
    assert speedtest.eligible_node_ids() == []


def test_queue_deduplicates_and_rejects_excluded_airport():
    airport_id, node_id = _node("香港06")
    q = speed_queue.SpeedQueue()
    job, fresh = q.enqueue(node_id, "manual")
    assert fresh and job.status == "queued"
    again, fresh2 = q.enqueue(node_id, "all")
    assert not fresh2 and again.id == job.id
    db.execute("UPDATE airports SET speed_excluded=1 WHERE id=?", (airport_id,))
    try:
        q.enqueue(node_id, "manual")
    except ValueError as exc:
        assert "排除" in str(exc)
    else:
        raise AssertionError("excluded airport should not enqueue")


def test_subscription_speed_filter_returns_only_selected_and_never_falls_back(monkeypatch, tmp_path):
    import json
    import yaml

    monkeypatch.setattr(builder.config, "SUB_DIR", tmp_path)
    _, fast = _node("香港07")
    _, slow = _node("香港08")
    _valid_run(fast, 6.0)
    _valid_run(slow, 0.1)
    names = {fast: "🟣测速机场·香港07", slow: "🔴测速机场·香港08"}
    (tmp_path / "meta.json").write_text(json.dumps({
        "name": "T", "nodes": 2, "node_ids": [fast, slow],
        "node_names": [{"id": i, "name": n} for i, n in names.items()],
    }, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "sub_all.yaml").write_text("proxies: [{name: stale-full}]", encoding="utf-8")
    _, content = builder.read("clash", {"excellent"})
    proxies = yaml.safe_load(content)["proxies"]
    assert [p["name"] for p in proxies] == ["🟢测速机场·香港07"]
    _, empty = builder.read("clash", {"poor"})
    assert yaml.safe_load(empty)["proxies"] in ([], None)


def test_source_bytes_include_warmup_and_failed_workers(monkeypatch):
    import asyncio

    class FakeClient:
        async def aclose(self):
            pass

    async def fake_worker(client, url, window, warmup, sample, conn_timeout,
                          total_deadline, max_bytes=None):
        if window[0] == 0:
            return speedtest.WorkerSample(
                False, bytes=10, traffic_bytes=70, error_type="source_error",
                message="range")
        return speedtest.WorkerSample(
            True, bytes=100, traffic_bytes=150, seconds=1,
            single_mbps=0.8, single_mb_per_s=0.1, connect_ms=30,
            status_code=206, range_ok=True)

    monkeypatch.setattr(speedtest, "_client", lambda *a, **k: FakeClient())
    monkeypatch.setattr(speedtest, "_worker", fake_worker)
    source = {"id": 1, "name": "T", "url": "https://example.com/100MB.bin",
              "size_bytes": 100 * 1024 * 1024}
    settings = {"connections": 4, "warmup": 2, "sample": 10, "conn_timeout": 8,
                "total_timeout": 20}
    result = asyncio.run(speedtest._test_source(source, 1080, {}, settings, max_bytes=1000))
    assert result.ok
    # 3 successful workers × 150 actual bytes + 1 failed worker × 70 bytes.
    assert result.bytes == 520


def test_run_node_records_bytes_and_connect_time(monkeypatch):
    import asyncio
    from backend.testers import engine as engine_mod

    _, node_id = _node("香港09")
    node = db.row("SELECT * FROM nodes WHERE id=?", (node_id,))
    src = {"id": 1, "name": "T", "url": "https://example.com/100MB.bin", "region": ""}

    class FakeEngine:
        name = "mihomo"
        block_ports = {node["fingerprint"]: 1081}

    async def fake_sources(primary):
        return [src]

    async def fake_source(source, port, node, settings, max_bytes=None):
        return speedtest.SourceResult(
            source, True, bytes=1234, seconds=5, single_mbps=16, multi_mbps=80,
            single_mb_per_s=2, multi_mb_per_s=10, status_code=206, range_ok=True,
            connect_ms=20, tls_ms=30, ttfb_ms=40, confidence="normal")

    monkeypatch.setattr(engine_mod.engine_manager, "engine", FakeEngine(), raising=False)
    monkeypatch.setattr(speedtest, "_ensure_sources", fake_sources)
    monkeypatch.setattr(speedtest, "_test_source", fake_source)
    run = asyncio.run(speedtest.run_node(node_id, trigger="manual", prepared=True))
    assert run["status"] == "partial"
    assert run["bytes_total"] == 1234
    assert run["connect_ms"] == 20
    assert run["mb_per_s"] == 10
    assert run["grade"] == "excellent"


def test_failed_source_does_not_consume_budget(monkeypatch):
    import asyncio
    from backend.testers import engine as engine_mod

    _, node_id = _node("香港11")
    node = db.row("SELECT * FROM nodes WHERE id=?", (node_id,))
    sources = [
        {"id": 11, "name": "A", "url": "https://example.com/a", "region": ""},
        {"id": 12, "name": "B", "url": "https://example.com/b", "region": ""},
    ]
    calls = []

    class FakeEngine:
        name = "mihomo"
        block_ports = {node["fingerprint"]: 1082}

    async def fake_sources(primary):
        return sources

    async def fake_confirm(source):
        return False

    async def fake_source(source, port, node, settings, max_bytes=None):
        calls.append((source["id"], max_bytes))
        if source["id"] == 11:
            return speedtest.SourceResult(
                source, False, bytes=5 * 1024 * 1024, error_type="status",
                message="HTTP 403")
        return speedtest.SourceResult(
            source, True, bytes=5 * 1024 * 1024, seconds=5, single_mbps=10,
            multi_mbps=40, single_mb_per_s=1.25, multi_mb_per_s=5,
            status_code=206, range_ok=True, confidence="normal")

    monkeypatch.setattr(engine_mod.engine_manager, "engine", FakeEngine(), raising=False)
    monkeypatch.setattr(speedtest, "_ensure_sources", fake_sources)
    monkeypatch.setattr(speedtest, "_test_source", fake_source)
    monkeypatch.setattr(speedtest, "_confirm_source_failure", fake_confirm)
    run = asyncio.run(speedtest.run_node(node_id, trigger="manual", prepared=True))
    assert run["status"] == "partial"
    # A fails both attempts; its 10 MB is not charged. B still gets the full 50 MB.
    assert calls[0] == (11, 50 * 1024 * 1024)
    assert calls[1] == (11, 50 * 1024 * 1024)
    assert calls[2] == (12, 50 * 1024 * 1024)
    assert run["bytes_total"] == 15 * 1024 * 1024


def test_successful_source_charges_remaining_cap(monkeypatch):
    import asyncio
    from backend.testers import engine as engine_mod

    _, node_id = _node("香港12")
    node = db.row("SELECT * FROM nodes WHERE id=?", (node_id,))
    sources = [
        {"id": 21, "name": "A", "url": "https://example.com/a", "region": ""},
        {"id": 22, "name": "B", "url": "https://example.com/b", "region": ""},
    ]
    calls = []

    class FakeEngine:
        name = "mihomo"
        block_ports = {node["fingerprint"]: 1082}

    async def fake_sources(primary):
        return sources

    async def fake_source(source, port, node, settings, max_bytes=None):
        calls.append((source["id"], max_bytes))
        return speedtest.SourceResult(
            source, True, bytes=5 * 1024 * 1024, seconds=5, single_mbps=10,
            multi_mbps=40, single_mb_per_s=1.25, multi_mb_per_s=5,
            status_code=206, range_ok=True, confidence="normal")

    monkeypatch.setattr(engine_mod.engine_manager, "engine", FakeEngine(), raising=False)
    monkeypatch.setattr(speedtest, "_ensure_sources", fake_sources)
    monkeypatch.setattr(speedtest, "_test_source", fake_source)
    run = asyncio.run(speedtest.run_node(node_id, trigger="manual", prepared=True))
    assert run["status"] == "ok"
    assert calls[0] == (21, 50 * 1024 * 1024)
    assert calls[1] == (22, 45 * 1024 * 1024)


def test_recent_direct_source_check_blocks_false_source_failure(monkeypatch):
    import asyncio

    source_id = db.execute(
        "INSERT INTO speed_sources (name, region, url, priority, enabled, health, range_ok, "
        "size_bytes, last_check_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("T", "", "https://example.com/probe", 1, 1, "ok", 1, 104857600,
         datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S"),
         "2026-11-25 09:00:00", "2026-11-25 09:00:00"),
    )
    src = db.row("SELECT * FROM speed_sources WHERE id=?", (source_id,))

    async def should_not_run(source_ids=None):
        raise AssertionError("a recent direct check should be trusted")

    monkeypatch.setattr(speedtest, "check_sources", should_not_run)
    assert asyncio.run(speedtest._confirm_source_failure(src)) is False

    async def direct_fail(source_ids=None):
        return [{"source_id": source_id, "ok": False}]

    db.execute("UPDATE speed_sources SET last_check_at='2026-11-24 10:00:00' WHERE id=?",
               (source_id,))
    src = db.row("SELECT * FROM speed_sources WHERE id=?", (source_id,))
    monkeypatch.setattr(speedtest, "check_sources", direct_fail)
    assert asyncio.run(speedtest._confirm_source_failure(src)) is True


def test_new_node_test_includes_expired_results():
    _, node_id = _node("香港10")
    _valid_run(node_id, 3.0, finished_at="2026-11-01 10:00:00")
    assert speedtest.eligible_node_ids(new_only=True) == [node_id]
    assert node_id in speedtest.expired_node_ids()


def test_speed_label_exclusion_is_global_and_checked_means_drop():
    _, fast = _node("香港09")
    _, slow = _node("香港10")
    _valid_run(fast, 6.0)
    _valid_run(slow, 0.1)
    nodes = [
        {"id": fast, "display_name": "🟣测速机场·香港09", "airport_id": 1,
         "speed": {"status": "ok", "grade": "excellent"}},
        {"id": slow, "display_name": "🔴测速机场·香港10", "airport_id": 1,
         "speed": {"status": "ok", "grade": "danger"}},
        {"id": 999, "display_name": "⚠️测速机场·异常", "airport_id": 1,
         "speed": {"status": "error", "grade": "excellent"}},
    ]
    db.set_setting("sub_filter_speed_levels", "优,劣")
    try:
        assert builder.speed_filter_tags() == ["excellent", "danger"]
        kept, dropped = builder.apply_speed_filter(nodes)
        assert [n["id"] for n in kept] == [999]
        assert {d["grade"] for d in dropped} == {"excellent", "danger"}
        assert all(d["reason_type"] == "speed" for d in dropped)
        assert builder.parse_speed_filter_tags("优,危") == {"excellent", "danger"}
        assert builder.parse_speed_filter_tags("🟡") == {"poor"}
    finally:
        db.set_setting("sub_filter_speed_levels", "")


def test_speed_exclusion_ignores_pending_and_failed_states():
    _, node_id = _node("香港11")
    nodes = [{"id": node_id, "display_name": "待测速测速机场·香港11", "airport_id": 1,
              "speed": {"status": "pending", "grade": None}}]
    db.set_setting("sub_filter_speed_levels", "excellent")
    try:
        kept, dropped = builder.apply_speed_filter(nodes)
        assert kept and not dropped
    finally:
        db.set_setting("sub_filter_speed_levels", "")


def test_publish_applies_speed_exclusion(monkeypatch, tmp_path):
    _, node_id = _node("香港12")
    _valid_run(node_id, 6.0)
    state = speedtest.speed_map()[node_id]
    entry = builder._snapshot_entry(node_id, "🟣测速机场·香港12", state)
    assert entry
    monkeypatch.setattr(builder.config, "SUB_DIR", tmp_path)
    monkeypatch.setattr(builder, "collect_nodes", lambda batch_id=None: ([entry], {"total": 1}))
    db.set_setting("sub_filter_keywords", "")
    db.set_setting("sub_filter_speed_levels", "excellent")
    try:
        result = builder.publish(reason="speed-test")
        assert result["stats"]["speed_filtered"] == 1
        assert result["stats"]["keyword_filtered"] == 0
        assert result["stats"]["after_filter"] == 0
    finally:
        db.set_setting("sub_filter_speed_levels", "")


def test_status_tags_can_be_excluded_individually():
    nodes = [
        {"id": 1, "display_name": "●已排除", "airport_id": 1,
         "speed": {"status": "excluded"}},
        {"id": 2, "display_name": "⚠️异常", "airport_id": 1,
         "speed": {"status": "error"}},
        {"id": 3, "display_name": "🚫全源失败", "airport_id": 1,
         "speed": {"status": "source_failed"}},
        {"id": 4, "display_name": "待测速", "airport_id": 1,
         "speed": {"status": "pending"}},
        {"id": 5, "display_name": "🟢良", "airport_id": 1,
         "speed": {"status": "ok", "grade": "good"}},
    ]
    db.set_setting("sub_filter_speed_levels", "已排除,异常,全源失败,待测速")
    try:
        assert builder.speed_filter_tags() == ["pending", "excluded", "error", "source_failed"]
        kept, dropped = builder.apply_speed_filter(nodes)
        assert [n["id"] for n in kept] == [5]
        assert {d["speed_tag"] for d in dropped} == {"excluded", "error", "source_failed", "pending"}
    finally:
        db.set_setting("sub_filter_speed_levels", "")
