"""Airport fetch pipeline: download -> parse -> validate -> transactional replace."""
import asyncio
import time
from datetime import datetime, timedelta

import httpx

from backend import config
from backend.database import db
from backend.parsers import subscription as parser

MAX_BYTES = 20 * 1024 * 1024


async def _download(url: str) -> str:
    """One fixed UA per request: switching identities repeatedly triggers risk control."""
    headers = {"User-Agent": config.HTTP_UA, "Accept": "*/*"}
    last_error = ""
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True,
                                         headers=headers, trust_env=True) as client:
                response = await client.get(url)
                if response.status_code >= 400:
                    last_error = f"服务器返回 HTTP {response.status_code}"
                else:
                    text = response.text
                    if len(text.encode()) > MAX_BYTES:
                        raise ValueError("订阅文件超过 20MB")
                    return text
        except ValueError as exc:
            last_error = str(exc)
        except Exception as exc:
            name = type(exc).__name__
            friendly = {
                "ConnectTimeout": "连接超时",
                "ReadTimeout": "读取超时",
                "TimeoutException": "请求超时",
                "ConnectError": "无法连接服务器",
                "ReadError": "读取响应失败",
                "RemoteProtocolError": "服务器响应异常",
                "TooManyRedirects": "重定向次数过多",
                "InvalidURL": "订阅地址无效",
                "SSLError": "TLS 证书校验失败",
            }.get(name, "网络请求异常")
            last_error = friendly
        await asyncio.sleep(1.5 * (attempt + 1))
    raise RuntimeError(last_error or "下载失败")


