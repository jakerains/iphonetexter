"""Opt-out phrase matcher.

Two tiers, because real inbound messages contain two very different kinds of
opt-out wording:

* STRONG phrases are self-referential requests the sender clearly means
  ("stop texting me", "remove me", "lose my number"). They never appear in
  carrier boilerplate, so we match them anywhere in the message.

* WEAK phrases are bare keywords ("stop", "cancel", "unsubscribe") that ALSO
  show up in instructional footers like "Reply STOP to cancel" on automated
  and marketing texts. Treating those as the sender opting out would wrongly
  drop legitimate businesses from future sends. So a weak keyword only counts
  when it IS / LEADS a short message (how carriers actually require it — a
  message that is just "STOP"), and never inside obvious boilerplate.

False positives are worse than false negatives here: a wrongly-flagged
contact silently disappears from future sends.

If `state/optout_phrases.txt` exists at server startup, its non-empty
non-comment lines REPLACE the defaults entirely and are all treated as STRONG
(matched anywhere) — you're taking explicit control. Restart the server, or
trigger a re-scan, to pick up edits.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Optional

# Self-referential — safe to match anywhere in the message.
DEFAULT_STRONG_PHRASES: tuple[str, ...] = (
    "stop texting me",
    "stop texting",
    "stop messaging me",
    "stop messaging",
    "stop contacting me",
    "stop sending",
    "quit texting",
    "do not text",
    "don't text me",
    "don't text",
    "please don't text",
    "not text me",
    "do not contact",
    "don't contact me",
    "leave me alone",
    "lose my number",
    "lose this number",
    "delete my number",
    "remove my number",
    "remove me",
    "take me off",
    "take me off your list",
    "take my number off",
    "unsubscribe me",
    "not interested",
    "no longer interested",
)

# Bare keywords — only count as the whole / leading text of a SHORT message,
# and never inside "reply STOP to cancel"-style boilerplate. "opt out" lives
# here (not strong) because "reply STOP to opt out" footers use it too.
DEFAULT_WEAK_PHRASES: tuple[str, ...] = (
    "stop",
    "cancel",
    "quit",
    "unsubscribe",
    "no more",
    "opt out",
    "opt-out",
)

# Instructional footer detector, e.g. "Reply STOP to cancel",
# "Text STOP to opt out", "Reply HELP for help, STOP to end".
_BOILERPLATE_RE = re.compile(
    r"(reply|text|txt|send)\b.{0,40}?\b(stop|cancel|quit|unsubscribe|end)\b"
    r"|\b(stop|cancel|unsubscribe|quit|end)\s+to\s+(cancel|stop|unsubscribe|opt|end|quit)\b",
    re.IGNORECASE,
)

# A message counts as "short" (so a bare keyword is a genuine opt-out) when it
# has at most this many words — e.g. "stop", "please stop", "cancel".
_SHORT_WORDS = 3


def _compile(phrases: tuple[str, ...]) -> Optional[re.Pattern]:
    cleaned = tuple(p.strip().lower() for p in phrases if p.strip())
    if not cleaned:
        return None
    # \b on both sides; inner whitespace becomes \s+ so "take   me off" matches.
    alternatives = [
        r"\b" + r"\s+".join(re.escape(tok) for tok in p.split()) + r"\b"
        for p in cleaned
    ]
    return re.compile("|".join(alternatives), re.IGNORECASE)


class OptOutMatcher:
    """Two-tier matcher. See module docstring for the strong/weak distinction."""

    def __init__(self, strong: tuple[str, ...], weak: tuple[str, ...] = ()):
        self.strong = tuple(p.strip().lower() for p in strong if p.strip())
        self.weak = tuple(p.strip().lower() for p in weak if p.strip())
        self._strong_re = _compile(self.strong)
        self._weak_re = _compile(self.weak)

    def scan(self, text: str) -> Optional[str]:
        if not text:
            return None
        stripped = text.strip()
        # iMessage stores curly apostrophes (U+2019); fold to ASCII so phrases
        # like "don't text me" match real messages.
        low = stripped.lower().replace("’", "'").replace("‘", "'")

        # 1. Strong, self-referential phrases — match anywhere.
        if self._strong_re is not None:
            m = self._strong_re.search(low)
            if m:
                return m.group(0).strip()

        if self._weak_re is None:
            return None

        # 2. Ignore weak keywords sitting inside instructional boilerplate.
        if _BOILERPLATE_RE.search(low):
            return None

        # 3. Weak keyword counts only if it is / leads a short message.
        wm = self._weak_re.search(low)
        if wm is None:
            return None
        is_short = len(low.split()) <= _SHORT_WORDS
        if is_short or wm.start() == 0:
            return wm.group(0).strip()
        return None

    def fingerprint(self) -> str:
        """Stable hash of the active phrase set; used to trigger a re-scan
        when the rules change."""
        payload = "|".join(sorted(self.strong)) + "##" + "|".join(sorted(self.weak))
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()


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
            # User-supplied list takes full control: treat all as strong.
            return OptOutMatcher(phrases, ())
    return OptOutMatcher(DEFAULT_STRONG_PHRASES, DEFAULT_WEAK_PHRASES)
