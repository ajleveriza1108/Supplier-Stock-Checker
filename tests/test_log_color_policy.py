from __future__ import annotations

import unittest

from ui.log_color_policy import (
    LogSeverity,
    classify_log_text,
    merge_severity,
    severity_tag,
)


class LogColorPolicyTests(unittest.TestCase):
    def test_no_change_is_white(self):
        text = """Row: 38
Comparison: Same
NOTICE: NO CHANGE"""
        self.assertEqual(classify_log_text(text), LogSeverity.NORMAL)
        self.assertEqual(severity_tag(LogSeverity.NORMAL), "ssc_normal")

    def test_stock_change_is_orange(self):
        text = """Row: 416
Comparison: Different — link is treated as OOS
NOTICE: CHANGE DETECTED — Stock In Stock → OOS — HELD FOR REVIEW"""
        self.assertEqual(classify_log_text(text), LogSeverity.CHANGE)
        self.assertEqual(severity_tag(LogSeverity.CHANGE), "ssc_change")

    def test_price_only_change_is_orange(self):
        text = """Row: 112
Comparison: Same
NOTICE: CHANGE DETECTED — Price $56.99 → $51.29 — HELD FOR REVIEW"""
        self.assertEqual(classify_log_text(text), LogSeverity.CHANGE)

    def test_conflict_is_red(self):
        text = """Link Status: Unable to Verify (CONFLICT)
Comparison: Not Compared
NOTICE: NO SHEET CHANGE — CONFLICT"""
        self.assertEqual(classify_log_text(text), LogSeverity.ERROR)
        self.assertEqual(severity_tag(LogSeverity.ERROR), "ssc_error")

    def test_error_wins_over_change(self):
        severity = merge_severity(
            LogSeverity.CHANGE,
            LogSeverity.ERROR,
        )
        self.assertEqual(severity, LogSeverity.ERROR)


if __name__ == "__main__":
    unittest.main()