def normalize_keywords(value) -> list[str]:
    """Accept a JSON string or a list; trim, de-duplicate and cap for safety."""
    if isinstance(value, str):
        parsed = db.loads(value, [])
    else:
        parsed = value or []
    if not isinstance(parsed, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in parsed:
        word = str(item or "").strip()
        if not word or len(word) > 24:
            continue
        key = word.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(word)
        if len(out) >= 40:
            break
    return out


def keyword_for(name: str, keywords: list[str]) -> str:
    """First configured keyword contained in the raw node name ('' = keep)."""
    target = (name or "").casefold()
    for word in keywords:
        if word.casefold() in target:
            return word
    return ""


def _params_json(row: dict) -> str:
    value = row["params"]
    return value if isinstance(value, str) else db.dumps(value)


def _upsert_filtered(conn, airport_id: int, row: dict, keyword: str, admitted: int, stamp: str) -> None:
    conn.execute(
        """INSERT INTO airport_filtered_nodes
           (airport_id, raw_name, protocol, server, port, fingerprint, params, region, keyword,
            admitted, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(airport_id, fingerprint) DO UPDATE SET
             raw_name=excluded.raw_name, protocol=excluded.protocol, server=excluded.server,
             port=excluded.port, params=excluded.params, region=excluded.region,
             keyword=excluded.keyword, admitted=excluded.admitted, updated_at=excluded.updated_at""",
        (airport_id, row["raw_name"], row["protocol"], row["server"], row["port"],
         row["fingerprint"], _params_json(row), row.get("region"), keyword,
         admitted, stamp, stamp),
    )


def _upsert_node(conn, airport_id: int, row: dict, stamp: str) -> None:
    conn.execute(
        """INSERT INTO nodes (airport_id, raw_name, protocol, server, port, fingerprint,
           params, region, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(airport_id, fingerprint) DO UPDATE SET
             raw_name=excluded.raw_name, protocol=excluded.protocol, server=excluded.server,
             port=excluded.port, params=excluded.params, region=excluded.region,
             updated_at=excluded.updated_at""",
        (airport_id, row["raw_name"], row["protocol"], row["server"], row["port"],
         row["fingerprint"], _params_json(row), row.get("region"), stamp, stamp),
    )


def _upsert(airport_id: int, fetched: list[dict], keywords: list[str] | None = None) -> dict:
    """All-or-nothing update of one airport's nodes. Old cache survives any failure.

    Nodes whose raw name hits a filter keyword never enter `nodes`; they are kept
    in `airport_filtered_nodes` so nothing tests or publishes them. Manually
    admitted fingerprints stay in the pool even while they still match.
    """
    stamp = db.now()
    keywords = normalize_keywords(keywords or [])
    placeholders = []
    inserted = 0
    updated = 0
    filtered_added = 0
    with db.transaction() as conn:
        existing = {
            r["fingerprint"]: r["id"]
            for r in conn.execute("SELECT id, fingerprint FROM nodes WHERE airport_id=?",
                                  (airport_id,)).fetchall()
        }
        admitted = {
            r["fingerprint"]
            for r in conn.execute(
                "SELECT fingerprint FROM airport_filtered_nodes WHERE airport_id=? AND admitted=1",
                (airport_id,)).fetchall()
        }
        existing_filtered = {
            r["fingerprint"] for r in conn.execute(
                "SELECT fingerprint FROM airport_filtered_nodes WHERE airport_id=?",
                (airport_id,)).fetchall()
        }
        for node in fetched:
            word = keyword_for(node["raw_name"], keywords)
            if word and node["fingerprint"] not in admitted:
                _upsert_filtered(conn, airport_id, node, word, 0, stamp)
                filtered_added += 1
                continue
            if node["fingerprint"] in existing:
                conn.execute(
                    """UPDATE nodes SET raw_name=?, protocol=?, server=?, port=?, params=?, region=?,
                       updated_at=? WHERE id=?""",
                    (node["raw_name"], node["protocol"], node["server"], node["port"],
                     db.dumps(node["params"]), node["region"], stamp,
                     existing[node["fingerprint"]]),
                )
                updated += 1
            else:
                _upsert_node(conn, airport_id, node, stamp)
                inserted += 1
            placeholders.append(node["fingerprint"])
            if word and node["fingerprint"] in admitted:
                # keep the admission record so the next fetch does not filter it again
                _upsert_filtered(conn, airport_id, node, word, 1, stamp)
            elif node["fingerprint"] in existing_filtered:
                conn.execute(
                    "DELETE FROM airport_filtered_nodes WHERE airport_id=? AND fingerprint=?",
                    (airport_id, node["fingerprint"]),
                )
        if placeholders:
            conn.execute(
                "DELETE FROM nodes WHERE airport_id=? AND fingerprint NOT IN"
                f" ({','.join('?' * len(placeholders))})",
                (airport_id, *placeholders),
            )
        else:
            conn.execute("DELETE FROM nodes WHERE airport_id=?", (airport_id,))
        # Drop filtered records that disappeared from the source subscription
        all_fps = [n["fingerprint"] for n in fetched]
        if all_fps:
            conn.execute(
                "DELETE FROM airport_filtered_nodes WHERE airport_id=? AND fingerprint NOT IN"
                f" ({','.join('?' * len(all_fps))})",
                (airport_id, *all_fps),
            )
        else:
            conn.execute("DELETE FROM airport_filtered_nodes WHERE airport_id=?", (airport_id,))
        count = conn.execute("SELECT COUNT(*) AS c FROM nodes WHERE airport_id=?",
                             (airport_id,)).fetchone()["c"]
        filtered_count = conn.execute(
            "SELECT COUNT(*) AS c FROM airport_filtered_nodes WHERE airport_id=? AND admitted=0",
            (airport_id,)).fetchone()["c"]
        total_count = count + filtered_count
        conn.execute(
            """UPDATE airports SET node_count=?, last_status='ok', last_error=NULL
               WHERE id=?""",
            (count, airport_id),
        )
    return {"inserted": inserted, "updated": updated, "total": count,
            "filtered": filtered_count, "filtered_added": filtered_added,
            "total_count": total_count}


def filtered_nodes(airport_id: int) -> list[dict]:
    return db.rows(
        "SELECT id, airport_id, raw_name, protocol, server, port, fingerprint, region,"
        " keyword, admitted, updated_at FROM airport_filtered_nodes"
        " WHERE airport_id=? AND admitted=0 ORDER BY id",
        (airport_id,),
    )


def filtered_count(airport_id: int) -> int:
    row = db.row(
        "SELECT COUNT(*) AS c FROM airport_filtered_nodes WHERE airport_id=? AND admitted=0",
        (airport_id,),
    )
    return row["c"] if row else 0


def reclassify_airport(airport_id: int, keywords: list[str] | None = None,
                       publish: bool = False) -> dict:
    """Apply keywords to nodes already cached, without re-downloading.

    Pool node that now matches -> move to filtered. Filtered (not admitted) node
    that no longer matches -> move back to the pool. Admitted rows stay in the
    pool; their admission record is dropped once the name no longer matches.
    """
    airport = airport_by_id(airport_id)
    if not airport:
        return {"ok": False, "error": "机场不存在"}
    kws = normalize_keywords(keywords if keywords is not None else airport["filter_keywords"])
    stamp = db.now()
    moved_out = 0
    moved_in = 0
    with db.transaction() as conn:
        admitted = {
            r["fingerprint"]
            for r in conn.execute(
                "SELECT fingerprint FROM airport_filtered_nodes WHERE airport_id=? AND admitted=1",
                (airport_id,)).fetchall()
        }
        for node in conn.execute("SELECT * FROM nodes WHERE airport_id=?", (airport_id,)).fetchall():
            word = keyword_for(node["raw_name"], kws)
            if not word:
                continue
            if node["fingerprint"] in admitted:
                # manually admitted nodes always stay in the pool
                conn.execute(
                    "UPDATE airport_filtered_nodes SET keyword=?, updated_at=? "
                    "WHERE airport_id=? AND fingerprint=?",
                    (word, stamp, airport_id, node["fingerprint"]),
                )
                continue
            _upsert_filtered(conn, airport_id, dict(node), word, 0, stamp)
            conn.execute("DELETE FROM nodes WHERE id=?", (node["id"],))
            moved_out += 1
        for item in conn.execute(
            "SELECT * FROM airport_filtered_nodes WHERE airport_id=? AND admitted=0",
            (airport_id,),
        ).fetchall():
            word = keyword_for(item["raw_name"], kws)
            if word:
                conn.execute(
                    "UPDATE airport_filtered_nodes SET keyword=?, updated_at=? WHERE id=?",
                    (word, stamp, item["id"]),
                )
                continue
            _upsert_node(conn, airport_id, dict(item), stamp)
            conn.execute("DELETE FROM airport_filtered_nodes WHERE id=?", (item["id"],))
            moved_in += 1
        for item in conn.execute(
            "SELECT * FROM airport_filtered_nodes WHERE airport_id=? AND admitted=1",
            (airport_id,),
        ).fetchall():
            word = keyword_for(item["raw_name"], kws)
            if word:
                conn.execute(
                    "UPDATE airport_filtered_nodes SET keyword=?, updated_at=? WHERE id=?",
                    (word, stamp, item["id"]),
                )
            else:
                conn.execute("DELETE FROM airport_filtered_nodes WHERE id=?", (item["id"],))
        count = conn.execute("SELECT COUNT(*) AS c FROM nodes WHERE airport_id=?",
                             (airport_id,)).fetchone()["c"]
        filtered = conn.execute(
            "SELECT COUNT(*) AS c FROM airport_filtered_nodes WHERE airport_id=? AND admitted=0",
            (airport_id,)).fetchone()["c"]
        total_count = count + filtered
        conn.execute("UPDATE airports SET node_count=?, updated_at=? WHERE id=?",
                     (count, stamp, airport_id))
    result = {"ok": True, "moved_out": moved_out, "moved_in": moved_in, "node_count": total_count,
              "pool_count": count, "filtered": filtered_count(airport_id),
              "total_count": total_count}
    if moved_out or moved_in:
        db.append_log(
            "INFO", "过滤",
            f"{airport['name']}：按关键词重新归类，{moved_out} 个移入过滤、{moved_in} 个恢复节点池",
        )
    if publish and (moved_out or moved_in):
        from backend.subscriptions import builder
        try:
            builder.publish(reason="airport filter changed")
        except Exception as exc:
            db.append_log("WARN", "下发", f"过滤关键词更新后重建订阅失败：{exc}")
    return result


def admit_filtered_node(airport_id: int, fingerprint: str) -> dict | None:
    """Move one filtered node into the pool and remember the manual admission."""
    airport = airport_by_id(airport_id)
    item = db.row(
        "SELECT * FROM airport_filtered_nodes WHERE airport_id=? AND fingerprint=? AND admitted=0",
        (airport_id, fingerprint),
    )
    if not item:
        return None
    stamp = db.now()
    with db.transaction() as conn:
        _upsert_node(conn, airport_id, dict(item), stamp)
        conn.execute(
            "UPDATE airport_filtered_nodes SET admitted=1, updated_at=? WHERE id=?",
            (stamp, item["id"]),
        )
        count = conn.execute("SELECT COUNT(*) AS c FROM nodes WHERE airport_id=?",
                             (airport_id,)).fetchone()["c"]
        filtered = conn.execute(
            "SELECT COUNT(*) AS c FROM airport_filtered_nodes WHERE airport_id=? AND admitted=0",
            (airport_id,)).fetchone()["c"]
        total_count = count + filtered
        conn.execute("UPDATE airports SET node_count=?, updated_at=? WHERE id=?",
                     (count, stamp, airport_id))
    if airport:
        db.append_log("INFO", "过滤", f"{airport['name']}：手动加入节点池 {item['raw_name'][:80]}")
    from backend.subscriptions import builder
    try:
        builder.publish(reason="filtered node admitted")
    except Exception as exc:
        db.append_log("WARN", "下发", f"过滤节点加入节点池后重建订阅失败：{exc}")
    return {"ok": True, "node_count": total_count, "pool_count": count,
            "filtered": filtered_count(airport_id), "total_count": total_count}


def migrate_filter_defaults() -> int:
    """One-time upgrade for existing databases: seed default keywords and reclassify."""
    if db.get("filter_defaults_migrated") == "1":
        return 0
    changed = 0
    for airport in all_airports():
        if normalize_keywords(airport.get("filter_keywords")):
            continue
        db.execute("UPDATE airports SET filter_keywords=? WHERE id=?",
                   (db.dumps(config.DEFAULT_FILTER_KEYWORDS), airport["id"]))
        result = reclassify_airport(airport["id"], config.DEFAULT_FILTER_KEYWORDS)
        changed += result.get("moved_out", 0)
    db.set_setting("filter_defaults_migrated", "1")
    return changed


async def fetch_airport(airport: dict, trigger: str = "schedule") -> dict:
    airport_id = airport["id"]
    started = time.perf_counter()
    started_at = db.now()
    db.execute("UPDATE airports SET last_attempt_at=? WHERE id=?", (started_at, airport_id))
    try:
        text = await _download(airport["url"])
        nodes, errors, parse_stats = parser.parse_text_full(text)
        # de-duplicate inside this airport before touching the database
        uniq: dict[str, dict] = {}
        for node in nodes:
            uniq.setdefault(node["fingerprint"], node)
        duplicates = len(nodes) - len(uniq)
        if not uniq:
            raise ValueError(f"未解析到支持的节点（{'; '.join(errors) or '无法识别内容'}）")
        keywords = normalize_keywords(airport.get("filter_keywords"))
        stats = _upsert(airport_id, list(uniq.values()), keywords)
        duration = int((time.perf_counter() - started) * 1000)
        trig_cn = {"schedule": "定时拉取", "manual": "手动拉取", "startup": "启动拉取"}.get(trigger, trigger)
        db.execute(
            "UPDATE airports SET last_success_at=?, last_status='ok', last_error=NULL, updated_at=? WHERE id=?",
            (db.now(), db.now(), airport_id),
        )
        db.execute(
            "INSERT INTO fetch_logs (airport_id, ts, ok, node_count, duration_ms, message) VALUES (?,?,?,?,?,?)",
            (airport_id, db.now(), 1, stats["total_count"], duration,
             f"{trig_cn}：新增 {stats['inserted']}，更新 {stats['updated']}"
             + (f"，过滤 {stats['filtered']}" if stats.get("filtered") else "") + "；"
             f"解析 {parse_stats['parsed']}/{parse_stats['total']}"
             + (f"，去重 {duplicates}" if duplicates else "")
             + (f"，跳过 {', '.join(f'{k}×{v}' for k, v in parse_stats['unsupported'].items())}"
                if parse_stats["unsupported"] else "")
             + f"，UA {config.HTTP_UA}"),
        )
        unsupported = parse_stats["unsupported"]
        if unsupported:
            errors = [f"跳过 {sum(unsupported.values())} 个不支持的节点："
                      + "、".join(f"{k}×{v}" for k, v in unsupported.items())] + list(errors)
        db.append_log("INFO", "拉取",
                      f"{airport['name']}：共 {stats['total_count']} 个节点 "
                      + (f"（节点池 {stats['total']}，过滤 {stats['filtered']}）"
                         if stats.get("filtered") else "")
                      + f"（解析 {parse_stats['parsed']}/{parse_stats['total']}"
                      + (f"，去重 {duplicates}" if duplicates else "") + f"，{trig_cn}，耗时 {duration}ms）")
        return {"ok": True, **stats, "duration_ms": duration, "warnings": errors[:10],
                "parse": parse_stats, "duplicates": duplicates}
    except Exception as exc:
        duration = int((time.perf_counter() - started) * 1000)
        detail = str(exc).strip()
        if detail.startswith(("服务器返回", "订阅文件", "未解析到支持", "下载失败")):
            message = detail[:300]
        else:
            message = "拉取失败，请查看系统日志"
            db.append_log("ERROR", "拉取",
                          f"机场 {airport['name']} 拉取失败：{type(exc).__name__}：{exc}")
        db.execute(
            "UPDATE airports SET last_status='error', last_error=?, updated_at=? WHERE id=?",
            (message, db.now(), airport_id),
        )
        db.execute(
            "INSERT INTO fetch_logs (airport_id, ts, ok, node_count, duration_ms, message) VALUES (?,?,?,?,?,?)",
            (airport_id, db.now(), 0, 0, duration, message),
        )
        db.append_log("ERROR", "拉取", f"{airport['name']} 拉取失败：{message}")
        return {"ok": False, "error": message, "duration_ms": duration}


def all_airports(enabled_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM airports"
    if enabled_only:
        sql += " WHERE enabled=1"
    return db.rows(sql + " ORDER BY id")


def airport_by_id(airport_id: int) -> dict | None:
    return db.row("SELECT * FROM airports WHERE id=?", (airport_id,))


def parse_interval(value) -> int:
    try:
        return max(1, int(float(value)))
    except (TypeError, ValueError):
        return 60


def next_run_at(airport: dict, now: datetime | None = None) -> datetime | None:
    """When this airport will fetch next on its own; None = manual only."""
    now = now or datetime.now().astimezone()
    daily = (airport.get("daily_time") or "").strip()
    interval = int(airport.get("interval_minutes") or 0)
    last_raw = airport.get("last_attempt_at")
    last = None
    if last_raw:
        try:
            last = datetime.fromisoformat(last_raw)
            if last.tzinfo is None:
                last = last.astimezone()
        except ValueError:
            last = None
    candidates: list[datetime] = []
    if daily:
        try:
            hour, _, minute = daily.partition(":")
            slot = now.replace(hour=int(hour), minute=int(minute or 0), second=0, microsecond=0)
        except ValueError:
            slot = None
        if slot is not None:
            if last is not None and last >= slot:
                slot = slot + timedelta(days=1)
            candidates.append(slot)
    if interval > 0:
        candidates.append((last or now) + timedelta(minutes=interval))
    return min(candidates) if candidates else None


def due_airports(now: datetime | None = None) -> list[dict]:
    now = now or datetime.now().astimezone()
    due = []
    for airport in all_airports(enabled_only=True):
        daily = (airport.get("daily_time") or "").strip()
        interval = int(airport.get("interval_minutes") or 0)
        if not daily and interval <= 0:
            continue   # both schedules off: manual fetch only
        last_attempt = airport["last_attempt_at"]
        if not last_attempt:
            due.append(airport)
            continue
        try:
            last = datetime.fromisoformat(last_attempt)
        except ValueError:
            due.append(airport)
            continue
        if last.tzinfo is None:
            last = last.astimezone()

        if daily:
            try:
                hour, _, minute = daily.partition(":")
                slot = now.replace(hour=int(hour), minute=int(minute or 0), second=0, microsecond=0)
            except ValueError:
                slot = None
            if slot is not None and now >= slot and last < slot:
                # daily slot wins, once per day, at or after the configured wall clock time
                due.append(airport)
                continue

        elapsed = (now - last).total_seconds() / 60
        if interval and elapsed >= interval:
            due.append(airport)
    return due
