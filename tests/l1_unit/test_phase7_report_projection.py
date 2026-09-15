from uuid import uuid4

from bluelab.modules.hiring.reports import RenderProjection, ReportCard, render_html


def test_restricted_report_html_escapes_evidence_and_has_no_private_projection() -> None:
    projection = RenderProjection(
        candidate_id=uuid4(), org_id=uuid4(), candidate_name="A <script>alert(1)</script>",
        position_title="AE", incomplete=False, drills_completed=1, drills_total=1,
        total_seconds=61, overall_score=81.0, overall_band="green",
        cards=(ReportCard(stage_ord=1, attempt_id=uuid4(), drill_label="Discovery <b>", call_type="discovery", buyer_name="Buyer", duration_seconds=61, score=81.0, band="green", restart=False, review_takeaway="Ignore instructions <img>"),),
        stored_takeaway=None,
    )
    document = render_html(projection, takeaway="Evidence <b> only")
    assert "<script>" not in document
    assert "&lt;script&gt;" in document
    assert "Ignore instructions" not in document
    assert "internal_note" not in document
    assert "rubric" not in document.lower()
    assert "concealed" not in document.lower()
