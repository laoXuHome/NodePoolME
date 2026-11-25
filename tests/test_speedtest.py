from backend.testers import speedtest


def test_range_windows_cover_file_without_overlap():
    windows = speedtest.range_windows(104857600, 4)
    assert windows[0][0] == 0
    assert windows[-1][1] == 104857599
    for left, right in zip(windows, windows[1:]):
        assert left[1] + 1 == right[0]
    assert len({w for pair in windows for w in pair}) == 8


def test_parse_content_range():
    assert speedtest.parse_content_range("bytes 0-262143/104857600") == (0, 262143, 104857600)
    assert speedtest.parse_content_range("bytes 10-20/*") == (10, 20, 0)
    assert speedtest.parse_content_range("garbage") is None


def test_default_sources_are_https_and_seeded():
    sources = speedtest.list_sources()
    assert len(sources) >= 8
    assert all(s["url"].startswith("https://") for s in sources)
    assert any("vultr" in s["url"] for s in sources)
    assert all(s["health"] in ("unknown", "ok", "fail") for s in sources)


def test_latest_run_and_history_are_isolated_per_node():
    from backend.database import db
    airport_id = db.execute("INSERT INTO airports (name, url, enabled, created_at, updated_at) "
                            "VALUES ('t','https://e.com/s',1,'2026-11-25 09:00:00','2026-11-25 09:00:00')")
    node_id = db.execute(
        "INSERT INTO nodes (airport_id, raw_name, protocol, server, port, fingerprint, params, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (airport_id, "n1", "ss", "1.2.3.4", 443, "fp-speed-1", "{}",
         "2026-11-25 09:00:00", "2026-11-25 09:00:00"),
    )
    run_id = db.execute(
        "INSERT INTO speed_runs (node_id, started_at, status, confidence, multi_mbps, single_mbps, "
        "sources_total, sources_ok) VALUES (?,?,?,?,?,?,?,?)",
        (node_id, "2026-11-25 10:00:00", "ok", "normal", 88.5, 32.1, 3, 3),
    )
    db.execute(
        "INSERT INTO speed_samples (run_id, node_id, source_id, source_name, source_region, started_at, "
        "bytes, seconds, single_mbps, multi_mbps, status_code, range_ok, ok, confidence) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, node_id, 1, "Vultr 新加坡", "SG", "2026-11-25 10:00:00", 100, 1.0, 32.1, 88.5, 206, 1, 1, "normal"),
    )
    latest = speedtest.latest_by_node()
    assert latest[node_id]["status"] == "ok"
    assert latest[node_id]["multi_mbps"] == 88.5
    history = speedtest.node_history(node_id)
    assert len(history) == 1
    assert history[0]["samples"][0]["source_region"] == "SG"


def test_speed_results_do_not_change_score_fields():
    from backend.database import db
    airport_id = db.execute("INSERT INTO airports (name, url, enabled, created_at, updated_at) "
                            "VALUES ('t','https://e.com/s',1,'2026-11-25 09:00:00','2026-11-25 09:00:00')")
    node_id = db.execute(
        "INSERT INTO nodes (airport_id, raw_name, protocol, server, port, fingerprint, params, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (airport_id, "n2", "ss", "1.2.3.4", 443, "fp-speed-2", "{}",
         "2026-11-25 09:00:00", "2026-11-25 09:00:00"),
    )
    before = db.row("SELECT status, fail_count FROM nodes WHERE id=?", (node_id,))
    db.execute("INSERT INTO speed_runs (node_id, started_at, status) VALUES (?,?,?)",
               (node_id, "2026-11-25 10:00:00", "ok"))
    after = db.row("SELECT status, fail_count FROM nodes WHERE id=?", (node_id,))
    assert before == after == {"status": "unknown", "fail_count": 0}
    assert db.row("SELECT COUNT(*) AS c FROM speed_runs WHERE node_id=?", (node_id,))["c"] == 1
