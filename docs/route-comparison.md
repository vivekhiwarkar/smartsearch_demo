# UK Individual AML vs International Individual AML

Field rosters extracted programmatically from the SmartSearch OpenAPI spec (v3.9.85), then
confirmed by running the same two personas through both routes against the sandbox on
2026-09-07. Every claim below is either a spec roster or a live response.

- `POST /v3/ukindividual/searches` (permission `SEARCH_CREATE` + `SEARCH_UK_INDIVIDUAL`)
- `POST /v3/internationalindividual/searches` (permission `SEARCH_INTERNATIONAL_INDIVIDUAL`)

## 1. Headline

| | UK Individual AML | International Individual AML |
|---|---|---|
| Jurisdiction | UK only | Any country, validated per country |
| Watchlist screening | Yes | Yes, **identical** (proven below) |
| Identity verification | Yes, credit reference agency | Yes, but a different model |
| Identity data source | Experian (this contract), spec also lists Equifax and TransUnion | Global Data Consortium |
| Identity model | Authentication: how confident are we the identity is real | Corroboration: which supplied details can be matched, field by field |
| Risk flags | Deceased, potential fraud alert, PAF | **None** |
| Document verification | NI, passport, driving licence, with reason codes | `national_ids[].result` only |
| Bank verification | In the schema, **rejected by this contract** | Not in the schema |

## 2. Watchlist: provably identical

Same person, both routes, sandbox, same day:

| Persona | UK route | International route | Ref sets identical |
|---|---|---|---|
| Nicholas Brown | 9 matches | 9 matches | **Yes** (`dj-165219` to `dj-165227`) |
| Kate Ward | 0 matches | 0 matches | **Yes** |

Summary labels and categories on the first match were identical too. Only the watchlist
match IDs differ, because each search creates a new `search-subject` and match IDs are
scoped to a subject. The stable identifier across searches is `meta.ref`, the provider's own
entry ID.

The reason is architectural: `/v3/watchlist/*` is not a search service. It is a shared
result surface keyed on the search subject, with its own permissions
(`WATCHLIST_MATCH_READ`, `WATCHLIST_MATCH_UPDATE`), and every search route writes into it.

**So watchlist coverage is not a reason to choose between these routes.**

## 3. Request fields

Union of both rosters, 43 fields. `REQUIRED` is as the spec marks it; see section 6 for
where the live API demands more than the spec says.

| Field | UK | International |
|---|---|---|
| `name` | REQUIRED | REQUIRED |
| `name.first` | REQUIRED | REQUIRED |
| `name.last` | REQUIRED | REQUIRED |
| `name.middle` | optional | optional |
| `name.title` | **REQUIRED** | optional |
| `date_of_birth` | optional (in practice required) | optional (in practice required) |
| `client_reference` | optional | optional |
| `addresses[]` (array) | **REQUIRED** | not present |
| `addresses[].building` | REQUIRED | not present |
| `addresses[].town` | REQUIRED | not present |
| `addresses[].postcode` | REQUIRED | not present |
| `addresses[].duration` (months) | **REQUIRED** | not present |
| `addresses[].flat` / `street_1` / `street_2` / `region` | optional | not present |
| `address` (single object) | not present | **REQUIRED** |
| `address.country` | not accepted, returns HTTP 400 "Unexpected field" | **REQUIRED** |
| `address.building` / `flat` / `street_1` / `street_2` / `town` / `region` / `postcode` | not present | optional |
| `bank` | optional | not present |
| `bank.account_type` / `sortcode` / `account_number` | REQUIRED within bank | not present |
| `bank.roll_number` | optional | not present |
| `documents` | optional | not present |
| `documents.national_insurance` | optional | not present |
| `documents.passport.country` / `.expiry` / `.mrz_line_2` | REQUIRED within passport | not present |
| `documents.driving_licence.number` | REQUIRED within driving_licence | not present |
| `contacts.telephone` | not present | optional |
| `national_ids[].type` / `.number` | not present | optional |

Structural differences worth noting: the UK route takes an **array** of addresses with a
time at each, the International route takes a **single** address plus a country. The UK
route is the only one that accepts documents or bank details. The International route is the
only one that accepts a telephone number or national IDs.

## 4. Identity result fields

Union of both result rosters, 61 fields. **The only shared fields are `outcome` and
`details[].outcome`.** Everything else is disjoint.

