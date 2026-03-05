# Copyright 2026 Tim Escolopio / 3D Tech Solutions
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Guardrails - PII masking and prompt injection filters.

Adversarial PII detection strategy (pre-scan normalisation pipeline):

1. Strip invisible / zero-width Unicode control characters (U+200B etc.).
2. NFKC-normalise (converts fullwidth digits/letters to ASCII equivalents).
3. URL-decode percent-encoded sequences (``%40`` → ``@``, ``%2E`` → ``.``).
4. Compact whitespace that surrounds ``@`` so that ``foo @ bar.com`` becomes
   ``foo@bar.com`` before patterns are applied.

The patterns themselves allow optional ``\\s`` (whitespace including newlines)
around ``.`` separators in email domains and IP octets, and accept ``[\\s\\-]+``
as SSN / phone separators.  Credit-card groups allow ``[\\s\\-]?`` between
them.  This covers all roadmap adversarial classes without touching surrounding
plain text.
"""

import re
import unicodedata
import urllib.parse
from dataclasses import dataclass
from typing import Final

from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

# Invisible / zero-width Unicode characters that should be stripped before scanning.
_INVISIBLE_RE: Final[re.Pattern[str]] = re.compile(
    r"[\u00ad\u200b\u200c\u200d\u200e\u200f"
    r"\u2028\u2029\u202a-\u202e\u2060\u2061\u2062\u2063\u2064\ufeff]"
)

# Compact whitespace (spaces, tabs, newlines) around the ``@`` sign only.
# Applying this to ``.`` selectively would risk collapsing sentence boundaries
# (``sentence. Next`` → ``sentence.Next``), so we restrict to ``@``.
_AT_WS_RE: Final[re.Pattern[str]] = re.compile(r"[ \t\n\r]*@[ \t\n\r]*")


def _normalize(text: str) -> str:
    """Normalise *text* for adversarial PII detection.

    Steps applied in order:

    1. Strip invisible / zero-width control characters.
    2. NFKC Unicode normalisation (fullwidth → ASCII, ligatures, etc.).
    3. URL-decode percent-encoded sequences.
    4. Collapse any whitespace surrounding ``@`` (email obfuscation).

    Args:
        text: Raw input text.

    Returns:
        Normalised text ready for PII pattern matching.
    """
    text = _INVISIBLE_RE.sub("", text)
    text = unicodedata.normalize("NFKC", text)
    text = urllib.parse.unquote(text)
    text = _AT_WS_RE.sub("@", text)
    return text


# ---------------------------------------------------------------------------
# PII patterns (applied to normalised text)
# ---------------------------------------------------------------------------

_PII_PATTERNS: Final[list[tuple[str, re.Pattern[str]]]] = [
    # Email — local part @  domain.  tld
    # Allow optional whitespace around the final TLD dot (handles newline
    # splits such as ``user@example\n.com``).
    (
        "email",
        re.compile(
            r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\s*\.\s*[A-Za-z]{2,}\b",
            re.IGNORECASE,
        ),
    ),
    # SSN: ``\d{3}[\s\-]+\d{2}[\s\-]+\d{4}``
    # ``[\s\-]+`` (one-or-more) handles ``123 - 45 - 6789`` and
    # ``123-45\n-6789`` (newline + hyphen as two-character separator).
    (
        "ssn",
        re.compile(
            r"\b(?!000|666|9\d\d)\d{3}[\s\-]+(?!00)\d{2}[\s\-]+(?!0000)\d{4}\b"
        ),
    ),
    # Credit card: four groups of four digits with optional whitespace/hyphen.
    # ``[\s\-]?`` allows ``4111 1111\n1111 1111`` and tab-separated variants.
    (
        "credit_card",
        re.compile(r"\b(?:\d{4}[\s\-]?){3}\d{4}\b"),
    ),
    # US Phone: optional ``+1`` country code; separators are ``[-.\s]?``
    # (already supports newlines via ``\s``).
    (
        "phone_us",
        re.compile(
            r"\b(?:\+1[\s\-.]?)?\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{4}\b"
        ),
    ),
    # IPv4: each octet separated by optional-whitespace dot.
    # ``\s*\.\s*`` handles ``192 . 168 . 1 . 1`` and ``10.0.0\n.1``.
    (
        "ip_address",
        re.compile(
            r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\s*\.\s*){3}"
            r"(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b"
        ),
    ),
]

# ---------------------------------------------------------------------------
# Prompt injection patterns
# ---------------------------------------------------------------------------

_INJECTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.IGNORECASE),
    re.compile(r"disregard\s+(all\s+)?prior\s+(instructions|rules)", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+(?:a|an)\s+", re.IGNORECASE),
    re.compile(r"act\s+as\s+(?:if\s+)?(?:you\s+are\s+)?(?:a|an)\s+", re.IGNORECASE),
    re.compile(r"jailbreak", re.IGNORECASE),
    re.compile(r"DAN\s+mode", re.IGNORECASE),
]


@dataclass
class MaskResult:
    """Result of a PII masking operation."""

    text: str
    found_types: list[str]


class PromptInjectionError(ValueError):
    """Raised when a prompt injection attempt is detected."""


class Guardrails:
    """Applies PII masking and prompt injection detection to agent inputs/outputs.

    All public methods normalise their input through the adversarial
    pre-processing pipeline (zero-width strip → NFKC → URL-decode → ``@``
    compaction) before applying the PII patterns.  The returned text is
    therefore always normalised, which is safe and desirable for a security
    control plane.
    """

    def mask_pii(self, text: str) -> MaskResult:
        """Replace PII patterns with redaction tokens and return the cleaned text.

        The input is normalised before scanning (see :func:`_normalize`), so
        the returned :class:`MaskResult` contains the normalised, masked version
        of *text*.  All adversarial variants (fullwidth chars, URL-encoding,
        zero-width insertions, whitespace obfuscation) are caught.

        Args:
            text: Raw input text (may contain adversarial PII variants).

        Returns:
            :class:`MaskResult` with ``text`` being the normalised+masked
            output and ``found_types`` listing all PII class labels discovered.
        """
        text = _normalize(text)
        found: list[str] = []
        for label, pattern in _PII_PATTERNS:
            if pattern.search(text):
                found.append(label)
                text = pattern.sub(f"[REDACTED-{label.upper()}]", text)
        return MaskResult(text=text, found_types=found)

    def scrub(self, text: str) -> MaskResult:
        """Apply PII masking to *text*; used explicitly on the OPA mask-instruction path.

        This method is semantically identical to :meth:`mask_pii` but provides
        a named entry point that tests and the orchestrator can mock / track
        independently from the Stage 1 pre-sanitise call.

        Args:
            text: Text to scrub (normalised before scanning).

        Returns:
            :class:`MaskResult` with masked text and discovered PII types.
        """
        return self.mask_pii(text)

    def check_prompt_injection(self, text: str) -> None:
        """Raise PromptInjectionError if an injection attempt is detected."""
        for pattern in _INJECTION_PATTERNS:
            if pattern.search(text):
                raise PromptInjectionError(
                    f"Potential prompt injection detected: matched pattern '{pattern.pattern}'"
                )

    def sanitize(self, text: str) -> str:
        """Apply both PII masking and injection detection; return sanitized text."""
        self.check_prompt_injection(text)
        result = self.mask_pii(text)
        return result.text


class AgentInput(BaseModel):
    """Validated and sanitized input to be passed to an agent."""

    raw_prompt: str
    sanitized_prompt: str
    pii_found: list[str]


def build_agent_input(raw_prompt: str) -> AgentInput:
    """Validate, sanitize, and wrap a raw prompt into a typed AgentInput."""
    guard = Guardrails()
    guard.check_prompt_injection(raw_prompt)
    result = guard.mask_pii(raw_prompt)
    return AgentInput(
        raw_prompt=raw_prompt,
        sanitized_prompt=result.text,
        pii_found=result.found_types,
    )
