# SmartSearch API research findings

Verified live against the sandbox on 2026-09-07 with the credentials in `.env`.
Spec: OpenAPI 3.1.0, SmartSearch API `v3.9.85`, JSON:API, 105 paths
(`https://docs.app.smartsearch.com/openapi.json`).

- Sandbox: `https://api.sandbox.app.smartsearch.com` (demo data)
- Live: `https://api.app.smartsearch.com`
- Every request and response uses `Content-Type: application/vnd.api+json`

## Authentication

A bearer-token exchange. Not OAuth2 client-credentials, not an API key header, not HMAC.
`components.securitySchemes` contains exactly one scheme: `{"ClientApi": {"type": "http",
"scheme": "bearer"}}`.

```
POST /v3/auth/token
{"data":{"type":"app-token","attributes":{"app_id":"<SS_APP_ID>","secret":"<SS_SECRET>"}}}
```

- Returns `HTTP 201`. The token is at **`meta.token`**, not in `data` (`data` is `null`).
- It is a JWT with a **900-second** lifetime (`exp - iat = 900`), matching the spec text.
- `account_identifier` is an optional third attribute; not needed here.
- Send it as `Authorization: Bearer <token>` on every other call.
- An expired or invalid token returns `HTTP 401` with
  `{"errors":[{"status":"401","title":"Unauthorized"}]}`.

`smartsearch_client.py` caches the token, decodes `exp` from the JWT, refreshes with 60
seconds of headroom, and replays a request once on a `401`.

## Endpoints: screening is three calls

Watchlist matches hang off the search **subject**, not off the search, so the match list and
the full profile are separate calls. There is no single call that returns everything.

| Step | Call |
|---|---|
| 1. Search | `POST /v3/internationalindividual/searches?include=subject` |
| 2. Match list | `GET /v3/watchlist/subjects/{subjectId}/matches` |
| 3. Full profile | `GET /v3/watchlist/matches/{matchId}?include=associations` |

Supporting calls: `GET /v3/watchlist/subjects/{subjectId}` returns a per-category rollup
(PEP / RCA / SIP / Worldwide Sanctions / totals) plus the `meta.is_processing` flag. The
match list paginates with `page[number]` and `page[size]` (default 50, max 250).

Individual routes are split by jurisdiction: `/v3/ukindividual/*`,
`/v3/internationalindividual/*`, `/v3/usindividual/*`, `/v3/nlindividual/*`. Company
searches are separate (`/v3/ukbusiness/*`, `/v3/internationalbusiness/*`,
`/v3/usbusiness/*`) and out of scope here.

`POST /v3/watchlist/searches/{searchId}` (Watchlist Only, formerly Dow Jones Only) is the
purest name-screening route, but it returns **`HTTP 500`** on this sandbox contract, with a
client-generated UUID and with a full payload. It needs the `SEARCH_WATCHLIST_ONLY`
permission this account appears to lack; returning 500 rather than 403 is a server-side bug.

## Required fields: the spec understates them

The spec's `required` for `international-individual` is only `name.first`, `name.last` and
`address.country`. The live API rejects more than that. Sending just `country`:

```json
{"errors":[
 {"status":"400","title":"Validation error","detail":"This value should not be blank.",
  "source":{"pointer":"/data/attributes/address/street_1"}},
 {"status":"400","title":"Validation error","detail":"This value should not be blank.",
  "source":{"pointer":"/data/attributes/address/postcode"}},
 {"status":"400","title":"Validation error","detail":"This value should not be blank.",
  "source":{"pointer":"/data/attributes/address/town"}}]}
```

Omitting `date_of_birth` gives the same error on `/data/attributes/date_of_birth`. Omitting
the `address` object entirely gives `HTTP 500`.

**Required in practice:** `name.first`, `name.last`, `date_of_birth` (`YYYY-MM-DD`),
`address.street_1`, `address.town`, `address.postcode`, `address.country` (3-letter ISO).

**Optional:** `name.title`, `name.middle`, `client_reference`, `address.flat`,
`address.building`, `address.region`, `national_ids[]`, `contacts.telephone`.

Date of birth is required but it does **not** narrow watchlist matching: Nicholas Brown
returns the same 9 matches with his real DOB (`1968-06-02`) and with `1990-01-01`.

## Screening is asynchronous, and the search does not tell you

This is the easiest thing to get wrong. The search returns `HTTP 201` with
`meta.status: "complete"` in about 1.7 seconds, but watchlist screening is still running at
that point. Reading the match list immediately returns an empty array:

