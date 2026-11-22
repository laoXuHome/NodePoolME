"""Dual-target, best-of-three latency gate: both Google and Wikipedia must pass."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_TMP = tempfile.mkdtemp(prefix="np-dual-")
os.environ["NODEPOOL_DATA_DIR"] = _TMP
os.environ["NODEPOOL_CONFIG_DIR"] = os.path.join(_TMP, "config")
os.environ["NODEPOOL_LOG_DIR"] = os.path.join(_TMP, "logs")
os.environ["NODEPOOL_BACKUP_DIR"] = os.path.join(_TMP, "backup")

from backend.database import db  # noqa: E402
from backend.subscriptions import builder  # noqa: E402

ROUNDS = {
    # name: ([(ok, ms), ...] for G, [(ok, ms), ...] for W); three rounds each
    "g-slow": ([(False, 1500), (False, 1400), (False, 1300)],
               [(True, 300), (True, 320), (True, 310)]),
    "w-fallback": ([(True, 1200), (True, 800), (True, 900)],
                   [(False, 1300), (False, 1100), (False, 1200)]),
    "both-ok": ([(True, 900), (False, 1000), (False, 950)],
                [(True, 950), (False, 1100), (True, 700)]),
    "third-rescue": ([(False, 1100), (False, 1050), (True, 980)],
                     [(False, 1200), (False, 1150), (True, 950)]),
}


def _seed():
    db.init_db()
    ap = db.execute("INSERT INTO airports (name,url,interval_minutes,created_at,updated_at,enabled) "
                    "VALUES ('双测','http://x',30,datetime('now'),datetime('now'),1)")
    db.set_setting("sub_max_latency", 1000)
    db.set_setting("sub_max_latency2", 1000)
    ids = {}
    for name, (g_rounds, w_rounds) in ROUNDS.items():
        ids[name] = db.execute(
            "INSERT INTO nodes (airport_id,raw_name,protocol,server,port,params,fingerprint,status,"
            "latency,fail_count,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))",
            (ap, name, "ss", "1.1.1.1", 443,
             db.dumps({"cipher": "aes-128-gcm", "password": "pw"}), name, "available", 0, 0))
        for target, rounds in (("G", g_rounds), ("W", w_rounds)):
            for ok, ms in rounds:
                db.execute("INSERT INTO latency_results (node_id,target,batch_id,ok,latency_ms,"
                           "error_type,message,ts) VALUES (?,?,NULL,?,?,?, '',?)",
                           (ids[name], target, 1 if ok else 0, ms,
                            None if ok else "timeout", db.now()))
    return ids


def test_dual_best_of_three():
    ids = _seed()
    latests = builder.latest_latency()
    assert latests[ids["g-slow"]]["ok"] is False
    assert latests[ids["g-slow"]]["targets"]["G"]["best"] is None
    assert latests[ids["g-slow"]]["targets"]["W"]["best"] == 300
    # Display values keep the fastest measured round even when the target failed the gate.
    assert latests[ids["g-slow"]]["targets"]["G"]["min"] == 1300
    assert latests[ids["g-slow"]]["targets"]["W"]["min"] == 300
    assert latests[ids["g-slow"]]["fastest_ms"] == 300

    assert latests[ids["w-fallback"]]["ok"] is False
    assert latests[ids["w-fallback"]]["targets"]["G"]["best"] == 800
    assert latests[ids["w-fallback"]]["targets"]["W"]["best"] is None
    assert latests[ids["w-fallback"]]["targets"]["G"]["min"] == 800
    assert latests[ids["w-fallback"]]["targets"]["W"]["min"] == 1100

    assert latests[ids["both-ok"]]["ok"] is True
    assert latests[ids["both-ok"]]["targets"]["G"]["best"] == 900
    assert latests[ids["both-ok"]]["targets"]["W"]["best"] == 700
    assert latests[ids["both-ok"]]["latency_ms"] == 900  # worst of the two best values
    assert latests[ids["both-ok"]]["targets"]["G"]["min"] == 900
    assert latests[ids["both-ok"]]["targets"]["W"]["min"] == 700
    assert latests[ids["both-ok"]]["fastest_ms"] == 700

    # Third-round success must count; keeping only the newest two rounds would drop it.
    assert latests[ids["third-rescue"]]["ok"] is True
    assert latests[ids["third-rescue"]]["targets"]["G"]["best"] == 980
    assert latests[ids["third-rescue"]]["targets"]["W"]["best"] == 950
    assert latests[ids["third-rescue"]]["fastest_ms"] == 950

    nodes, stats = builder.collect_nodes()
    assert [n["raw_name"] for n in nodes] == ["both-ok", "third-rescue"], nodes
    assert stats["skipped"]["latency_fail"] == 2, stats


def test_ceiling_is_per_target():
    ids = _seed()
    db.set_setting("sub_max_latency2", 600)
    latests = builder.latest_latency()
    # both-ok W best 700 now exceeds its own 600 ceiling -> whole node fails
    assert latests[ids["both-ok"]]["ok"] is False
    assert builder.collect_nodes()[0] == []


if __name__ == "__main__":
    test_dual_best_of_three()
    test_ceiling_is_per_target()
    print("PASS dual-target latency gate")
