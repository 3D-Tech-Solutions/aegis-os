"""Guardrails - PII masking and prompt injection filters."""

import re
from dataclasses import dataclass

from pydantic import BaseModel

# ---------------------------------------------------------------------------
# PII patterns
# ---------------------------------------------------------------------------

_PII_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("email", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    (
        "ssn",
        re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b"),
    ),
    (
        "credit_card",
        re.compile(r"\b(?:\d{4}[- ]?){3}\d{4}\b"),
    ),
    (
        "phone_us",
        re.compile(r"\b(?:\+1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"),
    ),
    (
        "ip_address",
        re.compile(
            r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}"
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
    """Applies PII masking and prompt injection detection to agent inputs/outputs."""

    def mask_pii(self, text: str) -> MaskResult:
        """Replace PII patterns with redaction tokens and return the cleaned text."""
        found: list[str] = []
        for label, pattern in _PII_PATTERNS:
            matches = pattern.findall(text)
            if matches:
                found.append(label)
                text = pattern.sub(f"[REDACTED-{label.upper()}]", text)
        return MaskResult(text=text, found_types=found)

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
