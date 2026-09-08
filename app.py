"""Streamlit AML screening workflow against the SmartSearch sandbox.

Four steps: Subject -> Review -> Resolve -> Report.

Every match must be classified before a final decision can be recorded, and the PDF
report is generated from the stored screening record, not from whatever happens to be
on screen.
"""

from __future__ import annotations

import os
from datetime import datetime

import streamlit as st
from dotenv import load_dotenv

import screening_store as ss
from report_pdf import build_report, match_display_name
from smartsearch_client import (
    SANDBOX_BASE,
    AuthError,
    MatchProfile,
    ServiceError,
    SmartSearchClient,
    SmartSearchError,
    ValidationError,
    CORROBORATION_GROUPS,
    IDENTITY_FLAGS,
    KIND_INTERNATIONAL,
    identity_flag_label,
    is_leaf_list,
    join_values,
    sections_to_map,
)

load_dotenv()

st.set_page_config(page_title="AML Screening", page_icon="🔎", layout="wide")

UNAVAILABLE = "_Not provided by sandbox_"
STEPS = [
    ("Subject", "Screening details"),
    ("Review", "Match analysis"),
    ("Resolve", "Classification"),
    ("Report", "Decision and document"),
]

# The API rejects a blank street_1, town, postcode or date_of_birth, so the form ships
# with a working subject prefilled rather than one that cannot be submitted as-is.
DEFAULTS = {
    "title": "Mr", "first": "Nicholas", "middle": "", "last": "Brown",
    "dob": "1968-06-02", "flat": "", "building": "25", "street_1": "High Street",
    "town": "Westbury", "region": "Wiltshire", "postcode": "BA13 3BN",
    "country": "GBR", "client_reference": "streamlit-demo", "duration": "36",
    "ni_number": "", "dl_number": "",
    "passport_country": "", "passport_expiry": "", "passport_mrz": "",
    "telephone": "", "national_id": "", "national_id_type": "ssn",
}

# Verified live against the sandbox data pack.
PERSONAS = {
    "Nicholas Brown — 9 matches": {
        "title": "Mr", "first": "Nicholas", "last": "Brown", "dob": "1968-06-02",
        "building": "25", "street_1": "High Street", "town": "Westbury",
        "region": "Wiltshire", "postcode": "BA13 3BN", "country": "GBR"},
    "Oleg Deripaska — 1 match, OFAC detail": {
        "title": "Mr", "first": "Oleg", "last": "Deripaska", "dob": "1974-06-10",
        "building": "1", "street_1": "High Street", "town": "London",
        "postcode": "WS13 8AF", "country": "GBR"},
    "Boris Johnson — 2 matches": {
        "title": "Mr", "first": "Boris", "last": "Johnson", "dob": "1964-06-19",
        "building": "10", "street_1": "Downing Street", "town": "London",
        "postcode": "SW1A 2AA", "country": "GBR"},
    "Luis Rodriguez Olivera — USA, international route": {
        "title": "Mr", "first": "Luis", "last": "Olivera", "dob": "1978-05-22",
        "building": "10", "street_1": "Forest Drive", "town": "Mclean", "region": "VA",
        "postcode": "22101", "country": "USA", "telephone": "+13165554994",
        "national_id": "382-23-2391", "national_id_type": "ssn"},
    "Zebulon Quartermaine — no matches": {
        "title": "Mr", "first": "Zebulon", "last": "Quartermaine", "dob": "1991-04-17",
        "building": "1", "street_1": "High Street", "town": "London",
        "postcode": "WS13 8AF", "country": "GBR"},
    "Phoebe Alexander — clean watchlist, flagged identity": {
        "title": "Ms", "first": "Phoebe", "last": "Alexander", "dob": "1996-12-24",
        "building": "8", "street_1": "Lucas Street", "town": "Lyme Regis",
        "postcode": "XG6 9HI", "country": "GBR"},
    "Carl Fisher — fraud alert": {
        "title": "Mr", "first": "Carl", "last": "Fisher", "dob": "1941-09-20",
        "building": "80B", "street_1": "Easy Road", "town": "Leeds",
        "postcode": "LS9 0AB", "country": "GBR"},
}

LONGFORM_SECTIONS = {"Profile Notes", "Descriptions"}

STATUS_COLOURS = {
    "POSITIVE": "#b42318", "POSSIBLE": "#b54708",
    "FALSE": "#067647", "UNSPECIFIED": "#6b7280",
}


# --------------------------------------------------------------------------- state


def init_state() -> None:
    for key, value in DEFAULTS.items():
        st.session_state.setdefault(key, value)
    st.session_state.setdefault("step", 1)
    st.session_state.setdefault("screening", None)
    st.session_state.setdefault("error", None)
    st.session_state.setdefault("queue_filter", "Unresolved")
    st.session_state.setdefault("res_status", "FALSE")
    st.session_state.setdefault("res_risk", "LOW")
    st.session_state.setdefault("res_reason", ss.REASONS[1])
    st.session_state.setdefault("res_comment", "")
    st.session_state.setdefault("decision_outcome", "")
    st.session_state.setdefault("decision_notes", "")
    st.session_state.setdefault("report_path", None)
    st.session_state.setdefault("clear_selection", False)
    st.session_state.setdefault("ack_identity", False)


