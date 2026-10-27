"""Logging for the job agent.

Every run writes to:
  logs/jobagent.log        - human-readable, rotated daily, kept `retention_days`
  logs/jobagent.jsonl      - same events as JSON lines (machine-readable), rotated daily
  logs/errors.log          - WARNING and above only, rotated by size
  logs/runs/<run_id>.log   - everything from a single run (easy to share when debugging)

Each line carries the run id and the pipeline stage. API keys are masked.
"""

from __future__ import annotations

import contextvars
import json
import logging
import logging.handlers
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

run_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("run_id", default="-")
stage_var: contextvars.ContextVar[str] = contextvars.ContextVar("stage", default="-")

TEXT_FORMAT = "%(asctime)s.%(msecs)03d | %(levelname)-7s | run=%(run_id)s | %(stage)-10s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)(api[_-]?key|authorization|x-api-key)(\s*[:=]\s*)([^\s,'\"]+)"),
]
_RESERVED = set(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime", "run_id", "stage"}

_run_handler: logging.Handler | None = None


def mask_secrets(text: str) -> str:
    text = _SECRET_PATTERNS[0].sub("sk-ant-***", text)
    return _SECRET_PATTERNS[1].sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)


class ContextFilter(logging.Filter):
    """Adds run_id/stage to every record and masks secrets in the message."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = run_id_var.get()
        record.stage = stage_var.get()
        if isinstance(record.msg, str):
            record.msg = mask_secrets(record.msg)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "run_id": getattr(record, "run_id", "-"),
            "stage": getattr(record, "stage", "-"),
            "logger": record.name,
            "msg": mask_secrets(record.getMessage()),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = mask_secrets(self.formatException(record.exc_info))
        return json.dumps(payload, default=str, ensure_ascii=False)


def setup_logging(logs_dir: Path, level: str = "INFO", console_level: str = "INFO",
                  retention_days: int = 30, json_log: bool = True) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / "runs").mkdir(exist_ok=True)
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    root.setLevel(logging.DEBUG)
    ctx = ContextFilter()
    text_fmt = logging.Formatter(TEXT_FORMAT, DATE_FORMAT)

    main = logging.handlers.TimedRotatingFileHandler(
        logs_dir / "jobagent.log", when="midnight", backupCount=retention_days, encoding="utf-8", delay=True)
    main.setLevel(level.upper())
    main.setFormatter(text_fmt)
    main.addFilter(ctx)
    root.addHandler(main)

    if json_log:
        js = logging.handlers.TimedRotatingFileHandler(
            logs_dir / "jobagent.jsonl", when="midnight", backupCount=retention_days, encoding="utf-8", delay=True)
        js.setLevel(level.upper())
        js.setFormatter(JsonFormatter())
        js.addFilter(ctx)
        root.addHandler(js)

    errors = logging.handlers.RotatingFileHandler(
        logs_dir / "errors.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8", delay=True)
    errors.setLevel(logging.WARNING)
    errors.setFormatter(text_fmt)
    errors.addFilter(ctx)
    root.addHandler(errors)

    console = logging.StreamHandler()
    console.setLevel(console_level.upper())
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(stage)-10s %(message)s", "%H:%M:%S"))
    console.addFilter(ctx)
    root.addHandler(console)

    for noisy in ("httpx", "httpcore", "anthropic", "hpack", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _prune_run_logs(logs_dir / "runs", retention_days)


def start_run_log(logs_dir: Path, run_id: str) -> Path:
    """Attach a per-run log file and set the run id on all records."""
    global _run_handler
    run_id_var.set(run_id)
    path = logs_dir / "runs" / f"{run_id}.log"
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(TEXT_FORMAT, DATE_FORMAT))
    handler.addFilter(ContextFilter())
    logging.getLogger().addHandler(handler)
    _run_handler = handler
    return path


def end_run_log() -> None:
    global _run_handler
    if _run_handler is not None:
        logging.getLogger().removeHandler(_run_handler)
        _run_handler.close()
        _run_handler = None


def _prune_run_logs(runs_dir: Path, retention_days: int) -> None:
    cutoff = time.time() - retention_days * 86400
    for f in runs_dir.glob("*.log"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


@contextmanager
def stage(name: str, logger: logging.Logger | None = None) -> Iterator[dict[str, Any]]:
    """Time a pipeline stage. Yields a dict for counters that are logged at the end."""
    log = logger or logging.getLogger("jobagent.pipeline")
    token = stage_var.set(name)
    stats: dict[str, Any] = {}
    start = time.perf_counter()
    log.info("stage started")
    try:
        yield stats
    except Exception:
        stats["duration_s"] = round(time.perf_counter() - start, 2)
        log.exception("stage failed after %.1fs", stats["duration_s"], extra={"stats": stats})
        raise
    else:
        stats["duration_s"] = round(time.perf_counter() - start, 2)
        summary = ", ".join(f"{k}={v}" for k, v in stats.items())
        log.info("stage finished: %s", summary, extra={"stats": stats})
    finally:
        stage_var.reset(token)
