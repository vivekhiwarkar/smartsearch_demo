"""Client for the SmartSearch AML API (v3, JSON:API).

Screening an individual is three calls, because watchlist matches hang off the
search *subject* rather than the search itself:

    search_individual()  -> POST /v3/internationalindividual/searches   (subject id)
    list_matches()       -> GET  /v3/watchlist/subjects/{id}/matches    (match list)
    get_match()          -> GET  /v3/watchlist/matches/{id}             (full profile)

Deliberately free of Streamlit imports so it can be lifted into another project.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field, fields as dataclass_fields
from typing import Any

import requests

SANDBOX_BASE = "https://api.sandbox.app.smartsearch.com"
LIVE_BASE = "https://api.app.smartsearch.com"
JSONAPI = "application/vnd.api+json"

# Tokens live for 900s. Refresh with headroom so a request never races the expiry.
TOKEN_REFRESH_HEADROOM = 60
TOKEN_FALLBACK_LIFETIME = 840
DEFAULT_TIMEOUT = 30

# Watchlist match refs are prefixed by the data provider that supplied them.
PROVIDER_PREFIXES = {"dj": "Dow Jones"}


# --------------------------------------------------------------------------- errors


class SmartSearchError(Exception):
    """Base error. Carries the HTTP status and the JSON:API errors[] array."""

    def __init__(self, message: str, status: int | None = None, errors: list[dict] | None = None):
        super().__init__(message)
        self.status = status
        self.errors = errors or []

    @property
    def details(self) -> list[str]:
        out = []
        for err in self.errors:
            text = err.get("detail") or err.get("title") or ""
            if text:
                out.append(text)
        return out


class AuthError(SmartSearchError):
    """Credentials rejected, or a token could not be obtained/refreshed."""


class ValidationError(SmartSearchError):
    """HTTP 400. Exposes per-field messages built from each error's source.pointer."""

    @property
    def field_errors(self) -> dict[str, str]:
        fields: dict[str, str] = {}
        for err in self.errors:
            pointer = (err.get("source") or {}).get("pointer")
            if not pointer:
                continue
            # "/data/attributes/address/street_1" -> "address.street_1"
            parts = [p for p in pointer.split("/") if p and p not in ("data", "attributes")]
            fields[".".join(parts)] = err.get("detail") or err.get("title") or "Invalid value"
        return fields


class ServiceError(SmartSearchError):
    """HTTP 500/503. The sandbox also returns 500 for permission failures."""


# --------------------------------------------------------------------------- models


@dataclass
class SearchResult:
    search_id: str
    subject_id: str | None
    status: str
    created_at: str | None
    raw: dict = field(repr=False, default_factory=dict)
    route: str = "international-individual"
    # Only the UK route performs identity verification; None on every other route.
    identity: IdentityResult | None = None


@dataclass
class WatchlistSummary:
    """Per-category match counts for a subject, plus the async processing flag."""

    categories: dict[str, dict] = field(default_factory=dict)
    is_processing: bool = False
    raw: dict = field(repr=False, default_factory=dict)

    def count(self, category: str) -> int:
        return int(self.categories.get(category, {}).get("num_matches", 0))

    @property
    def total(self) -> int:
        return self.count("totals")

    @property
    def risk_counts(self) -> dict[str, int]:
        """The four headline categories, in the order an analyst reads them."""
        wanted = [
            ("Worldwide Sanctions (SAN)", "Sanctions"),
            ("Politically Exposed Person (PEP)", "PEP"),
            ("Relative or Close Associate (RCA)", "RCA"),
            ("Special Interest Person (SIP)", "SIP"),
        ]
        return {label: self.count(key) for key, label in wanted}


