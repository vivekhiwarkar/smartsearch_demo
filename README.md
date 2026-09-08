# SmartSearch AML screening demo

A Streamlit proof-of-concept that screens an **individual person** against the SmartSearch
AML sandbox and takes the result all the way through to a filed document:

**Subject → Review → Resolve → Report**

Verify the subject's identity against a credit reference agency, review every watchlist
match, classify each one with a status, risk, reason and comment, record a final decision
once nothing is outstanding, then generate a PDF report carrying the whole record including
an audit trail.

The API research behind it is in **[`docs/api-findings.md`](docs/api-findings.md)**, verified
against the live sandbox rather than read off the spec. The original brief is in
[`docs/prompt.md`](docs/prompt.md).

## Run it

Requires Python 3.9+ (3.12 recommended).

```bash
uv venv --python 3.12 .venv          # or: python3 -m venv .venv
uv pip install -r requirements.txt   # or: .venv/bin/pip install -r requirements.txt
.venv/bin/streamlit run app.py
```

It opens on <http://localhost:8501>. Pick a demo persona from the sidebar and press
**Run screening**.

## Environment

`.env` in the project root:

| Variable | Required | Purpose |
|---|---|---|
| `SS_APP_ID` | yes | SmartSearch app ID (a UUID) |
| `SS_SECRET` | yes | SmartSearch app secret |
| `SS_BASE_URL` | no | Defaults to the sandbox, `https://api.sandbox.app.smartsearch.com` |
| `AML_REVIEWER` | no | Name recorded against resolutions and decisions. Defaults to `$USER@localhost` |
| `AML_STORE_DIR` | no | Where screening records are written. Defaults to `screenings/` |
| `AML_REPORT_DIR` | no | Where PDFs are written. Defaults to `docs/` |

Nothing is logged or displayed; the sidebar shows only whether credentials are present.

## The workflow

**1. Subject** — name, date of birth and address. All are required by the live API even for
a name screen.

**2. Review** — identity verification first, then every watchlist match with categories,
nationality, residence and an on-demand full profile.

Identity verification runs automatically for a `GBR` subject via the UK Individual AML
route, which returns the same watchlist matches plus a credit-reference check: a
`pass`/`refer`/`fail` outcome, authentication index, name and address match, Postcode
Address File confirmation, deceased and fraud flags, the evidence found (current accounts,
utilities, voters roll) and per-document verification. Any other country falls back to the
International route, which is watchlist only, and the panel says so rather than showing an
empty box.

**3. Resolve** — the resolution queue. Select one or many matches and record:

| Field | Values |
|---|---|
| Status | `UNSPECIFIED`, `POSITIVE`, `POSSIBLE`, `FALSE` |
| Risk | `UNKNOWN`, `LOW`, `MEDIUM`, `HIGH` |
| Reason | Full Match, Partial Match, Name Match Only, DOB/Nationality/Citizenship/Country Mismatch, Auto-Resolved, Unknown, Other |
| Comment | Free text |

`POSITIVE` and `FALSE` are also pushed to SmartSearch as `is_true_match`. `POSSIBLE` and
`UNSPECIFIED` have no API equivalent and stay local. The **Report** step stays locked until
every match is resolved.

**4. Report** — record `ACCEPT` / `REJECT` / `ESCALATE` with notes, then generate the PDF.

If identity verification raised anything, `ACCEPT` is blocked until the reviewer explicitly
acknowledges the alerts, which is captured in the audit trail and the report. `REJECT` and
`ESCALATE` are always available. The gate keys on the alerts, **not** on the outcome: the
API returns `pass` for a subject carrying a deceased flag and a fraud alert, so gating on
the outcome would let exactly that subject through.

## Where things are stored

One JSON file per screening at `screenings/<screening_id>.json`, written atomically. It
holds the subject, every match with its resolution and profile snapshot, the final decision,
and an append-only audit trail. Reports are written to
`docs/aml-screening-<screening_id>-v<n>.pdf`, one version per generation.

Audit events: `SCREENING_CREATED`, `SUBJECT_RESCREENED`, `PROFILE_SNAPSHOTTED`,
`RESOLUTION_RECORDED`, `DECISION_RECORDED`, `REPORT_GENERATED`. Every mutation increments
`revision`.

## How it works

```
POST  /v3/auth/token                        -> bearer JWT, 900s lifetime
POST  /v3/internationalindividual/searches  -> search + subject id
GET   /v3/watchlist/subjects/{id}           -> poll is_processing, risk rollup
GET   /v3/watchlist/subjects/{id}/matches   -> match list
GET   /v3/watchlist/matches/{id}            -> full profile, snapshotted on resolve
PATCH /v3/watchlist/matches/{id}            -> is_true_match, best-effort sync
```

- `smartsearch_client.py` — API client. No Streamlit imports, so it lifts into another
  project as-is. Token caching and refresh, typed errors, pagination, polling.
- `screening_store.py` — resolution vocabulary, audit trail, JSON persistence. No Streamlit
  and no HTTP.