@st.cache_resource(show_spinner=False)
def get_client(app_id: str, secret: str, base_url: str) -> SmartSearchClient:
    return SmartSearchClient(app_id, secret, base_url=base_url)


def screening() -> ss.Screening | None:
    return st.session_state["screening"]


def apply_persona(name: str) -> None:
    for key in DEFAULTS:
        st.session_state[key] = PERSONAS[name].get(key, "")
    st.session_state["client_reference"] = "streamlit-demo"
    st.session_state["duration"] = "36"
    st.session_state["step"] = 1
    st.session_state["error"] = None


def reset_screening() -> None:
    current = screening()
    for match in (current.matches if current else []):
        st.session_state.pop(f"sel_{match.match_id}", None)
    st.session_state.update(
        screening=None, step=1, error=None, report_path=None,
        decision_outcome="", decision_notes="",
    )


def selected_ids() -> list[str]:
    record = screening()
    if not record:
        return []
    return [m.match_id for m in record.matches
            if st.session_state.get(f"sel_{m.match_id}")]


# --------------------------------------------------------------------------- render


def muted(text: str) -> None:
    st.markdown(f"<span style='color:#8b949e'>{text}</span>", unsafe_allow_html=True)


def field(label: str, value: str | None, *, unavailable_note: str | None = None) -> None:
    st.caption(label)
    if value:
        st.markdown(f"**{value}**")
    elif unavailable_note:
        st.markdown(UNAVAILABLE)
        st.caption(unavailable_note)
    else:
        muted("Not recorded")


def chips(labels: list[str], colour: str = "#1f6feb") -> None:
    if not labels:
        return
    st.markdown(" ".join(
        f"<span style='background:{colour}22;border:1px solid {colour}55;"
        f"border-radius:12px;padding:2px 10px;margin-right:6px;font-size:0.78rem'>"
        f"{label}</span>" for label in labels
    ), unsafe_allow_html=True)


def fmt_ts(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).strftime("%d %b %Y %H:%M UTC")
    except ValueError:
        return value


def tabulate(nodes: list[dict]) -> list[dict] | None:
    """Flatten one-level-deep {label, data} nodes into table rows, or None if deeper."""
    rows = []
    for node in nodes:
        if not isinstance(node, dict):
            return None
        row = {"Name": node.get("label") or ""}
        for child in node.get("data") or []:
            if not isinstance(child, dict) or not is_leaf_list(child.get("data")):
                return None
            row[child.get("label") or "Value"] = join_values(child.get("data")) or ""
        rows.append(row)
    if not rows:
        return None
    if len({row["Name"] for row in rows}) == 1:
        for row in rows:
            row.pop("Name", None)
        rows = [row for row in rows if row]
    return rows or None


def render_data(data, label: str = "", depth: int = 0) -> None:
    """Render a SmartSearch {label, data[]} tree.

    Section names appear nowhere in the OpenAPI spec, so this walks whatever shape comes
    back rather than mapping known fields.
    """
    if not data:
        muted("None recorded")
        return

    if is_leaf_list(data):
        values = [str(v) for v in data if str(v).strip()]
        if not values:
            muted("None recorded")
        elif label == "Images":
            for start in range(0, len(values), 4):
                for col, url in zip(st.columns(4), values[start:start + 4]):
                    col.image(url, width="stretch")
        elif label in LONGFORM_SECTIONS or any(len(v) > 200 for v in values):
            for value in values:
                st.markdown(value.replace("\n", "  \n"))
        elif len(values) == 1:
            st.markdown(values[0])
        else:
            for value in values:
                st.markdown(f"- {value}")
        return

    nodes = [d for d in data if isinstance(d, dict)]

    if all(is_leaf_list(node.get("data")) for node in nodes):
        st.dataframe(
            [{"Field": n.get("label") or "", "Value": join_values(n.get("data")) or ""}
             for n in nodes],
            hide_index=True, width="stretch")
        return

    rows = tabulate(nodes)
    if rows:
        st.dataframe(rows, hide_index=True, width="stretch")
        return

    for node in nodes:
        st.markdown(f"{'#' * min(depth + 5, 6)} {node.get('label') or 'Detail'}")
        render_data(node.get("data"), node.get("label") or "", depth + 1)


def render_profile(profile: dict) -> None:
    for section in profile.get("sections") or []:
        label = section.get("label") or "Detail"
        st.markdown(f"##### {label}")
        render_data(section.get("data"), label)
        st.divider()

    st.markdown("##### Associates")
    associations = profile.get("associations") or []
    if associations:
        st.dataframe(
            [{"Relationship": a.get("association") or "", "Name": a.get("name") or "",
              "Prior": "Yes" if a.get("prior") else "No"} for a in associations],
            hide_index=True, width="stretch")
    else:
        muted("None recorded")


