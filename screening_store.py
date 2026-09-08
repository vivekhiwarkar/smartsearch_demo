"""Local persistence for AML screenings: resolutions, decisions and an audit trail.

SmartSearch offers exactly one resolution write across its 105 paths,
`PATCH /v3/watchlist/matches/{id}` with `is_true_match`. There is no risk field, no
reason field, no comment or note endpoint, and no concept of a final decision. So the
record of *why* an analyst classified a match, and what they decided about the subject
overall, has to live here. The provider sync is best-effort on top.

One JSON file per screening under `screenings/`, written atomically.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- vocabulary

STATUSES = ["UNSPECIFIED", "POSITIVE", "POSSIBLE", "FALSE"]
RISKS = ["UNKNOWN", "LOW", "MEDIUM", "HIGH"]
REASONS = [
    "Full Match",
    "Partial Match",
    "Name Match Only",
    "Date of Birth Mismatch",
    "Nationality Mismatch",
    "Citizenship Mismatch",
    "Country Mismatch",
    "Auto-Resolved",
    "Unknown",
    "Other",
]
OUTCOMES = ["ACCEPT", "REJECT", "ESCALATE"]

# Only a true/false classification has an API equivalent. POSSIBLE and UNSPECIFIED are
# analyst states SmartSearch cannot represent, so they are never synced.
STATUS_TO_TRUE_MATCH: dict[str, bool] = {"POSITIVE": True, "FALSE": False}

SCREENED, RESOLVED, DECIDED = "SCREENED", "RESOLVED", "DECIDED"

EVENT_SCREENING_CREATED = "SCREENING_CREATED"
EVENT_SUBJECT_RESCREENED = "SUBJECT_RESCREENED"
EVENT_PROFILE_SNAPSHOTTED = "PROFILE_SNAPSHOTTED"
EVENT_RESOLUTION_RECORDED = "RESOLUTION_RECORDED"
EVENT_DECISION_RECORDED = "DECISION_RECORDED"
EVENT_REPORT_GENERATED = "REPORT_GENERATED"
EVENT_IDENTITY_CHECKED = "IDENTITY_CHECKED"
EVENT_IDENTITY_ACKNOWLEDGED = "IDENTITY_ACKNOWLEDGED"

STORE_DIR = Path(os.getenv("AML_STORE_DIR", "screenings"))
REPORT_DIR = Path(os.getenv("AML_REPORT_DIR", "docs"))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_reviewer() -> str:
    return os.getenv("AML_REVIEWER") or f"{os.getenv('USER', 'analyst')}@localhost"


# --------------------------------------------------------------------------- models


@dataclass
class Resolution:
    status: str = "UNSPECIFIED"
    risk: str = "UNKNOWN"
    reason: str = ""
    comment: str = ""
    resolved_at: str | None = None
    resolved_by: str | None = None
    source: str = "USER"
    # None means "no API equivalent for this status", not "failed".
    synced: bool | None = None
    sync_error: str | None = None

    @property
    def is_resolved(self) -> bool:
        return self.resolved_at is not None


@dataclass
class MatchRecord:
    match_id: str
    ref: str | None = None
    summary: list = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    profile: dict | None = None
    resolution: Resolution = field(default_factory=Resolution)

    @property
    def is_resolved(self) -> bool:
        return self.resolution.is_resolved

    @property
    def has_profile(self) -> bool:
        return self.profile is not None

    @property
    def top_category(self) -> str:
        return min(self.categories, key=len) if self.categories else "Uncategorised"

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> MatchRecord:
        res = Resolution(**(d.get("resolution") or {}))
        return cls(
            match_id=d["match_id"],
            ref=d.get("ref"),
            summary=d.get("summary") or [],
            categories=d.get("categories") or [],
            meta=d.get("meta") or {},
            profile=d.get("profile"),
            resolution=res,
        )

    @classmethod
    def from_match(cls, match) -> MatchRecord:
        """Build from a smartsearch_client.Match."""
        return cls(
            match_id=match.id,
            ref=match.ref,
            summary=match.summary,
            categories=list(match.categories),
            meta=dict(match.raw.get("meta") or {}),
        )


# --------------------------------------------------------------------------- screening


@dataclass
class Screening:
    screening_id: str
    revision: int = 1
    report_version: int = 0
    status: str = SCREENED
    created_at: str = field(default_factory=now_iso)
    created_by: str = field(default_factory=default_reviewer)
    updated_at: str = field(default_factory=now_iso)
    updated_by: str = field(default_factory=default_reviewer)
    subject: dict = field(default_factory=dict)
    search: dict = field(default_factory=dict)
    matches: list[MatchRecord] = field(default_factory=list)
    decision: dict | None = None
    audit: list[dict] = field(default_factory=list)
    # Only the UK route performs identity verification; None on every other route.
    identity: dict | None = None
    identity_acknowledged: bool = False
    store_dir: Path = field(default=STORE_DIR, repr=False)

    # -- lifecycle ----------------------------------------------------------

    @classmethod
    def create(cls, subject: dict, search_result, matches, actor: str | None = None,
               store_dir: Path | None = None) -> Screening:
        actor = actor or default_reviewer()
        screening = cls(
            screening_id=str(uuid.uuid4()),
            created_by=actor,
            updated_by=actor,
            subject=dict(subject),
            search={
                "search_id": search_result.search_id,
                "subject_id": search_result.subject_id,
                "group_id": _group_id(search_result),
                "status": search_result.status,
                "created_at": search_result.created_at,
                "provider_types": ["WATCHLIST"],
                "route": getattr(search_result, "route", "international-individual"),
            },
            matches=[MatchRecord.from_match(m) for m in matches],
            store_dir=store_dir or STORE_DIR,
        )
        if getattr(search_result, "identity", None) is not None:
            screening._set_identity(search_result.identity, search_result.route)
        screening._audit(
            EVENT_SCREENING_CREATED, actor,
            subject_kind="INDIVIDUAL",
            result_count=len(screening.matches),
            search_id=search_result.search_id,
            subject_id=search_result.subject_id,
        )
        screening.save()
        return screening

    @property
    def path(self) -> Path:
        return Path(self.store_dir) / f"{self.screening_id}.json"

    def save(self) -> Path:
        """Atomic write: a crash mid-save must not truncate an existing record."""
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False))
        os.replace(tmp, path)
        return path

    def to_dict(self) -> dict:
        return {
            "screening_id": self.screening_id,
            "revision": self.revision,
            "report_version": self.report_version,
            "status": self.status,
            "created_at": self.created_at,
            "created_by": self.created_by,
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
            "subject": self.subject,
            "search": self.search,
            "matches": [m.to_dict() for m in self.matches],
            "decision": self.decision,
            "identity": self.identity,
            "identity_acknowledged": self.identity_acknowledged,
            "audit": self.audit,
        }

    @classmethod
    def from_dict(cls, d: dict, store_dir: Path | None = None) -> Screening:
        return cls(
            screening_id=d["screening_id"],
            revision=d.get("revision", 1),
            report_version=d.get("report_version", 0),
            status=d.get("status", SCREENED),
            created_at=d.get("created_at", ""),
            created_by=d.get("created_by", ""),
            updated_at=d.get("updated_at", ""),
            updated_by=d.get("updated_by", ""),
            subject=d.get("subject") or {},
            search=d.get("search") or {},
            matches=[MatchRecord.from_dict(m) for m in d.get("matches") or []],
            decision=d.get("decision"),
            identity=d.get("identity"),
            identity_acknowledged=bool(d.get("identity_acknowledged")),
            audit=d.get("audit") or [],
            store_dir=store_dir or STORE_DIR,
        )

    @classmethod
    def load(cls, screening_id: str, store_dir: Path | None = None) -> Screening:
        directory = Path(store_dir or STORE_DIR)
        return cls.from_dict(
            json.loads((directory / f"{screening_id}.json").read_text()), directory
        )

    @classmethod
    def list_saved(cls, store_dir: Path | None = None) -> list[dict]:
        """Lightweight listing for a picker: id, subject name, status, timestamp."""
        directory = Path(store_dir or STORE_DIR)
        rows = []
        for path in sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime,
                           reverse=True):
            try:
                d = json.loads(path.read_text())
            except (ValueError, OSError):
                continue
            rows.append({
                "screening_id": d.get("screening_id"),
                "name": d.get("subject", {}).get("display_name", "Unknown"),
                "status": d.get("status"),
                "updated_at": d.get("updated_at"),
                "matches": len(d.get("matches") or []),
            })
        return rows

    # -- bookkeeping --------------------------------------------------------

    def _bump(self, actor: str) -> None:
        self.revision += 1
        self.updated_at = now_iso()
        self.updated_by = actor

    def _audit(self, event_type: str, actor: str, **payload: Any) -> dict:
        event = {
            "event_id": str(uuid.uuid4()),
            "type": event_type,
            "occurred_at": now_iso(),
            "actor": actor,
            "revision": self.revision,
            **{k: v for k, v in payload.items() if v is not None},
        }
        self.audit.append(event)
        return event

    # -- accessors ----------------------------------------------------------

    def match(self, match_id: str) -> MatchRecord | None:
        return next((m for m in self.matches if m.match_id == match_id), None)

    @property
    def resolved_count(self) -> int:
        return sum(1 for m in self.matches if m.is_resolved)

    @property
    def unresolved_count(self) -> int:
        return len(self.matches) - self.resolved_count

    @property
    def all_resolved(self) -> bool:
        """Gates the final decision. No matches at all still counts as resolved."""
        return self.unresolved_count == 0

    @property
    def category_counts(self) -> dict[str, int]:
        """Headline categories only, so the summary is not swamped by sub-paths."""
        counts: dict[str, int] = {}
        for m in self.matches:
            for category in m.categories:
                if "/" in category:
                    continue
                counts[category] = counts.get(category, 0) + 1
        return dict(sorted(counts.items()))

    @property
    def resolution_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for m in self.matches:
            key = m.resolution.status if m.is_resolved else "UNRESOLVED"
            counts[key] = counts.get(key, 0) + 1
        return dict(sorted(counts.items()))

    # -- mutations ----------------------------------------------------------

    def snapshot_profile(self, match_id: str, profile, actor: str | None = None) -> None:
        """Cache a match's full profile. Needed for the report: primary name, gender and
        sources exist only on the per-match profile call."""
        actor = actor or default_reviewer()
        record = self.match(match_id)
        if record is None:
            return
        record.profile = {
            "sections": profile.sections,
            "meta": profile.meta,
            "associations": profile.associations,
            "snapshotted_at": now_iso(),
        }
        self._bump(actor)
        self._audit(EVENT_PROFILE_SNAPSHOTTED, actor, result_id=match_id)

    def record_resolution(
        self,
        match_ids: list[str],
        status: str,
        risk: str,
        reason: str,
        comment: str = "",
        actor: str | None = None,
        sync_results: dict[str, tuple[bool | None, str | None]] | None = None,
    ) -> dict:
        """Apply one classification to one or more matches, as a single audit event."""
        actor = actor or default_reviewer()
        stamp = now_iso()
        sync_results = sync_results or {}
        applied = []
        for match_id in match_ids:
            record = self.match(match_id)
            if record is None:
                continue
            synced, error = sync_results.get(match_id, (None, None))
            record.resolution = Resolution(
                status=status, risk=risk, reason=reason, comment=comment,
                resolved_at=stamp, resolved_by=actor, source="USER",
                synced=synced, sync_error=error,
            )
            applied.append(match_id)

        self._bump(actor)
        if self.all_resolved and self.status == SCREENED:
            self.status = RESOLVED
        event = self._audit(
            EVENT_RESOLUTION_RECORDED, actor,
            result_count=len(applied),
            match_ids=applied,
            classification=status,
            risk=risk,
            reason=reason,
            synced=sum(1 for mid in applied if sync_results.get(mid, (None,))[0] is True),
            sync_errors=[e for _, e in sync_results.values() if e] or None,
        )
        self.save()
        return event

    def clear_resolution(self, match_ids: list[str], actor: str | None = None) -> None:
        actor = actor or default_reviewer()
        for match_id in match_ids:
            record = self.match(match_id)
            if record is not None:
                record.resolution = Resolution()
        self._bump(actor)
        if not self.all_resolved and self.status == RESOLVED:
            self.status = SCREENED
        self._audit(EVENT_RESOLUTION_RECORDED, actor,
                    result_count=len(match_ids), match_ids=match_ids,
                    classification="CLEARED")
        self.save()

    def rescreen(self, subject: dict, search_result, matches,
                 actor: str | None = None) -> dict:
        """Re-run the screening for an edited subject, keeping this record.

        Resolutions are carried over by provider reference rather than match ID: a new
        search creates new match IDs, but `ref` identifies the same watchlist entry, so
        work already done is not silently thrown away. Anything that no longer matches
        drops out, and any genuinely new hit arrives unresolved.
        """
        actor = actor or default_reviewer()
        carried = {m.ref: m for m in self.matches if m.ref and m.is_resolved}

        self.subject = dict(subject)
        self.search = {
            "search_id": search_result.search_id,
            "subject_id": search_result.subject_id,
            "group_id": _group_id(search_result),
            "status": search_result.status,
            "created_at": search_result.created_at,
            "provider_types": ["WATCHLIST"],
            "route": getattr(search_result, "route", "international-individual"),
        }
        if getattr(search_result, "identity", None) is not None:
            self._set_identity(search_result.identity, search_result.route)
        else:
            self.identity = None
        # A new identity result invalidates any acknowledgement of the previous one.
        self.identity_acknowledged = False
        rebuilt = []
        for match in matches:
            record = MatchRecord.from_match(match)
            previous = carried.get(record.ref)
            if previous is not None:
                record.resolution = previous.resolution
                record.profile = previous.profile
            rebuilt.append(record)
        self.matches = rebuilt

        # A new match set invalidates a decision taken against the old one.
        self.decision = None
        self.status = RESOLVED if self.all_resolved else SCREENED
        self._bump(actor)
        event = self._audit(
            EVENT_SUBJECT_RESCREENED, actor,
            result_count=len(rebuilt),
            carried_resolutions=sum(1 for m in rebuilt if m.is_resolved),
            search_id=search_result.search_id,
            subject_id=search_result.subject_id,
        )
        self.save()
        return event

    # -- identity verification ----------------------------------------------

    def _set_identity(self, result, route: str) -> None:
        """Freeze the identity verdict onto the record.

        needs_attention and alerts are stored rather than recomputed on read: the record is
        a point-in-time audit artefact, and the reviewer acknowledged the verdict as it
        stood when they saw it.
        """
        self.identity = {
            "route": route,
            "kind": getattr(result, "kind", "uk-cra"),
            "outcome": result.outcome,
            "needs_attention": result.needs_attention,
            "alerts": list(result.alerts),
            "checks": [asdict(check) for check in result.checks],
            "checked_at": now_iso(),
            "raw": result.raw,
        }

    def record_identity(self, result, route: str, actor: str | None = None) -> dict:
        actor = actor or default_reviewer()
        self._set_identity(result, route)
        self._bump(actor)
        event = self._audit(
            EVENT_IDENTITY_CHECKED, actor,
            outcome=result.outcome,
            needs_attention=result.needs_attention,
            alerts=list(result.alerts) or None,
        )
        self.save()
        return event

    @property
    def identity_outcome(self) -> str | None:
        return (self.identity or {}).get("outcome")

    @property
    def identity_kind(self) -> str:
        """Records written before the International route was surfaced hold CRA results."""
        return (self.identity or {}).get("kind", "uk-cra")

    @property
    def identity_alerts(self) -> list[str]:
        return list((self.identity or {}).get("alerts") or [])

    @property
    def identity_needs_attention(self) -> bool:
        """False when there is no identity check at all: nothing to acknowledge."""
        return bool((self.identity or {}).get("needs_attention"))

    def acknowledge_identity(self, actor: str | None = None) -> dict:
        actor = actor or default_reviewer()
        self.identity_acknowledged = True
        self._bump(actor)
        event = self._audit(
            EVENT_IDENTITY_ACKNOWLEDGED, actor,
            outcome=self.identity_outcome,
            alerts=self.identity_alerts or None,
        )
        self.save()
        return event

    def record_decision(self, outcome: str, notes: str = "",
                        actor: str | None = None) -> dict:
        """Final decision against the subject. Blocked until every match is resolved."""
        if not self.all_resolved:
            raise ValueError(
                f"{self.unresolved_count} match(es) still unresolved; "
                "every match must be classified before a final decision."
            )
        if outcome not in OUTCOMES:
            raise ValueError(f"Outcome must be one of {OUTCOMES}, got {outcome!r}")
        # An identity check can return outcome "pass" while carrying a deceased flag or a
        # fraud alert (verified in sandbox), so this gate keys on the alerts, not on the
        # outcome. REJECT and ESCALATE are always allowed.
        if outcome == "ACCEPT" and self.identity_needs_attention \
                and not self.identity_acknowledged:
            raise ValueError(
                "Identity verification needs attention ("
                + "; ".join(self.identity_alerts or [self.identity_outcome or "unknown"])
                + "). Acknowledge it before accepting."
            )
        actor = actor or default_reviewer()
        self.decision = {
            "outcome": outcome,
            "reviewer": actor,
            "decided_at": now_iso(),
            "notes": notes,
            "identity_outcome": self.identity_outcome,
            "identity_acknowledged": self.identity_acknowledged,
        }
        self.status = DECIDED
        self._bump(actor)
        event = self._audit(EVENT_DECISION_RECORDED, actor, outcome=outcome)
        self.save()
        return event

    def next_report_path(self, report_dir: Path | None = None) -> Path:
        directory = Path(report_dir or REPORT_DIR)
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"aml-screening-{self.screening_id}-v{self.report_version + 1}.pdf"

    def record_report(self, path: Path, actor: str | None = None) -> dict:
        actor = actor or default_reviewer()
        self.report_version += 1
        self._bump(actor)
        event = self._audit(EVENT_REPORT_GENERATED, actor,
                            report_version=self.report_version, report_path=str(path))
        self.save()
        return event


def _group_id(search_result) -> str | None:
    parent = ((search_result.raw.get("data") or {}).get("relationships") or {}).get("parent")
    return ((parent or {}).get("data") or {}).get("id")


def sync_status_to_provider(client, match_id: str, status: str
                            ) -> tuple[bool | None, str | None]:
    """Best-effort push of a classification to SmartSearch.

    Returns (synced, error). synced is None when the status has no API equivalent,
    which is not a failure. Never raises: a provider problem must not lose the local
    resolution the analyst just recorded.
    """
    if status not in STATUS_TO_TRUE_MATCH:
        return None, None
    try:
        client.set_true_match(match_id, STATUS_TO_TRUE_MATCH[status])
        return True, None
    except Exception as exc:  # noqa: BLE001 - the local record must survive any failure
        return False, str(exc)
