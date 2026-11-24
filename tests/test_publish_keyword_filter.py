"""Regression: 下发关键词过滤只剔除订阅输出，不影响检测与评分统计。"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_TMP = tempfile.mkdtemp(prefix="np-kwfilter-")
os.environ["NODEPOOL_DATA_DIR"] = _TMP
os.environ["NODEPOOL_CONFIG_DIR"] = os.path.join(_TMP, "config")
os.environ["NODEPOOL_LOG_DIR"] = os.path.join(_TMP, "logs")
os.environ["NODEPOOL_BACKUP_DIR"] = os.path.join(_TMP, "backup")

from backend.database import db  # noqa: E402
from backend.subscriptions import builder  # noqa: E402


def _prepare(name: str = "kw-airport") -> tuple[int, int]:
    db.init_db()
    db.execute("UPDATE test_groups SET enabled=1")
    airport = db.execute(
        "INSERT INTO airports (name, url, interval_minutes, created_at, updated_at, enabled) "
        "VALUES (?,?,30,datetime('now'),datetime('now'),1)", (name, "http://x"))
    node_id = db.execute(
        "INSERT INTO nodes (airport_id, raw_name, protocol, server, port, params, fingerprint, "
        "status, latency, fail_count, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))",
        (airport, "官网剩余流量节点", "ss", "1.1.1.1", 443,
         db.dumps({"cipher": "aes-128-gcm", "password": "pw"}), "fp-kw", "available", 120, 0))
    for target in ("G", "W"):
        db.execute("INSERT INTO latency_results (node_id, target, batch_id, ok, latency_ms, error_type, message, ts) "
                   "VALUES (?,?,?,?,?,?,?,?)", (node_id, target, None, 1, 120, None, "", db.now()))
    return airport, node_id


def test_filter_keywords_parsing():
    db.init_db()
    db.set_setting("sub_filter_keywords", "  剩余流量\n# 注释\n官网, 过期，某机场\n\n剩余流量")
    assert builder.filter_keywords() == ["剩余流量", "官网", "过期", "某机场"]
    db.set_setting("sub_filter_keywords", "")
    assert builder.filter_keywords() == []


def test_publish_excludes_keyword_nodes_but_keeps_scoring_counts():
    _prepare("老狗")
    db.set_setting("sub_filter_keywords", "官网\n剩余流量")
    unfiltered, _ = builder.collect_nodes()
    assert len(unfiltered) == 1                      # 达标节点仍在检测/评分侧可见
    kept, dropped = builder.apply_name_filter(unfiltered)
    assert kept == [] and dropped and dropped[0]["keyword"] in ("官网", "剩余流量")

    result = builder.publish(reason="kw")
    # 全部被过滤时保留上一次订阅（不会写出空订阅）
    assert result["stats"]["filtered"] == 1 and result["stats"]["after_filter"] == 0
    # 评分用的快照仍记录这个达标节点，下发率不受过滤影响
    snapshot = builder.share_history(7)[-1]["airports"]
    assert snapshot[str(unfiltered[0]["airport_id"])]["published_count"] == 1


def test_filter_matches_airport_name():
    _prepare("小机场-X")
    db.set_setting("sub_filter_keywords", "小机场")
    kept, dropped = builder.apply_name_filter(builder.collect_nodes()[0])
    assert kept == [] and dropped[0]["keyword"] == "小机场"


def test_filter_off_keeps_all_nodes():
    _prepare("正常机场")
    db.set_setting("sub_filter_keywords", "")
    kept, dropped = builder.apply_name_filter(builder.collect_nodes()[0])
    assert len(kept) == 1 and dropped == []