def ensure_profile(record, client) -> None:
    """Fetch and snapshot a match profile if we do not already hold one."""
    if record.has_profile:
        return
    profile: MatchProfile = client.get_match(record.match_id)
    screening().snapshot_profile(record.match_id, profile)
    screening().save()


def render_match(index: int, record, client, *, selectable: bool = False) -> None:
    summary = sections_to_map(record.summary)
    resolution = record.resolution
    name = match_display_name(record) if record.has_profile else None

    with st.container(border=True):
        head, badge = st.columns([5, 2])
        with head:
            if selectable:
                st.checkbox(
                    name or f"Match {index}", key=f"sel_{record.match_id}",
                    help="Select to classify")
            else:
                st.markdown(f"### {name or f'Match {index}'}")
            st.caption(f"Reference {record.ref or record.match_id}")
        with badge:
            if resolution.is_resolved:
                chips([resolution.status], STATUS_COLOURS.get(resolution.status, "#6b7280"))
                st.caption(f"Risk {resolution.risk} · {resolution.reason}")
            else:
                chips(["Needs review"], "#b54708")

        row1 = st.columns(4)
        with row1[0]:
            field("Submitted term", screening().subject.get("display_name"))
        with row1[1]:
            field("Matched term", record.meta.get("match_name"),
                  unavailable_note="meta.match_name is null on every sandbox match.")
        with row1[2]:
            field("Match score", record.meta.get("match_strength"),
                  unavailable_note="meta.match_strength is null on every sandbox match.")
        with row1[3]:
            field("Provider", "Dow Jones" if (record.ref or "").startswith("dj-") else None)

        row2 = st.columns(4)
        with row2[0]:
            field("Nationality", join_values(summary.get("Citizenship")))
        with row2[1]:
            field("Residence", join_values(summary.get("Residency")))
        with row2[2]:
            field("Date of birth", join_values(summary.get("Date of Birth")))
        with row2[3]:
            field("Active status", join_values(summary.get("Active Status")))

        chips(sorted(record.categories))

        with st.expander("View full profile"):
            if not record.has_profile:
                if st.button("Load full profile", key=f"prof_{record.match_id}"):
                    try:
                        with st.spinner("Fetching profile…"):
                            ensure_profile(record, client)
                        st.rerun()
                    except SmartSearchError as exc:
                        st.error(f"Could not load the profile: {exc}")
                else:
                    st.caption(
                        "Fetched on demand from GET /v3/watchlist/matches/{id}. "
                        "Gender, aliases and sanctions detail live only on this call.")
            else:
                render_profile(record.profile)
                with st.expander("Raw match payload"):
                    st.json({"summary": record.summary, "meta": record.meta,
                             "categories": record.categories})



# --------------------------------------------------------------------------- identity

OUTCOME_COLOURS = {"pass": "#067647", "refer": "#b54708", "fail": "#b42318"}


def render_identity(record) -> None:
    identity = record.identity
    with st.container(border=True):
        st.markdown("### Identity verification")
        if not identity:
            muted("Not shown for this route. The International Individual AML route does "
                  "not run a credit-reference check; it returns a field-level match matrix "
                  "instead, which this build fetches but does not yet render.")
            return

        kind = identity.get("kind", "uk-cra")
        outcome = (identity.get("outcome") or "unknown").lower()
        alerts = identity.get("alerts") or []
        colour = OUTCOME_COLOURS.get(outcome, "#6b7280")
        # An outcome of "pass" carrying alerts is the dangerous case, so it is never
        # allowed to render as a plain green pass.
        if alerts:
            colour = OUTCOME_COLOURS["refer"]
            headline = f"{outcome.upper()} with {len(alerts)} alert" + ("s" if len(alerts) > 1 else "")
        else:
            headline = outcome.upper()
            if kind == KIND_INTERNATIONAL:
                # Every International result comes back "refer", so a bare refer with
                # corroborating sources is not a warning and must not be coloured like one.
                colour = OUTCOME_COLOURS["pass"]

        head, meta = st.columns([2, 4])
        with head:
            chips([headline], colour)
        with meta:
            st.caption(f"Route {identity.get('route')} · checked "
                       f"{fmt_ts(identity.get('checked_at'))}")

        if alerts:
            st.error("**Requires review before this subject can be accepted**")
            for alert in alerts:
                st.markdown(f"- {alert}")

        if kind == KIND_INTERNATIONAL:
            render_corroboration(identity)
            return

        for check in identity.get("checks") or []:
            st.markdown(f"##### {str(check.get('cra') or 'CRA').title()}"
                        f"  ·  {str(check.get('outcome') or '').upper()}")
            cols = st.columns(4)
            cols[0].metric("Authentication index", check.get("authentication_index") or 0)
            cols[1].metric("Primary checks", check.get("primary_check_count") or 0)
            cols[2].metric("Primary sources", check.get("primary_source_count") or 0)
            cols[3].metric("DOB matches", check.get("primary_data_date_of_birth_match") or 0)

            rows = []
            for key, label, _, _ in IDENTITY_FLAGS:
                text, bad = identity_flag_label(key, check.get(key))
                rows.append({"Check": label, "Result": ("⚠ " if bad else "") + text})
            oldest = check.get("primary_data_oldest_date")
            if oldest:
                rows.append({"Check": "Oldest primary data", "Result": oldest})
            if check.get("bank_account_match") is None:
                rows.append({"Check": "Bank account match",
                             "Result": "Not available: bank checks are not enabled on this "
                                       "contract"})
            st.dataframe(rows, hide_index=True, width="stretch")

            granular = check.get("granular_data") or []
            if granular:
                st.caption("Evidence found")
                st.dataframe(
                    [{"Category": g.get("category"), "Check": g.get("text"),
                      "Sources": g.get("source"), "Items": g.get("item"),
                      "Oldest": g.get("oldest_date") or "—"} for g in granular],
                    hide_index=True, width="stretch")

            documents = {k: v for k, v in (check.get("documents") or {}).items()
                         if v is not None}
            if documents:
                st.caption("Identity documents")
                doc_rows = []
                for name, verified in documents.items():
                    errors = [f"{e.get('code')}: {e.get('message')}"
                              for e in (check.get("documents_errors") or {}).get(name) or []]
                    doc_rows.append({
                        "Document": name.replace("_", " ").title(),
                        "Result": "Verified" if verified else "⚠ Did not verify",
                        "Detail": "; ".join(errors) or "—"})
                st.dataframe(doc_rows, hide_index=True, width="stretch")

        with st.expander("Raw identity response"):
            st.json(identity.get("raw") or {})



