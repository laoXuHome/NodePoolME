"""Manual test adds one node to the live subscription without republishing everything."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_TMP = tempfile.mkdtemp(prefix="np-inc-add-")
os.environ["NODEPOOL_DATA_DIR"] = _TMP
os.environ["NODEPOOL_CONFIG_DIR"] = os.path.join(_TMP, "config")
os.environ["NODEPOOL_LOG_DIR"] = os.path.join(_TMP, "logs")
os.environ["NODEPOOL_BACKUP_DIR"] = os.path.join(_TMP, "backup")

from backend.database import db  # noqa: E402
from backend.subscriptions import builder  # noqa: E402


def _latency(node, ms):
    for target in ("G", "W"):
        db.execute("INSERT INTO latency_results (node_id,target,batch_id,ok,latency_ms,error_type,"
                   "message,ts) VALUES (?,?,NULL,1,?,NULL,'',?)", (node, target, ms, db.now()))


def _wipe():
    for table in ("latency_results", "results", "nodes", "airports", "batches"):
        db.execute(f"DELETE FROM {table}")


def _seed():
    db.init_db()
    _wipe()
    ap = db.execute("INSERT INTO airports (name,url,interval_minutes,created_at,updated_at,enabled) "
                    "VALUES ('机场A','http://x',30,datetime('now'),datetime('now'),1)")
    ids = {}
    for name in ("a", "b", "c"):
        ids[name] = db.execute(
            "INSERT INTO nodes (airport_id,raw_name,protocol,server,port,params,fingerprint,status,"
            "latency,fail_count,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))",
            (ap, name, "ss", "1.1.1.1", 443,
             db.dumps({"cipher": "aes-128-gcm", "password": "pw"}), f"fp-{name}", "available", 100, 0))
    _latency(ids["a"], 100)
    _latency(ids["b"], 120)
    return ids


def test_incremental_add():
    ids = _seed()
    first = builder.publish(reason="test")
    assert first["nodes"] == 2
    stamp = first["updated_at"]
    assert set(first["node_ids"]) == {ids["a"], ids["b"]}

    # a later failing test for A must not remove it: we only append
    for target in ("G", "W"):
        db.execute("INSERT INTO latency_results (node_id,target,batch_id,ok,latency_ms,error_type,"
                   "message,ts) VALUES (?,?,NULL,0,2000,'timeout','',?)", (ids["a"], target, db.now()))
    _latency(ids["c"], 300)

    added = builder.add_published_node(ids["c"])
    assert added["ok"] and added["added"] is True, added
    assert added["nodes"] == 3
    assert added["updated_at"] == stamp, (stamp, added["updated_at"])
    assert added["batch_id"] == first["batch_id"]
    assert set(added["node_ids"]) == {ids["a"], ids["b"], ids["c"]}

    clash = builder.read("clash")[1]
    assert "机场A⁃a" in clash and "机场A⁃b" in clash and "机场A⁃c" in clash
    assert db.get("sub_last_generated_at") == stamp  # date displays stay put

    again = builder.add_published_node(ids["c"])
    assert again["added"] is False and again["nodes"] == 3, again


def test_legacy_manifest_falls_back_to_live_file():
    ids = _seed()
    first = builder.publish(reason="legacy")
    meta_path = __import__("backend").config.SUB_DIR / "meta.json"
    import json
    manifest = json.loads(meta_path.read_text(encoding="utf-8"))
    manifest.pop("node_ids", None)
    manifest.pop("node_names", None)
    meta_path.write_text(json.dumps(manifest), encoding="utf-8")
    for target in ("G", "W"):
        db.execute("INSERT INTO latency_results (node_id,target,batch_id,ok,latency_ms,error_type,"
                   "message,ts) VALUES (?,?,NULL,0,2000,'timeout','',?)", (ids["a"], target, db.now()))
    _latency(ids["c"], 300)
    added = builder.add_published_node(ids["c"])
    assert added["added"] and added["nodes"] == 3, added
    assert set(added["node_ids"]) == {ids["a"], ids["b"], ids["c"]}


def test_no_subscription_is_noop():
    db.set_setting("sub_node_count", "0")
    for fmt, (filename, _) in builder.FORMATS.items():
        path = __import__("backend").config.SUB_DIR / filename
        if path.exists():
            path.unlink()
    import json
    meta_path = __import__("backend").config.SUB_DIR / "meta.json"
    if meta_path.exists():
        meta_path.unlink()
    res = builder.add_published_node(999)
    assert res["ok"] is False and res["reason"] == "当前还没有已生成的订阅", res


if __name__ == "__main__":
    test_incremental_add()
    print("PASS incremental add")
    test_legacy_manifest_falls_back_to_live_file()
    print("PASS legacy manifest fallback")
    test_no_subscription_is_noop()
    print("PASS no-subscription noop")
