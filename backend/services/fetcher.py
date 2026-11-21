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
                    last_error = f"HTTP {response.status_code}"
                else:
                    text = response.text
                    if len(text.encode()) > MAX_BYTES:
                        raise ValueError("subscription larger than 20MB")
                    return text
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        await asyncio.sleep(1.5 * (attempt + 1))
    raise RuntimeError(last_error or "download failed")


def _upsert(airport_id: int, fetched: list[dict]) -> dict:
    """All-or-nothing update of one airport's nodes. Old cache survives any failure."""
    stamp = db.now()
    placeholders = []
    inserted = 0
    updated = 0
    with db.transaction() as conn:
        existing = {
            r["fingerprint"]: r["id"]
            for r in conn.execute("SELECT id, fingerprint FROM nodes WHERE airport_id=?",
                                  (airport_id,)).fetchall()
        }
        for node in fetched:
            params = db.dumps(node["params"])
            if node["fingerprint"] in existing:
                conn.execute(
                    """UPDATE nodes SET raw_name=?, protocol=?, server=?, port=?, params=?, region=?,
                       updated_at=? WHERE id=?""",
                    (node["raw_name"], node["protocol"], node["server"], node["port"], params,
                     node["region"], stamp, existing[node["fingerprint"]]),
                )
                updated += 1
            else:
                conn.execute(
                    """INSERT INTO nodes (airport_id, raw_name, protocol, server, port, fingerprint,
                       params, region, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (airport_id, node["raw_name"], node["protocol"], node["server"], node["port"],
                     node["fingerprint"], params, node["region"], stamp, stamp),
                )
                inserted += 1
            placeholders.append(node["fingerprint"])
        if placeholders:
            conn.execute(
                "DELETE FROM nodes WHERE airport_id=? AND fingerprint NOT IN"
                f" ({','.join('?' * len(placeholders))})",
                (airport_id, *placeholders),
            )
        else:
            conn.execute("DELETE FROM nodes WHERE airport_id=?", (airport_id,))
        count = conn.execute("SELECT COUNT(*) AS c FROM nodes WHERE airport_id=?",
                             (airport_id,)).fetchone()["c"]
        conn.execute(
            """UPDATE airports SET node_count=?, last_status='ok', last_error=NULL
               WHERE id=?""",
            (count, airport_id),
        )
    return {"inserted": inserted, "updated": updated, "total": count}


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
            raise ValueError(f"no supported node parsed ({'; '.join(errors) or 'unknown content'})")
        stats = _upsert(airport_id, list(uniq.values()))
        duration = int((time.perf_counter() - started) * 1000)
        trig_cn = {"schedule": "定时拉取", "manual": "手动拉取", "startup": "启动拉取"}.get(trigger, trigger)
        db.execute(
            "UPDATE airports SET last_success_at=?, last_status='ok', last_error=NULL, updated_at=? WHERE id=?",
            (db.now(), db.now(), airport_id),
        )
        db.execute(
            "INSERT INTO fetch_logs (airport_id, ts, ok, node_count, duration_ms, message) VALUES (?,?,?,?,?,?)",
            (airport_id, db.now(), 1, stats["total"], duration,
             f"{trig_cn}：新增 {stats['inserted']}，更新 {stats['updated']}；"
             f"解析 {parse_stats['parsed']}/{parse_stats['total']}"
             + (f"，去重 {duplicates}" if duplicates else "")
             + (f"，跳过 {', '.join(f'{k}×{v}' for k, v in parse_stats['unsupported'].items())}"
                if parse_stats["unsupported"] else "")
             + f"，UA {config.HTTP_UA}"),
        )
        unsupported = parse_stats["unsupported"]
        if unsupported:
            errors = [f"{sum(unsupported.values())} nodes skipped: "
                      + ", ".join(f"{k}×{v}" for k, v in unsupported.items())] + list(errors)
        db.append_log("INFO", "拉取",
                      f"{airport['name']}：共 {stats['total']} 个节点 "
                      f"（解析 {parse_stats['parsed']}/{parse_stats['total']}"
                      + (f"，去重 {duplicates}" if duplicates else "") + f"，{trig_cn}，耗时 {duration}ms）")
        return {"ok": True, **stats, "duration_ms": duration, "warnings": errors[:10],
                "parse": parse_stats, "duplicates": duplicates}
    except Exception as exc:
        duration = int((time.perf_counter() - started) * 1000)
        message = f"{type(exc).__name__}: {exc}"[:300]
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