| Result field | UK | International |
|---|---|---|
| `outcome` (pass / refer / fail) | yes | yes |
| `details[].outcome` | yes | yes |
| `details[].cra` | yes | - |
| `details[].authentication_index` | yes | - |
| `details[].primary_check_count` / `primary_source_count` | yes | - |
| `details[].collaborative_check_count` / `collaborative_source_count` | yes | - |
| `details[].primary_data_oldest_date` / `collaborative_data_oldest_date` | yes | - |
| `details[].primary_data_date_of_birth_match` / collaborative equivalent | yes | - |
| `details[].name_and_address_match` | yes | - |
| `details[].identity_confirmation_level` | yes | - |
| `details[].paf_check` | yes | - |
| `details[].deceased_check` | yes | - |
| `details[].potential_fraud_alert` | yes | - |
| `details[].bank_account_match` | yes | - |
| `details[].documents.national_insurance` / `.passport` / `.driving_licence` | yes | - |
| `details[].documents_errors.*[].code` / `.message` | yes | - |
| `details[].granular_data[].category` / `.text` / `.source` / `.item` / `.oldest_date` | yes | - |
| `details[].number_of_sources` | - | yes |
| `details[].source_limit_reached` | - | yes |
| `details[].name_full` / `name_first` / `name_middle` / `name_last` / `name_given_initials` | - | yes |
| `details[].name_and_address` | - | yes |
| `details[].date_of_birth_full` / `_day` / `_month` / `_year` | - | yes |
| `details[].address_full` / `_flat` / `_building` / `_street` / `_town` / `_region` / `_postcode` | - | yes |
| `details[].contacts_telephone` | - | yes |
| `details[].national_ids[]` (with `result`) | - | yes |
| `details[].granular_data` (flat, no per-item schema) | - | yes |

The UK fields are **counts and verdicts**. The International fields are **per-field match
grades** (`full` / `partial` / null).

## 5. Same subject, both routes, live

| | Kate Ward (UK CRA + GDC data) | Nicholas Brown (UK CRA only) |
|---|---|---|
| UK outcome | `pass`, Experian, index 80, 8 primary checks / 6 sources | `pass`, Experian, index 80, 4 checks / 3 sources |
| UK deceased / fraud | `deceased_check: true` (passed), `fraud: false` | same |
| International outcome | **`refer`**, 2 sources (Credit, Telco), `name_full: partial` | **`refer`**, **0 sources**, no field matches at all |
| Watchlist | 0 on both routes | 9 on both routes, identical refs |

The important line is the last identity row. **The route changes the identity verdict.** For
a UK subject the International route can return `refer` purely because Global Data
Consortium has thin coverage, while the credit reference agency verifies the same person
cleanly with an index of 80. Nicholas Brown is the extreme case: GDC has nothing on him
whatsoever.

## 6. Where the live API demands more than the spec says

| Route | Spec says required | Live API also rejects without |
|---|---|---|
| Both | name.first, name.last | `date_of_birth` |
| International | `address.country` | `address.street_1`, `address.town`, `address.postcode` |
| International, USA subject | as above | `address.region` (state), `contacts.telephone`, `national_ids`, and a valid US zip |
| UK | name.title, addresses[].building/town/postcode/duration | nothing extra observed |

The UK route additionally **rejects** `addresses[].country` with HTTP 400 "Unexpected field",
because it is jurisdiction-implicit.

## 7. Contract-level limits observed on this sandbox account

| Capability | Status |
|---|---|
| UK identity, Experian | Works |
| UK identity, Equifax / TransUnion | Never returned, even for personas the data pack lists under them |
| UK documents (NI, passport, driving licence) | Work, with reason codes such as `PP016 Passport has expired` |
| UK bank verification | **HTTP 400 "Bank checks are not allowed on this contract"** |
| International identity (GDC) | Works |
| Watchlist read and update | Work |
| Watchlist Only (`POST /v3/watchlist/searches/{id}`) | **HTTP 500**, needs `SEARCH_WATCHLIST_ONLY` |

## 8. Questions worth putting to SmartSearch

1. Is the Equifax and TransUnion absence a contract entitlement, or does a CRA have to be
   requested somehow? The request schema has no CRA selector.
2. Can `bank` be enabled on this contract, and does it materially improve the outcome?
3. For a UK subject, is the International route's thin GDC coverage expected, or is it a
   sandbox data artefact? This decides whether a dual-route strategy is ever worthwhile.
4. Is `match_strength` / `match_name` on watchlist matches populated on a live contract? Both
   are null on every sandbox match, on both routes.
5. Can `SEARCH_WATCHLIST_ONLY` be enabled? It would allow re-screening without re-running an
   identity check.
6. Is there any way to read the note that the `is_true_match` PATCH creates? `notes_count`
   increments but no endpoint or `include` path returns it.