@dataclass
class IdentityCheck:
    """One credit-reference-agency result inside a UK Individual AML check.

    The boolean fields do NOT share a polarity, which is verified rather than assumed.
    Probed against the sandbox data pack's own answer key:

        Nicholas Brown   (pack: not deceased, no fraud) -> deceased_check True,  alert False
        Phoebe Alexander (pack: DECEASED, FRAUD)        -> deceased_check False, alert True

    So deceased_check/paf_check/name_and_address_match/identity_confirmation_level are
    "this check passed" flags, while potential_fraud_alert is an "alert raised" flag.
    Rendering them with a single polarity would print "Deceased: Yes" for a living subject.
    """

    cra: str = ""
    outcome: str = ""
    authentication_index: int | None = None
    primary_check_count: int = 0
    primary_source_count: int = 0
    collaborative_check_count: int = 0
    collaborative_source_count: int = 0
    name_and_address_match: bool | None = None
    identity_confirmation_level: bool | None = None
    paf_check: bool | None = None
    deceased_check: bool | None = None
    potential_fraud_alert: bool | None = None
    bank_account_match: int | None = None
    primary_data_oldest_date: str | None = None
    collaborative_data_oldest_date: str | None = None
    primary_data_date_of_birth_match: int | None = None
    collaborative_data_date_of_birth_match: int | None = None
    granular_data: list[dict] = field(default_factory=list)
    documents: dict = field(default_factory=dict)
    documents_errors: dict = field(default_factory=dict)

    @property
    def alerts(self) -> list[str]:
        """Everything on this check that an analyst must not miss."""
        out = []
        if self.deceased_check is False:
            out.append("Deceased flag raised")
        if self.potential_fraud_alert is True:
            out.append("Potential fraud alert")
        if self.paf_check is False:
            out.append("Address not confirmed on the Postcode Address File")
        if self.name_and_address_match is False:
            out.append("Name and address did not match")
        if self.identity_confirmation_level is False:
            out.append("Identity not confirmed")
        for document, verified in (self.documents or {}).items():
            if verified is False:
                out.append(f"{document.replace('_', ' ').capitalize()} did not verify")
        return out

    def document_errors(self, document: str) -> list[str]:
        return [f"{e.get('code')}: {e.get('message')}"
                for e in (self.documents_errors or {}).get(document) or []]


@dataclass
class IdentityResult:
    """Subject-level identity verification. Two shapes, one container.

    `uk-cra` holds IdentityCheck (authentication model, one per credit reference agency).
    `international-corroboration` holds CorroborationCheck (field-grading model).
    """

    outcome: str = ""
    checks: list = field(default_factory=list)
    raw: dict = field(repr=False, default_factory=dict)
    kind: str = "uk-cra"

    @property
    def is_clear(self) -> bool:
        return not self.needs_attention

    @property
    def alerts(self) -> list[str]:
        seen, out = set(), []
        for check in self.checks:
            for alert in check.alerts:
                if alert not in seen:
                    seen.add(alert)
                    out.append(alert)
        return out

    @property
    def needs_attention(self) -> bool:
        """True when a reviewer must look. The rule differs by route, deliberately.

        UK: outcome is not pass, OR any alert. Verified in sandbox, Phoebe Alexander
        returns "pass" while carrying a deceased flag and a fraud alert, so keying off
        outcome alone would wave her through.

        International: alerts only. Every result on that route comes back "refer", so
        including the outcome would gate every non-UK subject and mean nothing.
        """
        if self.kind == KIND_INTERNATIONAL:
            return bool(self.alerts)
        return self.outcome.lower() != "pass" or bool(self.alerts)


def _parse_identity(payload: dict) -> IdentityResult | None:
    """Pull the uk-individual-result out of a JSON:API response."""
    result = next((inc for inc in payload.get("included") or []
                   if inc.get("type") == "uk-individual-result"), None)
    if result is None:
        return None
    attrs = result.get("attributes") or {}
    fields = {f.name for f in dataclass_fields(IdentityCheck)}
    checks = [
        IdentityCheck(**{k: v for k, v in (detail or {}).items() if k in fields})
        for detail in attrs.get("details") or []
    ]
    return IdentityResult(kind=KIND_UK, outcome=attrs.get("outcome", ""),
                          checks=checks, raw=result)



