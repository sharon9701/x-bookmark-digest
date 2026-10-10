#!/usr/bin/env python3
"""Local state and I/O helpers for an X bookmark/like digest."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None


ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT.parent
CONFIG_PATH = ROOT / "config.json"
UTC = timezone.utc
CANONICAL_SOURCES = {"bookmark", "like"}
CLI_SOURCES = {"bookmark": "bookmarks", "like": "likes"}
ALLOWED_STATUSES = {"backlog", "unread", "keep", "read", "removed"}


class DigestError(RuntimeError):
    """Expected, user-actionable failure with a stable error kind."""

    def __init__(self, message: str, kind: str = "error", exit_code: int = 2):
        super().__init__(message)
        self.kind = kind
        self.exit_code = exit_code


def load_config() -> dict[str, Any]:
    config_path = Path(os.environ.get("X_BOOKMARK_DIGEST_CONFIG", str(CONFIG_PATH))).expanduser()
    try:
        value = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DigestError(f"cannot read config file {config_path}: {exc}", "config_failure") from exc
    if not isinstance(value, dict):
        raise DigestError("config.json must contain an object", "config_failure")
    return value


def defaults() -> dict[str, Any]:
    value = load_config().get("defaults", {})
    return value if isinstance(value, dict) else {}


def configured_timezone() -> ZoneInfo:
    name = str(defaults().get("timezone") or "Asia/Shanghai")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise DigestError(f"unknown timezone in config.json: {name}", "config_failure") from exc


def now_iso() -> str:
    """Return a canonical UTC timestamp for SQLite comparisons."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except (TypeError, ValueError):
        return None


def local_date(value: str | None = None) -> str:
    moment = parse_iso(value) if value else datetime.now(UTC)
    if moment is None:
        moment = datetime.now(UTC)
    return moment.astimezone(configured_timezone()).date().isoformat()


def resolve_path(value: str | os.PathLike[str] | None, fallback: Path) -> Path:
    if value is None or str(value).strip() == "":
        return fallback
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    if path.parts and path.parts[0] == ROOT.name:
        return PROJECT_ROOT / path
    return ROOT / path


def default_db() -> Path:
    return resolve_path(defaults().get("database"), ROOT / "data" / "state.sqlite3")


def json_loads_output(raw: str) -> Any:
    """Decode JSON even when opencli prefixes logs or appends notices."""
    if not raw or not raw.strip():
        raise DigestError("opencli returned empty output", "malformed_json")
    decoder = json.JSONDecoder()
    for match in re.finditer(r"[\[{]", raw):
        try:
            value, _ = decoder.raw_decode(raw[match.start() :])
            return value
        except json.JSONDecodeError:
            continue
    raise DigestError("opencli returned malformed JSON", "malformed_json")


def unwrap_rows(value: Any) -> list[dict[str, Any]]:
    """Accept documented list/wrapper shapes and reject schema drift."""
    if isinstance(value, list):
        if any(not isinstance(row, dict) for row in value):
            raise DigestError("opencli JSON list contains a non-object row", "schema_failure")
        return value
    if isinstance(value, dict):
        if any(key in value for key in ("error", "errors", "exception")):
            raise DigestError("opencli reported an error in JSON output", "cli_failure")
        for key in ("data", "items", "results", "tweets", "bookmarks", "likes"):
            if key in value:
                return unwrap_rows(value[key])
        if value.get("id") or value.get("tweet_id"):
            return [value]
    raise DigestError("opencli JSON shape is not a tweet list", "schema_failure")


def canonical_source(source: str) -> str:
    return {"bookmarks": "bookmark", "likes": "like"}.get(source, source)


def normalize_sources(values: Iterable[str]) -> list[str]:
    return sorted({canonical_source(str(value)) for value in values if canonical_source(str(value)) in CANONICAL_SOURCES})


