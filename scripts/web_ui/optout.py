"""Opt-out phrase matcher.

Defaults are conservative — only phrases that are unambiguous opt-out
signals in a one-to-one SMS/iMessage context. We deliberately don't
include "no" or "stop it" because those can carry too many meanings in
casual conversation. False positives are worse than false negatives here:
a wrongly-flagged contact silently disappears from future sends.

If `state/optout_phrases.txt` exists at server startup, its non-empty
non-comment lines REPLACE the defaults entirely. The file is read once
at module import (or via `reload_phrases()`); restart the server to pick
up edits.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

DEFAULT_PHRASES: tuple[str, ...] = (
    "stop",
    "stop sending",
    "stop messaging",
    "unsubscribe",
    "remove me",
    "take me off",
    "take me off your list",
    "opt out",
    "opt-out",
    "no more",
    "quit",
    "cancel",
    "do not text",
    "don't text me",
)


class OptOutMatcher:
    """Compile phrases once into a single regex; reuse across watcher events."""

    def __init__(self, phrases: tuple[str, ...]):
        self.phrases = tuple(p.strip().lower() for p in phrases if p.strip())
        if not self.phrases:
            self._regex = None
            return
        # \b on both sides so "stop" matches "stop." and "stop, please" but
        # not "nonstop". For multi-word phrases the inner whitespace is
        # \s+ so "take   me off" matches.
        alternatives = [
            r"\b" + r"\s+".join(re.escape(tok) for tok in p.split()) + r"\b"
            for p in self.phrases
        ]
        self._regex = re.compile("|".join(alternatives), re.IGNORECASE)

    def scan(self, text: str) -> Optional[str]:
        if not text or self._regex is None:
            return None
        match = self._regex.search(text)
        if match is None:
            return None
        return match.group(0).lower()


def load_matcher(override_path: Optional[Path] = None) -> OptOutMatcher:
    if override_path is not None and override_path.exists():
        try:
            lines = override_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        phrases = tuple(
            line.strip()
            for line in lines
            if line.strip() and not line.strip().startswith("#")
        )
        if phrases:
            return OptOutMatcher(phrases)
    return OptOutMatcher(DEFAULT_PHRASES)