```
dob=1968-06-02 search_status=complete | immediate: is_processing=True matches=0
                                      | after 2.7s: settled=True matches=9
```

The signal is `meta.is_processing` on `GET /v3/watchlist/subjects/{id}`. Poll it until it is
false before reading matches, which is what `SmartSearchClient.wait_for_matches()` does.
Trusting the search's own `complete` status produces silent false negatives, which on an AML
screen is the worst possible failure mode.

## Response shapes

**Search** — `data.id` is the search ID; the subject ID is at
`data.relationships.subject.data.id`; `data.meta.status` carries the search status.

**Match list** — one `watchlist-match` per hit. `attributes.summary` is a `[{label, data}]`
array (Type, Date of Birth, Citizenship, Residency, Images, Active Status),
`attributes.categories` is the risk taxonomy, and `meta` carries `ref`, `matched_at`,
`entry_updated_at`, `is_worked`, `is_suppressed`, `notes_count`, `match_strength`,
`match_name`, `match_name_type`.

**Full profile** — `data.attributes.data` is a **recursive `{label, data[]}` tree**. Leaves
are scalars; anything else is another labelled node. The section labels appear nowhere in
the OpenAPI spec, which documents only `label: string` and `data: array`; they are
discoverable only from live responses. Observed sections:

```
Active Status · Gender · Deceased · Images · Name Aliases · Descriptions · Addresses
Country Associations · Roles · Dates · Birth Place · Sanctions · ID Number Types
Profile Notes · Sources
```

Because that list is data rather than schema, the app walks the tree generically instead of
mapping known fields, so a section we have never seen still renders.

`?include=associations` adds `watchlist-association` entries (relationship, name, prior) to
`included`. The per-associate endpoint
`GET /v3/watchlist/matches/{id}/associate/{associateId}` returns `data: null` in sandbox, so
the `included` block is the only associate data available.

## Fields with no sandbox data

Three fields requested in the brief come back `null` or empty on every match, on both the
International and UK AML routes, so it is not a route artefact:

| Field | API location | Status |
|---|---|---|
| Match score | `meta.match_strength` | `null` on every match |
| Matched term | `meta.match_name`, `meta.match_name_type` | `null` on every match |
| Source list | `Sources` section | Empty array on every profile |

Workarounds used in the app: the provider is derived from the `meta.ref` prefix (`dj-` =
Dow Jones, the only provider seen); the matched name is taken from the profile's
`Name Aliases → Primary Name`; and sanctions list names are read from `Sanctions[].label`
(for example "OFAC - Specially Designated National List"). Match score is shown as
explicitly unavailable rather than faked.

## Identity verification (UK Individual AML)

`POST /v3/ukindividual/searches` performs a credit-reference identity check **and** returns
the same watchlist matches as the International route (verified: the identical 9 matches,
`dj-165219` to `dj-165227`, for Nicholas Brown). For a UK subject it is a strict superset
for one search. The result arrives inline on the POST with `?include=subject,result`.

Extra requirements over the International route: `name.title`, and `duration` in months on
every address. Addresses must **not** carry `country`: the route is jurisdiction-implicit
and returns `HTTP 400 "Unexpected field"` on `addresses.0.country`.

### The flag polarity is not uniform, and getting it wrong is dangerous

`deceased_check` and `paf_check` are "this check passed" booleans. `potential_fraud_alert`
is an "alert raised" boolean. Rendering them with one polarity would print "Deceased: Yes"
for a living subject. Settled against the sandbox data pack's own answer key:

| Persona | Data pack (Experian) | `deceased_check` | `potential_fraud_alert` | **`outcome`** |
|---|---|---|---|---|
| Nicholas Brown | not deceased, no fraud | `true` | `false` | `pass` |
| Phoebe Alexander | **deceased, fraud alert** | `false` | `true` | **`pass`** |

Note the last column. **`outcome` alone is not safe to gate on**: Phoebe Alexander comes
back `pass` with an authentication index of 77 while carrying both a deceased flag and a
fraud alert. Anything that keys on `outcome != "pass"` will wave her through. The app
therefore treats a subject as needing attention when the outcome is not `pass` **or** any
of `deceased_check`/`name_and_address_match`/`identity_confirmation_level`/`paf_check` is
`false`, or `potential_fraud_alert` is `true`, or any document failed.

A subject absent from the data pack (Zebulon Quartermaine) returns `outcome: "refer"` with
`authentication_index: 0`, no primary checks and no evidence, which is the correct answer
for someone who cannot be verified.