def as_count(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def derive_title(text: str) -> str:
    """Keep an original first line when X does not expose a formal title."""
    lines = [" ".join(line.split()) for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return "原贴未提供标题"
    title = lines[0].strip()
    if title.startswith(("http://", "https://")):
        return "原贴未提供标题"
    return title[:160]


def normalize_media(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list):
        return []
    urls: list[str] = []
    for entry in value:
        if isinstance(entry, str):
            urls.append(entry)
        elif isinstance(entry, dict):
            url = entry.get("url") or entry.get("media_url") or entry.get("src") or entry.get("original_url")
            if url:
                urls.append(str(url))
    return urls


def normalize_row(row: dict[str, Any], source: str) -> dict[str, Any]:
    source = canonical_source(source)
    tweet_id = row.get("id") or row.get("tweet_id") or row.get("tweetId")
    if not tweet_id:
        raise DigestError("a tweet row has no id; opencli output may have changed", "schema_failure")
    article = row.get("article") if isinstance(row.get("article"), dict) else {}
    title = row.get("title") or row.get("article_title") or article.get("title") or derive_title(row.get("text") or row.get("full_text") or "")
    media = normalize_media(row.get("media_urls") or row.get("media") or [])
    return {
        "id": str(tweet_id),
        "url": row.get("url") or row.get("tweet_url") or f"https://x.com/i/status/{tweet_id}",
        "author": row.get("author") or row.get("username") or row.get("screen_name") or "",
        "name": row.get("name") or "",
        "title": str(title).strip() or "原贴未提供标题",
        "text": row.get("text") or row.get("full_text") or "",
        "created_at": row.get("created_at") or row.get("createdAt") or "",
        "likes": as_count(row.get("likes") or row.get("likeCount")),
        "retweets": as_count(row.get("retweets") or row.get("retweetCount")),
        "bookmarks": as_count(row.get("bookmarks") or row.get("bookmarkCount")),
        "sources": [source],
        "media_urls": media,
    }


def opencli_path() -> str:
    path = shutil.which("opencli")
    if not path:
        raise DigestError("opencli is not installed or is not on PATH", "cli_unavailable")
    return path


def cli_error_kind(detail: str) -> str:
    lowered = detail.lower()
    if any(word in lowered for word in ("login", "logged in", "auth", "session", "unauthorized", "forbidden")):
        return "auth_failure"
    return "cli_failure"


def run_cli(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    path = opencli_path()
    proc = subprocess.run([path, *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise DigestError(f"opencli {' '.join(args)} failed: {detail[-1000:]}", cli_error_kind(detail))
    return proc


def preflight(expected_username: str | None = None) -> str:
    """Check executable, account/session, and both read commands before sync."""
    path = opencli_path()
    help_proc = subprocess.run([path, "twitter", "--help"], capture_output=True, text=True)
    help_text = f"{help_proc.stdout}\n{help_proc.stderr}"
    if help_proc.returncode != 0:
        detail = help_text.strip()
        raise DigestError(f"cannot inspect opencli twitter commands: {detail[-800:]}", "cli_failure")
    missing = [name for name in CLI_SOURCES.values() if not re.search(rf"(?m)^\s*{re.escape(name)}(?:\s|\[|$)", help_text)]
    if missing:
        raise DigestError(f"opencli lacks required X read command(s): {', '.join(missing)}", "cli_unavailable")
    proc = subprocess.run(
        [path, "twitter", "whoami", "-f", "json", "--window", "background", "--site-session", "persistent"],
        capture_output=True,
        text=True,
    )
    detail = (proc.stderr or proc.stdout).strip()
    if proc.returncode != 0:
        raise DigestError(f"cannot verify X login with whoami: {detail[-800:]}", cli_error_kind(detail))
    try:
        info = json_loads_output(proc.stdout)
    except DigestError as exc:
        raise DigestError("whoami did not return readable account JSON", "auth_failure") from exc
    if not isinstance(info, dict) or info.get("logged_in") is not True:
        raise DigestError("X session is not logged in; opencli twitter whoami reported no active account", "auth_failure")
    username = str(info.get("username") or info.get("screen_name") or "").strip().lstrip("@")
    if not username:
        raise DigestError("whoami did not identify the logged-in X account", "auth_failure")
    expected = (expected_username or "").strip().lstrip("@")
    if expected and username.casefold() != expected.casefold():
        raise DigestError(f"logged-in X account is @{username}, expected @{expected}", "auth_failure")
    return username


def fetch_source(source: str, limit: int) -> list[dict[str, Any]]:
    source = canonical_source(source)
    if source not in CANONICAL_SOURCES:
        raise DigestError(f"unsupported source: {source}", "config_failure")
    proc = run_cli(
        [
            "twitter",
            CLI_SOURCES[source],
            "--limit",
            str(limit),
            "-f",
            "json",
            "--window",
            "background",
            "--site-session",
            "persistent",
        ]
    )
    rows = unwrap_rows(json_loads_output(proc.stdout))
    return [normalize_row(row, source) for row in rows]


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS items (
            id TEXT PRIMARY KEY,
            url TEXT NOT NULL,
            author TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            text TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT '',
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            sources_json TEXT NOT NULL,
            media_json TEXT NOT NULL,
            likes INTEGER NOT NULL DEFAULT 0,
            retweets INTEGER NOT NULL DEFAULT 0,
            bookmarks INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'unread',
            category TEXT,
            summary TEXT,
            decision_note TEXT,
            last_digest_at TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS review_operations (
            operation_id TEXT PRIMARY KEY,
            session_path TEXT NOT NULL,
            item_id TEXT NOT NULL,
            action TEXT NOT NULL,
            result TEXT NOT NULL,
            completed_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_items_pending ON items(status, first_seen_at, last_digest_at);
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(items)").fetchall()}
    if "title" not in columns:
        conn.execute("ALTER TABLE items ADD COLUMN title TEXT NOT NULL DEFAULT ''")
    if "latest_source_observed_at" not in columns:
        conn.execute("ALTER TABLE items ADD COLUMN latest_source_observed_at TEXT")
    for row in conn.execute("SELECT id, title, text FROM items WHERE title IS NULL OR title='' ").fetchall():
        conn.execute("UPDATE items SET title=? WHERE id=?", (derive_title(row["text"]), row["id"]))
    for row in conn.execute("SELECT id, sources_json FROM items").fetchall():
        try:
            old = json.loads(row["sources_json"])
            new = normalize_sources(old)
        except (TypeError, json.JSONDecodeError) as exc:
            raise DigestError(f"invalid sources_json for item {row['id']}", "state_failure") from exc
        if old != new:
            conn.execute("UPDATE items SET sources_json=? WHERE id=?", (json.dumps(new), row["id"]))
    conn.commit()
    return conn


def merge_sources(old: Iterable[str], new: Iterable[str]) -> list[str]:
    return normalize_sources([*old, *new])


def source_additions(conn: sqlite3.Connection, rows: Iterable[dict[str, Any]]) -> dict[str, list[str]]:
    """Compare source membership, not just whether the tweet already exists."""
    known = {row['id']: set(json.loads(row['sources_json']))
             for row in conn.execute('SELECT id,sources_json FROM items')}
    additions: dict[str, list[str]] = {source: [] for source in sorted(CANONICAL_SOURCES)}
    for item in rows:
        present = known.setdefault(item['id'], set())
        for source in normalize_sources(item['sources']):
            if source not in present:
                additions[source].append(item['id'])
                present.add(source)
    return additions


def ingest(conn: sqlite3.Connection, rows: Iterable[dict[str, Any]], bootstrap: bool = False) -> int:
    now = now_iso()
    count = 0
    for item in rows:
        existing = conn.execute("SELECT * FROM items WHERE id = ?", (item["id"],)).fetchone()
        if existing:
            old_sources = normalize_sources(json.loads(existing["sources_json"]))
            sources = merge_sources(old_sources, item["sources"])
            conn.execute(
                """UPDATE items SET url=?, author=?, name=?, title=?, text=?, created_at=?, last_seen_at=?,
                   sources_json=?, media_json=?, likes=?, retweets=?, bookmarks=?, updated_at=? WHERE id=?""",
                (
                    item["url"], item["author"], item["name"], item["title"], item["text"], item["created_at"], now,
                    json.dumps(sources, ensure_ascii=False), json.dumps(item["media_urls"], ensure_ascii=False),
                    item["likes"], item["retweets"], item["bookmarks"], now, item["id"],
                ),
            )
            if not bootstrap and set(sources) - set(old_sources):
                conn.execute(
                    "UPDATE items SET latest_source_observed_at=?, last_digest_at=NULL, "
                    "status=CASE WHEN status='keep' THEN 'keep' ELSE 'unread' END WHERE id=?",
                    (now, item['id']),
                )
        else:
            conn.execute(
                """INSERT INTO items (id,url,author,name,title,text,created_at,first_seen_at,last_seen_at,
                   sources_json,media_json,likes,retweets,bookmarks,status,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    item["id"], item["url"], item["author"], item["name"], item["title"], item["text"], item["created_at"],
                    now, now, json.dumps(normalize_sources(item["sources"]), ensure_ascii=False),
                    json.dumps(item["media_urls"], ensure_ascii=False), item["likes"], item["retweets"],
                    item["bookmarks"], "backlog" if bootstrap else "unread", now,
                ),
            )
            count += 1
    if bootstrap:
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('bootstrapped_at',?)", (now,))
    conn.commit()
    return count


def establish_baseline(conn: sqlite3.Connection) -> bool:
    """Initialize an empty database only; never reclassify existing observations."""
    if conn.execute("SELECT 1 FROM meta WHERE key='bootstrapped_at'").fetchone():
        return False
    if conn.execute('SELECT 1 FROM items LIMIT 1').fetchone():
        raise DigestError('existing observations require an explicit migration; refusing to reset their baseline or reminder times', 'state_failure')
    now = now_iso()
    conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('bootstrapped_at',?)", (now,))
    conn.commit()
    return True


def previous_local_day_window(moment: datetime | None = None) -> tuple[str, str, str]:
    """Return UTC start/end timestamps and the prior local calendar date."""
    timezone = configured_timezone()
    local_now = (moment or datetime.now(UTC)).astimezone(timezone)
    target = local_now.date() - timedelta(days=1)
    start_local = datetime.combine(target, datetime.min.time(), tzinfo=timezone)
    end_local = start_local + timedelta(days=1)

    def iso(value: datetime) -> str:
        return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    return iso(start_local), iso(end_local), target.isoformat()


def row_to_item(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    try:
        result["sources"] = normalize_sources(json.loads(result.pop("sources_json")))
        result["media_urls"] = json.loads(result.pop("media_json"))
    except (TypeError, json.JSONDecodeError) as exc:
        raise DigestError(f"invalid local state for item {row['id']}", "state_failure") from exc
    return result


def observation_diagnostics(conn: sqlite3.Connection, since: str, until: str, now: str) -> dict[str, Any]:
    """Explain observations excluded by a calendar filter without inventing like dates."""
    local_now = (parse_iso(now) or datetime.now(UTC)).astimezone(configured_timezone())
    day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC).isoformat().replace('+00:00', 'Z')
    today = [dict(row) for row in conn.execute(
        'SELECT id,status,first_seen_at,latest_source_observed_at FROM items '
        'WHERE COALESCE(latest_source_observed_at, first_seen_at)>=? '
        'AND COALESCE(latest_source_observed_at, first_seen_at)<=? ORDER BY first_seen_at,id',
        (day_start, now),
    )]
    window_rows = conn.execute(
        'SELECT status,count(*) AS count FROM items '
        'WHERE COALESCE(latest_source_observed_at, first_seen_at)>=? '
        'AND COALESCE(latest_source_observed_at, first_seen_at)<? GROUP BY status',
        (since, until),
    ).fetchall()
    return {
        'time_basis': 'local_observation_not_x_action_time',
        'actual_added_at_available': False,
        'today_observed_count': len(today),
        'today_observed': today,
        'window_observed_by_status': {row['status']: row['count'] for row in window_rows},
        'empty_means': 'No eligible local observations; this does not prove no X likes/bookmarks were added on the target date.',
    }


def pending(
    conn: sqlite3.Connection,
    hours: float,
    include_backlog: bool,
    *,
    claim: bool = True,
    force: bool = False,
    max_items: int = 50,
    window: str = "rolling",
) -> dict[str, Any]:
    if hours <= 0:
        raise DigestError("--hours must be greater than zero", "config_failure")
    if max_items < 0:
        raise DigestError("--max-items must be zero or greater", "config_failure")
    if window not in ("rolling", "previous_day"):
        raise DigestError("window must be rolling or previous_day", "config_failure")
    now = now_iso()
    now_moment = parse_iso(now) or datetime.now(UTC)
    rolling_since = now_moment - timedelta(hours=hours)
    if window == "previous_day":
        since_text, until_text, target_date = previous_local_day_window(now_moment)
        since = parse_iso(since_text) or rolling_since
    else:
        since = rolling_since
        since_text = since.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        until_text = now
        target_date = None
    statuses = ("unread", "keep") + (("backlog",) if include_backlog else ())
    marks = ",".join("?" for _ in statuses)
    dedupe = "1=1" if force else "(last_digest_at IS NULL OR last_digest_at < ?)"
    params: list[Any] = [*statuses]
    if not force:
        params.append((now_moment - timedelta(hours=hours)).replace(microsecond=0).isoformat().replace("+00:00", "Z"))
    if window == "previous_day":
        first_seen_clause = "(status IN ('keep','backlog') OR (status='unread' AND (last_digest_at IS NULL OR (COALESCE(latest_source_observed_at,first_seen_at) >= ? AND COALESCE(latest_source_observed_at,first_seen_at) < ?))))"
        params.extend([since_text, until_text])
    else:
        first_seen_clause = "(status IN ('keep','backlog') OR (status='unread' AND (last_digest_at IS NULL OR COALESCE(latest_source_observed_at,first_seen_at) >= ?)))"
        params.append(since_text)
    limit_clause = "" if max_items == 0 else " LIMIT ?"
    if max_items:
        params.append(max_items)
    rows = conn.execute(
        f"""SELECT * FROM items WHERE status IN ({marks})
            AND {dedupe}
            AND {first_seen_clause}
            ORDER BY CASE status WHEN 'unread' THEN 0 WHEN 'keep' THEN 1 ELSE 2 END,
                     created_at DESC, first_seen_at DESC{limit_clause}""",
        params,
    ).fetchall()
    items = [row_to_item(row) for row in rows]
    if claim and items:
        conn.executemany("UPDATE items SET last_digest_at=?, updated_at=? WHERE id=?", [(now, now, item["id"]) for item in items])
        conn.commit()
    return {
        "generated_at": now,
        "since": since.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "until": until_text,
        "window": window,
        "target_date": target_date,
        "timezone": str(configured_timezone()),
        "claimed": claim,
        "count": len(items),
        "max_items": max_items,
        "diagnostics": observation_diagnostics(conn, since_text, until_text, now),
        "items": items,
    }


def category_names() -> set[str]:
    categories = load_config().get("categories", [])
    names = {str(item.get("name")) for item in categories if isinstance(item, dict) and item.get("name")}
    names.add("待复核")
    return names


def category_order() -> list[str]:
    """Return configured category order, with the review bucket last."""
    categories = load_config().get("categories", [])
    ordered = [
        str(item["name"])
        for item in categories
        if isinstance(item, dict) and item.get("name") and str(item["name"]) != "待复核"
    ]
    return ordered + ["待复核"]


def validate_annotation_item(item: Any, names: set[str]) -> None:
    if not isinstance(item, dict) or not str(item.get("id") or ""):
        raise DigestError("each annotation must be an object with an id", "annotation_failure")
    category = item.get("category")
    if category not in names:
        raise DigestError(f"invalid category for {item['id']}: {category!r}", "annotation_failure")
    title = item.get("title")
    if not isinstance(title, str) or not title.strip() or len(title) > 160:
        raise DigestError(f"title must be non-empty and at most 160 characters for {item['id']}", "annotation_failure")
    secondary = item.get("secondary_category")
    if secondary is not None and secondary not in names:
        raise DigestError(f"invalid secondary_category for {item['id']}", "annotation_failure")
    confidence = item.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise DigestError(f"confidence must be a number from 0 to 1 for {item['id']}", "annotation_failure")
    summary = item.get("summary")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 80:
        raise DigestError(f"summary must be non-empty and at most 80 characters for {item['id']}", "annotation_failure")
    points = item.get("key_points")
    if not isinstance(points, list) or len(points) > 8 or any(not isinstance(point, str) for point in points):
        raise DigestError(f"key_points must be a list of at most eight strings for {item['id']}", "annotation_failure")
    points_length = sum(len("".join(point.split())) for point in points)
    if points_length > 200:
        raise DigestError(f"key_points must stay within 200 characters for {item['id']} (got {points_length})", "annotation_failure")
    action = item.get("recommended_action")
    if not isinstance(action, str) or not action.strip():
        raise DigestError(f"recommended_action is required for {item['id']}", "annotation_failure")
    if item.get("priority") not in (1, 2, 3):
        raise DigestError(f"priority must be 1, 2, or 3 for {item['id']}", "annotation_failure")
    if confidence < 0.6 and category != "待复核":
        raise DigestError(f"low-confidence item {item['id']} must use 待复核", "annotation_failure")


def validate_annotations(annotations: dict[str, Any]) -> None:
    if not isinstance(annotations, dict) or not isinstance(annotations.get("items"), list):
        raise DigestError("annotations JSON must contain an items list", "annotation_failure")
    seen: set[str] = set()
    names = category_names()
    for item in annotations["items"]:
        validate_annotation_item(item, names)
        if item["id"] in seen:
            raise DigestError(f"duplicate annotation id: {item['id']}", "annotation_failure")
        seen.add(item["id"])


def media_url_for_preview(url: str) -> str:
    if "pbs.twimg.com/media/" in url and "name=" not in url:
        joiner = "&" if "?" in url else "?"
        return f"{url}{joiner}format=jpg&name=large"
    return url


def media_lines(item: dict[str, Any]) -> list[str]:
    urls = item.get("media_urls") or []
    if not urls:
        return []
    inline_previews = bool(defaults().get("inline_media_previews", False))
    lines = ["媒体："]
    entries: list[tuple[int, str, str, str]] = []
    for index, raw_url in enumerate(urls, 1):
        url = str(raw_url)
        preview = media_url_for_preview(url)
        lower = url.lower()
        is_video = any(ext in lower for ext in (".mp4", ".mov", ".webm", "video.twimg.com"))
        is_image = any(ext in lower for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp")) or "pbs.twimg.com/media/" in lower
        kind = "video" if is_video else "image" if is_image else "media"
        entries.append((index, url, preview, kind))
    image_entries = [entry for entry in entries if entry[3] == "image"]
    if image_entries and not inline_previews:
        lines.append(f"- 原贴包含 {len(image_entries)} 张配图（聊天内嵌已关闭，请打开原帖查看）")
    for index, url, preview, kind in entries:
        if kind == "video":
            lines.append(f"- 视频 {index}：[播放/打开媒体]({url})")
        elif kind == "image":
            if inline_previews:
                lines.append(f"![原贴配图 {index}]({preview})")
                lines.append(f"  [图片加载失败时打开媒体链接]({url})")
            else:
                lines.append(f"  - 图片 {index}：[打开原贴图片]({url})")
        else:
            lines.append(f"- 媒体 {index}：[打开媒体]({url})")
    return lines


def step_card_lines(index: int, total: int, slots: int = 5) -> list[str]:
    """Render a native-Markdown approximation of the screenshot step card.

    The play glyph is a visual continuation marker, not a clickable control. The
    actual action remains the four plain-text choices at the bottom of the card.
    Five slots keep the header compact even when a queue contains many items.
    """
    if total <= 0:
        return []
    safe_index = max(1, min(index, total))
    active = max(1, min(slots, math.ceil(safe_index / total * slots)))
    progress = ["━━━━"] + ["●"] * (active - 1) + ["○"] * (slots - active)
    return [
        f"> `{' '.join(progress)}`  `▶`",
        f"> **Step {safe_index} of {total}**",
        "",
    ]


def render(annotations: dict[str, Any]) -> str:
    validate_annotations(annotations)
    generated = annotations.get("generated_at", now_iso())
    items = annotations.get("items", [])
    lines = [
        f"# X Bookmarks + Likes Digest — {local_date(generated)}",
        "",
        f"> 新增近似窗口：{annotations.get('since', '未指定')} 起；依据首次观察时间，不等同于 X 的实际喜欢/点赞或书签操作时间。时区：{annotations.get('timezone', str(configured_timezone()))}。",
        "",
        f"共 {len(items)} 条待处理内容。交互支持单条或每组 3/5 条：`1` read 并移出来源、`2` keep、`3` 纳入 Obsidian、`4 <分类>` 调整分类。",
        "",
    ]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        grouped.setdefault(item.get("category") or "待复核", []).append(item)
    order = category_order()
    for category in order + [x for x in grouped if x not in order]:
        group = grouped.get(category, [])
        if not group:
            continue
        lines += [f"## {category}（{len(group)}）", ""]
        for idx, item in enumerate(group, 1):
            author = item.get("author") or item.get("name") or "未知作者"
            sources = ", ".join(item.get("sources", []))
            lines += [
                f"### {idx}. {item.get('title') or '原贴未提供标题'}",
                f"作者：{author} · `{item['id']}`",
                f"来源：{sources}  |  [打开原帖]({item.get('url', '')})",
            ]
            lines.extend(media_lines(item))
            if item.get("media_urls"):
                lines.append("")
            lines.append(f"摘要：{item.get('summary', '待 AI 摘要')}")
            points = item.get("key_points") or []
            if points:
                lines.append("要点：")
                lines.extend(f"- {point}" for point in points[:8])
            lines.append(f"建议动作：{item['recommended_action']}")
            lines.append(f"优先级：{item['priority']}  |  置信度：{item['confidence']}")
            lines.append("")
    lines += [
        "## 决策格式", "",
        "- `1 <ID>`：标记为 read，并在确认来源后移出书签/喜欢。",
        "- `2 <ID>`：标记为 keep；下次提醒继续展示。",
        "- `3 <ID>`：纳入 Obsidian 知识库，保留标题、正文、摘要、要点、分类、媒体和原帖链接。",
        "- `4 <ID> <分类>`：调整分类，保留当前阅读状态。",
        "",
    ]
    return "\n".join(lines)


def review_progress(session: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    """Validate a frozen queue and its ordered successful decision receipts."""
    if not isinstance(session, dict):
        raise DigestError("review session must be an object", "state_failure")
    ids = session.get("item_ids")
    completed = session.get("completed")
    if (not isinstance(ids, list) or any(not isinstance(i, str) or not i for i in ids)
            or len(ids) != len(set(ids)) or not isinstance(completed, list) or len(completed) > len(ids)):
        raise DigestError("invalid review queue or completed receipts", "state_failure")
    for index, receipt in enumerate(completed):
        if (not isinstance(receipt, dict) or receipt.get("id") != ids[index]
                or receipt.get("action") not in ("read", "keep", "obsidian", "category")
                or not isinstance(receipt.get("result"), str) or not receipt["result"].strip()):
            raise DigestError("completed receipts must match the queue prefix and contain successful results", "state_failure")
    return ids, completed


def queue_fingerprint(annotations: dict[str, Any]) -> str:
    """Return a stable fingerprint for a frozen annotation queue."""
    payload = json.dumps(annotations, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


@contextmanager
def session_lock(path: Path):
    """Serialize session read/modify/write operations across local Agents.

    Unix uses an advisory lock beside the session file. On platforms without
    ``fcntl`` this is a documented no-op so offline rendering remains portable.
    """
    lock_path = path.with_name(f"{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def operation_fingerprint(session_path: str, item_id: str, action: str, category: str | None = None) -> str:
    material = "\0".join((str(Path(session_path).expanduser().resolve()), item_id, action, category or ""))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def write_obsidian_note(item: dict[str, Any], *, pending_dir: Path | None = None) -> tuple[Path, bool]:
    """Write a safe Markdown note, using a configured Vault or a pending-import folder."""
    configured = defaults().get("obsidian_vault")
    fallback = pending_dir or resolve_path(defaults().get("draft_dir"), ROOT / "runs") / "obsidian-pending"
    base = Path(str(configured)).expanduser() if configured else fallback
    note_dir = base / "X Digest"
    note_dir.mkdir(parents=True, exist_ok=True)
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", str(item["id"]))
    path = note_dir / f"{safe_id}.md"
    sources = ", ".join(item.get("sources") or [])
    lines = [
        f"# {item.get('title') or '原贴未提供标题'}",
        "",
        f"- 作者：{item.get('author') or item.get('name') or '未知作者'}",
        f"- 分类：{item.get('category') or '待复核'}",
        f"- 来源：{sources}",
        f"- 原帖：{item.get('url', '')}",
        "",
        item.get("text") or "",
        "",
        f"摘要：{item.get('summary') or ''}",
        "",
        "## 要点",
        *[f"- {point}" for point in (item.get("key_points") or [])[:8]],
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path, bool(configured)


def remove_sources(conn: sqlite3.Connection, row: sqlite3.Row, requested: set[str]) -> tuple[list[str], dict[str, str]]:
    """Remove each requested source and persist partial success immediately."""
    sources = normalize_sources(json.loads(row["sources_json"]))
    present = sorted(requested.intersection(sources))
    if not present:
        raise DigestError(f"item {row['id']} has no requested local source", "state_failure")
    removed: list[str] = []
    errors: dict[str, str] = {}
    for source in present:
        command = "unbookmark" if source == "bookmark" else "unlike"
        proc = run_cli(["twitter", command, row["url"], "--window", "background", "--site-session", "persistent"], check=False)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()
            errors[source] = f"{cli_error_kind(detail)}: {detail[-500:]}"
            continue
        removed.append(source)
        remaining = sorted(set(sources) - set(removed))
        status = "removed" if not remaining else row["status"]
        conn.execute(
            "UPDATE items SET sources_json=?, status=?, updated_at=? WHERE id=?",
            (json.dumps(remaining, ensure_ascii=False), status, now_iso(), row["id"]),
        )
        conn.commit()
    return removed, errors


def render_card(annotations: dict[str, Any], index: int | None = None,
                processed: int | None = None, session: dict[str, Any] | None = None) -> str:
    """Render the current card only; never execute decisions or mutate review state."""
    if not isinstance(annotations, dict) or not isinstance(annotations.get("items"), list):
        raise DigestError("annotations JSON must contain an items list", "annotation_failure")
    items = annotations["items"]
    prefix: list[str] = []
    if session is not None:
        if index is not None or processed is not None:
            raise DigestError("use --session or --index/--processed, not both", "state_failure")
        ids, completed = review_progress(session)
        total, processed = len(ids), len(completed)
        index = processed + 1
        if total == 0:
            return "本轮没有待处理条目。"
        if completed:
            receipt = completed[-1]
            prefix = [f"上一条已完成：{receipt['result']}。本轮已处理 {processed}/{total} 条，还剩 {total - processed} 条。", ""]
        if processed == total:
            counts = {a: sum(r['action'] == a for r in completed) for a in ("read", "keep", "obsidian", "category")}
            return "\n".join(prefix + [f"本轮 {total} 条已处理完，剩余 0 条。",
                f"read 并移出 {counts['read']} 条｜keep {counts['keep']} 条｜纳入 Obsidian {counts['obsidian']} 条｜调整分类 {counts['category']} 条。",
                "keep 条目留到下次提醒。"])
        matches = [item for item in items if isinstance(item, dict) and item.get("id") == ids[index - 1]]
        if len(matches) != 1:
            raise DigestError("provide exactly one annotation for the current review item", "annotation_failure")
        item = matches[0]
    else:
        total = len(items)
        index = 1 if index is None else index
        if not 1 <= index <= total:
            raise DigestError(f"card index must be between 1 and {total}", "state_failure")
        processed = index - 1 if processed is None else processed
        if processed != index - 1:
            raise DigestError("sequential review requires processed = index - 1", "state_failure")
        item = items[index - 1]
    # Future raw queue entries need not be annotated yet.
    validate_annotation_item(item, category_names())
    sources_list = item.get("sources")
    if (not isinstance(sources_list, list) or not sources_list
            or any(s not in CANONICAL_SOURCES for s in sources_list)):
        raise DigestError("current card must identify bookmark/like sources before offering removal", "state_failure")
    sources = "和".join({"bookmark": "书签", "like": "喜欢"}[s] for s in normalize_sources(sources_list))
    author = item.get("author") or item.get("name") or "未知作者"
    category_example = next((name for name in category_order() if name != "待复核"), "分类名")
    lines = step_card_lines(index, total) + prefix + [
        f"**{'下一条 · ' if processed else ''}X Digest｜第 {index}/{total} 条**",
        f"本轮已处理 {processed} 条｜当前待处理 {total - processed} 条（含本条）｜本条完成后剩余 {total - index} 条",
        "",
        f"**{item['title']}**",
        f"作者：{author}｜分类：{item['category']}｜来源：{sources}｜[原帖]({item.get('url', '')})",
        "",
    ]
    lines.extend(media_lines(item))
    if item.get("media_urls"):
        lines.append("")
    lines.extend([
        f"摘要：{item.get('summary', '待 AI 摘要')}",
        "",
        "**要点**",
    ])
    lines.extend(f"- {point}" for point in (item.get("key_points") or [])[:8])
    lines.extend([
        "",
        "**请选择**",
        "",
        f"1. **read**，移出{sources}",
        "2. **keep**，保留至下次提醒",
        "3. **纳入 Obsidian 知识库**",
        f"4. **调整分类**，如 `4 {category_example}`",
        "",
        (f"回复 `1`、`2`、`3` 或 `4 分类名`；成功处理后推送第 {index + 1}/{total} 条。"
         if index < total else "回复 `1`、`2`、`3` 或 `4 分类名`；成功处理后本轮结束。"),
    ])
    return "\n".join(lines)


def render_cards(annotations: dict[str, Any], start: int, count: int, processed: int | None = None) -> str:
    """Render a small batch without mutating review state."""
    if count not in (3, 5):
        raise DigestError("batch card count must be 3 or 5", "state_failure")
    items = annotations.get("items") if isinstance(annotations, dict) else None
    if not isinstance(items, list) or not items:
        raise DigestError("annotations JSON must contain an items list", "annotation_failure")
    if start < 1 or start > len(items) or start + count - 1 > len(items):
        raise DigestError("batch card range is outside the annotation queue", "state_failure")
    processed = start - 1 if processed is None else processed
    if processed != start - 1:
        raise DigestError("sequential review requires processed = start - 1", "state_failure")
    return "\n\n---\n\n".join(
        render_card(annotations, index=index, processed=index - 1)
        for index in range(start, start + count)
    )


def get_db(args: argparse.Namespace) -> Path:
    return resolve_path(args.db, default_db()) if getattr(args, "db", None) else default_db()


def cmd_sync(args: argparse.Namespace) -> int:
    if args.limit <= 0:
        raise DigestError("--limit must be greater than zero", "config_failure")
    config_defaults = defaults()
    expected = args.account or os.environ.get("X_BOOKMARK_DIGEST_USERNAME") or config_defaults.get("expected_username")
    username = preflight(str(expected) if expected else None)
    rows: list[dict[str, Any]] = []
    failures: dict[str, dict[str, str]] = {}
    for source in ("bookmark", "like"):
        try:
            rows.extend(fetch_source(source, args.limit))
        except DigestError as exc:
            failures[source] = {"kind": exc.kind, "message": str(exc)}
    if failures:
        print(json.dumps({"ok": False, "account": username, "failures": failures}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    conn = connect(get_db(args))
    first_sync = not conn.execute("SELECT 1 FROM meta WHERE key='bootstrapped_at'").fetchone()
    baseline_initialized = False
    if not failures and first_sync:
        baseline_initialized = establish_baseline(conn)
    effective_bootstrap = bool(args.bootstrap or baseline_initialized)
    additions = source_additions(conn, rows) if not effective_bootstrap else {s: [] for s in sorted(CANONICAL_SOURCES)}
    added = ingest(conn, rows, bootstrap=effective_bootstrap)
    payload = {
        "ok": not failures,
        "account": username,
        "fetched": len(rows),
        "new": added,
        "new_count_basis": "newly_inserted_tweet_ids_this_sync_not_daily_activity",
        "new_source_memberships": additions,
        "bootstrap": effective_bootstrap,
        "baseline_initialized": baseline_initialized,
        "failures": failures,
        "db": str(get_db(args)),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 2 if failures else 0


def cmd_pending(args: argparse.Namespace) -> int:
    conn = connect(get_db(args))
    payload = pending(
        conn,
        args.hours,
        args.include_backlog,
        claim=bool(args.claim and not args.preview),
        force=args.force,
        max_items=args.max_items,
        window=args.window,
    )
    output = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        path = Path(args.output).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output + "\n", encoding="utf-8")
    else:
        print(output)
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    try:
        payload = json.loads(Path(args.annotations).expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DigestError(f"cannot read annotations JSON: {exc}", "annotation_failure") from exc
    if args.output:
        output = Path(args.output).expanduser()
    else:
        draft_dir = resolve_path(defaults().get("draft_dir"), ROOT / "runs")
        output = draft_dir / f"{local_date(payload.get('generated_at'))}.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render(payload) + "\n", encoding="utf-8")
    print(output)
    return 0


def cmd_card(args: argparse.Namespace) -> int:
    try:
        payload = json.loads(Path(args.annotations).expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DigestError(f"cannot read annotations JSON: {exc}", "annotation_failure") from exc
    session = None
    if args.session:
        try:
            session = json.loads(Path(args.session).expanduser().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DigestError(f"cannot read review session: {exc}", "state_failure") from exc
        if session.get("queue_id") and session["queue_id"] != queue_fingerprint(payload):
            raise DigestError("review session does not match the annotations queue", "state_failure")
    output = render_card(payload, args.index, args.processed, session)
    if args.output:
        path = Path(args.output).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output + "\n", encoding="utf-8")
        print(path)
    else:
        print(output)
    return 0


def cmd_cards(args: argparse.Namespace) -> int:
    try:
        payload = json.loads(Path(args.annotations).expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DigestError(f"cannot read annotations JSON: {exc}", "annotation_failure") from exc
    output = render_cards(payload, args.start, args.count, args.processed)
    if args.output:
        path = Path(args.output).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output + "\n", encoding="utf-8")
        print(path)
    else:
        print(output)
    return 0


def read_json_file(path_value: str, kind: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path_value).expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DigestError(f"cannot read {kind}: {exc}", "state_failure") from exc
    if not isinstance(value, dict):
        raise DigestError(f"{kind} must contain an object", "state_failure")
    return value


def cmd_review_start(args: argparse.Namespace) -> int:
    path = Path(args.session).expanduser()
    with session_lock(path):
        return _cmd_review_start_locked(args, path)


def _cmd_review_start_locked(args: argparse.Namespace, path: Path) -> int:
    annotations = read_json_file(args.annotations, "annotations JSON")
    validate_annotations(annotations)
    ids = [item["id"] for item in annotations["items"]]
    if path.exists() and not args.replace:
        raise DigestError(f"review session already exists: {path}; pass --replace to create a new queue", "state_failure")
    session = {
        "schema_version": 2,
        "created_at": now_iso(),
        "queue_id": queue_fingerprint(annotations),
        "account": annotations.get("account") or os.environ.get("X_BOOKMARK_DIGEST_USERNAME") or None,
        "generated_at": annotations.get("generated_at"),
        "since": annotations.get("since"),
        "annotation_file": str(Path(args.annotations).expanduser().resolve()),
        "item_ids": ids,
        "completed": [],
    }
    atomic_write_json(path, session)
    if args.claim:
        conn = connect(get_db(args))
        now = now_iso()
        conn.executemany("UPDATE items SET last_digest_at=?, updated_at=? WHERE id=?", [(now, now, item_id) for item_id in ids])
        conn.commit()
    print(json.dumps({"session": str(path), "total": len(ids), "processed": 0}, ensure_ascii=False))
    return 0


def cmd_review_record(args: argparse.Namespace) -> int:
    session = read_json_file(args.session, "review session")
    ids, completed = review_progress(session)
    if len(completed) >= len(ids):
        raise DigestError("review session is already complete", "state_failure")
    expected_id = ids[len(completed)]
    if args.id != expected_id:
        raise DigestError(f"expected next item {expected_id}, received {args.id}", "state_failure")
    completed.append({"id": args.id, "action": args.action, "result": args.result, "completed_at": now_iso()})
    path = Path(args.session).expanduser()
    path.write_text(json.dumps(session, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"session": str(path), "processed": len(completed), "remaining": len(ids) - len(completed)}, ensure_ascii=False))
    return 0


def cmd_review_apply(args: argparse.Namespace) -> int:
    session_path = Path(args.session).expanduser()
    with session_lock(session_path):
        return _cmd_review_apply_locked(args, session_path)


def _cmd_review_apply_locked(args: argparse.Namespace, session_path: Path) -> int:
    """Apply one decision, update local state, and append an idempotent receipt."""
    session = read_json_file(str(session_path), "review session")
    annotations = read_json_file(args.annotations, "annotations JSON")
    validate_annotations(annotations)
    if session.get("queue_id") and session["queue_id"] != queue_fingerprint(annotations):
        raise DigestError("review session does not match the annotations queue", "state_failure")
    ids, completed = review_progress(session)
    operation_id = args.operation_id or operation_fingerprint(str(session_path), args.id, args.action, args.category)
    if any(receipt.get("operation_id") == operation_id for receipt in completed):
        result = next(receipt["result"] for receipt in completed if receipt.get("operation_id") == operation_id)
        print(json.dumps({"session": str(session_path), "id": args.id, "action": args.action,
                          "result": result, "processed": len(completed), "remaining": len(ids) - len(completed),
                          "idempotent": True}, ensure_ascii=False))
        return 0
    if len(completed) >= len(ids):
        raise DigestError("review session is already complete", "state_failure")
    expected_id = ids[len(completed)]
    if args.id != expected_id:
        raise DigestError(f"expected next item {expected_id}, received {args.id}", "state_failure")
    conn = connect(get_db(args))
    existing_operation = conn.execute(
        "SELECT result FROM review_operations WHERE operation_id=?", (operation_id,)
    ).fetchone()
    if existing_operation:
        result = existing_operation[0]
    else:
        row = conn.execute("SELECT * FROM items WHERE id=?", (args.id,)).fetchone()
        if not row:
            raise DigestError(f"unknown item id: {args.id}", "state_failure")
        matches = [item for item in annotations["items"] if item.get("id") == args.id]
        if len(matches) != 1:
            raise DigestError(f"annotation missing for item {args.id}", "annotation_failure")
        item = matches[0]
        now = now_iso()
        if args.action == "read":
            if args.confirm != f"READ:{args.id}":
                raise DigestError(f"read requires --confirm READ:{args.id}", "confirmation_required")
            requested = set(normalize_sources(json.loads(row["sources_json"])))
            removed, errors = remove_sources(conn, row, requested)
            if errors:
                raise DigestError(
                    f"X removal partially failed; removed={removed}, errors={json.dumps(errors, ensure_ascii=False)}",
                    "cli_failure",
                )
            refreshed = conn.execute("SELECT * FROM items WHERE id=?", (args.id,)).fetchone()
            final_status = "removed" if not json.loads(refreshed["sources_json"]) else "read"
            conn.execute(
                "UPDATE items SET status=?, last_digest_at=?, decision_note=?, updated_at=? WHERE id=?",
                (final_status, now, "用户选择 read 并移出来源", now, args.id),
            )
            result = args.result or f"已 read，并移出 {','.join(removed)}"
        elif args.action == "keep":
            conn.execute(
                "UPDATE items SET status='keep', last_digest_at=?, decision_note=?, updated_at=? WHERE id=?",
                (now, args.result or "用户选择 keep：保留至下次提醒", now, args.id),
            )
            result = args.result or "已标记 keep，保留至下次提醒"
        elif args.action == "category":
            if not args.category or args.category not in category_names():
                raise DigestError("category action requires a valid --category", "state_failure")
            conn.execute(
                "UPDATE items SET category=?, last_digest_at=?, decision_note=?, updated_at=? WHERE id=?",
                (args.category, now, args.result or f"已调整分类为 {args.category}", now, args.id),
            )
            result = args.result or f"已调整分类为 {args.category}"
        elif args.action == "obsidian":
            note_path, written_to_vault = write_obsidian_note(item)
            conn.execute(
                "UPDATE items SET status='read', last_digest_at=?, decision_note=?, updated_at=? WHERE id=?",
                (now, args.result or f"已生成知识库笔记：{note_path}", now, args.id),
            )
            result = args.result or (f"已写入 Obsidian：{note_path}" if written_to_vault else f"已生成待导入 Markdown：{note_path}")
        else:
            raise DigestError(f"unsupported review action: {args.action}", "state_failure")
        conn.execute(
            "INSERT INTO review_operations(operation_id,session_path,item_id,action,result,completed_at) VALUES(?,?,?,?,?,?)",
            (operation_id, str(session_path.resolve()), args.id, args.action, result, now),
        )
        conn.commit()
    if len(completed) == 0 or completed[-1].get("id") != args.id:
        completed.append({"id": args.id, "action": args.action, "result": result, "operation_id": operation_id, "completed_at": now_iso()})
        atomic_write_json(session_path, session)
    print(json.dumps({"session": str(session_path), "id": args.id, "action": args.action,
                      "result": result, "processed": len(completed), "remaining": len(ids) - len(completed)}, ensure_ascii=False))
    return 0


def cmd_review_batch(args: argparse.Namespace) -> int:
    decisions = read_json_file(args.decisions, "batch decisions")
    values = decisions.get("decisions")
    if not isinstance(values, list) or not values:
        raise DigestError("batch decisions must contain a non-empty decisions list", "state_failure")
    session = read_json_file(args.session, "review session")
    ids, completed = review_progress(session)
    expected_ids = ids[len(completed):len(completed) + len(values)]
    if len(values) > len(expected_ids):
        raise DigestError("batch decisions exceed the remaining review queue", "state_failure")
    # Validate the whole input before the first side effect. A malformed line
    # must never cause only the prefix of a user reply to be applied.
    for index, value in enumerate(values):
        if not isinstance(value, dict) or not value.get("id") or not value.get("action"):
            raise DigestError("each batch decision needs id and action", "state_failure")
        if str(value["id"]) != expected_ids[index]:
            raise DigestError(f"batch decision {index + 1} must target {expected_ids[index]}", "state_failure")
        action = str(value["action"])
        if action not in ("read", "keep", "obsidian", "category"):
            raise DigestError(f"unsupported batch action: {action}", "state_failure")
        if action == "read" and value.get("confirm") != f"READ:{value['id']}":
            raise DigestError(f"read decision for {value['id']} needs confirm READ:{value['id']}", "confirmation_required")
        if action == "category" and value.get("category") not in category_names():
            raise DigestError(f"category decision for {value['id']} needs a valid category", "state_failure")
    results = []
    for value in values:
        namespace = argparse.Namespace(
            session=args.session, annotations=args.annotations, id=str(value["id"]),
            action=str(value["action"]), category=value.get("category"),
            confirm=value.get("confirm"), result=value.get("result"),
            operation_id=value.get("operation_id"), db=args.db,
        )
        cmd_review_apply(namespace)
        results.append(value["id"])
    print(json.dumps({"processed_ids": results}, ensure_ascii=False))
    return 0


def parse_batch_reply(reply: str, session: dict[str, Any], count: int) -> dict[str, Any]:
    ids, completed = review_progress(session)
    if count not in (3, 5):
        raise DigestError("batch reply count must be 3 or 5", "state_failure")
    start = len(completed)
    available = ids[start:start + count]
    lines = [line.strip() for line in reply.splitlines() if line.strip()]
    if not lines or len(lines) > len(available):
        raise DigestError(f"reply must contain 1-{len(available)} non-empty decision lines", "state_failure")
    decisions: list[dict[str, Any]] = []
    for item_id, line in zip(available, lines):
        if line == "1":
            decisions.append({"id": item_id, "action": "read", "confirm": f"READ:{item_id}"})
        elif line == "2":
            decisions.append({"id": item_id, "action": "keep"})
        elif line == "3":
            decisions.append({"id": item_id, "action": "obsidian"})
        else:
            match = re.match(r"^4(?:\s*[|｜:]\s*|\s+)(.+?)\s*$", line)
            if not match or match.group(1) not in category_names():
                raise DigestError(f"invalid batch decision: {line}", "state_failure")
            decisions.append({"id": item_id, "action": "category", "category": match.group(1)})
    return {"decisions": decisions}


def cmd_parse_reply(args: argparse.Namespace) -> int:
    session = read_json_file(args.session, "review session")
    reply = Path(args.reply).expanduser().read_text(encoding="utf-8")
    payload = parse_batch_reply(reply, session, args.count)
    output = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        path = Path(args.output).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output, encoding="utf-8")
        print(path)
    else:
        print(output, end="")
    return 0


def cmd_mark(args: argparse.Namespace) -> int:
    conn = connect(get_db(args))
    fields = ["status = ?", "updated_at = ?"]
    values: list[Any] = [args.status, now_iso()]
    if args.category:
        if args.category not in category_names():
            raise DigestError(f"unknown category: {args.category}", "state_failure")
        fields.append("category = ?")
        values.append(args.category)
    if args.summary:
        fields.append("summary = ?")
        values.append(args.summary)
    if args.note:
        fields.append("decision_note = ?")
        values.append(args.note)
    values.append(args.id)
    cur = conn.execute(f"UPDATE items SET {', '.join(fields)} WHERE id = ?", values)
    conn.commit()
    if cur.rowcount != 1:
        raise DigestError(f"unknown item id: {args.id}", "state_failure")
    print(json.dumps({"id": args.id, "status": args.status}, ensure_ascii=False))
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    if args.confirm != "REMOVE":
        raise DigestError("refusing external mutation: pass --confirm REMOVE after explicit user confirmation", "confirmation_required")
    conn = connect(get_db(args))
    row = conn.execute("SELECT * FROM items WHERE id = ?", (args.id,)).fetchone()
    if not row:
        raise DigestError(f"unknown item id: {args.id}", "state_failure")
    sources = normalize_sources(json.loads(row["sources_json"]))
    requested = CANONICAL_SOURCES if args.source == "both" else {args.source}
    present = requested.intersection(sources)
    if not present:
        raise DigestError(f"item {args.id} has no local {args.source} source", "state_failure")
    removed, errors = remove_sources(conn, row, present)
    if errors:
        raise DigestError(f"X removal failed: {json.dumps(errors, ensure_ascii=False)}", "cli_failure")
    refreshed = conn.execute("SELECT * FROM items WHERE id=?", (args.id,)).fetchone()
    remaining = normalize_sources(json.loads(refreshed["sources_json"]))
    status = refreshed["status"]
    print(json.dumps({"id": args.id, "status": status, "removed_sources": sorted(present), "remaining_sources": remaining}, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    config_defaults = defaults()
    parser = argparse.ArgumentParser(description="X bookmarks/likes digest state manager")
    sub = parser.add_subparsers(dest="command", required=True)
    sync = sub.add_parser("sync", help="read the logged-in user's X bookmarks and likes")
    sync.add_argument("--limit", type=int, default=int(config_defaults.get("sync_limit", 200)))
    sync.add_argument("--bootstrap", action="store_true", help="mark first-seen rows as backlog")
    sync.add_argument("--account", help="expected X username; otherwise the verified account is shown")
    sync.add_argument("--db")
    sync.set_defaults(func=cmd_sync)
    pend = sub.add_parser("pending", help="emit items for AI annotation")
    pend.add_argument("--hours", type=float, default=float(config_defaults.get("digest_hours", 24)))
    pend.add_argument(
        "--window",
        choices=["previous_day", "rolling"],
        default=str(config_defaults.get("digest_window", "previous_day")),
        help="daily calendar window or rolling hours window",
    )
    pend.add_argument("--max-items", type=int, default=int(config_defaults.get("digest_max_items", 50)), help="cap one digest; 0 means no cap")
    pend.add_argument("--include-backlog", action="store_true")
    pend.add_argument("--preview", action="store_true", help="do not claim items for this digest")
    pend.add_argument("--claim", action="store_true", help="claim items after the queue has been validated")
    pend.add_argument("--force", action="store_true", help="ignore last_digest_at and intentionally repeat a digest")
    pend.add_argument("--output")
    pend.add_argument("--db")
    pend.set_defaults(func=cmd_pending)
    rend = sub.add_parser("render", help="validate AI annotations and render Markdown")
    rend.add_argument("--annotations", required=True)
    rend.add_argument("--output", help="Markdown path; defaults to config.json defaults.draft_dir/YYYY-MM-DD.md")
    rend.set_defaults(func=cmd_render)
    card = sub.add_parser("card", help="render one interactive chat card from an annotation queue")
    card.add_argument("--annotations", required=True)
    card.add_argument("--session", help="frozen queue and successful receipts; derives index and counts")
    card.add_argument("--index", type=int, help="1-based item index for preview without a session")
    card.add_argument("--processed", type=int, help="items completed before this card; defaults to index - 1")
    card.add_argument("--output", help="optional Markdown output; otherwise print the card")
    card.set_defaults(func=cmd_card)
    cards = sub.add_parser("cards", help="render a small batch of interactive chat cards")
    cards.add_argument("--annotations", required=True)
    cards.add_argument("--start", type=int, required=True)
    cards.add_argument("--count", type=int, choices=[3, 5], default=3)
    cards.add_argument("--processed", type=int)
    cards.add_argument("--output")
    cards.set_defaults(func=cmd_cards)
    review_start = sub.add_parser("review-start", help="freeze an annotation queue for sequential card review")
    review_start.add_argument("--annotations", required=True)
    review_start.add_argument("--session", required=True)
    review_start.add_argument("--claim", action="store_true", help="claim the validated queue in SQLite")
    review_start.add_argument("--replace", action="store_true", help="replace an existing session explicitly")
    review_start.add_argument("--db")
    review_start.set_defaults(func=cmd_review_start)
    review_record = sub.add_parser("review-record", help="append one successful card decision to a review session")
    review_record.add_argument("--session", required=True)
    review_record.add_argument("--id", required=True)
    review_record.add_argument("--action", required=True, choices=["read", "keep", "obsidian", "category"])
    review_record.add_argument("--result", required=True)
    review_record.set_defaults(func=cmd_review_record)
    review_apply = sub.add_parser("review-apply", help="atomically apply one decision and append its receipt")
    review_apply.add_argument("--annotations", required=True)
    review_apply.add_argument("--session", required=True)
    review_apply.add_argument("--id", required=True)
    review_apply.add_argument("--action", required=True, choices=["read", "keep", "obsidian", "category"])
    review_apply.add_argument("--category")
    review_apply.add_argument("--confirm")
    review_apply.add_argument("--result")
    review_apply.add_argument("--operation-id")
    review_apply.add_argument("--db")
    review_apply.set_defaults(func=cmd_review_apply)
    review_batch = sub.add_parser("review-batch", help="apply a validated batch of decisions in queue order")
    review_batch.add_argument("--annotations", required=True)
    review_batch.add_argument("--session", required=True)
    review_batch.add_argument("--decisions", required=True, help="JSON file with a decisions list")
    review_batch.add_argument("--db")
    review_batch.set_defaults(func=cmd_review_batch)
    parse_reply = sub.add_parser("parse-reply", help="parse ordered batch chat decisions")
    parse_reply.add_argument("--session", required=True)
    parse_reply.add_argument("--reply", required=True, help="text file containing one decision per line")
    parse_reply.add_argument("--count", type=int, choices=[3, 5], default=3)
    parse_reply.add_argument("--output")
    parse_reply.set_defaults(func=cmd_parse_reply)
    mark = sub.add_parser("mark", help="update local review state")
    mark.add_argument("--id", required=True)
    mark.add_argument("--status", required=True, choices=["unread", "keep", "read", "backlog"])
    mark.add_argument("--category")
    mark.add_argument("--summary")
    mark.add_argument("--note")
    mark.add_argument("--db")
    mark.set_defaults(func=cmd_mark)
    remove = sub.add_parser("remove", help="explicitly remove a liked/bookmarked tweet from X")
    remove.add_argument("--id", required=True)
    remove.add_argument("--source", required=True, choices=["bookmark", "like", "both"])
    remove.add_argument("--confirm", required=True)
    remove.add_argument("--db")
    remove.set_defaults(func=cmd_remove)
    return parser


def main() -> int:
    try:
        parsed = build_parser().parse_args()
        return parsed.func(parsed)
    except KeyboardInterrupt:
        return 130
    except DigestError as exc:
        print(json.dumps({"ok": False, "kind": exc.kind, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