# Polarity is NOT uniform across these fields, and rendering them with a single polarity
# would print "Deceased: Yes" for a living subject. Verified against the sandbox data
# pack's own answer key (see IdentityCheck). Order is the order a reviewer reads them.
KIND_UK = "uk-cra"
KIND_INTERNATIONAL = "international-corroboration"

IDENTITY_FLAGS = [
    ("identity_confirmation_level", "Identity confirmation", "Confirmed", "Not confirmed"),
    ("name_and_address_match", "Name and address", "Matched", "Did not match"),
    ("paf_check", "Postcode Address File", "Address confirmed", "Not confirmed"),
    ("deceased_check", "Deceased check", "Passed, not recorded deceased",
     "DECEASED FLAG RAISED"),
    ("potential_fraud_alert", "Fraud alert", "POTENTIAL FRAUD ALERT", "No alert"),
]
# The one field where True is the bad news rather than the good news.
INVERTED_IDENTITY_FLAGS = {"potential_fraud_alert"}


def identity_flag_label(key: str, value) -> tuple[str, bool]:
    """Return (label, is_bad) for one identity boolean."""
    spec = next((f for f in IDENTITY_FLAGS if f[0] == key), None)
    if spec is None or value is None:
        return "Not checked", False
    _, _, when_true, when_false = spec
    bad = bool(value) if key in INVERTED_IDENTITY_FLAGS else not value
    return (when_true if value else when_false), bad



# The International route grades each supplied detail rather than scoring the identity.
# Grouped in the order a reviewer reads them; the grade values observed are "full",
# "partial" and null (absent from the payload entirely when nothing matched).
CORROBORATION_GROUPS = [
    ("Name", ["name_full", "name_given_initials", "name_first", "name_middle", "name_last"]),
    ("Name and address", ["name_and_address"]),
    ("Date of birth", ["date_of_birth_full", "date_of_birth_day", "date_of_birth_month",
                       "date_of_birth_year"]),
    ("Address", ["address_full", "address_flat", "address_building", "address_street",
                 "address_town", "address_region", "address_postcode"]),
    ("Contact", ["contacts_telephone"]),
]
CORROBORATION_FIELDS = [f for _, fields in CORROBORATION_GROUPS for f in fields]


@dataclass
class CorroborationCheck:
    """One International Individual AML result.

    A corroboration model: how many independent sources matched, and which of the details
    supplied could be graded against them. There is no authentication index, no deceased
    check and no fraud alert on this route.
    """

    outcome: str = ""
    number_of_sources: int = 0
    source_limit_reached: bool | None = None
    field_grades: dict[str, str] = field(default_factory=dict)
    national_ids: list[dict] = field(default_factory=list)
    granular_data: list[dict] = field(default_factory=list)

    @property
    def alerts(self) -> list[str]:
        """`refer` is the normal outcome on this route, so it is not an alert.

        Every International result observed in sandbox is `refer`, including one with two
        sources and sixteen fully graded fields. Treating that as an alert would fire on
        every non-UK subject and train the reviewer to click through it. No corroborating
        source at all is the signal that actually means something.
        """
        if not self.number_of_sources:
            return ["No corroborating sources found for this identity"]
        return []

    def grade(self, field_name: str) -> str | None:
        return self.field_grades.get(field_name)


def _parse_corroboration(payload: dict) -> IdentityResult | None:
    result = next((inc for inc in payload.get("included") or []
                   if inc.get("type") == "international-individual-result"), None)
    if result is None:
        return None
    attrs = result.get("attributes") or {}
    checks = []
    for detail in attrs.get("details") or []:
        detail = detail or {}
        checks.append(CorroborationCheck(
            outcome=detail.get("outcome", ""),
            number_of_sources=int(detail.get("number_of_sources") or 0),
            source_limit_reached=detail.get("source_limit_reached"),
            field_grades={f: detail[f] for f in CORROBORATION_FIELDS
                          if detail.get(f) is not None},
            national_ids=detail.get("national_ids") or [],
            granular_data=detail.get("granular_data") or [],
        ))
    return IdentityResult(kind=KIND_INTERNATIONAL, outcome=attrs.get("outcome", ""),
                          checks=checks, raw=result)


