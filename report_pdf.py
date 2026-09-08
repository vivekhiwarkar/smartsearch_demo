"""PDF report for an AML screening.

Reproduces the section order of the reference World-Check report:

    1 Report metadata   2 Subject details   3 Screening parameters   4 Match summary
    5 Match details (per match + reviewer resolution)   6 Final decision
    Appendix A Audit trail

Where the reference carried LSEG identifiers, SmartSearch equivalents are substituted.
Fields SmartSearch never populates print as "Not provided by SmartSearch" rather than a
bare dash, so the document never implies a value was checked and found empty.

ReportLab is used rather than an HTML engine so there are no system dependencies.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    KeepTogether,
    LongTable,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    TableStyle,
)

from screening_store import Screening, MatchRecord
from smartsearch_client import (
    CORROBORATION_GROUPS,
    IDENTITY_FLAGS,
    KIND_INTERNATIONAL,
    identity_flag_label,
    join_values,
    provider_from_ref,
    sections_to_map,
)

DASH = "—"
NOT_PROVIDED = "Not provided by SmartSearch"

ROUTE_LABELS = {
    "uk-individual": "UK Individual AML "
                     "(POST /v3/ukindividual/searches) "
                     "- watchlist screening and identity verification",
    "international-individual": "International Individual AML "
                               "(POST /v3/internationalindividual/searches) "
                               "- watchlist screening only",
}

INK = colors.HexColor("#111418")
MUTED = colors.HexColor("#6b7280")
RULE = colors.HexColor("#d8dce2")
BAND = colors.HexColor("#f4f5f7")

_ss = getSampleStyleSheet()
TITLE = ParagraphStyle("t", parent=_ss["Title"], fontName="Helvetica-Bold",
                       fontSize=20, leading=24, textColor=INK, alignment=TA_LEFT)
SUBTITLE = ParagraphStyle("st", parent=_ss["Normal"], fontSize=10, leading=14,
                          textColor=MUTED, spaceAfter=14)
H1 = ParagraphStyle("h1", parent=_ss["Heading1"], fontName="Helvetica-Bold",
                    fontSize=13, leading=16, textColor=INK, spaceBefore=16, spaceAfter=8)
H2 = ParagraphStyle("h2", parent=_ss["Heading2"], fontName="Helvetica-Bold",
                    fontSize=10.5, leading=13, textColor=INK, spaceBefore=10, spaceAfter=5)
BODY = ParagraphStyle("b", parent=_ss["Normal"], fontSize=8.5, leading=12, textColor=INK)
CELL = ParagraphStyle("c", parent=BODY, fontSize=8, leading=11)
LABEL = ParagraphStyle("l", parent=CELL, textColor=MUTED)
NOTE = ParagraphStyle("n", parent=BODY, fontSize=8, leading=11, textColor=MUTED)


def _txt(value) -> str:
    """Escape for Paragraph and normalise empties to an em dash."""
    if value is None or value == "" or value == []:
        return DASH
    if isinstance(value, (list, tuple)):
        value = ", ".join(str(v) for v in value if str(v).strip()) or DASH
    return (str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def kv_table(rows: list[tuple[str, object]], label_width: float = 52 * mm) -> LongTable:
    """The label/value table that makes up most of the document."""
    data = [[Paragraph(_txt(k), LABEL), Paragraph(_txt(v), CELL)] for k, v in rows]
    table = LongTable(data, colWidths=[label_width, None], repeatRows=0, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, RULE),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (0, -1), 0),
    ]))
    return table


def grid_table(header: list[str], rows: list[list[object]]) -> LongTable:
    data = [[Paragraph(f"<b>{_txt(h)}</b>", CELL) for h in header]]
    data += [[Paragraph(_txt(c), CELL) for c in row] for row in rows]
    table = LongTable(data, repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (-1, 0), BAND),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, RULE),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return table


# --------------------------------------------------------------------------- extraction


def match_display_name(record: MatchRecord) -> str:
    """Primary name from the profile snapshot, in first-middle-last order.

    The match list itself carries no name at all (match_name is null), so without a
    snapshot the best available identifier is the provider reference.
    """
    if not record.profile:
        return record.ref or record.match_id
    for node in sections_to_map(record.profile.get("sections")).get("Name Aliases") or []:
        if isinstance(node, dict) and node.get("label") == "Primary Name":
            parts = {
                child.get("label"): join_values(child.get("data"))
                for child in node.get("data") or []
                if isinstance(child, dict)
            }
            joined = " ".join(
                p for p in (parts.get("FirstName"), parts.get("MiddleName"),
                            parts.get("Surname")) if p
            )
            if joined:
                return joined
    return record.ref or record.match_id


def profile_field(record: MatchRecord, label: str) -> str | None:
    if not record.profile:
        return None
    return join_values(sections_to_map(record.profile.get("sections")).get(label))


def summary_field(record: MatchRecord, label: str) -> str | None:
    return join_values(sections_to_map(record.summary).get(label))


def sanction_lists(record: MatchRecord) -> list[str]:
    """Sanctions list names. The API's own Sources section is always empty, but the
    Sanctions section carries the list name for sanctioned entities."""
    if not record.profile:
        return []
    out = []
    for node in sections_to_map(record.profile.get("sections")).get("Sanctions") or []:
        if isinstance(node, dict) and node.get("label"):
            out.append(node["label"])
    return out


def fmt_ts(value: str | None) -> str:
    if not value:
        return DASH
    try:
        return datetime.fromisoformat(value).strftime("%d %b %Y %H:%M:%S UTC")
    except ValueError:
        return value


# --------------------------------------------------------------------------- sections


def _page_furniture(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(MUTED)
    canvas.drawString(18 * mm, 12 * mm, doc.report_footer)
    canvas.drawRightString(A4[0] - 18 * mm, 12 * mm, f"Page {canvas.getPageNumber()}")
    canvas.restoreState()


def _metadata(s: Screening) -> list:
    provider = provider_from_ref(next((m.ref for m in s.matches if m.ref), None))
    provider_label = f"SmartSearch ({provider} watchlist)" if provider else "SmartSearch"
    return [
        Paragraph("1  Report metadata", H1),
        kv_table([
            ("Screening ID", s.screening_id),
            ("Screening status", s.status),
            ("Screening revision", s.revision),
            ("Report version", f"v{s.report_version + 1}"),
            ("Provider", provider_label),
            ("Search ID", s.search.get("search_id")),
            ("Subject ID", s.search.get("subject_id")),
            ("Group ID", s.search.get("group_id")),
            ("Created at", fmt_ts(s.created_at)),
            ("Created by", s.created_by),
            ("Updated at", fmt_ts(s.updated_at)),
            ("Updated by", s.updated_by),
        ]),
    ]


def _subject(s: Screening) -> list:
    subj = s.subject
    address = ", ".join(
        str(subj.get(k)) for k in ("flat", "building", "street_1", "town", "region",
                                   "postcode", "country") if subj.get(k)
    )
    return [
        Paragraph("2  Subject details", H1),
        kv_table([
            ("Subject type", "INDIVIDUAL"),
            ("Full name", subj.get("display_name")),
            ("Date of birth", subj.get("dob")),
            ("Address", address),
            ("Country", subj.get("country")),
            ("Client reference", subj.get("client_reference")),
        ]),
    ]


def _parameters(s: Screening) -> list:
    return [
        Paragraph("3  Screening parameters", H1),
        kv_table([
            ("Search route", ROUTE_LABELS.get(s.search.get("route"),
                                              s.search.get("route") or DASH)),
            ("Provider types", ", ".join(s.search.get("provider_types") or [])),
            ("Search status", s.search.get("status")),
            ("Search created at", fmt_ts(s.search.get("created_at"))),
            ("Environment", "Sandbox (demo data)"),
        ]),
    ]


GRADE_LABELS = {"full": "Full match", "partial": "Partial match"}


def _corroboration(identity: dict) -> list:
    """International Individual AML: per-field corroboration rather than a CRA score."""
    out = [Paragraph(
        "This route grades each supplied detail against independent sources. It returns "
        "<b>refer</b> as its normal outcome, so the source count and the field grades carry "
        "the meaning, not the outcome. This route provides no deceased check and no fraud "
        "alert.", NOTE)]
    for check in identity.get("checks") or []:
        sources = check.get("number_of_sources") or 0
        grades = check.get("field_grades") or {}
        out += [
            Spacer(1, 6),
            kv_table([
                ("Corroborating sources", sources),
                ("Source limit reached", "Yes" if check.get("source_limit_reached") else "No"),
                ("Fields graded", len(grades)),
                ("Detail outcome", str(check.get("outcome") or "").upper()),
            ]),
            Spacer(1, 6),
            Paragraph("Field corroboration", H2),
            grid_table(["Group", "Field", "Result"],
                       [[group, name.replace("_", " ").capitalize(),
                         GRADE_LABELS.get(grades.get(name),
                                          "Not matched" if sources else DASH)]
                        for group, fields in CORROBORATION_GROUPS for name in fields]),
        ]
        national = check.get("national_ids") or []
        if national:
            out += [
                Spacer(1, 6),
                Paragraph("National IDs", H2),
                grid_table(["Type", "Result"],
                           [[str(n.get("type") or "").upper(),
                             GRADE_LABELS.get(n.get("result"), "Not matched")]
                            for n in national]),
            ]
        granular = check.get("granular_data") or []
        if granular:
            out += [
                Spacer(1, 6),
                Paragraph("Sources found", H2),
                grid_table(["Category", "Source type", "Sources", "Items"],
                           [[g.get("category"), g.get("text"), g.get("source"), g.get("item")]
                            for g in granular]),
            ]
        out.append(Spacer(1, 8))
    return out


def _identity(s: Screening) -> list:
    """Section 4. Identity verification, or a plain statement that none was performed."""
    out = [Paragraph("4  Identity verification", H1)]
    identity = s.identity
    if not identity:
        out.append(Paragraph(
            "No identity result was returned for this screening.", BODY))
        return out

    alerts = identity.get("alerts") or []
    kind = identity.get("kind", "uk-cra")
    out.append(kv_table([
        ("Overall outcome", (identity.get("outcome") or "").upper()),
        ("Route", identity.get("route")),
        ("Check type", "Credit reference agency authentication"
                       if kind != KIND_INTERNATIONAL
                       else "International corroboration (per-field matching)"),
        ("Checked at", fmt_ts(identity.get("checked_at"))),
        ("Reviewer attention required", "Yes" if identity.get("needs_attention") else "No"),
        ("Acknowledged by reviewer",
         "Yes" if s.identity_acknowledged else ("No" if alerts else "Not required")),
    ]))

    if alerts:
        out += [
            Spacer(1, 6),
            Paragraph("Alerts raised", H2),
            Paragraph(
                "The provider returned an overall outcome of "
                f"<b>{_txt((identity.get('outcome') or '').upper())}</b>, but the following "
                "were raised against this subject and were reviewed before any decision:",
                NOTE),
            grid_table(["Alert"], [[a] for a in alerts]),
        ]

    if kind == KIND_INTERNATIONAL:
        out += _corroboration(identity)
        return out

    for check in identity.get("checks") or []:
        out.append(Paragraph(
            f"{str(check.get('cra') or 'CRA').title()}  {DASH}  "
            f"{str(check.get('outcome') or '').upper()}", H2))

        rows = [(label, identity_flag_label(key, check.get(key))[0])
                for key, label, _, _ in IDENTITY_FLAGS]
        rows += [
            ("Authentication index", check.get("authentication_index")),
            ("Primary checks / sources",
             f"{check.get('primary_check_count')} / {check.get('primary_source_count')}"),
            ("Collaborative checks / sources",
             f"{check.get('collaborative_check_count')} / "
             f"{check.get('collaborative_source_count')}"),
            ("Date of birth matches", check.get("primary_data_date_of_birth_match")),
            ("Oldest primary data", check.get("primary_data_oldest_date")),
            ("Bank account match",
             check.get("bank_account_match")
             if check.get("bank_account_match") is not None
             else "Not available: bank checks are not enabled on this contract"),
        ]
        out.append(kv_table(rows))

        granular = check.get("granular_data") or []
        if granular:
            out += [
                Spacer(1, 6),
                Paragraph("Evidence found", H2),
                grid_table(["Category", "Check", "Sources", "Items", "Oldest"],
                           [[g.get("category"), g.get("text"), g.get("source"),
                             g.get("item"), g.get("oldest_date")] for g in granular]),
            ]

        documents = {k: v for k, v in (check.get("documents") or {}).items()
                     if v is not None}
        if documents:
            doc_rows = []
            for name, verified in documents.items():
                errors = "; ".join(
                    f"{e.get('code')}: {e.get('message')}"
                    for e in (check.get("documents_errors") or {}).get(name) or [])
                doc_rows.append([name.replace("_", " ").title(),
                                 "Verified" if verified else "Did not verify",
                                 errors or DASH])
            out += [
                Spacer(1, 6),
                Paragraph("Identity documents", H2),
                grid_table(["Document", "Result", "Detail"], doc_rows),
            ]
        out.append(Spacer(1, 8))
    return out


def _summary(s: Screening) -> list:
    out = [
        Paragraph("5  Match summary", H1),
        Paragraph(f"Total matches returned: <b>{len(s.matches)}</b>", BODY),
        Spacer(1, 6),
        Paragraph("By category", H2),
        grid_table(["Category", "Count"],
                   [[k, v] for k, v in s.category_counts.items()] or [[DASH, 0]]),
        Spacer(1, 8),
        Paragraph("By resolution", H2),
        grid_table(["Resolution", "Count"],
                   [[k, v] for k, v in s.resolution_counts.items()] or [[DASH, 0]]),
    ]
    synced = sum(1 for m in s.matches if m.resolution.synced is True)
    failed = [m for m in s.matches if m.resolution.synced is False]
    out += [
        Spacer(1, 8),
        Paragraph(
            f"{synced} of {len(s.matches)} classifications were synchronised to SmartSearch. "
            "Only POSITIVE and FALSE have an API equivalent (is_true_match); POSSIBLE and "
            "UNSPECIFIED are recorded locally only."
            + (f" {len(failed)} synchronisation(s) failed." if failed else ""),
            NOTE),
    ]
    return out


def _match_block(index: int, total: int, record: MatchRecord) -> list:
    res = record.resolution
    if res.synced is True:
        sync = "Yes"
    elif res.synced is False:
        sync = "No"
    else:
        sync = "Not applicable for this status"

    detail = [
        ("Primary name", match_display_name(record)),
        ("Matched term", record.meta.get("match_name") or NOT_PROVIDED),
        ("Match strength", record.meta.get("match_strength") or NOT_PROVIDED),
        ("Match score", NOT_PROVIDED),  # no score field exists anywhere in the API
        ("Categories", record.categories),
        ("Entry type", summary_field(record, "Type")),
        ("Dates of birth", summary_field(record, "Date of Birth")),
        ("Nationality (citizenship)", summary_field(record, "Citizenship")),
        ("Residence", summary_field(record, "Residency")),
        ("Gender", profile_field(record, "Gender") or "Profile not snapshotted"),
        ("Birth place", profile_field(record, "Birth Place")),
        ("Sanctions lists", sanction_lists(record) or NOT_PROVIDED),
        ("Sources", NOT_PROVIDED),
        ("Active status", summary_field(record, "Active Status")),
        ("Provider type", "WATCHLIST"),
        ("Result ID", record.match_id),
        ("Reference ID", record.ref),
        ("Entry updated at", fmt_ts(record.meta.get("entry_updated_at"))),
        ("Matched at", fmt_ts(record.meta.get("matched_at"))),
    ]
    resolution = [
        ("Classification", res.status),
        ("Risk level", res.risk),
        ("Reason", res.reason or DASH),
        ("Reviewer comment", res.comment or DASH),
        ("Resolved at", fmt_ts(res.resolved_at)),
        ("Resolved by", res.resolved_by),
        ("Resolution source", res.source),
        ("Synchronised to SmartSearch", sync),
        ("Synchronisation error", res.sync_error),
    ]
    header = Paragraph(
        f"Match {index} of {total}  {DASH}  {_txt(res.status if res.is_resolved else 'UNRESOLVED')}",
        H2)
    return [KeepTogether([header, kv_table(detail)]),
            Paragraph("Reviewer resolution", H2), kv_table(resolution), Spacer(1, 10)]


def _matches(s: Screening) -> list:
    out = [Paragraph("6  Match details", H1)]
    if not s.matches:
        out.append(Paragraph("No watchlist matches were returned for this subject.", BODY))
        return out
    for i, record in enumerate(s.matches, start=1):
        out += _match_block(i, len(s.matches), record)
    return out


def _decision(s: Screening) -> list:
    out = [Paragraph("7  Final decision", H1)]
    if not s.decision:
        out.append(Paragraph("No final decision has been recorded for this screening.", BODY))
        return out
    rows = [
        ("Overall AML outcome", s.decision.get("outcome")),
        ("Reviewer", s.decision.get("reviewer")),
        ("Decision timestamp", fmt_ts(s.decision.get("decided_at"))),
        ("Notes", s.decision.get("notes")),
    ]
    if s.identity:
        rows += [
            ("Identity outcome at decision", (s.decision.get("identity_outcome") or "").upper()),
            ("Identity alerts acknowledged",
             "Yes" if s.decision.get("identity_acknowledged")
             else ("No alerts raised" if not s.identity_alerts else "No")),
        ]
    out.append(kv_table(rows))
    return out


def _audit(s: Screening) -> list:
    out = [PageBreak(), Paragraph("Appendix A  Audit trail", H1)]
    skip = {"event_id", "type", "occurred_at", "actor", "revision"}
    for i, event in enumerate(s.audit, start=1):
        rows = [
            ("Event ID", event.get("event_id")),
            ("Event type", event.get("type")),
            ("Occurred at", fmt_ts(event.get("occurred_at"))),
            ("Actor", event.get("actor")),
            ("Revision", event.get("revision")),
        ]
        rows += [(k.replace("_", " ").capitalize(), v)
                 for k, v in event.items() if k not in skip]
        out.append(KeepTogether([Paragraph(f"Audit event {i}", H2), kv_table(rows)]))
        out.append(Spacer(1, 6))
    return out


# --------------------------------------------------------------------------- entry point


def build_report(screening: Screening, path: Path | str) -> Path:
    """Render the screening to a PDF. Returns the path written."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    doc = SimpleDocTemplate(
        str(path), pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=16 * mm, bottomMargin=20 * mm,
        title="AML Screening Report",
        author=screening.created_by,
        subject=f"AML screening {screening.screening_id}",
    )
    doc.report_footer = (
        f"AML Screening Report  {DASH}  {screening.screening_id}  {DASH}  "
        f"SmartSearch sandbox"
    )

    story = [
        Paragraph("AML Screening Report", TITLE),
        Paragraph(
            f"SmartSearch {DASH} screening outcome and resolution record for "
            f"{_txt(screening.subject.get('display_name'))}",
            SUBTITLE),
    ]
    story += _metadata(screening)
    story += _subject(screening)
    story += _parameters(screening)
    story += _identity(screening)
    story += _summary(screening)
    story += _matches(screening)
    story += _decision(screening)
    story += _audit(screening)

    doc.build(story, onFirstPage=_page_furniture, onLaterPages=_page_furniture)
    return path
