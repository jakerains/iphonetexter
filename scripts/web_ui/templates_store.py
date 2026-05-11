"""File-backed message-template store.

Used by the local web UI to persist named message bodies that can be loaded
into the bulk compose form or the one-off send form. Single JSON file under
`scripts/web_ui/state/templates.json`, atomic writes, no concurrency primitives
beyond a process-local lock — single-user local tool.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

SCHEMA_VERSION = 1


@dataclass
class Template:
    id: str
    name: str
    body: str
    created_at: str
    updated_at: str


class TemplateStoreError(RuntimeError):
    pass


class TemplateStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _read(self) -> dict:
        if not self.path.exists():
            return {"version": SCHEMA_VERSION, "templates": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as err:
            raise TemplateStoreError(f"templates.json unreadable: {err}") from err
        if not isinstance(data, dict) or "templates" not in data:
            raise TemplateStoreError("templates.json missing 'templates' key")
        return data

    def _write(self, data: dict) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def list(self) -> list[Template]:
        with self._lock:
            data = self._read()
        return sorted(
            (Template(**t) for t in data["templates"]),
            key=lambda t: t.updated_at,
            reverse=True,
        )

    def get(self, template_id: str) -> Optional[Template]:
        for tpl in self.list():
            if tpl.id == template_id:
                return tpl
        return None

    def create(self, name: str, body: str) -> Template:
        clean_name = name.strip()
        if not clean_name:
            raise TemplateStoreError("Template name is required.")
        if not body.strip():
            raise TemplateStoreError("Template body is required.")
        now = _now_iso()
        template = Template(
            id=uuid.uuid4().hex[:12],
            name=clean_name,
            body=body,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            data = self._read()
            for existing in data["templates"]:
                if existing["name"].strip().lower() == clean_name.lower():
                    raise TemplateStoreError(
                        f"A template named {clean_name!r} already exists."
                    )
            data["templates"].append(asdict(template))
            self._write(data)
        return template

    def update(self, template_id: str, name: str, body: str) -> Template:
        clean_name = name.strip()
        if not clean_name:
            raise TemplateStoreError("Template name is required.")
        if not body.strip():
            raise TemplateStoreError("Template body is required.")
        with self._lock:
            data = self._read()
            updated: Optional[Template] = None
            for entry in data["templates"]:
                if entry["id"] != template_id and entry["name"].strip().lower() == clean_name.lower():
                    raise TemplateStoreError(
                        f"A template named {clean_name!r} already exists."
                    )
            for entry in data["templates"]:
                if entry["id"] == template_id:
                    entry["name"] = clean_name
                    entry["body"] = body
                    entry["updated_at"] = _now_iso()
                    updated = Template(**entry)
                    break
            if updated is None:
                raise TemplateStoreError("Template not found.")
            self._write(data)
        return updated

    def delete(self, template_id: str) -> bool:
        with self._lock:
            data = self._read()
            before = len(data["templates"])
            data["templates"] = [t for t in data["templates"] if t["id"] != template_id]
            if len(data["templates"]) == before:
                return False
            self._write(data)
        return True


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
