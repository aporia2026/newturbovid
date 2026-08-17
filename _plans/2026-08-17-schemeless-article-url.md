# Scheme-less pasted article URLs fail every row

Date: 2026-08-17
Status: approved (chat 2026-08-17) — implementing
Supersedes the wrong diagnosis in `_plans/2026-08-17-locale-market-accent.md`

## Goal

Rows pasted as `www.drexur.com/dsr?q=...&locale=en_US` must produce a video.
Today every one of them fails before a single fetch is attempted.

## The bug

From the sidebar, 6 of 9 rows failed on a `google-simple-motion` job:

```
Row 2  Invalid URL: 'www.drexur.com/dsr?q=pet%20insurance%20for%20older%20dogs&locale=en_US'
Row 5  Invalid URL: 'www.drexur.com/dsr?q=seized%20cars%20ireland&locale=en_IE'
...
```

That string is `adapters/article_fetch.py`:

```python
if not url or not url.startswith(("http://", "https://")):
    raise ArticleFetchError(f"Invalid URL: {url!r}")
```

The operator pastes the URL without a scheme. A browser fills `https://` in
silently, so the cell looks completely correct in the sheet, but every HTTP
client rejects it. The row dies at the first pipeline step, so there is no
article, no script, no TTS and no video.

Verified by reproduction: the scheme-less form raises the exact error above,
and the same URL with `https://` prepended fetches a real 15 KB article.

This is the single rejection point — no processor pre-validates `article_url`,
and `Code.gs` does not check it either. All 12 processors call
`clients.article.fetch(row.article_url)`.

## Why the previous change did not fix it

`_plans/2026-08-17-locale-market-accent.md` fixed a real but *unrelated* gap
(the `locale=` region was discarded, so accents never matched the market). It
was diagnosed from the URLs alone without the operator's actual symptom, which
was "I am not getting any video". Accent quality is irrelevant to a row that
never fetches. That work stands on its own; it just was not this.

Compounding it: that change added a test asserting `urlparse` handles
scheme-less URLs, which proved the *locale parsing* tolerated them while the
fetch guard was rejecting them outright a few frames later.

## Approach

New stdlib-only `pipeline/urls.py` with `normalize_url()`: prefixes `https://`
only when the text before the first `/`, `?` or `#` actually looks like a
hostname (dotted, no whitespace). Free text in the cell stays untouched and
still fails loudly rather than becoming a bogus request. Mirrors the idiom
already in `orchestrator/hrana.py`.

Applied in two places:

  1. `ArticleFetcher.fetch` — the exact site that rejected them, so every tab
     is fixed at once and any direct caller benefits.
  2. `_MarketRow.__post_init__` — so the stored payload, the logs and every
     other consumer see the corrected URL, not just the fetcher. Runs before
     `effective_country`, which is unaffected either way.

## Security / safety

Only ever *adds* a scheme; never rewrites host, path or query, so the `q=` and
`locale=` params that drive the market are preserved byte for byte (asserted).
The hostname-shaped check keeps free text from being turned into an outbound
request, so a junk cell cannot become an unintended fetch. `ftp://` and other
schemes pass through untouched and are still rejected by the existing guard.

## QA plan

  * Unit (`tests/unit/test_url_normalization.py`): all 7 failing URLs from the
    screenshot verbatim; query string preserved exactly; `http`/`https`/other
    schemes untouched; protocol-relative; whitespace; bare domain; free text
    and `None` safe.
  * The guard itself, via `respx` (no network, matching the existing file's
    convention): each failing URL now reaches ScrapingBee with the scheme
    filled in, and junk/empty still raise `Invalid URL`.
  * Regression: `tests/unit/test_article_fetch.py` untouched and passing —
    `not-a-url` (no dot), `""` and `ftp://` are all still rejected.
  * Row level: URL normalized on construction, and the locale-derived country
    from the previous change still works.
  * Full `pytest`.

## Rejected alternatives

**Fix it in `Code.gs` / the sheet.** Would need every operator to re-paste
every URL, and a new sheet or a copied column reintroduces it immediately.

**Normalize only in `fetch()`.** Smallest diff and fixes the outage, but the
scheme-less value still gets stored and logged, so anything reading
`row.article_url` later hits the same class of bug.

**Reject the row with a clearer error message.** Honest, but it makes the
operator do work a single line of code can do correctly every time.
