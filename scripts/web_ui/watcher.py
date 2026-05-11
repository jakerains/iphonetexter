"""Inbound watcher: long-lived `imsg watch --json` subscription.

Spawned during the FastAPI app lifespan. Reads NDJSON one line per message,
upserts contacts, records inbound messages, and runs opt-out detection on
text replies. Persists the highest seen rowid in `watch_state` so a server
restart resumes from the right place.

Process model: a Python `subprocess.Popen` runs `imsg watch --json` with
`--since-rowid` if a cursor exists. A worker thread reads stdout line by
line and dispatches to the asyncio loop via `call_soon_threadsafe`. On
subprocess death we exponential-backoff and respawn.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
from typing import Callable, Optional

from optout import OptOutMatcher
from repository import Repo


class InboundWatcher:
    def __init__(
        self,
        *,
        repo: Repo,
        matcher: OptOutMatcher,
        imsg_binary: str,
        normalize: Callable[[str, str], tuple[str, bool, str]],
        region: str = "US",
    ):
        self.repo = repo
        self.matcher = matcher
        self.binary = imsg_binary
        self.normalize = normalize
        self.region = region
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._proc: Optional[subprocess.Popen] = None
        self._handle_cache: dict[str, str] = {}

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._task = loop.create_task(self._run_loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except Exception:
                pass
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()

    async def _run_loop(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                await self._run_once()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                print(f"watcher: {type(err).__name__}: {err}", file=sys.stderr, flush=True)
            if self._stop.is_set():
                break
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 30.0)

    async def _run_once(self) -> None:
        cursor = self.repo.watch_state.get_cursor()
        cmd = [self.binary, "watch", "--json"]
        if cursor is not None:
            cmd += ["--since-rowid", str(cursor)]

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=os.environ.copy(),
        )
        self._proc = proc
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=1024)

        def reader() -> None:
            try:
                assert proc.stdout is not None
                for line in iter(proc.stdout.readline, ""):
                    if not line:
                        break
                    loop.call_soon_threadsafe(queue.put_nowait, ("line", line))
            except Exception as err:  # noqa: BLE001
                loop.call_soon_threadsafe(queue.put_nowait, ("err", str(err)))
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, ("eof", None))

        thread = threading.Thread(target=reader, daemon=True, name="watcher-reader")
        thread.start()

        try:
            while not self._stop.is_set():
                try:
                    kind, payload = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    if proc.poll() is not None:
                        break
                    continue
                if kind == "eof":
                    return
                if kind == "err":
                    raise RuntimeError(payload)
                try:
                    msg = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                # Run DB work in a thread so we don't block the loop
                await loop.run_in_executor(None, self._handle_message, msg)
        finally:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()
            self._proc = None

    def _handle_message(self, msg: dict) -> None:
        rowid = msg.get("id")
        guid = msg.get("guid")
        if not guid or rowid is None:
            return
        try:
            rowid_int = int(rowid)
        except (TypeError, ValueError):
            return

        # Always advance the cursor — including for our own outbound rows —
        # so resume after restart does not re-read the same range.
        try:
            self.repo.watch_state.set_cursor(rowid_int)
        except Exception:
            pass

        if bool(msg.get("is_from_me")):
            return

        is_reaction = bool(msg.get("is_reaction"))
        sender = msg.get("sender") or ""
        text = msg.get("text") or ""
        chat_id = msg.get("chat_id")
        received_at = msg.get("created_at")

        contact_id: Optional[int] = None
        if sender:
            normalized = self._normalize_cached(sender)
            if "@" in normalized:
                kind = "email"
            elif normalized.startswith("+"):
                kind = "phone"
            else:
                kind = "unknown"
            contact = self.repo.contacts.upsert(normalized_handle=normalized, kind=kind)
            contact_id = contact.id

        new_id = self.repo.received.insert_idempotent(
            guid=str(guid),
            chat_id=int(chat_id) if isinstance(chat_id, (int, float)) else None,
            contact_id=contact_id,
            sender_handle=sender or None,
            text=text,
            is_reaction=is_reaction,
            received_at=received_at,
            message_rowid=rowid_int,
        )

        if new_id is None or is_reaction or contact_id is None or not text:
            return

        match = self.matcher.scan(text)
        if match:
            self.repo.optouts.record(contact_id, new_id, match)
            self.repo.contacts.mark_opted_out(contact_id, match)

    def _normalize_cached(self, handle: str) -> str:
        cached = self._handle_cache.get(handle)
        if cached is not None:
            return cached
        try:
            normalized, _valid, _kind = self.normalize(handle, self.region)
        except Exception:
            normalized = handle
        self._handle_cache[handle] = normalized
        return normalized