GRADE_LABELS = {"full": "Full match", "partial": "Partial match"}


def render_corroboration(identity: dict) -> None:
    """International Individual AML: a per-field corroboration report, not a CRA score."""
    for check in identity.get("checks") or []:
        sources = check.get("number_of_sources") or 0
        st.caption(
            "This route grades each detail you supplied against independent sources. It "
            "returns `refer` as its normal outcome, so read the source count and the field "
            "grades rather than the outcome. There is no deceased check or fraud alert on "
            "this route.")
        cols = st.columns(4)
        cols[0].metric("Corroborating sources", sources)
        cols[1].metric("Fields graded", len(check.get("field_grades") or {}))
        cols[2].metric("Source limit reached",
                       "Yes" if check.get("source_limit_reached") else "No")
        cols[3].metric("Outcome", str(check.get("outcome") or "").upper())

        grades = check.get("field_grades") or {}
        rows = []
        for group, fields in CORROBORATION_GROUPS:
            for name in fields:
                grade = grades.get(name)
                rows.append({
                    "Group": group,
                    "Field": name.replace("_", " ").capitalize(),
                    "Result": GRADE_LABELS.get(grade, "Not matched" if sources else "—"),
                })
        st.dataframe(rows, hide_index=True, width="stretch")

        national = check.get("national_ids") or []
        if national:
            st.caption("National IDs")
            st.dataframe(
                [{"Type": str(n.get("type") or "").upper(),
                  "Result": GRADE_LABELS.get(n.get("result"), "Not matched")}
                 for n in national],
                hide_index=True, width="stretch")

        granular = check.get("granular_data") or []
        if granular:
            st.caption("Sources found")
            st.dataframe(
                [{"Category": g.get("category"), "Source type": g.get("text"),
                  "Sources": g.get("source"), "Items": g.get("item"),
                  "Oldest": g.get("oldest_date") or "—"} for g in granular],
                hide_index=True, width="stretch")

    with st.expander("Raw identity response"):
        st.json(identity.get("raw") or {})


# --------------------------------------------------------------------------- chrome


def step_indicator(current: int) -> None:
    cols = st.columns(len(STEPS))
    for i, (col, (name, sub)) in enumerate(zip(cols, STEPS), start=1):
        done, active = i < current, i == current
        mark = "✓" if done else str(i)
        colour = "#111418" if (done or active) else "#c7ccd3"
        weight = "600" if active else "400"
        col.markdown(
            f"<div style='text-align:center'>"
            f"<div style='display:inline-block;width:26px;height:26px;line-height:26px;"
            f"border-radius:50%;background:{colour};color:#fff;font-size:0.8rem'>{mark}</div>"
            f"<div style='font-weight:{weight};font-size:0.9rem;margin-top:4px'>{name}</div>"
            f"<div style='color:#8b949e;font-size:0.75rem'>{sub}</div></div>",
            unsafe_allow_html=True)
    st.divider()


def subject_bar() -> None:
    record = screening()
    if not record:
        return
    with st.container(border=True):
        left, right = st.columns([5, 2])
        left.caption("Screened subject")
        left.markdown(
            f"**{record.subject.get('display_name')}** · INDIVIDUAL · "
            f"`{record.screening_id}`")
        if right.button("Edit subject and re-screen", width="stretch"):
            st.session_state["step"] = 1
            st.rerun()


