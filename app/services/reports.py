"""Human-readable exports of one client's record — CSV and PDF.

`GET /admin/clients/{id}/export` (in `app.api.v1.endpoints.admin.clients`)
originally returned this data as a single JSON blob. That is the right shape
for a data-portability request under GDPR/CCPA — a full machine-readable copy
— but it is the wrong shape for what a coach actually clicks the download
button for day to day: a record they can open, skim, and hand to someone
without a JSON viewer. This module builds the two formats a coach expects
instead — CSV for spreadsheet work, PDF for a record that reads like a
document — from the exact same `ClientDetail` the JSON export already
assembles, so nothing here can drift out of sync with what the dashboard
displays.

Both functions are synchronous and CPU-only (string building, `reportlab`
drawing calls) — no I/O, so no need to run them off the event loop for the
record sizes a single client ever produces.
"""

import csv
import io
from datetime import UTC, datetime

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.schemas.admin import ClientDetail

BRAND_RED = colors.HexColor("#DC2626")
INK = colors.HexColor("#111827")
MUTED = colors.HexColor("#6B7280")
LINE = colors.HexColor("#E5E7EB")


def _display_name(detail: ClientDetail) -> str:
    return detail.account.display_name or detail.account.full_name


def build_filename_stem(detail: ClientDetail) -> str:
    """A safe, readable base filename — no path separators, no whitespace
    that would need quoting in a `Content-Disposition` header."""
    stem = _display_name(detail).strip().lower().replace(" ", "-")
    safe = "".join(ch for ch in stem if ch.isalnum() or ch == "-") or "client"
    return f"{safe}-record"


# --- CSV -----------------------------------------------------------------------
#
# One CSV, several sections. A client record is inherently several small
# tables (measurements, sleep, cardio...) rather than one big one, and most
# spreadsheet tools cope fine with blank-line-separated blocks in a single
# file — that beats asking a coach to manage a zip of six tiny CSVs for one
# client.


def build_client_csv(detail: ClientDetail) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)

    writer.writerow(["Client record export"])
    writer.writerow(["Name", _display_name(detail)])
    writer.writerow(["Email", detail.account.email])
    writer.writerow(["Level", detail.account.level.value if detail.account.level else "—"])
    writer.writerow(["Goal", detail.account.goal.value if detail.account.goal else "—"])
    writer.writerow(["Member since", detail.account.created_at.date().isoformat()])
    writer.writerow(["Exported", datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")])
    writer.writerow([])

    writer.writerow(["Weight log"])
    writer.writerow(["Date", "Weight (kg)"])
    for point in detail.weight_series:
        writer.writerow([point.log_date.isoformat(), point.value])
    writer.writerow([])

    writer.writerow(["Body measurements"])
    writer.writerow(
        [
            "Date",
            "Chest (cm)",
            "Waist (cm)",
            "Hips (cm)",
            "Left arm (cm)",
            "Right arm (cm)",
            "Left thigh (cm)",
            "Right thigh (cm)",
            "Neck (cm)",
            "Body fat (%)",
            "Note",
        ]
    )
    for row in detail.measurements:
        writer.writerow(
            [
                row.log_date.isoformat(),
                row.chest_cm,
                row.waist_cm,
                row.hips_cm,
                row.left_arm_cm,
                row.right_arm_cm,
                row.left_thigh_cm,
                row.right_thigh_cm,
                row.neck_cm,
                row.body_fat_pct,
                row.note or "",
            ]
        )
    writer.writerow([])

    writer.writerow(["Sleep"])
    writer.writerow(["Date", "Hours slept", "Quality (1-5)", "Bedtime", "Wake time"])
    for row in detail.sleep:
        writer.writerow(
            [
                row.log_date.isoformat(),
                row.hours_slept,
                row.quality if row.quality is not None else "",
                row.bedtime.isoformat() if row.bedtime else "",
                row.wake_time.isoformat() if row.wake_time else "",
            ]
        )
    writer.writerow([])

    writer.writerow(["Cardio"])
    writer.writerow(
        ["Date", "Activity", "Duration (min)", "Distance (km)", "Avg HR", "Calories", "Intensity"]
    )
    for row in detail.cardio:
        writer.writerow(
            [
                row.log_date.isoformat(),
                row.activity_type,
                row.duration_minutes,
                row.distance_km if row.distance_km is not None else "",
                row.avg_heart_rate if row.avg_heart_rate is not None else "",
                row.calories_burned if row.calories_burned is not None else "",
                row.intensity,
            ]
        )
    writer.writerow([])

    writer.writerow(["Training sessions"])
    writer.writerow(["Date", "Status", "Day", "Focus", "Duration (min)", "Sets", "Volume (kg)"])
    for row in detail.sessions:
        writer.writerow(
            [
                row.session_date.isoformat(),
                row.status,
                row.day_label or "",
                row.focus or "",
                row.duration_minutes if row.duration_minutes is not None else "",
                row.set_count,
                row.volume_kg,
            ]
        )
    writer.writerow([])

    writer.writerow(["Progress photos"])
    writer.writerow(["Date", "Pose", "Note", "URL"])
    for row in detail.photos:
        writer.writerow([row.log_date.isoformat(), row.pose, row.note or "", row.url])

    # Excel opens a UTF-8 CSV with no BOM as if every accented character were
    # mojibake. The BOM is what makes it detect the encoding correctly instead
    # of guessing the system codepage.
    return b"\xef\xbb\xbf" + buffer.getvalue().encode("utf-8")


# --- PDF -------------------------------------------------------------------------
#
# One flowing document rather than a fixed-position layout: client records
# vary wildly in length (a brand-new signup has almost nothing logged; a
# two-year client has hundreds of rows), and `platypus`'s flowables paginate
# automatically instead of needing every table's height calculated by hand.


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "ReportTitle", parent=base["Title"], textColor=INK, fontSize=20, spaceAfter=4
        ),
        "subtitle": ParagraphStyle(
            "ReportSubtitle", parent=base["Normal"], textColor=MUTED, fontSize=10, spaceAfter=16
        ),
        "section": ParagraphStyle(
            "ReportSection",
            parent=base["Heading2"],
            textColor=BRAND_RED,
            fontSize=13,
            spaceBefore=18,
            spaceAfter=8,
        ),
        "meta": ParagraphStyle("ReportMeta", parent=base["Normal"], textColor=INK, fontSize=10),
        "empty": ParagraphStyle(
            "ReportEmpty", parent=base["Normal"], textColor=MUTED, fontSize=9, spaceAfter=4
        ),
    }


