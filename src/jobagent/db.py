"""SQLite storage. The database is the source of truth; the Excel workbook is rebuilt from it.

Performance notes: WAL journal, one transaction per batch of writes, executemany everywhere,
and indexes on the columns each stage filters by.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from .models import Company, Job

log = logging.getLogger("jobagent.db")
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    industry TEXT DEFAULT '',
    ats TEXT DEFAULT 'unknown',
    slug TEXT DEFAULT '',
    careers_url TEXT DEFAULT '',
    priority INTEGER DEFAULT 2,
    active INTEGER DEFAULT 1,
    notes TEXT DEFAULT '',
    last_fetched_at TEXT,
    last_status TEXT,
    last_job_count INTEGER,
    etag TEXT,
    last_modified TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    uid TEXT NOT NULL UNIQUE,
    company_id INTEGER REFERENCES companies(id),
    company TEXT NOT NULL,
    ats TEXT NOT NULL,
    external_id TEXT NOT NULL,
    title TEXT NOT NULL,
    location TEXT DEFAULT '',
    country TEXT,
    is_remote INTEGER DEFAULT 0,
    department TEXT DEFAULT '',
    employment_type TEXT DEFAULT '',
    salary TEXT DEFAULT '',
    url TEXT DEFAULT '',
    apply_url TEXT DEFAULT '',
    description TEXT,
    posted_at TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    listing_hash TEXT NOT NULL,      -- hash of the board listing: detects changed postings
    content_hash TEXT NOT NULL,      -- hash incl. full description: re-score when it changes
    is_active INTEGER DEFAULT 1,
    extra TEXT DEFAULT '{}',
    -- filtering
    filter_status TEXT,          -- NULL (pending) | pass | excluded | needs_detail
    filter_reason TEXT,
    filtered_at TEXT,
    sponsorship_flag TEXT,       -- blocked | positive | not_mentioned
    track_hint TEXT,
    -- scoring
    score INTEGER,
    band TEXT,                   -- High | Medium | Low
    track TEXT,                  -- business | research
    reason TEXT,
    matched_skills TEXT,
    gaps TEXT,
    seniority_fit TEXT,
    scored_at TEXT,
    scored_hash TEXT,
    score_model TEXT,
    score_error TEXT,
    -- Riya's edits from the workbook
    user_status TEXT DEFAULT '',
    user_note TEXT DEFAULT '',
    -- phase 2
    apply_status TEXT,
    apply_method TEXT,
    applied_at TEXT,
    apply_error TEXT
);
CREATE INDEX IF NOT EXISTS ix_jobs_company ON jobs(company_id, is_active);
CREATE INDEX IF NOT EXISTS ix_jobs_filter ON jobs(filter_status, is_active);
CREATE INDEX IF NOT EXISTS ix_jobs_band ON jobs(band, score DESC);
CREATE INDEX IF NOT EXISTS ix_jobs_first_seen ON jobs(first_seen_at);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    run_id TEXT UNIQUE NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT,                -- running | ok | partial | failed
    stats TEXT DEFAULT '{}',
    cost_usd REAL DEFAULT 0,
    error TEXT
);

CREATE TABLE IF NOT EXISTS llm_usage (
    id INTEGER PRIMARY KEY,
    run_id TEXT,
    job_id INTEGER,
    purpose TEXT,
    model TEXT,
    batch INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_usage_created ON llm_usage(created_at);

CREATE TABLE IF NOT EXISTS pending_batches (
    batch_id TEXT PRIMARY KEY,
    run_id TEXT,
    purpose TEXT,
    model TEXT,
    created_at TEXT,
    status TEXT DEFAULT 'in_progress',
    job_ids TEXT DEFAULT '[]'
);
"""