def audit_history() -> None:
    record = screening()
    if not record:
        return
    with st.expander(f"Audit history ({len(record.audit)})"):
        for event in record.audit:
            st.markdown(f"**{event['type']}**")
            st.caption(
                f"{fmt_ts(event.get('occurred_at'))} · {event.get('actor')} · "
                f"revision {event.get('revision')}")


def show_error(error: dict) -> None:
    kind, message, fields = error["kind"], error["message"], error.get("fields") or {}
    if kind == "validation":
        # Distinguish our own pre-flight check from a rejection by the API, so the
        # message never claims SmartSearch saw a request it was never sent.
        local = error.get("local")
        st.error(message if local else "SmartSearch rejected the search.")
        for name, detail in fields.items():
            st.markdown(f"- **{name}** — {detail}")
        st.caption(
            "The OpenAPI spec marks only name and address.country required, but the live "
            "API also rejects a blank date_of_birth, street_1, town or postcode.")
    elif kind == "auth":
        st.error(f"Authentication failed: {message}")
        st.caption("Check SS_APP_ID and SS_SECRET in .env, then restart the app.")
    elif kind == "service":
        st.error(f"SmartSearch returned a server error: {message}")
        st.caption(
            "The sandbox returns HTTP 500 both for genuine faults and for search types "
            "your contract lacks permission for. Retrying is usually worthwhile.")
    else:
        st.error(message)


def record_error(exc: SmartSearchError) -> None:
    if isinstance(exc, ValidationError):
        kind, fields = "validation", exc.field_errors
    elif isinstance(exc, AuthError):
        kind, fields = "auth", {}
    elif isinstance(exc, ServiceError):
        kind, fields = "service", {}
    else:
        kind, fields = "other", {}
    st.session_state["error"] = {"kind": kind, "message": str(exc), "fields": fields}


# --------------------------------------------------------------------------- steps


def step_subject(client) -> None:
    st.subheader("Screening subject")
    with st.form("search"):
        name_cols = st.columns([1, 3, 3, 3])
        name_cols[0].text_input("Title", key="title")
        name_cols[1].text_input("First name *", key="first")
        name_cols[2].text_input("Middle name", key="middle")
        name_cols[3].text_input("Last name *", key="last")

        dob_cols = st.columns([2, 6])
        dob_cols[0].text_input("Date of birth *", key="dob", placeholder="YYYY-MM-DD")
        dob_cols[1].caption(
            "Required. The spec marks it optional, but the live API rejects a blank "
            "date_of_birth. It does not narrow watchlist matching.")

        st.markdown("**Address** — required by the API even for a name screen.")
        addr1 = st.columns(4)
        addr1[0].text_input("Flat", key="flat")
        addr1[1].text_input("Building", key="building")
        addr1[2].text_input("Street *", key="street_1")
        addr1[3].text_input("Town *", key="town")
        addr2 = st.columns(4)
        addr2[0].text_input("Region", key="region")
        addr2[1].text_input("Postcode *", key="postcode")
        addr2[2].text_input("Country (ISO-3) *", key="country", max_chars=3)
        addr2[3].text_input("Time at address (months)", key="duration",
                            help="Required for a UK identity check.")
        st.text_input("Your reference", key="client_reference")

        with st.expander("Identity documents (optional, UK only)"):
            st.caption(
                "Each document is verified against the credit reference agency and comes "
                "back with a pass or fail plus reason codes. Bank verification is not "
                "offered: this contract rejects it with \"Bank checks are not allowed\".")
            doc1 = st.columns(2)
            doc1[0].text_input("National Insurance number", key="ni_number",
                               placeholder="AB123456C")
            doc1[1].text_input("Driving licence number", key="dl_number")
            doc2 = st.columns(3)
            doc2[0].text_input("Passport country (ISO-3)", key="passport_country",
                               max_chars=3)
            doc2[1].text_input("Passport expiry", key="passport_expiry",
                               placeholder="YYYY-MM-DD")
            doc2[2].text_input("Passport MRZ line 2", key="passport_mrz")

        with st.expander("Non-UK subjects (required by some jurisdictions)"):
            st.caption(
                "Validation on the International route is country-specific. A USA subject "
                "is rejected without a state in Region, a telephone number and a national "
                "ID, none of which the OpenAPI spec marks required.")
            intl = st.columns(3)
            intl[0].text_input("Telephone", key="telephone", placeholder="+1...")
            intl[1].text_input("National ID", key="national_id")
            intl[2].text_input("National ID type", key="national_id_type",
                               placeholder="ssn")

        existing = screening()
        label = "Re-screen subject" if existing else "Run screening"
        submitted = st.form_submit_button(label, type="primary", width="stretch")

    if submitted:
        run_search(client)