@dataclass
class Match:
    """One row in the match list."""

    id: str
    ref: str | None = None
    summary: list[dict] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    matched_at: str | None = None
    entry_updated_at: str | None = None
    match_strength: Any = None
    match_name: str | None = None
    match_name_type: str | None = None
    is_true_match: Any = None
    is_worked: bool = False
    is_suppressed: bool = False
    notes_count: int = 0
    raw: dict = field(repr=False, default_factory=dict)

    @property
    def summary_map(self) -> dict[str, list]:
        return sections_to_map(self.summary)

    @property
    def provider(self) -> str | None:
        return provider_from_ref(self.ref)

    @property
    def top_category(self) -> str | None:
        """Shortest category string, i.e. the least specific / headline one."""
        return min(self.categories, key=len) if self.categories else None


@dataclass
class MatchProfile:
    """Full detail for one match."""

    id: str
    sections: list[dict] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    associations: list[dict] = field(default_factory=list)
    raw: dict = field(repr=False, default_factory=dict)

    @property
    def sections_map(self) -> dict[str, list]:
        return sections_to_map(self.sections)


# --------------------------------------------------------------------------- helpers


def sections_to_map(sections: list[dict] | None) -> dict[str, list]:
    """Flatten a [{label, data}] list into {label: data} for direct lookups."""
    out: dict[str, list] = {}
    for section in sections or []:
        label = section.get("label")
        if label is not None:
            out[label] = section.get("data") or []
    return out


def is_leaf_list(data: Any) -> bool:
    """True when every element is a scalar, i.e. stop descending the label tree."""
    if not isinstance(data, list):
        return True
    return all(not isinstance(item, dict) for item in data)


def provider_from_ref(ref: str | None) -> str | None:
    """'dj-165219' -> 'Dow Jones'. Falls back to the raw prefix for unknown providers."""
    if not ref or "-" not in ref:
        return None
    prefix = ref.split("-", 1)[0]
    return PROVIDER_PREFIXES.get(prefix, prefix.upper())


def first_value(data: Any) -> str | None:
    """First scalar in a section's data list, as a display string."""
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, (dict, list)):
                return str(item)
        return None
    return None if data is None else str(data)


def join_values(data: Any, sep: str = ", ") -> str | None:
    """All scalars in a section's data list, joined. Handles multi-nationality etc."""
    if not isinstance(data, list):
        return first_value(data)
    scalars = [str(i) for i in data if not isinstance(i, (dict, list)) and str(i).strip()]
    return sep.join(scalars) if scalars else None


def _decode_jwt_exp(token: str) -> int | None:
    """Read exp from a JWT payload. Returns None if the token is not decodable."""
    try:
        payload = token.split(".")[1]
        payload = payload.replace("-", "+").replace("_", "/")
        payload += "=" * (-len(payload) % 4)
        text = base64.b64decode(payload).decode("utf-8", "replace")
        # SmartSearch pads its segments, so trim to the end of the JSON object.
        text = text[: text.rfind("}") + 1]
        return int(json.loads(text)["exp"])
    except Exception:
        return None


# --------------------------------------------------------------------------- client