SCORE_RESET = ("score=NULL, band=NULL, track=NULL, reason=NULL, matched_skills=NULL, gaps=NULL, "
               "seniority_fit=NULL, scored_at=NULL, scored_hash=NULL, score_model=NULL, score_error=NULL")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, timeout=30, isolation_level=None)  # autocommit; explicit txns
        self.conn.row_factory = sqlite3.Row
        for pragma in ("journal_mode=WAL", "synchronous=NORMAL", "foreign_keys=ON",
                       "temp_store=MEMORY", "cache_size=-32000", "busy_timeout=30000"):
            self.conn.execute(f"PRAGMA {pragma}")
        self.conn.executescript(SCHEMA)
        self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))

    def maintenance(self, every_days: int = 7) -> dict:
        """Weekly VACUUM: reclaims space left by dropped descriptions and closed jobs."""
        last = self.scalar("SELECT value FROM meta WHERE key='last_vacuum'")
        if last and (datetime.now(timezone.utc) - datetime.fromisoformat(last)).days < every_days:
            return {"vacuum": "skipped"}
        before = self.path.stat().st_size if self.path.exists() else 0
        self.conn.execute("DELETE FROM jobs WHERE is_active=0 AND last_seen_at < datetime('now', '-120 days') "
                          "AND COALESCE(apply_status,'')='' AND COALESCE(user_status,'')=''")
        self.conn.execute("VACUUM")
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('last_vacuum', ?)", (utcnow(),))
        after = self.path.stat().st_size
        return {"vacuum": "done", "mb_before": round(before / 1e6, 1), "mb_after": round(after / 1e6, 1)}

    def close(self) -> None:
        try:
            self.conn.execute("PRAGMA optimize")
        finally:
            self.conn.close()

    # ---- transactions -------------------------------------------------
    def tx(self):
        store = self

        class _Tx:
            def __enter__(self_inner):
                store.conn.execute("BEGIN IMMEDIATE")
                return store.conn

            def __exit__(self_inner, exc_type, *_):
                store.conn.execute("ROLLBACK" if exc_type else "COMMIT")
                return False

        return _Tx()

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        row = self.conn.execute(sql, params).fetchone()
        return row[0] if row else None

    # ---- companies ----------------------------------------------------
    def sync_companies(self, companies: Iterable[Company]) -> int:
        rows = [(c.name, c.industry, c.ats, c.slug, c.careers_url, c.priority, int(c.active), c.notes)
                for c in companies]
        with self.tx() as conn:
            conn.executemany(
                """INSERT INTO companies(name, industry, ats, slug, careers_url, priority, active, notes)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(name) DO UPDATE SET industry=excluded.industry, ats=excluded.ats,
                     slug=excluded.slug, careers_url=excluded.careers_url, priority=excluded.priority,
                     active=excluded.active, notes=excluded.notes""", rows)
            names = [r[0] for r in rows]
            if names:
                placeholders = ",".join("?" * len(names))
                conn.execute(f"UPDATE companies SET active=0 WHERE name NOT IN ({placeholders})", names)
        return len(rows)

    def fetchable_companies(self, supported: Iterable[str]) -> list[sqlite3.Row]:
        supported = list(supported)
        ph = ",".join("?" * len(supported))
        return self.query(f"SELECT * FROM companies WHERE active=1 AND slug<>'' AND ats IN ({ph}) "
                          "ORDER BY priority, name", supported)

    def record_company_fetch(self, company_id: int, status: str, count: int | None,
                             etag: str | None, last_modified: str | None) -> None:
        self.conn.execute(
            "UPDATE companies SET last_fetched_at=?, last_status=?, last_job_count=COALESCE(?, last_job_count), "
            "etag=?, last_modified=? WHERE id=?",
            (utcnow(), status, count, etag, last_modified, company_id))

    # ---- jobs ---------------------------------------------------------
    def upsert_jobs(self, company_id: int, jobs: list[Job], now: str) -> dict[str, int]:
        """Insert new jobs, update changed ones (reset filter + score), touch unchanged ones."""
        existing = {r["uid"]: (r["id"], r["listing_hash"]) for r in self.query(
            "SELECT id, uid, listing_hash FROM jobs WHERE company_id=?", (company_id,))}
        new_rows, changed_rows, touch_rows = [], [], []
        seen: set[str] = set()
        for j in jobs:
            uid = j.uid
            if uid in seen:
                continue
            seen.add(uid)
            h = j.content_hash()
            if uid not in existing:
                new_rows.append((uid, company_id, j.company, j.ats, j.external_id, j.title, j.location, j.country,
                                 int(j.is_remote), j.department, j.employment_type, j.salary, j.url, j.apply_url,
                                 j.description, j.posted_at, now, now, h, h, json.dumps(j.extra or {})))
            elif existing[uid][1] != h:
                changed_rows.append((j.title, j.location, j.country, int(j.is_remote), j.department,
                                     j.employment_type, j.salary, j.url, j.apply_url, j.description, j.posted_at,
                                     now, h, h, json.dumps(j.extra or {}), existing[uid][0]))
            else:
                touch_rows.append((now, existing[uid][0]))
        with self.tx() as conn:
            conn.executemany(
                """INSERT INTO jobs(uid, company_id, company, ats, external_id, title, location, country, is_remote,
                   department, employment_type, salary, url, apply_url, description, posted_at, first_seen_at,
                   last_seen_at, listing_hash, content_hash, extra)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", new_rows)
            conn.executemany(
                f"""UPDATE jobs SET title=?, location=?, country=?, is_remote=?, department=?, employment_type=?,
                   salary=?, url=?, apply_url=?, description=?, posted_at=?, last_seen_at=?,
                   listing_hash=?, content_hash=?, extra=?, is_active=1, filter_status=NULL, filter_reason=NULL, {SCORE_RESET}
                   WHERE id=?""", changed_rows)
            conn.executemany("UPDATE jobs SET last_seen_at=?, is_active=1 WHERE id=?", touch_rows)
            closed = conn.execute(
                "UPDATE jobs SET is_active=0 WHERE company_id=? AND is_active=1 AND last_seen_at<?",
                (company_id, now)).rowcount
        return {"new": len(new_rows), "changed": len(changed_rows), "unchanged": len(touch_rows), "closed": closed}

    def touch_company_jobs(self, company_id: int, now: str) -> None:
        """Board unchanged since last fetch (HTTP 304): keep all its jobs alive."""
        self.conn.execute("UPDATE jobs SET last_seen_at=? WHERE company_id=? AND is_active=1", (now, company_id))

    def jobs_to_filter(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM jobs WHERE is_active=1 AND filter_status IS NULL")

    def jobs_needing_detail(self) -> list[sqlite3.Row]:
        return self.query("SELECT j.*, c.slug AS company_slug FROM jobs j JOIN companies c ON c.id=j.company_id "
                          "WHERE j.is_active=1 AND j.filter_status='needs_detail'")

    def save_filter_results(self, rows: list[tuple]) -> None:
        """rows: (filter_status, filter_reason, sponsorship_flag, track_hint, country, is_remote, job_id)"""
        now = utcnow()
        with self.tx() as conn:
            conn.executemany(
                "UPDATE jobs SET filter_status=?, filter_reason=?, sponsorship_flag=?, track_hint=?, country=?, "
                f"is_remote=?, filtered_at='{now}' WHERE id=?", rows)
            # Excluded jobs never need their description again (a changed posting is re-fetched in full),
            # so drop it: keeps the database small and fast.
            conn.executemany("UPDATE jobs SET description=NULL WHERE id=? AND filter_status='excluded'",
                             [(r[-1],) for r in rows if r[0] == "excluded"])

    def save_descriptions(self, rows: list[tuple[str, str, str, int]]) -> None:
        """rows: (description, apply_url, content_hash, job_id); resets filter so pass 2 runs."""
        with self.tx() as conn:
            conn.executemany("UPDATE jobs SET description=?, apply_url=COALESCE(NULLIF(?, ''), apply_url), "
                             "content_hash=?, filter_status=NULL WHERE id=?", rows)

    def jobs_to_score(self, limit: int) -> list[sqlite3.Row]:
        return self.query(
            """SELECT j.*, c.priority FROM jobs j JOIN companies c ON c.id=j.company_id
               WHERE j.is_active=1 AND j.filter_status='pass'
                 AND (j.scored_hash IS NULL OR j.scored_hash<>j.content_hash)
                 AND j.id NOT IN (SELECT value FROM pending_batches pb, json_each(pb.job_ids)
                                  WHERE pb.status='in_progress')
               ORDER BY c.priority, COALESCE(j.posted_at, j.first_seen_at) DESC LIMIT ?""", (limit,))

    def save_scores(self, rows: list[dict[str, Any]]) -> None:
        now = utcnow()
        with self.tx() as conn:
            conn.executemany(
                """UPDATE jobs SET score=:score, band=:band, track=:track, reason=:reason,
                   matched_skills=:matched_skills, gaps=:gaps, seniority_fit=:seniority_fit,
                   sponsorship_flag=CASE WHEN :sponsorship='blocked' THEN 'blocked'
                                         WHEN sponsorship_flag='positive' THEN 'positive'
                                         ELSE :sponsorship END,
                   scored_at=:now, scored_hash=content_hash, score_model=:model, score_error=NULL
                   WHERE id=:job_id""", [{**r, "now": now} for r in rows])

    def save_score_errors(self, rows: list[tuple[str, int]]) -> None:
        with self.tx() as conn:
            conn.executemany("UPDATE jobs SET score_error=? WHERE id=?", rows)

    def apply_user_statuses(self, rows: list[tuple[str, str, int]]) -> int:
        with self.tx() as conn:
            conn.executemany("UPDATE jobs SET user_status=?, user_note=? WHERE id=?", rows)
        return len(rows)

    # ---- runs & usage -------------------------------------------------
    def start_run(self, run_id: str) -> None:
        self.conn.execute("INSERT INTO runs(run_id, started_at, status) VALUES(?,?, 'running')", (run_id, utcnow()))

    def finish_run(self, run_id: str, status: str, stats: dict, cost: float, error: str | None = None) -> None:
        self.conn.execute("UPDATE runs SET finished_at=?, status=?, stats=?, cost_usd=?, error=? WHERE run_id=?",
                          (utcnow(), status, json.dumps(stats, default=str), cost, error, run_id))

    def update_run_progress(self, run_id: str, stats: dict, cost: float) -> None:
        self.conn.execute("UPDATE runs SET stats=?, cost_usd=? WHERE run_id=?",
                          (json.dumps(stats, default=str), cost, run_id))

    def record_usage(self, rows: list[tuple]) -> None:
        """rows: (run_id, job_id, purpose, model, batch, in, out, cache_read, cache_write, cost)"""
        now = utcnow()
        with self.tx() as conn:
            conn.executemany(
                "INSERT INTO llm_usage(run_id, job_id, purpose, model, batch, input_tokens, output_tokens, "
                "cache_read_tokens, cache_write_tokens, cost_usd, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                [(*r, now) for r in rows])

    def spend_since(self, since_iso: str) -> float:
        return float(self.scalar("SELECT COALESCE(SUM(cost_usd),0) FROM llm_usage WHERE created_at>=?",
                                 (since_iso,)) or 0.0)

    def add_pending_batch(self, batch_id: str, run_id: str, purpose: str, model: str, job_ids: list[int]) -> None:
        self.conn.execute("INSERT INTO pending_batches(batch_id, run_id, purpose, model, created_at, job_ids) "
                          "VALUES(?,?,?,?,?,?)", (batch_id, run_id, purpose, model, utcnow(), json.dumps(job_ids)))

    def open_batches(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM pending_batches WHERE status='in_progress' ORDER BY created_at")

    def close_batch(self, batch_id: str, status: str) -> None:
        self.conn.execute("UPDATE pending_batches SET status=? WHERE batch_id=?", (status, batch_id))
