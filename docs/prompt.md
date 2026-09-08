# Task: Build a SmartSearch AML Screening Demo (Streamlit)

## Goal
Build a working **Streamlit + Python** demo that screens an **individual person** against the SmartSearch AML API (sandbox). This is a proof-of-concept for later integration elsewhere — prioritize correctness and clarity over polish, but the UI should be clean and usable.

## Before writing any code
1. Fetch and read the OpenAPI spec: `https://docs.app.smartsearch.com/openapi.json`
2. Identify:
   - The correct **authentication flow** (the sandbox App ID + Secret are in `.env` — figure out whether this is OAuth2 client-credentials, a signed request, an API key header, etc., and implement token refresh if the token expires)
   - The correct **endpoint(s)** for an **individual person search** (as opposed to entity/company search) — there may be a separate "search" call and a "get full profile / detail" call using a match reference/ID
   - **All required and optional request fields** for an individual search (name is given; check if DOB, country, or other fields are required or strongly recommended for match quality)
   - The **response schema** for both the search/match-list call and the detailed profile call
3. Review the demo data pack (`https://docs.app.smartsearch.com/data-pack/`) to get realistic sample names/profiles to test against (including at least one name expected to produce multiple matches, and one expected to produce zero).
4. Summarize your findings (auth method, endpoints, required fields, key response fields) before building, so I can confirm before you proceed.

## Functional Requirements

### Input
- A form to search an individual:
  - Full name (required)
  - Any other fields the API requires or recommends for search (e.g., DOB, country/nationality) — add these as optional inputs with sensible labels
- A "Search" button that calls the API and handles loading/error states

### Results list (one card/row per match)
For each match returned, display:
- Submitted term (what we searched)
- Matched term (what the API matched against)
- Match score
- Provider / source list name
- Nationality
- Residence/country
- Gender
- A **"View full profile"** button/expander per match

### Full profile view (on demand, per match)
When expanded/opened, fetch (if a separate call is needed) and display all available detail, including at least:
- Categories (e.g., PEP, sanctions, adverse media, etc.)
- Sources
- Biography
- Reports / case notes
- Roles / positions held
- Sanctions details (list name, program, dates)
- ID numbers (passport, national ID, etc.)
- Aliases / different names
- Citizenship
- Active status
- Reference number
- Entry updated at (timestamp)
- Matched at (timestamp)

### Explore beyond the spec above
The API response likely includes useful fields not listed above (e.g., date of birth, place of birth, images/photos, related entities/associates, risk score breakdown, list-specific metadata, address history). **Inspect real sandbox responses** and surface anything materially useful that isn't already covered, organized sensibly in the UI (don't just dump raw JSON — though a "raw response" expander for debugging is welcome).

## Technical Requirements
- **Streamlit** app, **Python** backend logic
- Load `SS_APP_ID` / `SS_SECRET` (or whatever the actual `.env` var names are) via `python-dotenv`
- Handle auth token caching/refresh if applicable
- Handle API errors gracefully (no match found, invalid auth, rate limits, malformed input) — show clear messages in the UI, not stack traces
- Keep API client logic separate from UI code (e.g., a small `smartsearch_client.py` module) so it's easy to lift into another project later
- Add a `requirements.txt` and a short `README.md` (how to run, what env vars are needed, known limitations)

## Deliverables
1. Working Streamlit app (`app.py` or similar) + client module
2. `requirements.txt`
3. `README.md`
4. A brief summary of the API research findings (auth, endpoints, fields) before/alongside the build

## Scope note
Individual/person search only for this pass — do not build entity/company search yet.