### What this contract allows

| Check | Status |
|---|---|
| CRA identity (Experian) | Works. Only Experian is returned, even for personas the data pack lists under Equifax and TransUnion |
| `documents.national_insurance` | Works. `"AB123456C"` returns `true` and adds "National Insurance Number" to the evidence |
| `documents.driving_licence` | Works. Returns `false` with codes, e.g. `DL008 The Driving Licence Decade is Invalid` |
| `documents.passport` | Works. Returns `false` with codes, e.g. `PP016 Passport has expired`, `PP021` DOB mismatch |
| `bank` | **Rejected**: `HTTP 400 "Bank checks are not allowed on this contract."` So `bank_account_match` is permanently null here |

`documents_errors` carries `{code, message}` per document and is worth rendering: it is the
difference between "did not verify" and "the passport has expired".

## Two different identity checks, not one

Both individual routes perform identity verification, but on different data with different
shapes. Neither is the watchlist check.

| | UK Individual AML | International Individual AML |
|---|---|---|
| Result type | `uk-individual-result` | `international-individual-result` |
| Data source | Credit reference agency (Experian here) | Global Data Consortium |
| Model | Authentication: how confident are we the identity is real | Corroboration: which of the details given can be matched |
| Headline | `authentication_index` (0 to 100) | `number_of_sources` plus `source_limit_reached` |
| Per-field detail | Counts of primary/collaborative checks, `granular_data` by category | `full`/`partial`/null per field: name_first, name_last, date_of_birth_full, address_postcode, contacts_telephone, national_ids |
| Risk flags | `deceased_check`, `potential_fraud_alert`, `paf_check` | **None** |
| Document checks | NI, passport, driving licence with reason codes | `national_ids[].result` only |
| Outcome | `pass` / `refer` / `fail` | `pass` / `refer` / `fail` |

Real International result (Christine Fulton, USA):

```json
{"outcome": "refer",
 "details": [{"outcome": "refer", "number_of_sources": 2, "source_limit_reached": true,
   "name_first": "full", "name_last": "full", "name_full": "partial",
   "name_and_address": "partial", "date_of_birth_full": "full",
   "address_full": "full", "address_postcode": "full", "contacts_telephone": "full",
   "national_ids": [{"type": "ssn", "result": "full"}],
   "granular_data": [{"category": "primary", "text": "Credit", "source": 2, "item": 1}]}]}
```

The critical difference for AML purposes: the International route has **no deceased check and
no fraud alert**. A subject can corroborate perfectly on every field and still be deceased or
fraud-flagged without the International route saying so.

## The International route validates per country

The spec's `required` list is jurisdiction-blind, but the API is not. A `GBR` subject needs
`street_1`, `town`, `postcode`, `country` and `date_of_birth`. A `USA` subject is
additionally rejected without:

```json
{"errors": [
 {"source": {"pointer": "address/postcode"}, "detail": "This value must be a valid US Zip code."},
 {"source": {"pointer": "contacts/telephone"}, "detail": "This value should not be blank."},
 {"source": {"pointer": "national_ids"},      "detail": "This value should not be blank."},
 {"source": {"pointer": "address/region"},    "detail": "This value should not be blank."}]}
```

None of `contacts`, `national_ids` or `address.region` is marked required in the spec. The
`source.pointer` on each error names the field precisely, so the practical approach is to
send what you have and map the 400 back onto the form rather than trying to encode every
jurisdiction's rules in advance.

## Recording a resolution

`PATCH /v3/watchlist/matches/{id}` is the **only** resolution write in all 105 paths. It
sets one field:

```json
{"data":{"type":"watchlist-match","id":"<match id>","attributes":{"is_true_match":false}}}
```

Verified live against the sandbox:

| Probe | Result |
|---|---|
| `is_true_match: true` (real boolean) | `HTTP 200`, echoes `true` |
| `is_true_match: "true"` / `"TRUE"` (string) | `HTTP 200`, coerced to `true` |
| `is_true_match: false` | `HTTP 200`, echoes `false` |
| `is_true_match: null` | `HTTP 400` — "You may only set this value to true or false." |
| Persistence | Confirmed: re-reading the match list shows the value |
| `WATCHLIST_MATCH_UPDATE` permission | Granted on this contract |

