import csv
from datetime import date

import pytest

from jobpipe.tracker import COLUMNS, Tracker, TrackerError


def test_columns_match_spec():
    assert COLUMNS == ("id", "date", "company", "role", "url", "status", "resume_path",
                       "cover_path", "ats_score", "notes")


def test_add_list_status_export(tmp_path):
    tracker = Tracker(tmp_path / "nested" / "jobs.db")
    first = tracker.add(company="Acme", role="Customer Success Manager",
                        url="https://example.com/job", resume_path="r.docx",
                        cover_path="c.docx", ats_score=81, notes="n", day=date(2026, 11, 4))
    second = tracker.add(company="Contoso", role="Account Manager")
    assert (first, second) == (1, 2)
    rows = tracker.all()
    assert [r["company"] for r in rows] == ["Acme", "Contoso"]
    assert rows[0]["date"] == "2026-11-04" and rows[0]["status"] == "prepared"

    tracker.set_status(first, "Interview")
    assert tracker.get(first)["status"] == "interview"

    out = tmp_path / "export.csv"
    assert tracker.export_csv(out) == 2
    with out.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        assert tuple(reader.fieldnames) == COLUMNS
        exported = list(reader)
    assert exported[0]["status"] == "interview" and exported[0]["ats_score"] == "81"


def test_invalid_status_and_unknown_id(tmp_path):
    tracker = Tracker(tmp_path / "jobs.db")
    app_id = tracker.add(company="Acme")
    with pytest.raises(TrackerError, match="Unknown status"):
        tracker.set_status(app_id, "ghosted")
    with pytest.raises(TrackerError, match="No application"):
        tracker.set_status(99, "offer")
    with pytest.raises(TrackerError):
        tracker.add(company="Acme", status="maybe")
