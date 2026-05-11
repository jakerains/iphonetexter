#!/usr/bin/env python3
"""Bulk iMessage/SMS sender wrapping `imsg` CLI/RPC.

Reads recipients from a CSV (single `handle` column), normalizes each handle
via `imsg normalize`, buckets recipients by service using `imsg rpc`'s
`chats.list`, and sends a fixed message body with separate pacing for iMessage
vs SMS/Unknown buckets. Persistence is delegated to caller-supplied `Hooks`
so the same engine drives the web UI's SQLite-backed flow and the standalone
CLI without bulk_send having to know the schema.

Usable as a CLI (run directly) or as a module — `run_job(config, on_event,
cancel, hooks)` is the entry point reused by the local web UI.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

DEFAULT_IMESSAGE_PACE = (3.0, 6.0)
DEFAULT_SMS_PACE = (15.0, 30.0)
DEFAULT_FAILURE_CEILING = 5
DEFAULT_CHAT_LIMIT = 9999


@dataclass
class Recipient:
    handle: str           # raw, as in CSV
    normalized: str       # E.164 phone, email, or echoed input on failure
    valid: bool
    kind: str             # "phone" | "email" | "unknown"
    bucket: str           # "imessage" | "sms" | "unknown"


@dataclass
class JobConfig:
    recipients_path: Path
    message: str
    imessage_pace: tuple[float, float] = DEFAULT_IMESSAGE_PACE
    sms_pace: tuple[float, float] = DEFAULT_SMS_PACE
    failure_ceiling: int = DEFAULT_FAILURE_CEILING
    dry_run: bool = True
    region: str = "US"
    imsg_binary: str = "imsg"
    chat_limit: int = DEFAULT_CHAT_LIMIT


@dataclass
class Hooks:
    """Persistence callbacks. All default to no-ops so callers that just
    want to dry-run or run without a database can omit them."""

    is_already_done: Callable[[str], bool] = field(default=lambda _: False)
    is_opted_out: Callable[[str], bool] = field(default=lambda _: False)
    record: Callable[[dict], None] = field(default=lambda _row: None)


@dataclass
class JobEvent:
    kind: str             # plan | send_start | send_result | warn | error | done | skip
    payload: dict = field(default_factory=dict)


class JobCancelled(Exception):
    pass


class RpcError(RuntimeError):
    def __init__(self, error: dict):
        self.error = error
        super().__init__(error.get("message") or str(error))


class RpcClient:
    """Thin synchronous wrapper around `imsg rpc` over stdio.

    Sends one request, drains stdout until a response with the matching id
    arrives, and ignores notifications in between (we don't subscribe to
    watch streams in the bulk path).
    """

    def __init__(self, binary: str = "imsg"):
        self._proc = subprocess.Popen(
            [binary, "rpc"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._next_id = 0
        self._lock = threading.Lock()

    def call(self, method: str, params: Optional[dict] = None) -> dict:
        with self._lock:
            self._next_id += 1
            req_id = self._next_id
            request = {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": method,
                "params": params or {},
            }
            assert self._proc.stdin is not None
            assert self._proc.stdout is not None
            self._proc.stdin.write(json.dumps(request) + "\n")
            self._proc.stdin.flush()
            while True:
                line = self._proc.stdout.readline()
                if not line:
                    err = ""
                    if self._proc.stderr is not None:
                        try:
                            err = self._proc.stderr.read() or ""
                        except Exception:
                            err = ""
                    raise RuntimeError(
                        "imsg rpc closed unexpectedly" + (f": {err}" if err else "")
                    )
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("id") != req_id:
                    continue
                if "error" in msg:
                    raise RpcError(msg["error"])
                return msg.get("result", {})

    def close(self) -> None:
        if self._proc.poll() is not None:
            return
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
        except Exception:
            pass
        try:
            self._proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass

    def __enter__(self) -> "RpcClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_handle(binary: str, raw: str, region: str) -> tuple[str, bool, str]:
    """Shell out to `imsg normalize`. Returns (normalized, valid, kind)."""
    proc = subprocess.run(
        [binary, "normalize", "--to", raw, "--region", region, "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return raw, False, "unknown"
    line = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    if not line:
        return raw, False, "unknown"
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return raw, False, "unknown"
    return (
        str(data.get("normalized", raw)),
        bool(data.get("valid", False)),
        str(data.get("kind", "unknown")),
    )


def build_chat_map(rpc: RpcClient, limit: int) -> dict[str, str]:
    """Return {normalized_handle -> service ('iMessage'|'SMS')} for direct chats.

    iMessage wins over SMS when both exist for the same handle, since Apple
    routes to iMessage when available.
    """
    result = rpc.call("chats.list", {"limit": limit})
    chat_map: dict[str, str] = {}
    for chat in result.get("chats", []):
        if chat.get("is_group"):
            continue
        service = chat.get("service") or ""
        handles: list[str] = []
        identifier = chat.get("identifier") or ""
        if identifier:
            handles.append(identifier)
        for participant in chat.get("participants", []) or []:
            if participant:
                handles.append(participant)
        for handle in handles:
            existing = chat_map.get(handle)
            if existing is None:
                chat_map[handle] = service
            elif existing.lower() != "imessage" and service.lower() == "imessage":
                chat_map[handle] = service
    return chat_map


def bucket_for(service: str) -> str:
    s = (service or "").lower()
    if s == "imessage":
        return "imessage"
    if s == "sms":
        return "sms"
    return "unknown"


def read_recipients_csv(path: Path) -> list[str]:
    """Read a CSV with a `handle` column (or single-column header-less file)."""
    rows: list[str] = []
    with path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        first = True
        header_index: Optional[int] = None
        for row in reader:
            if not row:
                continue
            stripped = [cell.strip() for cell in row]
            if first:
                first = False
                lowered = [c.lower() for c in stripped]
                if "handle" in lowered:
                    header_index = lowered.index("handle")
                    continue
                header_index = 0
            value = stripped[header_index or 0]
            if value:
                rows.append(value)
    return rows


def jitter_sleep(pace: tuple[float, float], cancel: Callable[[], bool]) -> None:
    low, high = pace
    if high <= 0:
        return
    delay = random.uniform(max(0.0, low), max(low, high))
    end = time.monotonic() + delay
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        if cancel():
            raise JobCancelled()
        time.sleep(min(0.25, remaining))


def plan_job(
    config: JobConfig,
    on_event: Callable[[JobEvent], None],
    rpc: RpcClient,
    hooks: Hooks,
) -> list[Recipient]:
    raw_handles = read_recipients_csv(config.recipients_path)
    on_event(JobEvent("plan", {"stage": "preflight", "raw_count": len(raw_handles)}))

    chat_map = build_chat_map(rpc, config.chat_limit)
    on_event(JobEvent("plan", {"stage": "chat_map", "size": len(chat_map)}))

    recipients: list[Recipient] = []
    seen: set[str] = set()
    for raw in raw_handles:
        normalized, valid, kind = normalize_handle(config.imsg_binary, raw, config.region)
        if normalized in seen:
            on_event(JobEvent("warn", {"reason": "duplicate", "handle": raw, "normalized": normalized}))
            continue
        seen.add(normalized)
        service = chat_map.get(normalized, "")
        bucket = bucket_for(service) if service else "unknown"
        recipients.append(
            Recipient(
                handle=raw,
                normalized=normalized,
                valid=valid,
                kind=kind,
                bucket=bucket,
            )
        )

    summary = {
        "total": len(recipients),
        "imessage": sum(1 for r in recipients if r.bucket == "imessage"),
        "sms": sum(1 for r in recipients if r.bucket == "sms"),
        "unknown": sum(1 for r in recipients if r.bucket == "unknown"),
        "invalid": sum(1 for r in recipients if not r.valid),
        "already_sent": sum(1 for r in recipients if hooks.is_already_done(r.normalized)),
        "opted_out": sum(1 for r in recipients if hooks.is_opted_out(r.normalized)),
    }
    on_event(JobEvent("plan", {"stage": "summary", **summary}))
    return recipients


def estimate_eta_seconds(recipients: Iterable[Recipient], config: JobConfig, hooks: Hooks) -> float:
    total = 0.0
    for r in recipients:
        if hooks.is_already_done(r.normalized) or hooks.is_opted_out(r.normalized):
            continue
        if r.bucket == "imessage":
            total += sum(config.imessage_pace) / 2
        else:
            total += sum(config.sms_pace) / 2
    return total


def send_one(
    rpc: RpcClient,
    recipient: Recipient,
    message: str,
    service: str,
    region: str,
) -> dict:
    params = {
        "to": recipient.normalized,
        "text": message,
        "service": service,
        "region": region,
    }
    return rpc.call("send", params)


def _row_for(recipient: Recipient, attempt_service: str, message: str) -> dict:
    return {
        "ts": now_iso(),
        "handle": recipient.handle,
        "normalized": recipient.normalized,
        "kind": recipient.kind,
        "bucket": recipient.bucket,
        "attempt_service": attempt_service,
        "message_body": message,
    }


def run_job(
    config: JobConfig,
    on_event: Callable[[JobEvent], None],
    cancel: Callable[[], bool] = lambda: False,
    hooks: Optional[Hooks] = None,
) -> int:
    """Execute the bulk job. Returns 0 on success, 1 on failure-ceiling abort.

    The job persists nothing on its own — all writes go through `hooks`.
    Default hooks are no-ops, which is fine for dry-runs and tests.
    """
    if hooks is None:
        hooks = Hooks()

    if not config.recipients_path.exists():
        on_event(JobEvent("error", {"reason": "missing_recipients", "path": str(config.recipients_path)}))
        return 1

    with RpcClient(binary=config.imsg_binary) as rpc:
        try:
            recipients = plan_job(config, on_event, rpc, hooks)
        except RpcError as err:
            on_event(JobEvent("error", {"reason": "rpc_error", "stage": "plan", "error": err.error}))
            return 1

        eta = estimate_eta_seconds(recipients, config, hooks)
        on_event(JobEvent("plan", {"stage": "eta", "seconds": int(eta)}))

        if config.dry_run:
            would_send = sum(
                1 for r in recipients
                if not hooks.is_already_done(r.normalized) and not hooks.is_opted_out(r.normalized)
            )
            on_event(JobEvent("done", {"reason": "dry_run", "would_send": would_send}))
            return 0

        consecutive_failures = 0
        order = (
            [r for r in recipients if r.bucket == "imessage"]
            + [r for r in recipients if r.bucket != "imessage"]
        )

        for index, recipient in enumerate(order):
            if cancel():
                raise JobCancelled()

            if hooks.is_opted_out(recipient.normalized):
                row = _row_for(recipient, attempt_service="", message=config.message)
                row.update({"status": "skipped_optout", "message_id": "", "guid": "", "error": "contact opted out"})
                hooks.record(row)
                on_event(JobEvent("skip", {"normalized": recipient.normalized, "reason": "opted_out"}))
                continue

            if hooks.is_already_done(recipient.normalized):
                on_event(JobEvent("skip", {"normalized": recipient.normalized, "reason": "already_ok"}))
                continue

            if not recipient.valid:
                row = _row_for(recipient, attempt_service="", message=config.message)
                row.update({"status": "invalid", "message_id": "", "guid": "", "error": "unparseable handle"})
                hooks.record(row)
                on_event(JobEvent("send_result", row))
                consecutive_failures += 1
                if consecutive_failures >= config.failure_ceiling:
                    on_event(JobEvent("error", {"reason": "failure_ceiling", "count": consecutive_failures}))
                    return 1
                continue

            attempt_service = "imessage" if recipient.bucket == "imessage" else "auto"
            on_event(JobEvent("send_start", {
                "normalized": recipient.normalized,
                "bucket": recipient.bucket,
                "attempt_service": attempt_service,
            }))

            row = _row_for(recipient, attempt_service=attempt_service, message=config.message)
            try:
                result = send_one(rpc, recipient, config.message, attempt_service, config.region)
                row.update({
                    "status": "ok" if result.get("guid") else "ok_unverified",
                    "message_id": str(result.get("id") or ""),
                    "guid": str(result.get("guid") or ""),
                    "error": "",
                })
                consecutive_failures = 0
            except RpcError as err:
                error = err.error or {}
                msg = (error.get("message") if isinstance(error, dict) else str(error)) or "rpc error"
                status = "ghost_send" if "ghost" in msg.lower() or "misroute" in msg.lower() else "error"
                row.update({"status": status, "message_id": "", "guid": "", "error": msg})
                consecutive_failures += 1
            except Exception as err:  # noqa: BLE001
                row.update({"status": "error", "message_id": "", "guid": "", "error": str(err)})
                consecutive_failures += 1

            hooks.record(row)
            on_event(JobEvent("send_result", row))

            if consecutive_failures >= config.failure_ceiling:
                on_event(JobEvent("error", {"reason": "failure_ceiling", "count": consecutive_failures}))
                return 1

            if index < len(order) - 1:
                pace = config.imessage_pace if recipient.bucket == "imessage" else config.sms_pace
                jitter_sleep(pace, cancel)

        on_event(JobEvent("done", {"reason": "complete"}))
        return 0


def parse_pace(value: str) -> tuple[float, float]:
    if "-" in value:
        low_str, high_str = value.split("-", 1)
        return (float(low_str), float(high_str))
    one = float(value)
    return (one, one)


# ---------------------------------------------------------------------------
# Standalone CLI: imports the web_ui DB layer so a direct invocation also
# writes into the same SQLite store the web UI uses.

def _build_cli_hooks(db_path: Path, list_id: int, job_id: str):
    """Construct SQLite-backed hooks for the standalone CLI.

    Imports lazily so the bulk module remains import-light when used as
    a library.
    """
    web_ui = Path(__file__).resolve().parent / "web_ui"
    if str(web_ui) not in sys.path:
        sys.path.insert(0, str(web_ui))
    from db import Database  # type: ignore
    from repository import Repo  # type: ignore

    repo = Repo(Database(db_path))

    def is_already_done(normalized: str) -> bool:
        contact = repo.contacts.get_by_handle(normalized)
        if contact is None:
            return False
        return repo.sends.is_already_done(list_id, contact.id)

    def is_opted_out(normalized: str) -> bool:
        return repo.contacts.is_opted_out(normalized)

    def record(row: dict) -> None:
        contact = repo.contacts.upsert(
            normalized_handle=row["normalized"],
            kind=row.get("kind", "unknown"),
        )
        repo.lists.add_members(list_id, [contact.id])
        repo.sends.record(
            contact_id=contact.id,
            list_id=list_id,
            job_id=job_id,
            chat_id=None,
            target_type="handle",
            target=row.get("handle") or row["normalized"],
            service=row.get("attempt_service") or None,
            region=None,
            message_body=row.get("message_body", ""),
            attachment_path=None,
            status=row["status"],
            message_rowid=int(row["message_id"]) if row.get("message_id") else None,
            guid=row.get("guid") or None,
            error=row.get("error") or None,
            ts=row.get("ts"),
        )

    return Hooks(
        is_already_done=is_already_done,
        is_opted_out=is_opted_out,
        record=record,
    ), repo


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bulk send a message to recipients via imsg.",
    )
    parser.add_argument("--recipients", required=True, type=Path,
                        help="Path to a CSV with a `handle` column.")
    body = parser.add_mutually_exclusive_group(required=True)
    body.add_argument("--message", help="Message body (single line).")
    body.add_argument("--message-file", type=Path,
                      help="File whose contents become the message body.")
    parser.add_argument("--db", type=Path, default=None,
                        help="Path to the SQLite store (default: <recipients dir>/imsg.db).")
    parser.add_argument("--list-name", default=None,
                        help="Name for the list backing this run (default: recipients filename).")
    parser.add_argument("--imessage-pace", default="3-6",
                        help="Seconds between iMessage sends. Range like 3-6 or single number.")
    parser.add_argument("--sms-pace", default="15-30",
                        help="Seconds between SMS/Unknown sends. Range or single number.")
    parser.add_argument("--failure-ceiling", type=int, default=DEFAULT_FAILURE_CEILING,
                        help="Abort after this many consecutive failures.")
    parser.add_argument("--region", default="US",
                        help="Default region for phone normalization.")
    parser.add_argument("--imsg", default=os.environ.get("IMSG_BIN", "imsg"),
                        help="Path to imsg binary (default: `imsg` on PATH).")
    parser.add_argument("--confirm", action="store_true",
                        help="Actually send. Without this, the run is dry-run only.")
    args = parser.parse_args(argv)

    message = args.message if args.message is not None else args.message_file.read_text(encoding="utf-8")
    db_path = args.db or args.recipients.parent / "imsg.db"
    list_name = args.list_name or args.recipients.stem

    config = JobConfig(
        recipients_path=args.recipients,
        message=message,
        imessage_pace=parse_pace(args.imessage_pace),
        sms_pace=parse_pace(args.sms_pace),
        failure_ceiling=args.failure_ceiling,
        dry_run=not args.confirm,
        region=args.region,
        imsg_binary=args.imsg,
    )

    job_id = datetime.now(timezone.utc).strftime("cli-%Y%m%d-%H%M%S")
    hooks, repo = _build_cli_hooks(db_path, list_id=_ensure_cli_list(db_path, list_name), job_id=job_id)

    cancelled = {"value": False}

    def handle_sigint(signum, frame):
        cancelled["value"] = True

    signal.signal(signal.SIGINT, handle_sigint)
    signal.signal(signal.SIGTERM, handle_sigint)

    def emit(event: JobEvent) -> None:
        line = json.dumps({"kind": event.kind, **event.payload})
        print(line, flush=True)

    try:
        return run_job(config, emit, cancel=lambda: cancelled["value"], hooks=hooks)
    except JobCancelled:
        emit(JobEvent("error", {"reason": "cancelled"}))
        return 130


def _ensure_cli_list(db_path: Path, name: str) -> int:
    web_ui = Path(__file__).resolve().parent / "web_ui"
    if str(web_ui) not in sys.path:
        sys.path.insert(0, str(web_ui))
    from db import Database  # type: ignore
    from repository import Repo  # type: ignore

    repo = Repo(Database(db_path))
    for lst in repo.lists.all():
        if lst.name == name:
            return lst.id
    return repo.lists.create(name=name, kind="named").id


if __name__ == "__main__":
    sys.exit(main())