- `report_pdf.py` — ReportLab renderer for the seven-section report.
- `app.py` — the wizard. Makes no HTTP calls of its own.

## Test data

Only the sandbox demo-data-pack personas return matches. The sidebar ships five, all with
counts verified live:

| Persona | Watchlist | Identity |
|---|---|---|
| Nicholas Brown | 9 matches (1 PEP, 8 SIP) | `pass`, index 80 |
| Boris Johnson | 2 matches | `pass` |
| Oleg Deripaska | 1 match, OFAC SDN detail | `pass` |
| **Phoebe Alexander** | **none** | **`pass` but deceased-flagged and fraud-flagged** |
| Carl Fisher | none | fraud alert |
| **Zebulon Quartermaine** | **none** | **`refer`, index 0, cannot be verified** |
| Luis Rodriguez Olivera | 4 matches | not available (USA, International route) |

The two zero-match personas are the point of identity verification: a watchlist-only screen
calls both of them clean.

## Known limitations

- **The API cannot store a resolution.** `PATCH /v3/watchlist/matches/{id}` with
  `is_true_match` is the only resolution write in all 105 paths. There is no risk field, no
  reason field, no readable note (the PATCH does create one server-side, but no endpoint or
  `include` path returns it) and no concept of a final decision. Everything except the true/false flag lives
  in the local JSON store, which is therefore the system of record. Anything built on this
  for real use needs a proper database and a considered retention policy.
- **Match score and matched term are always empty.** `meta.match_strength`,
  `meta.match_name` and `meta.match_name_type` are `null` on every sandbox match, on both
  the International and UK AML routes. They render as "Not provided by sandbox" rather than
  invented values, which is also why the screenshots' Strong/Medium/Weak filter tabs are not
  reproduced: there is no strength to filter on.
- **The `Sources` section is empty** on every profile. Sanctions list names are available
  inside the `Sanctions` section instead.
- **Date of birth is mandatory**, despite the spec marking it optional, and so are
  `street_1`, `town` and `postcode`. A pure name-only screen is not possible on this route.
- **Screening is asynchronous and the search does not say so.** The search returns
  `status: "complete"` while matches are still being written; the client polls
  `meta.is_processing` before reading. Skipping that poll silently returns zero matches.
- **Webhooks are deliberately not implemented here**, deferred to the production codebase.
  `SEARCH_MANAGE_WEBHOOKS` is available on this contract and `POST /v3/searches/{id}/webhooks`
  would register fine, but two things make it wrong for a local POC. It needs a public HTTPS
  URL, which a localhost app does not have. More importantly the callback payload carries
  only the **search** status, and our searches already return `complete` while the watchlist
  side is still processing, so a status webhook fires at a moment we already know about. In
  production it should be paired with the `is_processing` check, not substituted for it.
  `GET /v3/searches/{id}/webhooks` exposes `triggered_count` and `last_triggered_at`, which
  is how firing can be proven without inbound reachability.
- **A report cannot contain the record of its own generation.** `REPORT_GENERATED` is
  appended after the PDF is built, so the audit appendix shows every event up to that point.
  Regenerating produces `v2` with the previous generation included.
- **Re-screening carries resolutions across by provider reference** (`meta.ref`), because a
  new search mints new match IDs. Any match that no longer appears drops out, new hits
  arrive unresolved, and the previous final decision is cleared as it no longer applies to
  the current match set.
- **The two routes run different identity checks.** UK is credit-reference
  authentication (index, check counts, deceased and fraud flags, document verification).
  International is corroboration: a field-by-field match matrix (`name_first: full`,
  `date_of_birth_full: full`, `national_ids: [{ssn, full}]`) with a source count, from Global
  Data Consortium. Both are surfaced. **The International route has no deceased check and no
  fraud alert**, so a non-UK subject can corroborate on every field and still carry risk this
  route cannot see.
- **`refer` is the normal International outcome**, seen on every result including one with
  two sources and sixteen fully graded fields. The reviewer gate therefore fires on
  `number_of_sources == 0` (nothing corroborated at all) rather than on the outcome, which
  would otherwise stop every non-UK subject and become noise.
- **Bank verification is not available on this contract.** The API rejects it with
  `HTTP 400 "Bank checks are not allowed on this contract"`, so `bank_account_match` is
  permanently null. Identity documents (National Insurance, passport, driving licence) do
  work and return reason codes on failure.
- **Only Experian is returned**, even for personas the data pack lists under Equifax and
  TransUnion, so a flag that exists only on another agency will not be seen.
- **The International route validates per country.** A USA subject additionally requires a
  state, a telephone number and a national ID, none of which the spec marks required. The
  form exposes them and maps any 400 back onto the offending field.
- **Individual search only.** Entity/company search is out of scope.
- **Sandbox only by default.** Point `SS_BASE_URL` at the live host to switch; live searches
  are billable.
- No rate limiting is documented (no `429` in the spec), so the client does not implement
  backoff. Add it before any bulk use.
- Every search consumes a sandbox search and creates a new subject; there is no cleanup.
