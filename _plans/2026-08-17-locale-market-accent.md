# Locale-derived market (accent + script context) from the article URL

Date: 2026-08-17
Status: approved (chat 2026-08-17) — implementing
Follows: the explicit-market safety net in `pipeline/language.py` (chat 2026-06-17)

## Goal

A row whose only market signal is a `locale=` in the article URL must still get
the right accent and the right market context. Today it silently gets neither.
Fix it at a choke point no future tab can bypass.

Example URLs from chat:

```
www.drexur.com/dsr?q=seized%20cars%20ireland&locale=en_IE
www.drexur.com/dsr?q=seized%20cars%20australia&locale=en_AU
www.drexur.com/dsr?q=takavarikoidut%20autot&locale=fi_FI
```

## The bug

`parse_locale_language` throws the region away:

```python
values[0].strip().replace("-", "_").split("_", 1)[0][:2].lower()   # "en_IE" -> "en"
```

So the region (`IE`, `AU`, `CA`, `US`) never reaches anything. Accent is driven
entirely by `row.country`, the sheet's Country column:

  `row.country` -> `accent_directive(language, country)` -> prompt directive

`accent_directive` already knows `ie -> Irish`, `au -> Australian`,
`ca -> Canadian`. The map is fine. It simply never receives a country when the
Country column is blank, and returns `""`.

Net effect for a blank-Country row on `locale=en_IE`:
  * language `en` — correct (the locale fallback in `reconcile_language` works)
  * accent — **none**, so an Irish-market row ships in the default voice
  * script market context — `generate_script(country="")`, so the copy is
    written for nowhere in particular

Language was never the broken part. Region was.

## Approach

Derive the country from the URL locale **once, at row construction**, and only
when the Country column is blank. Every downstream consumer (accent, script
gen, logging) then works unchanged.

### 1. New leaf module `pipeline/market.py`

Explicit-market signal parsing, stdlib-only, no heavy imports. Owns what
`language.py` currently holds plus the two new helpers:

  * `SUPPORTED_LANGUAGES`, `DEFAULT_LANGUAGE`, `COUNTRY_TO_LANGUAGE`
  * `parse_locale_language(url)` — unchanged behavior
  * `parse_locale_region(url)` — **new**; `en_IE` -> `IE`, `pt-BR` -> `BR`,
    `fr` -> `None`, `es_419` -> `None` (not an alpha-2 country)
  * `effective_country(article_url, country)` — **new**; the Country column
    always wins, locale region fills a blank
  * `expected_language(article_url, country)` — moved (pure, belongs here)

It is a separate module so `models/row.py` can import it without dragging in
the OpenAI SDK (`language.py` imports `adapters.openai_client`) and without a
models -> pipeline layering inversion. `language.py` re-exports every moved
name, so existing imports and tests keep working untouched.

### 2. `models/row.py` — the unmissable choke point

A field-less `_MarketRow` mixin whose `__post_init__` fills a blank `country`
from the URL locale. All 14 row dataclasses inherit it.

Verified: a plain mixin contributes no fields, so positional argument order in
every existing `Row(...)` call site is unchanged, and `@dataclass` still calls
the inherited `__post_init__`.

Chosen over the alternatives because it is the only point every construction
path passes through: the 14 `_build_*_row` route builders, `_row_from_payload`
in `queue.py` (worker replay / retries), the local worker, and tests.

### 3. `expected_language` precedence (found during QA)

Filling `country` from the region fed a *derived* value into a function that
assumed an *operator-typed* one, which flipped the language on English
campaigns aimed at non-English markets:

  `locale=en_FI` -> derived country `FI` -> `COUNTRY_TO_LANGUAGE["FI"]` -> `fi`

An English ad for the Finnish market would have gotten a Finnish voiceover —
the exact bug class this change exists to end. Fixed by making a full
`locale=xx_YY` outrank the country map: the locale states the language, the
map only infers it. The Country column still wins when it names a *different*
market than the URL, which is the operator correcting a stale or copied-in URL.

This flips one existing assertion's *signal label* (`("es", "country")` ->
`("es", "locale")` for Country MX + `locale=es_MX`). The language verdict is
identical, so the 2026-06-17 regression it guards still holds; the label only
ever appears in the `language_conflict` log line. The test is updated with the
reasoning, and a new test covers the case where the two genuinely disagree.

### 4. Region parameter coverage

`locale=` plus `gl=` and `country=` (both standard region carriers) so a
differently-shaped URL is still read correctly. Schemeless URLs work —
verified `urlparse("www.drexur.com/dsr?q=a&locale=en_IE")` still yields the
query string.

## Security / safety

Read-only parsing of a URL the operator already pasted. The derived country is
internal: it feeds the accent directive, the script's market context, and logs.
It is **never written back to the sheet**, so the operator's Country column is
never silently rewritten. An explicit Country column always beats the URL, so
this can only fill a gap, never override a deliberate choice. Region values are
validated as two ASCII letters before use, so a hostile `locale=` cannot inject
free text into a prompt.

## QA plan

  * Unit (`tests/unit/test_market_signals.py`): region parsing across
    `en_US`/`en_IE`/`pt-BR`/`fr`/`es_419`/absent/garbage/schemeless; the
    Country-column-wins precedence; every example URL from chat.
  * Regression: `tests/unit/test_language_reconcile.py` must pass untouched
    (proves the re-export and the moved helpers kept their behavior).
  * Row-level: a blank-Country row built with an `en_IE` URL ends up
    `country == "IE"`; a row with `country="US"` and an `en_IE` URL stays `US`.
  * End-to-end assertion that `accent_directive` now returns the Irish
    directive for that row.
  * `en_FI` end to end: country `FI`, language stays `en`, and no bogus
    "Finnish-accented English" directive.
  * Full `pytest`.

## Result

1570 passed, 0 failures. `ruff` clean on every changed file; `mypy` clean on
the changed modules. Two findings confirmed byte-identical on `main` and left
alone as out of scope: one `RUF003` en-dash in a `language.py` comment, and
three pre-existing `mypy` errors in `config.py` / `logging.py`.

## Rejected alternatives

**Patch the 14 `_build_*_row` route builders.** Fixes today's rows and misses
tomorrow's: a new tab is 5 backend spots already, and this would be a 6th thing
to remember. Directly fails the "never happen again with any url" bar.

**Thread `article_url` into `accent_directive`.** Touches 13 row processors at
multiple call sites each, and still leaves the script-gen market context blank.
Large diff, partial fix.

**Write the derived country back to the sheet.** Makes the derivation visible,
but silently edits the operator's data and would fight a deliberate blank.