Match IDs are stable for a given subject (two consecutive reads return identical
`ref -> id` mappings), so a resolution can safely be keyed on the match ID. Across a
*re-screen* the IDs change, but `meta.ref` (the provider's own entry ID, e.g. `dj-165219`)
stays constant, which is what carries resolutions from one screening run to the next.

Relationships should be omitted from the PATCH body; the spec says including them adds a
validation step for no benefit, since they cannot be altered by this endpoint.

`GET /v3/watchlist/subjects/{id}/true-matches` returns only the matches flagged `true`.

### What the API cannot store

Everything else an analyst records has no API equivalent anywhere in the spec:

| Field | API support |
|---|---|
| True/false classification | `is_true_match` |
| Risk level | **None** |
| Reason code | **None** |
| Reviewer comment | **None readable** — see the note below |
| Resolved at / by | **None** |
| Final decision on the subject | **None** |

### Side effects of the PATCH, and a filter trap

Verified by natural experiment on one subject where 8 of 9 matches were PATCHed:

| Field | Before | After |
|---|---|---|
| `meta.is_worked` | `false` | `true` on exactly the 8 patched |
| `meta.notes_count` | `0` | `1` on exactly the 8 patched |
| `meta.is_true_match` | `null` | `false` |

So SmartSearch *does* keep provider-side resolution state, and the PATCH creates a note.
That note is not retrievable: `include=notes`, `note`, `match_notes` all return
`HTTP 400 "Unable to include unknown resource path"`, and no path in the spec exposes note
text. It presumably surfaces in SmartSearch's own web UI.

`meta.is_worked` is genuinely useful as a cross-check on local state, and
`filter[is-worked]` on the match list filters by it. **But the filter takes truthy strings,
not booleans:**

| Value passed | Matches returned (of 9, 8 worked) |
|---|---|
| none | 9 |
| `1` | 8 (worked) |
| `0` | 1 (unworked) |
| `true` | 8 (worked) |
| `false` | **8 (worked)** |

Passing `false` returns the *worked* set, because a non-empty string is truthy server-side.
Use `1` and `0`. On an AML screen, `filter[is-worked]=false` silently returning the already
worked matches is the kind of trap that produces a confidently wrong answer.

This is why the demo keeps a local JSON record per screening and treats the `is_true_match`
PATCH as a best-effort sync on top of it. `POSSIBLE` and `UNSPECIFIED` have no API
equivalent and are never synced.

## Webhooks

`POST /v3/searches/{id}/webhooks` registers an HTTPS callback; `SEARCH_MANAGE_WEBHOOKS` is
present on this contract (`GET` returns HTTP 200 with an empty list). The documented payload:

```json
{"data": {"id": "search_id", "type": "search",
          "attributes": {"client_reference": "...", "search_type": "advanced",
                         "status": "search_status", "status_detail": "..."}},
 "meta": {"created_at": "...", "expires_at": "..."}}
```

Two caveats that decide how to use it:

1. **It reports the search status, not watchlist readiness.** Searches already return
   `status: "complete"` synchronously while `meta.is_processing` on the watchlist subject is
   still true, so a status webhook fires at a point we already know about. It does not on its
   own remove the need to poll `is_processing` before reading matches.
2. It requires a publicly reachable HTTPS URL, so it cannot be demonstrated from localhost
   without a tunnel. `GET /v3/searches/{id}/webhooks` returns `triggered_count` and
   `last_triggered_at`, which proves firing without any inbound reachability.

## Errors

Documented codes: `200 201 202 400 401 403 405 500 503`. There is **no `429`** and no
rate-limit language anywhere in the spec.

- `400` — validation. Each error carries `source.pointer`, which maps straight back onto a
  form field.
- `401` — expired or invalid token. Refresh and replay.
- `500` — used both for genuine faults and for search types the contract lacks permission
  for, so it is not safely retryable-or-not on its own.
- **Zero matches is `HTTP 200` with `data: []`**, a successful search, not an error.

## Test data

`https://docs.app.smartsearch.com/data-pack/personas.json` — 101 personas (75 individuals,
24 businesses, 1 aircraft, 1 vessel). 19 carry `watchlistMatches`; only these names produce
hits. Counts below verified live:

| Persona | DOB | Matches |
|---|---|---|
| Nicholas Brown | 1968-06-02 | 9 (1 PEP, 8 SIP) |
| Boris Johnson | 1964-06-19 | 2 (1 PEP, 1 SIP) |
| Oleg Deripaska | 1974-06-10 | 1, with OFAC SDN + NSDC Ukraine sanctions detail |
| Donald Trump | 1972-08-22 | 1 |
| Zebulon Quartermaine | 1991-04-17 | 0 (not in the data pack) |
