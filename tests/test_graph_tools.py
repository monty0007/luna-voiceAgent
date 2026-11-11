"""Offline tests for the Graph calendar tool (no network, no sign-in)."""

import json
import os
import sys
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, "src"))

import graph_tools  # noqa: E402
import local_tools  # noqa: E402


def event(subject, start, end, **kw):
    return {"subject": subject, "isCancelled": False, "isAllDay": False,
            "start": {"dateTime": start + ".0000000"}, "end": {"dateTime": end + ".0000000"}, **kw}


class PeriodTests(unittest.TestCase):
    def test_week_starts_monday(self):
        wed = datetime(2026, 11, 11, 15, 30)  # a Wednesday
        start, end = graph_tools.period_bounds("this_week", wed)
        self.assertEqual((start.weekday(), start.hour, (end - start).days), (0, 0, 7))
        self.assertEqual(start.day, 9)
        nstart, _ = graph_tools.period_bounds("next_week", wed)
        self.assertEqual(nstart.day, 16)

    def test_bad_period(self):
        with self.assertRaises(ValueError):
            graph_tools.period_bounds("last_year")


class SummaryTests(unittest.TestCase):
    def test_counts_exclude_cancelled_and_all_day(self):
        start, end = graph_tools.period_bounds("this_week", datetime(2026, 11, 11))
        events = [
            event("Standup", "2026-11-10T09:00:00", "2026-11-10T09:15:00"),
            event("Client call", "2026-11-11T13:00:00", "2026-11-11T14:00:00"),
            event("Cancelled", "2026-11-11T15:00:00", "2026-11-11T16:00:00", isCancelled=True),
            event("Holiday", "2026-11-12T00:00:00", "2026-11-13T00:00:00", isAllDay=True),
        ]
        out = graph_tools.summarize(events, start, end, "this_week")
        self.assertEqual(out["meeting_count"], 2)
        self.assertEqual(out["all_day_events"], 1)
        self.assertEqual(sum(out["meetings_per_day"].values()), 2)
        self.assertFalse(out["truncated"])


class GetCalendarTests(unittest.TestCase):
    def test_follows_paging(self):
        pages = [
            {"value": [event("A", "2026-11-10T09:00:00", "2026-11-10T10:00:00")],
             "@odata.nextLink": "https://graph.microsoft.com/next"},
            {"value": [event("B", "2026-11-11T09:00:00", "2026-11-11T10:00:00")]},
        ]
        with mock.patch.object(graph_tools, "_token", return_value="t"), \
                mock.patch.object(graph_tools, "_graph_get", side_effect=pages) as get:
            out = graph_tools.get_calendar("this_week")
        self.assertEqual(out["meeting_count"], 2)
        self.assertEqual(get.call_count, 2)


class ToolRegistrationTests(unittest.TestCase):
    def test_graph_tool_only_when_configured(self):
        with mock.patch.dict(os.environ, {"GRAPH_CLIENT_ID": ""}):
            self.assertNotIn("get_calendar", local_tools.active_tools())
        with mock.patch.dict(os.environ, {"GRAPH_CLIENT_ID": "abc"}):
            self.assertIn("get_calendar", local_tools.active_tools())

    def test_not_signed_in_returns_error_not_crash(self):
        with mock.patch.dict(os.environ, {"GRAPH_CLIENT_ID": "abc",
                                          "GRAPH_AUTH_RECORD": "/nonexistent/rec.json"}):
            out = json.loads(local_tools.run_tool("get_calendar", '{"period": "this_week"}'))
        self.assertIn("graph_login.py", out["error"])


if __name__ == "__main__":
    unittest.main()