def run_search(client) -> None:
    st.session_state["error"] = None
    missing = [
        label for label, key in [
            ("First name", "first"), ("Last name", "last"), ("Date of birth", "dob"),
            ("Street", "street_1"), ("Town", "town"), ("Postcode", "postcode"),
            ("Country", "country"),
        ] if not st.session_state[key].strip()
    ]
    dob = st.session_state["dob"].strip()
    if dob:
        try:
            datetime.strptime(dob, "%Y-%m-%d")
        except ValueError:
            missing.append("Date of birth (use YYYY-MM-DD)")
    if missing:
        st.session_state["error"] = {
            "kind": "validation", "message": "Fill in the required fields before searching.",
            "local": True, "fields": {name: "Required" for name in missing}}
        st.rerun()

    address = {key: st.session_state[key].strip()
               for key in ("flat", "building", "street_1", "town", "region", "postcode")
               if st.session_state.get(key)}
    address["country"] = st.session_state["country"].strip().upper()
    uk_route = address["country"] == "GBR"

    # The UK route additionally requires a title and a time at address.
    if uk_route:
        if not st.session_state["title"].strip():
            missing.append("Title (required for a UK identity check)")
        try:
            duration = int(st.session_state["duration"].strip() or 0)
            if duration <= 0:
                raise ValueError
        except ValueError:
            duration = 0
            missing.append("Time at address in whole months")
        if missing:
            st.session_state["error"] = {
                "kind": "validation",
                "message": "Fill in the required fields before searching.",
                "local": True, "fields": {name: "Required" for name in missing}}
            st.rerun()
    display_name = " ".join(p for p in (
        st.session_state["title"].strip(), st.session_state["first"].strip(),
        st.session_state["middle"].strip(), st.session_state["last"].strip()) if p)
    subject = {"display_name": display_name, "dob": dob,
               "client_reference": st.session_state["client_reference"].strip(), **address}
    subject["duration_months"] = st.session_state["duration"].strip()

    documents = {}
    if st.session_state["ni_number"].strip():
        documents["national_insurance"] = st.session_state["ni_number"].strip()
    if st.session_state["dl_number"].strip():
        documents["driving_licence"] = {"number": st.session_state["dl_number"].strip()}
    if all(st.session_state[k].strip() for k in
           ("passport_country", "passport_expiry", "passport_mrz")):
        documents["passport"] = {
            "country": st.session_state["passport_country"].strip().upper(),
            "expiry": st.session_state["passport_expiry"].strip(),
            "mrz_line_2": st.session_state["passport_mrz"].strip()}

    try:
        with st.spinner("Running AML search…"):
            if uk_route:
                # UK subjects get the fuller route: the same watchlist screening plus
                # credit-reference identity verification, for one search.
                result = client.search_uk_individual(
                    st.session_state["first"].strip(), st.session_state["last"].strip(),
                    [{**address, "duration": duration}], dob,
                    st.session_state["title"].strip(),
                    middle=st.session_state["middle"].strip() or None,
                    documents=documents or None,
                    client_reference=st.session_state["client_reference"].strip() or None)
            else:
                national_id = st.session_state["national_id"].strip()
                result = client.search_individual(
                    st.session_state["first"].strip(), st.session_state["last"].strip(),
                    address, dob=dob,
                    title=st.session_state["title"].strip() or None,
                    middle=st.session_state["middle"].strip() or None,
                    client_reference=st.session_state["client_reference"].strip() or None,
                    telephone=st.session_state["telephone"].strip() or None,
                    national_ids=[{
                        "type": st.session_state["national_id_type"].strip() or "ssn",
                        "number": national_id}] if national_id else None)
            # The search reports "complete" before screening has finished writing
            # matches, so poll before reading or the queue comes back empty.
            client.wait_for_matches(result.subject_id)
            matches = client.list_all_matches(result.subject_id)
    except SmartSearchError as exc:
        record_error(exc)
        st.rerun()
        return

    existing = screening()
    stale_ids = [m.match_id for m in existing.matches] if existing else []
    if existing is not None:
        existing.rescreen(subject, result, matches)
    else:
        st.session_state["screening"] = ss.Screening.create(subject, result, matches)
    for match_id in stale_ids:
        st.session_state.pop(f"sel_{match_id}", None)
    st.session_state["step"] = 2
    st.session_state["report_path"] = None
    st.rerun()


def step_review(client) -> None:
    record = screening()
    st.subheader("Screening result")
    st.caption("Identity verification, then every watchlist match to review.")

    render_identity(record)

    counts = record.category_counts
    cols = st.columns(max(len(counts) + 1, 2))
    cols[0].metric("Total", len(record.matches))
    for col, (label, count) in zip(cols[1:], counts.items()):
        col.metric(label.split("(")[-1].rstrip(")") or label, count)

    st.subheader(f"{len(record.matches)} potential match"
                 f"{'es' if len(record.matches) != 1 else ''}")
    if not record.matches:
        st.info("**No watchlist matches.** This subject did not hit any watchlist entry.")
        st.caption("An empty result is a successful search, not an error. In sandbox, "
                   "only the demo data pack personas produce matches. Note that a clean "
                   "watchlist says nothing about identity: check the panel above.")
    else:
        for index, match in enumerate(record.matches, start=1):
            render_match(index, match, client)

    if st.button("Continue to classification →", type="primary"):
        st.session_state["step"] = 3
        st.rerun()