def _table(rows: list[list[str]], *, col_widths: list[float] | None = None) -> Table:
    table = Table(rows, colWidths=col_widths, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F9FAFB")),
                ("TEXTCOLOR", (0, 0), (-1, 0), INK),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("LINEBELOW", (0, 0), (-1, 0), 0.75, LINE),
                ("LINEBELOW", (0, 1), (-1, -1), 0.5, LINE),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    return table


def _section(story: list, styles: dict, title: str, header: list[str], rows: list[list]) -> None:
    story.append(Paragraph(title, styles["section"]))
    if not rows:
        story.append(Paragraph("Nothing logged in this range.", styles["empty"]))
        return
    story.append(_table([header, *[[str(cell) for cell in row] for row in rows]]))


def build_client_pdf(detail: ClientDetail) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=LETTER,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        title=f"{_display_name(detail)} — client record",
    )
    styles = _styles()
    story: list = []

    story.append(Paragraph(_display_name(detail), styles["title"]))
    story.append(
        Paragraph(
            f"{detail.account.email} &nbsp;·&nbsp; "
            f"Level {detail.account.level.value.replace('level_', '')} "
            if detail.account.level
            else f"{detail.account.email} &nbsp;·&nbsp; No plan yet",
            styles["subtitle"],
        )
    )
    story.append(
        Paragraph(
            f"Member since {detail.account.created_at.strftime('%B %-d, %Y')} &nbsp;·&nbsp; "
            f"Exported {datetime.now(UTC).strftime('%B %-d, %Y')}",
            styles["meta"],
        )
    )

    _section(
        story,
        styles,
        "Weight log",
        ["Date", "Weight (kg)"],
        [[p.log_date.isoformat(), f"{p.value:.1f}"] for p in detail.weight_series],
    )

    _section(
        story,
        styles,
        "Body measurements",
        ["Date", "Chest", "Waist", "Hips", "L Arm", "R Arm", "L Thigh", "R Thigh", "Body fat %"],
        [
            [
                m.log_date.isoformat(),
                m.chest_cm or "—",
                m.waist_cm or "—",
                m.hips_cm or "—",
                m.left_arm_cm or "—",
                m.right_arm_cm or "—",
                m.left_thigh_cm or "—",
                m.right_thigh_cm or "—",
                m.body_fat_pct or "—",
            ]
            for m in detail.measurements
        ],
    )

    _section(
        story,
        styles,
        "Sleep",
        ["Date", "Hours", "Quality", "Bedtime", "Wake"],
        [
            [
                s.log_date.isoformat(),
                s.hours_slept,
                s.quality or "—",
                s.bedtime.strftime("%H:%M") if s.bedtime else "—",
                s.wake_time.strftime("%H:%M") if s.wake_time else "—",
            ]
            for s in detail.sleep
        ],
    )

    _section(
        story,
        styles,
        "Cardio",
        ["Date", "Activity", "Minutes", "Distance", "Avg HR", "Calories", "Intensity"],
        [
            [
                c.log_date.isoformat(),
                c.activity_type,
                c.duration_minutes,
                c.distance_km or "—",
                c.avg_heart_rate or "—",
                c.calories_burned or "—",
                c.intensity,
            ]
            for c in detail.cardio
        ],
    )

    story.append(PageBreak())

    _section(
        story,
        styles,
        "Training sessions",
        ["Date", "Status", "Day", "Focus", "Minutes", "Sets", "Volume (kg)"],
        [
            [
                s.session_date.isoformat(),
                s.status,
                s.day_label or "—",
                s.focus or "—",
                s.duration_minutes or "—",
                s.set_count,
                f"{s.volume_kg:.0f}",
            ]
            for s in detail.sessions
        ],
    )

    story.append(Spacer(1, 12))
    story.append(Paragraph("Progress photos", styles["section"]))
    if detail.photos:
        story.append(
            _table(
                [
                    ["Date", "Pose", "Note"],
                    *[[p.log_date.isoformat(), p.pose, p.note or "—"] for p in detail.photos],
                ]
            )
        )
        story.append(
            Paragraph(
                "Photo files are private and are not embedded in this PDF — view them in "
                "the dashboard.",
                styles["empty"],
            )
        )
    else:
        story.append(Paragraph("No photos logged in this range.", styles["empty"]))

    doc.build(story)
    return buffer.getvalue()