#!/usr/bin/env python3
"""Local web UI for imsg.

FastAPI + HTMX. Localhost-only by default. The lifespan owns:
  - one long-lived `imsg rpc` subprocess (browse + one-off send)
  - one SQLite database (contacts, lists, sends, received, opt-outs)
  - one inbound watcher (consumes `imsg watch --json`, writes to DB,
    flags opt-outs)

Run:
    pip install -r scripts/web_ui/requirements.txt
    python scripts/web_ui/server.py

Open http://127.0.0.1:8765/ in a browser.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Optional

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# Make sibling bulk_send module importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bulk_send import (  # noqa: E402
    DEFAULT_FAILURE_CEILING,
    DEFAULT_IMESSAGE_PACE,
    DEFAULT_SMS_PACE,
    Hooks,
    JobConfig,
    JobEvent,
    RpcClient,
    RpcError,
    normalize_handle,
    now_iso,
    parse_pace,
    read_recipients_csv,
    run_job,
)
from db import Database  # noqa: E402
from import_from_messages import import_contacts  # noqa: E402
from optout import load_matcher  # noqa: E402
from scan_optouts import scan_history  # noqa: E402
from repository import Repo  # noqa: E402
from templates_store import TemplateStore, TemplateStoreError  # noqa: E402
from watcher import InboundWatcher  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = BASE_DIR / "state"
JOBS_DIR = STATE_DIR / "jobs"
DB_PATH = STATE_DIR / "imsg.db"
OPTOUT_PHRASES_PATH = STATE_DIR / "optout_phrases.txt"
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))
IMSG_BIN = os.environ.get("IMSG_BIN", "imsg")
HOST = os.environ.get("IMSG_WEB_HOST", "127.0.0.1")
PORT = int(os.environ.get("IMSG_WEB_PORT", "8765"))


class AsyncRpcClient:
    """Lazy async wrapper over bulk_send.RpcClient.

    The underlying client is synchronous and serializes via its own
    threading lock. We forward calls into a worker thread via
    asyncio.to_thread so the FastAPI event loop never blocks.
    """

    def __init__(self, binary: str):
        self._binary = binary
        self._client: Optional[RpcClient] = None
        self._lock = asyncio.Lock()

    async def call(self, method: str, params: Optional[dict] = None) -> dict:
        async with self._lock:
            if self._client is None or self._client._proc.poll() is not None:
                self._client = await asyncio.to_thread(RpcClient, self._binary)
        return await asyncio.to_thread(self._client.call, method, params)

    async def close(self) -> None:
        if self._client is not None:
            await asyncio.to_thread(self._client.close)
            self._client = None


@dataclass
class JobRecord:
    id: str
    config: JobConfig
    list_id: Optional[int]
    cancel_flag: threading.Event
    queue: asyncio.Queue
    history: list[dict]
    started_at: float
    exit_code: Optional[int] = None
    finished_at: Optional[float] = None


class JobManager:
    """Tracks at most one active bulk job at a time."""

    def __init__(self, repo: Repo) -> None:
        self.repo = repo
        self._lock = threading.Lock()
        self._current_id: Optional[str] = None
        self._jobs: dict[str, JobRecord] = {}

    def is_busy(self) -> bool:
        with self._lock:
            if self._current_id is None:
                return False
            record = self._jobs.get(self._current_id)
            return bool(record and record.exit_code is None)

    def start(
        self,
        config: JobConfig,
        list_id: int,
        loop: asyncio.AbstractEventLoop,
    ) -> JobRecord:
        with self._lock:
            if self._current_id is not None:
                current = self._jobs.get(self._current_id)
                if current and current.exit_code is None:
                    raise HTTPException(status_code=409, detail="A job is already running.")
            job_id = uuid.uuid4().hex[:12]
            record = JobRecord(
                id=job_id,
                config=config,
                list_id=list_id,
                cancel_flag=threading.Event(),
                queue=asyncio.Queue(),
                history=[],
                started_at=time.time(),
            )
            self._jobs[job_id] = record
            self._current_id = job_id

        thread = threading.Thread(
            target=self._run_thread,
            args=(record, loop),
            daemon=True,
            name=f"bulk-job-{job_id}",
        )
        thread.start()
        return record

    def cancel(self, job_id: str) -> bool:
        record = self._jobs.get(job_id)
        if record is None or record.exit_code is not None:
            return False
        record.cancel_flag.set()
        return True

    def get(self, job_id: str) -> Optional[JobRecord]:
        return self._jobs.get(job_id)

    def _run_thread(self, record: JobRecord, loop: asyncio.AbstractEventLoop) -> None:
        def on_event(event: JobEvent) -> None:
            loop.call_soon_threadsafe(self._enqueue, record, event)

        def is_cancelled() -> bool:
            return record.cancel_flag.is_set()

        hooks = self._build_hooks(list_id=record.list_id, job_id=record.id)

        try:
            code = run_job(record.config, on_event, cancel=is_cancelled, hooks=hooks)
            record.exit_code = code
        except Exception as err:  # noqa: BLE001
            on_event(JobEvent("error", {"reason": "exception", "error": str(err)}))
            record.exit_code = 1
        finally:
            record.finished_at = time.time()
            loop.call_soon_threadsafe(record.queue.put_nowait, None)

    def _build_hooks(self, list_id: int, job_id: str) -> Hooks:
        repo = self.repo

        def is_already_done(normalized: str) -> bool:
            contact = repo.contacts.get_by_handle(normalized)
            if contact is None:
                return False
            return repo.sends.is_already_done(list_id, contact.id)

        def is_opted_out(normalized: str) -> bool:
            return repo.contacts.is_opted_out(normalized)

        def record(row: dict) -> None:
            normalized = row.get("normalized") or ""
            if not normalized:
                return
            kind = row.get("kind") or "unknown"
            contact = repo.contacts.upsert(normalized_handle=normalized, kind=kind)
            repo.lists.add_members(list_id, [contact.id])
            repo.sends.record(
                contact_id=contact.id,
                list_id=list_id,
                job_id=job_id,
                chat_id=None,
                target_type="handle",
                target=row.get("handle") or normalized,
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

        return Hooks(is_already_done=is_already_done, is_opted_out=is_opted_out, record=record)

    @staticmethod
    def _enqueue(record: JobRecord, event: JobEvent) -> None:
        payload = {"kind": event.kind, **event.payload}
        record.history.append(payload)
        record.queue.put_nowait(payload)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    db = Database(DB_PATH)
    repo = Repo(db)
    matcher = load_matcher(OPTOUT_PHRASES_PATH)
    rpc = AsyncRpcClient(IMSG_BIN)
    watcher = InboundWatcher(
        repo=repo,
        matcher=matcher,
        imsg_binary=IMSG_BIN,
        normalize=lambda raw, region: normalize_handle(IMSG_BIN, raw, region),
    )

    app.state.db = db
    app.state.repo = repo
    app.state.matcher = matcher
    app.state.rpc = rpc
    app.state.jobs = JobManager(repo)
    app.state.templates = TemplateStore(STATE_DIR / "templates.json")
    app.state.watcher = watcher

    await watcher.start()
    try:
        yield
    finally:
        await watcher.stop()
        await rpc.close()


app = FastAPI(lifespan=lifespan, title="imsg local UI")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    return Response(status_code=204)


def render(request: Request, template: str, **ctx) -> HTMLResponse:
    ctx.setdefault("busy", request.app.state.jobs.is_busy())
    return TEMPLATES.TemplateResponse(request, template, ctx)


# ---------------------------------------------------------------------------
# Compose / Bulk jobs

@app.get("/", response_class=HTMLResponse)
async def compose(request: Request, template_id: Optional[str] = None) -> HTMLResponse:
    defaults = {
        "imessage_pace": f"{DEFAULT_IMESSAGE_PACE[0]:g}-{DEFAULT_IMESSAGE_PACE[1]:g}",
        "sms_pace": f"{DEFAULT_SMS_PACE[0]:g}-{DEFAULT_SMS_PACE[1]:g}",
        "failure_ceiling": DEFAULT_FAILURE_CEILING,
    }
    initial_body = ""
    if template_id:
        tpl = request.app.state.templates.get(template_id)
        if tpl is not None:
            initial_body = tpl.body
    templates_list = _templates_for_picker(request)
    lists = request.app.state.repo.lists.named()
    return render(
        request,
        "compose.html",
        defaults=defaults,
        templates=templates_list,
        initial_body=initial_body,
        lists=lists,
    )


@app.post("/jobs")
async def create_job(
    request: Request,
    message: str = Form(""),
    recipients: Optional[UploadFile] = Form(None),
    list_id: Optional[str] = Form(None),
    imessage_pace: str = Form("3-6"),
    sms_pace: str = Form("15-30"),
    failure_ceiling: int = Form(DEFAULT_FAILURE_CEILING),
    confirm: Optional[str] = Form(None),
) -> RedirectResponse:
    if not message.strip():
        raise HTTPException(status_code=400, detail="Message body is required.")

    repo: Repo = request.app.state.repo
    has_recipients = recipients is not None and getattr(recipients, "filename", "")
    has_list = list_id is not None and list_id.strip()
    if has_recipients and has_list:
        raise HTTPException(
            status_code=400,
            detail="Provide either a CSV upload or an existing list, not both.",
        )
    if not has_recipients and not has_list:
        raise HTTPException(status_code=400, detail="Provide a CSV upload or pick an existing list.")

    job_dir = JOBS_DIR / time.strftime("%Y%m%d-%H%M%S")
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "message.txt").write_text(message, encoding="utf-8")
    recipients_path = job_dir / "recipients.csv"

    if has_recipients:
        raw = await recipients.read()
        if not raw:
            raise HTTPException(status_code=400, detail="recipients.csv is empty.")
        recipients_path.write_bytes(raw)
        bound_list = repo.lists.create_adhoc()
    else:
        try:
            list_id_int = int(list_id)  # type: ignore[arg-type]
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid list id.")
        bound_list = repo.lists.get(list_id_int)
        if bound_list is None:
            raise HTTPException(status_code=404, detail="List not found.")
        members = repo.lists.members(bound_list.id)
        if not members:
            raise HTTPException(status_code=400, detail="List has no members.")
        with recipients_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["handle"])
            for m in members:
                writer.writerow([m.normalized_handle])

    try:
        imessage_range = parse_pace(imessage_pace)
        sms_range = parse_pace(sms_pace)
    except ValueError:
        raise HTTPException(status_code=400, detail="Pace ranges must look like '3-6' or '5'.")

    config = JobConfig(
        recipients_path=recipients_path,
        message=message,
        imessage_pace=imessage_range,
        sms_pace=sms_range,
        failure_ceiling=failure_ceiling,
        dry_run=(confirm is None),
        imsg_binary=IMSG_BIN,
    )

    loop = asyncio.get_running_loop()
    record = request.app.state.jobs.start(config, list_id=bound_list.id, loop=loop)
    return RedirectResponse(url=f"/jobs/{record.id}", status_code=303)


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_page(request: Request, job_id: str) -> HTMLResponse:
    record = request.app.state.jobs.get(job_id)
    if record is None:
        raise HTTPException(status_code=404)
    return render(
        request,
        "job.html",
        job_id=job_id,
        config=record.config,
        history=record.history,
        finished=record.exit_code is not None,
        exit_code=record.exit_code,
    )


@app.get("/jobs/{job_id}/events")
async def job_events(request: Request, job_id: str) -> StreamingResponse:
    record = request.app.state.jobs.get(job_id)
    if record is None:
        raise HTTPException(status_code=404)

    async def gen() -> AsyncIterator[bytes]:
        for past in record.history:
            yield _sse_data(past)
        while True:
            if await request.is_disconnected():
                return
            try:
                payload = await asyncio.wait_for(record.queue.get(), timeout=15.0)
            except asyncio.TimeoutError:
                yield b": keep-alive\n\n"
                continue
            if payload is None:
                yield _sse_data({"kind": "stream_end", "exit_code": record.exit_code})
                return
            yield _sse_data(payload)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/jobs/{job_id}/cancel")
async def job_cancel(request: Request, job_id: str) -> HTMLResponse:
    ok = request.app.state.jobs.cancel(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Job not running.")
    return HTMLResponse("<p>Cancellation requested.</p>")


# ---------------------------------------------------------------------------
# One-off send

VALID_SERVICES = {"auto", "imessage", "sms"}


@app.get("/send", response_class=HTMLResponse)
async def send_form(
    request: Request,
    to: str = "",
    chat_id: Optional[int] = None,
    text: str = "",
    template_id: Optional[str] = None,
) -> HTMLResponse:
    mode = "chat" if chat_id is not None else "handle"
    if template_id:
        tpl = request.app.state.templates.get(template_id)
        if tpl is not None and not text:
            text = tpl.body
    templates_list = _templates_for_picker(request)
    return render(
        request,
        "send.html",
        mode=mode,
        to=to,
        chat_id=chat_id,
        text=text,
        service="auto",
        region="US",
        result=None,
        error=None,
        templates=templates_list,
    )


@app.post("/send", response_class=HTMLResponse)
async def send_action(
    request: Request,
    mode: str = Form("handle"),
    to: str = Form(""),
    chat_id: str = Form(""),
    text: str = Form(""),
    service: str = Form("auto"),
    region: str = Form("US"),
    attachment: Optional[UploadFile] = Form(None),
) -> HTMLResponse:
    repo: Repo = request.app.state.repo

    def re_render(error: Optional[str] = None, result: Optional[dict] = None) -> HTMLResponse:
        return render(
            request,
            "send.html",
            mode=mode,
            to=to,
            chat_id=chat_id,
            text=text,
            service=service,
            region=region,
            result=result,
            error=error,
            templates=_templates_for_picker(request),
        )

    if request.app.state.jobs.is_busy():
        return re_render(error="A bulk job is running. Wait for it to finish before sending one-offs.")

    chat_id_int: Optional[int]
    if mode == "chat":
        if not chat_id.strip():
            return re_render(error="Chat-id mode requires a chat id.")
        try:
            chat_id_int = int(chat_id.strip())
        except ValueError:
            return re_render(error="Chat id must be an integer.")
        recipient = ""
    else:
        recipient = to.strip()
        chat_id_int = None
        if not recipient:
            return re_render(error="Recipient is required for handle mode.")

    has_attachment = bool(attachment and attachment.filename)
    if not text.strip() and not has_attachment:
        return re_render(error="Provide a message, an attachment, or both.")
    if service not in VALID_SERVICES:
        return re_render(error="Service must be auto, imessage, or sms.")

    # Pre-flight: if it's a handle send and the contact is opted out, refuse early
    contact_id: Optional[int] = None
    normalized_handle: Optional[str] = None
    handle_kind: str = "unknown"
    if recipient:
        normalized_handle, _valid, handle_kind = normalize_handle(IMSG_BIN, recipient, region)
        if repo.contacts.is_opted_out(normalized_handle):
            return re_render(error=f"{normalized_handle} has opted out. Clear the flag from /optouts to send.")

    file_path: Optional[Path] = None
    if has_attachment:
        oneoff_dir = STATE_DIR / "oneoff"
        oneoff_dir.mkdir(parents=True, exist_ok=True)
        safe_name = Path(attachment.filename or "attachment").name
        file_path = oneoff_dir / f"{int(time.time())}-{safe_name}"
        file_path.write_bytes(await attachment.read())

    params: dict = {"text": text, "service": service, "region": region}
    if recipient:
        params["to"] = recipient
    if chat_id_int is not None:
        params["chat_id"] = chat_id_int
    if file_path is not None:
        params["file"] = str(file_path.resolve())

    target_type = "chat" if chat_id_int is not None else "handle"
    target = str(chat_id_int) if chat_id_int is not None else recipient
    attachment_path = str(file_path.resolve()) if file_path is not None else None

    if normalized_handle:
        contact = repo.contacts.upsert(normalized_handle=normalized_handle, kind=handle_kind, region=region)
        contact_id = contact.id

    def log(status: str, result: Optional[dict] = None, error: str = "") -> None:
        repo.sends.record(
            contact_id=contact_id,
            list_id=None,
            job_id=None,
            chat_id=chat_id_int,
            target_type=target_type,
            target=target,
            service=service,
            region=region,
            message_body=text,
            attachment_path=attachment_path,
            status=status,
            message_rowid=int((result or {}).get("id")) if (result or {}).get("id") else None,
            guid=(result or {}).get("guid") or None,
            error=error or None,
        )

    try:
        result = await request.app.state.rpc.call("send", params)
    except RpcError as err:
        msg = (err.error.get("message") if isinstance(err.error, dict) else None) or str(err)
        status = "ghost_send" if "ghost" in msg.lower() or "misroute" in msg.lower() else "error"
        log(status=status, error=msg)
        return re_render(error=f"RPC error: {msg}")
    except Exception as err:  # noqa: BLE001
        log(status="error", error=str(err))
        return re_render(error=f"Send failed: {err}")

    log(status="ok" if result.get("guid") else "ok_unverified", result=result)
    return re_render(result=result)


# ---------------------------------------------------------------------------
# Chats / history (read-only via RPC, with opt-out badges from DB)

@app.get("/chats", response_class=HTMLResponse)
async def chats_page(request: Request, limit: int = 50) -> HTMLResponse:
    repo: Repo = request.app.state.repo
    try:
        result = await request.app.state.rpc.call("chats.list", {"limit": limit})
    except Exception as err:  # noqa: BLE001
        return render(request, "chats.html", chats=[], error=str(err), opted_out=set())
    chats = result.get("chats", [])
    handles_in_view = {c.get("identifier") or "" for c in chats if not c.get("is_group")}
    opted_out = {h for h in handles_in_view if h and repo.contacts.is_opted_out(h)}
    return render(request, "chats.html", chats=chats, error=None, opted_out=opted_out)


@app.get("/chats/{chat_id}", response_class=HTMLResponse)
async def chat_history_page(request: Request, chat_id: int, limit: int = 50) -> HTMLResponse:
    try:
        result = await request.app.state.rpc.call(
            "messages.history", {"chat_id": chat_id, "limit": limit}
        )
    except Exception as err:  # noqa: BLE001
        return render(request, "history.html", chat_id=chat_id, messages=[], error=str(err))
    return render(
        request, "history.html", chat_id=chat_id, messages=result.get("messages", []), error=None
    )


# ---------------------------------------------------------------------------
# Lists

@app.get("/lists", response_class=HTMLResponse)
async def lists_page(request: Request, error: str = "", flash: str = "") -> HTMLResponse:
    return render(
        request,
        "lists.html",
        lists=request.app.state.repo.lists.all(),
        error=error or None,
        flash=flash or None,
    )


@app.post("/lists")
async def list_create(
    request: Request,
    name: str = Form(""),
    notes: str = Form(""),
) -> RedirectResponse:
    try:
        lst = request.app.state.repo.lists.create(name=name, kind="named", notes=notes or None)
    except ValueError as err:
        return RedirectResponse(url=f"/lists?error={_qs(str(err))}", status_code=303)
    return RedirectResponse(url=f"/lists/{lst.id}", status_code=303)


def _scan_optouts_blocking(repo: Repo, matcher) -> dict:
    """Run the historical opt-out scan via a short-lived `imsg rpc` subprocess.

    Spawns its own RpcClient (rather than reusing the app's async client) so it
    is safe to call from a worker thread without touching the event loop.
    """
    with RpcClient(IMSG_BIN) as rpc:
        return scan_history(rpc.call, repo, matcher, region="US")


@app.post("/contacts/import-messages")
async def contacts_import_messages(request: Request) -> RedirectResponse:
    repo: Repo = request.app.state.repo
    matcher = request.app.state.matcher
    try:
        summary = await asyncio.to_thread(import_contacts, repo, "US")
    except SystemExit as err:  # raised by fetch_handles when chat.db is missing
        return RedirectResponse(url=f"/lists?error={_qs(str(err))}", status_code=303)
    # Honor anyone who already replied STOP/cancel/etc. in your history.
    try:
        scan = await asyncio.to_thread(_scan_optouts_blocking, repo, matcher)
        opt_note = f" Flagged {scan['flagged']} opt-out(s) from past replies."
    except Exception as err:  # noqa: BLE001 — import succeeded; scan is best-effort
        opt_note = f" (Opt-out scan failed: {err})"
    flash = (
        f"Imported {summary['created']} new contacts from Messages "
        f"({summary['updated']} already existed, {summary['found']} handles scanned)."
        + opt_note
    )
    return RedirectResponse(url=f"/lists?flash={_qs(flash)}", status_code=303)


@app.post("/contacts/scan-optouts")
async def contacts_scan_optouts(request: Request) -> RedirectResponse:
    repo: Repo = request.app.state.repo
    matcher = request.app.state.matcher
    try:
        scan = await asyncio.to_thread(_scan_optouts_blocking, repo, matcher)
    except Exception as err:  # noqa: BLE001
        return RedirectResponse(url=f"/lists?error={_qs(f'Opt-out scan failed: {err}')}", status_code=303)
    flash = (
        f"Scanned {scan['messages_scanned']} inbound messages across "
        f"{scan['chats_scanned']} chats. Flagged {scan['flagged']} new opt-out(s) "
        f"({scan['already_flagged']} already flagged)."
    )
    return RedirectResponse(url=f"/lists?flash={_qs(flash)}", status_code=303)


@app.get("/lists/{list_id}", response_class=HTMLResponse)
async def list_detail(request: Request, list_id: int, error: str = "", flash: str = "") -> HTMLResponse:
    repo: Repo = request.app.state.repo
    lst = repo.lists.get(list_id)
    if lst is None:
        raise HTTPException(status_code=404)
    members = repo.lists.members(list_id)
    return render(
        request,
        "list_detail.html",
        list=lst,
        members=members,
        error=error or None,
        flash=flash or None,
    )


@app.post("/lists/{list_id}/import")
async def list_import(
    request: Request,
    list_id: int,
    recipients: UploadFile = Form(...),
) -> RedirectResponse:
    repo: Repo = request.app.state.repo
    lst = repo.lists.get(list_id)
    if lst is None:
        raise HTTPException(status_code=404)
    raw = await recipients.read()
    if not raw:
        return RedirectResponse(
            url=f"/lists/{list_id}?error={_qs('Empty CSV')}", status_code=303
        )

    text = raw.decode("utf-8", errors="replace")
    handles: list[str] = []
    reader = csv.reader(io.StringIO(text))
    first = True
    header_index: Optional[int] = None
    for row in reader:
        if not row:
            continue
        stripped = [c.strip() for c in row]
        if first:
            first = False
            lowered = [c.lower() for c in stripped]
            if "handle" in lowered:
                header_index = lowered.index("handle")
                continue
            header_index = 0
        value = stripped[header_index or 0]
        if value:
            handles.append(value)

    added = 0
    skipped_optout = 0
    invalid = 0
    seen: set[str] = set()
    for raw_handle in handles:
        normalized, valid, kind = normalize_handle(IMSG_BIN, raw_handle, "US")
        if not valid:
            invalid += 1
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        contact = repo.contacts.upsert(normalized_handle=normalized, kind=kind)
        if contact.opted_out:
            skipped_optout += 1
            continue
        added += repo.lists.add_members(list_id, [contact.id])

    flash = f"Imported {added} new contacts (skipped {skipped_optout} opt-outs, {invalid} invalid)."
    return RedirectResponse(
        url=f"/lists/{list_id}?flash={_qs(flash)}", status_code=303
    )


@app.post("/lists/{list_id}/remove")
async def list_remove_member(
    request: Request,
    list_id: int,
    contact_id: int = Form(...),
) -> RedirectResponse:
    request.app.state.repo.lists.remove_member(list_id, contact_id)
    return RedirectResponse(url=f"/lists/{list_id}", status_code=303)


@app.post("/lists/{list_id}/delete")
async def list_delete(request: Request, list_id: int) -> RedirectResponse:
    request.app.state.repo.lists.delete(list_id)
    return RedirectResponse(url="/lists", status_code=303)


# ---------------------------------------------------------------------------
# Opt-outs / contacts

@app.get("/optouts", response_class=HTMLResponse)
async def optouts_page(request: Request, error: str = "", flash: str = "") -> HTMLResponse:
    repo: Repo = request.app.state.repo
    return render(
        request,
        "optouts.html",
        contacts=repo.contacts.list_opted_out(),
        recent_events=repo.optouts.list_recent(50),
        error=error or None,
        flash=flash or None,
    )


@app.post("/contacts/{contact_id}/optout")
async def contact_optout(
    request: Request,
    contact_id: int,
    reason: str = Form("manual"),
) -> RedirectResponse:
    request.app.state.repo.contacts.mark_opted_out(contact_id, reason or "manual")
    return RedirectResponse(url="/optouts?flash=Opted+out.", status_code=303)


@app.post("/contacts/{contact_id}/clear-optout")
async def contact_clear_optout(request: Request, contact_id: int) -> RedirectResponse:
    request.app.state.repo.contacts.clear_optout(contact_id)
    return RedirectResponse(url="/optouts?flash=Cleared.", status_code=303)


# ---------------------------------------------------------------------------
# Contacts table: browse / filter / bulk actions / CSV enrichment

CONTACTS_PAGE_SIZE = 50

_CSV_HANDLE_KEYS = {
    "handle", "phone", "phone number", "phonenumber", "number", "mobile",
    "cell", "cellphone", "telephone", "tel", "to", "msisdn",
}
_CSV_EMAIL_KEYS = {"email", "e-mail", "email address", "mail"}
_CSV_NAME_KEYS = {
    "name", "full name", "fullname", "display name", "display_name",
    "displayname", "contact", "contact name",
}
_CSV_FIRST_KEYS = {"first name", "first", "firstname", "given name"}
_CSV_LAST_KEYS = {"last name", "last", "lastname", "surname", "family name"}
_CSV_NOTES_KEYS = {
    "notes", "note", "info", "comment", "comments", "company", "organization",
    "organisation", "org", "title", "address", "tag", "tags", "city", "state",
    "country", "zip", "label",
}
_CSV_KNOWN_KEYS = (
    _CSV_HANDLE_KEYS | _CSV_EMAIL_KEYS | _CSV_NAME_KEYS
    | _CSV_FIRST_KEYS | _CSV_LAST_KEYS | _CSV_NOTES_KEYS
)


def _parse_contacts_csv(text: str) -> list[dict]:
    """Parse a contacts CSV into [{handle, name, notes}].

    Recognizes common column headers case-insensitively (phone/handle, name or
    first/last, email, plus assorted info columns folded into notes). With no
    recognizable header, treats column 1 as the handle and column 2 as a name.
    """
    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if any(c.strip() for c in r)]
    if not rows:
        return []
    header = [c.strip() for c in rows[0]]
    lowered = [c.lower() for c in header]
    has_header = any(h in _CSV_KNOWN_KEYS for h in lowered)

    if not has_header:
        out = []
        for r in rows:
            handle = (r[0].strip() if r else "")
            name = (r[1].strip() if len(r) > 1 else "")
            if handle:
                out.append({"handle": handle, "name": name, "notes": ""})
        return out

    handle_idx = email_idx = name_idx = first_idx = last_idx = None
    notes_cols: list[tuple[str, int]] = []
    for i, h in enumerate(lowered):
        if h in _CSV_HANDLE_KEYS and handle_idx is None:
            handle_idx = i
        elif h in _CSV_EMAIL_KEYS and email_idx is None:
            email_idx = i
        elif h in _CSV_NAME_KEYS and name_idx is None:
            name_idx = i
        elif h in _CSV_FIRST_KEYS and first_idx is None:
            first_idx = i
        elif h in _CSV_LAST_KEYS and last_idx is None:
            last_idx = i
        elif h in _CSV_NOTES_KEYS:
            notes_cols.append((header[i], i))

    def cell(r: list[str], i: Optional[int]) -> str:
        return r[i].strip() if (i is not None and i < len(r)) else ""

    out = []
    for r in rows[1:]:
        handle = cell(r, handle_idx) or cell(r, email_idx)
        if not handle:
            continue
        if name_idx is not None:
            name = cell(r, name_idx)
        else:
            name = " ".join(p for p in (cell(r, first_idx), cell(r, last_idx)) if p)
        notes = "; ".join(
            f"{label}: {cell(r, i)}" for label, i in notes_cols if cell(r, i)
        )
        out.append({"handle": handle, "name": name, "notes": notes})
    return out


def _run_csv_import(repo: Repo, rows: list[dict], target_list_id: Optional[int], region: str) -> dict:
    """Normalize + upsert + enrich each CSV row (blocking; run off the loop)."""
    created = updated = invalid = 0
    seen: set[int] = set()
    member_ids: list[int] = []
    for row in rows:
        normalized, valid, kind = normalize_handle(IMSG_BIN, row["handle"], region)
        if not valid:
            invalid += 1
            continue
        before = repo.contacts.get_by_handle(normalized)
        contact = repo.contacts.upsert(normalized_handle=normalized, kind=kind)
        repo.contacts.update_details(
            contact.id,
            display_name=row["name"] or None,
            notes=row["notes"] or None,
        )
        if contact.id not in seen:
            seen.add(contact.id)
            member_ids.append(contact.id)
            if before is None:
                created += 1
            else:
                updated += 1
    added = 0
    if target_list_id is not None and member_ids:
        added = repo.lists.add_members(target_list_id, member_ids)
    return {"created": created, "updated": updated, "invalid": invalid, "added": added}


def _redirect_with(dest: str, *, flash: str = "", error: str = "") -> RedirectResponse:
    parts = []
    if flash:
        parts.append(f"flash={_qs(flash)}")
    if error:
        parts.append(f"error={_qs(error)}")
    if parts:
        sep = "&" if "?" in dest else "?"
        dest = dest + sep + "&".join(parts)
    return RedirectResponse(url=dest, status_code=303)


def _resolve_target_list(repo: Repo, list_id: str, new_list_name: str):
    """Return (List|None, error). Prefers a new named list, else an existing id."""
    name = new_list_name.strip()
    if name:
        try:
            return repo.lists.create(name), None
        except ValueError:
            existing = next((lst for lst in repo.lists.all() if lst.name == name), None)
            return existing, (None if existing else f"Could not create list {name!r}.")
    if list_id.strip():
        return repo.lists.get(int(list_id)), None
    return None, None


@app.get("/contacts", response_class=HTMLResponse)
async def contacts_page(
    request: Request,
    q: str = "",
    kind: str = "",
    status: str = "",
    list_id: str = "",
    page: int = 1,
    error: str = "",
    flash: str = "",
) -> HTMLResponse:
    from urllib.parse import urlencode

    repo: Repo = request.app.state.repo
    lid = int(list_id) if list_id.strip().isdigit() else None
    filters = dict(
        search=q or None,
        kind=kind or None,
        status=status or None,
        list_id=lid,
    )
    total = repo.contacts.count_all(**filters)
    pages = max((total + CONTACTS_PAGE_SIZE - 1) // CONTACTS_PAGE_SIZE, 1)
    page = min(max(page, 1), pages)
    offset = (page - 1) * CONTACTS_PAGE_SIZE
    contacts = repo.contacts.list_all(**filters, limit=CONTACTS_PAGE_SIZE, offset=offset)

    filt = {k: v for k, v in (("q", q), ("kind", kind), ("status", status), ("list_id", list_id)) if v}
    return render(
        request,
        "contacts.html",
        contacts=contacts,
        total=total,
        page=page,
        pages=pages,
        start=(offset + 1) if total else 0,
        end=min(offset + CONTACTS_PAGE_SIZE, total),
        q=q,
        kind=kind,
        status=status,
        sel_list_id=list_id,
        lists=repo.lists.all(),
        filter_qs=urlencode(filt),
        error=error or None,
        flash=flash or None,
    )


@app.post("/contacts/bulk")
async def contacts_bulk(
    request: Request,
    action: str = Form(...),
    contact_ids: list[int] = Form([]),
    list_id: str = Form(""),
    new_list_name: str = Form(""),
    back: str = Form("/contacts"),
) -> RedirectResponse:
    repo: Repo = request.app.state.repo
    dest = back or "/contacts"
    if not contact_ids:
        return _redirect_with(dest, error="No contacts selected.")

    if action == "opt_out":
        n = repo.contacts.bulk_mark_opted_out(contact_ids, "manual")
        return _redirect_with(dest, flash=f"Opted out {n} contact(s).")
    if action == "clear_optout":
        n = repo.contacts.bulk_clear_optout(contact_ids)
        return _redirect_with(dest, flash=f"Cleared opt-out on {n} contact(s).")
    if action == "add_to_list":
        target, err = _resolve_target_list(repo, list_id, new_list_name)
        if err:
            return _redirect_with(dest, error=err)
        if target is None:
            return _redirect_with(dest, error="Pick a list or enter a new list name.")
        added = repo.lists.add_members(target.id, contact_ids)
        already = len(contact_ids) - added
        return _redirect_with(
            dest, flash=f"Added {added} to {target.name} ({already} already there)."
        )
    return _redirect_with(dest, error=f"Unknown action: {action}")


@app.post("/contacts/import-csv")
async def contacts_import_csv(
    request: Request,
    file: UploadFile = Form(...),
    list_id: str = Form(""),
    new_list_name: str = Form(""),
    region: str = Form("US"),
) -> RedirectResponse:
    repo: Repo = request.app.state.repo
    raw = await file.read()
    if not raw:
        return _redirect_with("/contacts", error="Empty CSV.")
    rows = _parse_contacts_csv(raw.decode("utf-8", errors="replace"))
    if not rows:
        return _redirect_with("/contacts", error="No usable rows found in CSV.")

    target, err = _resolve_target_list(repo, list_id, new_list_name)
    if err:
        return _redirect_with("/contacts", error=err)

    summary = await asyncio.to_thread(
        _run_csv_import, repo, rows, target.id if target else None, region or "US"
    )
    note = f" Added {summary['added']} to {target.name}." if target else ""
    flash = (
        f"Imported {len(rows)} row(s): {summary['created']} new, "
        f"{summary['updated']} updated, {summary['invalid']} invalid.{note}"
    )
    return _redirect_with("/contacts", flash=flash)


# ---------------------------------------------------------------------------
# Inbound

@app.get("/inbound", response_class=HTMLResponse)
async def inbound_page(request: Request) -> HTMLResponse:
    return render(
        request,
        "inbound.html",
        messages=request.app.state.repo.received.recent(100),
    )


# ---------------------------------------------------------------------------
# Results (now SQLite-backed)

@app.get("/results", response_class=HTMLResponse)
async def results_index(request: Request) -> HTMLResponse:
    repo: Repo = request.app.state.repo
    return render(
        request,
        "results.html",
        oneoffs=repo.sends.recent_oneoffs(50),
        jobs=repo.sends.jobs_summary(),
        rows=None,
        selected=None,
    )


@app.get("/results/job/{job_id}", response_class=HTMLResponse)
async def results_view_job(request: Request, job_id: str) -> HTMLResponse:
    repo: Repo = request.app.state.repo
    rows = repo.sends.for_job(job_id)
    if not rows:
        raise HTTPException(status_code=404)
    return render(
        request,
        "results.html",
        oneoffs=None,
        jobs=None,
        rows=rows,
        selected=job_id,
    )


# ---------------------------------------------------------------------------
# Templates

def _sse_data(payload: dict) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode("utf-8")


def _templates_for_picker(request: Request) -> list[dict]:
    return [
        {"id": t.id, "name": t.name, "body": t.body}
        for t in request.app.state.templates.list()
    ]


@app.get("/templates", response_class=HTMLResponse)
async def templates_page(request: Request, error: str = "", flash: str = "") -> HTMLResponse:
    return render(
        request,
        "templates.html",
        templates=request.app.state.templates.list(),
        error=error or None,
        flash=flash or None,
    )


@app.post("/templates")
async def template_create(
    request: Request,
    name: str = Form(""),
    body: str = Form(""),
) -> RedirectResponse:
    try:
        request.app.state.templates.create(name=name, body=body)
    except TemplateStoreError as err:
        return RedirectResponse(url=f"/templates?error={_qs(str(err))}", status_code=303)
    return RedirectResponse(url=f"/templates?flash={_qs('Saved.')}", status_code=303)


@app.post("/templates/{template_id}")
async def template_update(
    request: Request,
    template_id: str,
    name: str = Form(""),
    body: str = Form(""),
) -> RedirectResponse:
    try:
        request.app.state.templates.update(template_id, name=name, body=body)
    except TemplateStoreError as err:
        return RedirectResponse(url=f"/templates?error={_qs(str(err))}", status_code=303)
    return RedirectResponse(url=f"/templates?flash={_qs('Updated.')}", status_code=303)


@app.post("/templates/{template_id}/delete")
async def template_delete(request: Request, template_id: str) -> RedirectResponse:
    if not request.app.state.templates.delete(template_id):
        return RedirectResponse(url=f"/templates?error={_qs('Template not found.')}", status_code=303)
    return RedirectResponse(url=f"/templates?flash={_qs('Deleted.')}", status_code=303)


@app.post("/api/templates")
async def template_create_json(
    request: Request,
    name: str = Form(""),
    body: str = Form(""),
) -> dict:
    try:
        tpl = request.app.state.templates.create(name=name, body=body)
    except TemplateStoreError as err:
        raise HTTPException(status_code=400, detail=str(err))
    return {"id": tpl.id, "name": tpl.name, "body": tpl.body}


def _qs(value: str) -> str:
    from urllib.parse import quote
    return quote(value, safe="")


def main() -> None:
    import uvicorn

    uvicorn.run(
        "server:app",
        host=HOST,
        port=PORT,
        app_dir=str(BASE_DIR),
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
