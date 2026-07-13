"""Color classification for Supplier Stock Checker runtime log blocks."""

from __future__ import annotations

from enum import IntEnum


class LogSeverity(IntEnum):
    NORMAL = 0
    CHANGE = 1
    ERROR = 2


ERROR_MARKERS = (
    "unable to verify",
    "(conflict)",
    "comparison: not compared",
    "notice: no sheet change",
    "verification error",
    "captcha",
    "traceback",
    "exception:",
    "error:",
    "navigation failed",
    "scrape error",
    "blocked page",
    "failed to",
)

CHANGE_MARKERS = (
    "notice: change detected",
    "comparison: different",
    "verified update(s) waiting",
    "final verified updates",
)


def classify_log_text(text: object) -> LogSeverity:
    """Classify one log message or a complete row block."""
    normalized = str(text or "").casefold()

    if any(marker in normalized for marker in ERROR_MARKERS):
        return LogSeverity.ERROR
    if any(marker in normalized for marker in CHANGE_MARKERS):
        return LogSeverity.CHANGE
    return LogSeverity.NORMAL


def merge_severity(
    current: LogSeverity,
    incoming: LogSeverity,
) -> LogSeverity:
    return LogSeverity(max(int(current), int(incoming)))


def severity_tag(severity: LogSeverity) -> str:
    if severity == LogSeverity.ERROR:
        return "ssc_error"
    if severity == LogSeverity.CHANGE:
        return "ssc_change"
    return "ssc_normal"