def step_resolve(client) -> None:
    record = screening()
    if st.session_state.get("clear_selection"):
        st.session_state["clear_selection"] = False
        for match in record.matches:
            st.session_state[f"sel_{match.match_id}"] = False

    st.subheader("Classify matches")
    st.caption("Select one or more results, then record a classification. The final "
               "decision stays blocked until every match is resolved.")

    if not record.matches:
        st.info("No matches to classify. Continue to the report.")
        if st.button("Continue to report →", type="primary"):
            st.session_state["step"] = 4
            st.rerun()
        return

    queue, panel = st.columns([3, 2])

    with queue:
        head = st.columns([3, 2, 2])
        head[0].radio("Show", ["Unresolved", "All", "Resolved"], key="queue_filter",
                      horizontal=True, label_visibility="collapsed")
        head[1].markdown(f"**{record.unresolved_count} unresolved** "
                         f"/ {len(record.matches)}")

        if st.session_state["queue_filter"] == "Unresolved":
            visible = [m for m in record.matches if not m.is_resolved]
        elif st.session_state["queue_filter"] == "Resolved":
            visible = [m for m in record.matches if m.is_resolved]
        else:
            visible = list(record.matches)

        picker = st.columns(2)
        if picker[0].button(f"Select all visible ({len(visible)})", width="stretch"):
            for match in visible:
                st.session_state[f"sel_{match.match_id}"] = True
            st.rerun()
        if picker[1].button("Clear selection", width="stretch"):
            for match in record.matches:
                st.session_state[f"sel_{match.match_id}"] = False
            st.rerun()

        if not visible:
            st.success("Nothing in this view.")
        for index, match in enumerate(visible, start=1):
            render_match(index, match, client, selectable=True)

    with panel:
        render_resolution_panel(client)


def render_resolution_panel(client) -> None:
    record = screening()
    chosen = selected_ids()

    with st.container(border=True):
        st.markdown("### Resolution")
        st.caption(f"{len(chosen)} match{'es' if len(chosen) != 1 else ''} selected")

        st.selectbox("Status", ss.STATUSES, key="res_status")
        st.selectbox("Risk", ss.RISKS, key="res_risk")
        st.selectbox("Reason (required)", ss.REASONS, key="res_reason")
        st.text_area("Resolution comment", key="res_comment",
                     placeholder="Why this classification?")

        status = st.session_state["res_status"]
        if status in ss.STATUS_TO_TRUE_MATCH:
            st.caption(f"Will also PATCH is_true_match="
                       f"{str(ss.STATUS_TO_TRUE_MATCH[status]).lower()} to SmartSearch.")
        else:
            st.caption(f"{status} has no SmartSearch equivalent; recorded locally only.")

        if st.button("Save resolution", type="primary", width="stretch",
                     disabled=not chosen):
            save_resolution(client, chosen)

        if not st.session_state["res_reason"]:
            st.warning("A reason is required.")

        nav = st.columns(2)
        if nav[0].button("← Review", width="stretch"):
            st.session_state["step"] = 2
            st.rerun()
        if nav[1].button("Report →", width="stretch", type="primary",
                         disabled=not record.all_resolved):
            st.session_state["step"] = 4
            st.rerun()
        if not record.all_resolved:
            st.caption(f"{record.unresolved_count} match(es) still to classify.")


def save_resolution(client, chosen: list[str]) -> None:
    record = screening()
    status = st.session_state["res_status"]
    reason = st.session_state["res_reason"]
    if not reason:
        st.warning("Pick a reason before saving.")
        return

    sync_results = {}
    progress = st.progress(0.0, text="Recording resolution…")
    for i, match_id in enumerate(chosen, start=1):
        match = record.match(match_id)
        # Snapshot while we are here: the report needs the primary name and gender,
        # which only exist on the per-match profile call.
        if match is not None and not match.has_profile:
            try:
                ensure_profile(match, client)
            except SmartSearchError:
                pass  # the resolution still stands; the report says "not snapshotted"
        sync_results[match_id] = ss.sync_status_to_provider(client, match_id, status)
        progress.progress(i / len(chosen), text=f"Recording resolution… {i}/{len(chosen)}")
    progress.empty()

    record.record_resolution(
        chosen, status, st.session_state["res_risk"], reason,
        st.session_state["res_comment"], sync_results=sync_results)
    # Streamlit forbids writing a widget's state after it has been instantiated, and the
    # checkboxes are already on screen by the time this panel runs. Defer the clear.
    st.session_state["clear_selection"] = True
    failed = [e for _, e in sync_results.values() if e]
    if failed:
        st.warning(f"Recorded locally, but {len(failed)} provider sync(s) failed: "
                   f"{failed[0][:120]}")
    st.rerun()