class SmartSearchClient:
    """Bearer-token client. Tokens are fetched on demand and refreshed before expiry."""

    def __init__(
        self,
        app_id: str,
        secret: str,
        base_url: str = SANDBOX_BASE,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        if not app_id or not secret:
            raise AuthError("SS_APP_ID and SS_SECRET must both be set.")
        self.app_id = app_id
        self.secret = secret
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    # -- auth ---------------------------------------------------------------

    def _fetch_token(self) -> str:
        body = {
            "data": {
                "type": "app-token",
                "attributes": {"app_id": self.app_id, "secret": self.secret},
            }
        }
        try:
            resp = self.session.post(
                f"{self.base_url}/v3/auth/token",
                headers={"Content-Type": JSONAPI, "Accept": JSONAPI},
                json=body,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise SmartSearchError(f"Could not reach SmartSearch: {exc}") from exc

        payload = _safe_json(resp)
        if resp.status_code >= 400:
            raise AuthError(
                "SmartSearch rejected the app credentials.",
                status=resp.status_code,
                errors=payload.get("errors", []),
            )

        token = (payload.get("meta") or {}).get("token")
        if not token:
            raise AuthError("Auth succeeded but no token was returned.", status=resp.status_code)

        exp = _decode_jwt_exp(token)
        self._token = token
        self._token_expires_at = float(exp) if exp else time.time() + TOKEN_FALLBACK_LIFETIME
        return token

    def _ensure_token(self) -> str:
        if self._token and time.time() < self._token_expires_at - TOKEN_REFRESH_HEADROOM:
            return self._token
        return self._fetch_token()

    @property
    def token_expires_in(self) -> int:
        """Seconds until the cached token expires. 0 when there is no token."""
        return max(0, int(self._token_expires_at - time.time())) if self._token else 0

    # -- transport ----------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        json_body: dict | None = None,
        params: dict | None = None,
        _retried: bool = False,
    ) -> dict:
        token = self._ensure_token()
        headers = {"Authorization": f"Bearer {token}", "Accept": JSONAPI}
        if json_body is not None:
            headers["Content-Type"] = JSONAPI

        try:
            resp = self.session.request(
                method,
                f"{self.base_url}{path}",
                headers=headers,
                json=json_body,
                params=params,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise SmartSearchError(f"Could not reach SmartSearch: {exc}") from exc

        # A token can expire mid-session; refresh once and replay.
        if resp.status_code == 401 and not _retried:
            self._token = None
            return self._request(method, path, json_body, params, _retried=True)

        return self._handle(resp)

    @staticmethod
    def _handle(resp: requests.Response) -> dict:
        payload = _safe_json(resp)
        if resp.status_code < 400:
            return payload

        errors = payload.get("errors", []) if isinstance(payload, dict) else []
        detail = "; ".join(
            e.get("detail") or e.get("title") or "" for e in errors
        ).strip("; ") or resp.reason

        if resp.status_code == 400:
            raise ValidationError(detail or "The request was rejected.", resp.status_code, errors)
        if resp.status_code in (401, 403):
            raise AuthError(detail or "Not authorised.", resp.status_code, errors)
        if resp.status_code >= 500:
            raise ServiceError(
                detail or "SmartSearch returned a server error.", resp.status_code, errors
            )
        raise SmartSearchError(detail or f"HTTP {resp.status_code}", resp.status_code, errors)

    # -- searches -----------------------------------------------------------

    def search_individual(
        self,
        first: str,
        last: str,
        address: dict,
        dob: str | None = None,
        title: str | None = None,
        middle: str | None = None,
        client_reference: str | None = None,
        telephone: str | None = None,
        national_ids: list[dict] | None = None,
    ) -> SearchResult:
        """Run an International Individual AML search.

        The spec marks only name.first, name.last and address.country required, but the
        live API also rejects a blank date_of_birth, street_1, town or postcode, so all
        of those are required in practice.

        Validation is additionally country-specific. A USA subject, for example, is also
        rejected without address.region, contacts.telephone and national_ids, none of
        which the spec marks required. Pass them when the destination country needs them;
        a 400 names the exact field.

        Note the search returns status="complete" before watchlist screening has finished.
        Call wait_for_matches() before list_matches(), or you will read an empty list.
        """
        name = {"first": first, "last": last}
        if title:
            name["title"] = title
        if middle:
            name["middle"] = middle

        attributes: dict[str, Any] = {
            "name": name,
            "address": {k: v for k, v in address.items() if v},
        }
        if dob:
            attributes["date_of_birth"] = dob
        if client_reference:
            attributes["client_reference"] = client_reference
        if telephone:
            attributes["contacts"] = {"telephone": telephone}
        if national_ids:
            attributes["national_ids"] = national_ids

        payload = self._request(
            "POST",
            "/v3/internationalindividual/searches",
            json_body={"data": {"type": "international-individual", "attributes": attributes}},
            params={"include": "subject,result"},
        )

        data = payload.get("data") or {}
        subject = ((data.get("relationships") or {}).get("subject") or {}).get("data") or {}
        meta = data.get("meta") or {}
        identity = _parse_corroboration(payload)
        if identity is None and data.get("id"):
            identity = _parse_corroboration(self._request(
                "GET", f"/v3/internationalindividual/searches/{data['id']}",
                params={"include": "result"}))
        return SearchResult(
            search_id=data.get("id", ""),
            subject_id=subject.get("id"),
            status=meta.get("status", "unknown"),
            created_at=meta.get("created_at"),
            raw=payload,
            route="international-individual",
            identity=identity,
        )

    def search_uk_individual(
        self,
        first: str,
        last: str,
        addresses: list[dict],
        dob: str,
        title: str,
        middle: str | None = None,
        documents: dict | None = None,
        client_reference: str | None = None,
    ) -> SearchResult:
        """Run a UK Individual AML search: watchlist screening AND identity verification.

        For a UK subject this is a strict superset of the International route. Verified in
        sandbox: it returns the identical 9 watchlist matches for Nicholas Brown, plus a
        credit-reference-agency identity result the International route does not provide.

        Extra requirements over the International route: name.title, and a duration in
        months on every address.

        `documents` may carry any of national_insurance (string), passport
        ({country, expiry, mrz_line_2}) and driving_licence ({number}); each comes back
        verified true/false with structured error codes.

        Bank details are deliberately not sent: this contract rejects them with
        HTTP 400 "Bank checks are not allowed on this contract", so bank_account_match is
        permanently null here.
        """
        name = {"title": title, "first": first, "last": last}
        if middle:
            name["middle"] = middle

        attributes: dict[str, Any] = {
            "name": name,
            "date_of_birth": dob,
            # The UK route is jurisdiction-implicit: passing country on an address gets
            # HTTP 400 "Unexpected field", so it is stripped here rather than at each
            # call site.
            "addresses": [{k: v for k, v in address.items()
                           if v not in (None, "") and k != "country"}
                          for address in addresses],
        }
        if documents:
            attributes["documents"] = documents
        if client_reference:
            attributes["client_reference"] = client_reference

        payload = self._request(
            "POST",
            "/v3/ukindividual/searches",
            json_body={"data": {"type": "uk-individual", "attributes": attributes}},
            params={"include": "subject,result"},
        )

        data = payload.get("data") or {}
        subject = ((data.get("relationships") or {}).get("subject") or {}).get("data") or {}
        meta = data.get("meta") or {}
        identity = _parse_identity(payload)
        if identity is None and data.get("id"):
            # The result is inline on the POST in every sandbox run, but the schema does not
            # promise it, so fall back rather than silently reporting no identity check.
            identity = _parse_identity(self._request(
                "GET", f"/v3/ukindividual/searches/{data['id']}", params={"include": "result"}))

        return SearchResult(
            search_id=data.get("id", ""),
            subject_id=subject.get("id"),
            status=meta.get("status", "unknown"),
            created_at=meta.get("created_at"),
            raw=payload,
            route="uk-individual",
            identity=identity,
        )

    # -- watchlist ----------------------------------------------------------

    def watchlist_summary(self, subject_id: str) -> WatchlistSummary:
        payload = self._request("GET", f"/v3/watchlist/subjects/{subject_id}")
        categories = {}
        for row in payload.get("data") or []:
            attrs = row.get("attributes") or {}
            if attrs.get("category"):
                categories[attrs["category"]] = attrs
        return WatchlistSummary(
            categories=categories,
            is_processing=bool((payload.get("meta") or {}).get("is_processing")),
            raw=payload,
        )

    def list_matches(self, subject_id: str, page: int = 1, size: int = 50):
        """Return (matches, page_meta) for one page of a subject's watchlist matches."""
        payload = self._request(
            "GET",
            f"/v3/watchlist/subjects/{subject_id}/matches",
            params={"page[number]": page, "page[size]": size},
        )
        matches = [_parse_match(row) for row in payload.get("data") or []]
        return matches, (payload.get("meta") or {}).get("page", {})

    def list_all_matches(self, subject_id: str, size: int = 50) -> list[Match]:
        """Page through every match for a subject."""
        matches, page_meta = self.list_matches(subject_id, page=1, size=size)
        total_pages = int(page_meta.get("number_of_pages") or 1)
        for page in range(2, total_pages + 1):
            more, _ = self.list_matches(subject_id, page=page, size=size)
            matches.extend(more)
        return matches

    def get_match(self, match_id: str) -> MatchProfile:
        payload = self._request(
            "GET", f"/v3/watchlist/matches/{match_id}", params={"include": "associations"}
        )
        data = payload.get("data") or {}
        associations = [
            {"id": inc.get("id"), **(inc.get("attributes") or {})}
            for inc in payload.get("included") or []
            if inc.get("type") == "watchlist-association"
        ]
        return MatchProfile(
            id=data.get("id", match_id),
            sections=(data.get("attributes") or {}).get("data") or [],
            meta=data.get("meta") or {},
            associations=associations,
            raw=payload,
        )

    def set_true_match(self, match_id: str, is_true_match: bool) -> bool:
        """Record a true/false classification against a match.

        This is the ONLY resolution write SmartSearch exposes. There is no risk, reason
        or comment field anywhere in the API, so everything else an analyst records has
        to be stored locally (see screening_store.py).

        Verified against the sandbox: real booleans are accepted, the value persists, and
        null is rejected with "You may only set this value to true or false."
        Relationships are omitted deliberately; the spec recommends it to skip a
        validation step.
        """
        payload = self._request(
            "PATCH",
            f"/v3/watchlist/matches/{match_id}",
            json_body={
                "data": {
                    "type": "watchlist-match",
                    "id": match_id,
                    "attributes": {"is_true_match": bool(is_true_match)},
                }
            },
        )
        attrs = (payload.get("data") or {}).get("attributes") or {}
        return bool(attrs.get("is_true_match"))

    def list_true_matches(self, subject_id: str) -> list[Match]:
        """Matches already flagged true at the provider."""
        payload = self._request("GET", f"/v3/watchlist/subjects/{subject_id}/true-matches")
        return [_parse_match(row) for row in payload.get("data") or []]

    def wait_for_matches(self, subject_id: str, timeout: int = 30, interval: float = 2.0) -> bool:
        """Block until the subject's watchlist screening settles.

        Searches have completed synchronously in every sandbox run, but the status enum
        allows pending/processing, so poll rather than assume. Returns False on timeout.
        """
        deadline = time.time() + timeout
        while True:
            if not self.watchlist_summary(subject_id).is_processing:
                return True
            if time.time() >= deadline:
                return False
            time.sleep(interval)


def _parse_match(row: dict) -> Match:
    attrs = row.get("attributes") or {}
    meta = row.get("meta") or {}
    return Match(
        id=row.get("id", ""),
        ref=meta.get("ref"),
        summary=attrs.get("summary") or [],
        categories=[
            c.get("category") for c in (attrs.get("categories") or []) if c.get("category")
        ],
        matched_at=meta.get("matched_at"),
        entry_updated_at=meta.get("entry_updated_at"),
        match_strength=meta.get("match_strength"),
        match_name=meta.get("match_name"),
        match_name_type=meta.get("match_name_type"),
        is_true_match=attrs.get("is_true_match"),
        is_worked=bool(meta.get("is_worked")),
        is_suppressed=bool(meta.get("is_suppressed")),
        notes_count=int(meta.get("notes_count") or 0),
        raw=row,
    )


def _safe_json(resp: requests.Response) -> dict:
    try:
        payload = resp.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {"data": payload}