def step_report(client) -> None:
    record = screening()

    st.subheader("Final decision")
    if not record.all_resolved:
        st.warning(f"{record.unresolved_count} match(es) are still unresolved. "
                   "Every match must be classified before a decision can be recorded.")
        if st.button("← Back to classification"):
            st.session_state["step"] = 3
            st.rerun()
        return

    # An identity check can return "pass" while carrying a deceased flag or a fraud alert,
    # so the gate keys on the alerts rather than the outcome.
    blocking = record.identity_needs_attention and not record.identity_acknowledged
    if record.identity_needs_attention:
        with st.container(border=True):
            st.error("**Identity verification requires review**")
            for alert in record.identity_alerts:
                st.markdown(f"- {alert}")
            st.caption(
                f"Identity outcome was reported as "
                f"`{record.identity_outcome}`, but the checks above were raised against "
                "this subject. Reject and Escalate are available without acknowledgement.")
            if record.identity_acknowledged:
                st.success("Acknowledged by the reviewer.")
            else:
                st.checkbox(
                    "I have reviewed these identity alerts and still wish to accept",
                    key="ack_identity")
                if st.session_state["ack_identity"] and st.button("Record acknowledgement"):
                    record.acknowledge_identity()
                    st.rerun()

    with st.container(border=True):
        if record.decision:
            st.success(f"Decision recorded: **{record.decision['outcome']}**")
            st.caption(f"{record.decision['reviewer']} · "
                       f"{fmt_ts(record.decision['decided_at'])}")
            if record.decision.get("notes"):
                st.markdown(f"> {record.decision['notes']}")
        else:
            st.selectbox("Outcome", [""] + ss.OUTCOMES, key="decision_outcome",
                         format_func=lambda v: "Select outcome" if not v else v.title())
            st.text_area("Notes", key="decision_notes")
            chosen = st.session_state["decision_outcome"]
            blocked = blocking and chosen == "ACCEPT"
            if blocked:
                st.warning("Acknowledge the identity alerts above before accepting, "
                           "or choose Reject or Escalate.")
            if st.button("Record final decision", type="primary",
                         disabled=not chosen or blocked):
                try:
                    record.record_decision(chosen, st.session_state["decision_notes"])
                except ValueError as exc:
                    st.error(str(exc))
                else:
                    st.rerun()

    st.subheader("Report")
    st.caption("Generated from the stored screening record: subject, every match with its "
               "classification, the final decision and the full audit trail.")

    missing = [m for m in record.matches if not m.has_profile]
    if missing:
        st.caption(f"{len(missing)} profile snapshot(s) will be fetched first so the "
                   "report can show primary names and gender.")

    if st.button("Generate PDF report", type="primary", disabled=not record.decision):
        generate_report(client, missing)

    if st.session_state["report_path"]:
        path = st.session_state["report_path"]
        st.success(f"Report written to `{path}`")
        with open(path, "rb") as handle:
            st.download_button("Download PDF", handle, file_name=path.name,
                               mime="application/pdf", type="primary")

    if not record.decision:
        st.caption("Record the final decision before generating the report.")


def generate_report(client, missing: list) -> None:
    record = screening()
    if missing:
        progress = st.progress(0.0, text="Fetching profile snapshots…")
        for i, match in enumerate(missing, start=1):
            try:
                ensure_profile(match, client)
            except SmartSearchError:
                pass  # a missing snapshot is reported in the PDF, not fatal
            progress.progress(i / len(missing),
                              text=f"Fetching profile snapshots… {i}/{len(missing)}")
        progress.empty()

    with st.spinner("Building PDF…"):
        path = record.next_report_path()
        build_report(record, path)
        record.record_report(path)
    st.session_state["report_path"] = path
    st.rerun()


# --------------------------------------------------------------------------- app

init_state()

app_id = os.getenv("SS_APP_ID", "")
secret = os.getenv("SS_SECRET", "")
base_url = os.getenv("SS_BASE_URL", SANDBOX_BASE)

with st.sidebar:
    st.header("Connection")
    if app_id and secret:
        st.success("Credentials loaded from .env")
    else:
        st.error("SS_APP_ID / SS_SECRET missing from .env")
    st.caption(f"Endpoint: `{base_url}`")
    st.caption(f"Reviewer: {ss.default_reviewer()}")
    st.caption(f"Store: `{ss.STORE_DIR}/`")

    st.header("Demo personas")
    for persona in PERSONAS:
        st.button(persona, key=f"persona_{persona}", width="stretch",
                  on_click=apply_persona, args=(persona,))

    st.header("Screening")
    if screening():
        st.caption(f"Status: **{screening().status}** · revision {screening().revision}")
    if st.button("New screening", width="stretch"):
        reset_screening()
        st.rerun()

header, badge = st.columns([6, 1])
header.title("AML Screening")
if screening():
    badge.markdown(f"<div style='margin-top:18px'>{screening().status}</div>",
                   unsafe_allow_html=True)

step = st.session_state["step"]
step_indicator(step)

if st.session_state["error"]:
    show_error(st.session_state["error"])

if not app_id or not secret:
    st.stop()

client = get_client(app_id, secret, base_url)

if step > 1 and not screening():
    st.session_state["step"] = 1
    step = 1

if step > 1:
    subject_bar()

if step == 1:
    step_subject(client)
elif step == 2:
    step_review(client)
elif step == 3:
    step_resolve(client)
elif step == 4:
    step_report(client)

audit_history()
