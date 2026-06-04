"""Tests for the per-row sensitive-apparel safety detector."""

from __future__ import annotations

import pytest

from bulkvid.pipeline.safety import (
    SAFE,
    SafetyContext,
    append_safety_block,
    detect_sensitive_apparel,
    parse_keywords,
)


# ── parse_keywords ───────────────────────────────────────────────────────────


def test_parse_keywords_handles_commas() -> None:
    assert parse_keywords("bra, panties, lingerie") == ("bra", "panties", "lingerie")


def test_parse_keywords_handles_newlines_and_semicolons() -> None:
    blob = "bra\npanties;lingerie,\n swimwear "
    assert parse_keywords(blob) == ("bra", "panties", "lingerie", "swimwear")


def test_parse_keywords_lowercases_and_trims() -> None:
    assert parse_keywords("  Lingerie ,  BRA ") == ("lingerie", "bra")


def test_parse_keywords_drops_empty_entries() -> None:
    assert parse_keywords(", ,bra,,,lingerie,") == ("bra", "lingerie")


def test_parse_keywords_empty_blob() -> None:
    assert parse_keywords("") == ()
    assert parse_keywords("   ") == ()


# ── detect_sensitive_apparel ─────────────────────────────────────────────────


KW = ("bra", "panties", "lingerie", "swimwear", "intimate apparel")


def test_detect_exact_match() -> None:
    result = detect_sensitive_apparel("lingerie", KW)
    assert result.matched is True
    assert result.matched_keyword == "lingerie"


def test_detect_partial_substring_match() -> None:
    result = detect_sensitive_apparel("Lingerie Boutique", KW)
    assert result.matched is True
    assert result.matched_keyword == "lingerie"


def test_detect_case_insensitive() -> None:
    result = detect_sensitive_apparel("LINGERIE", KW)
    assert result.matched is True


def test_detect_multi_word_keyword() -> None:
    result = detect_sensitive_apparel("luxury intimate apparel store", KW)
    assert result.matched is True
    assert result.matched_keyword == "intimate apparel"


def test_detect_no_match() -> None:
    result = detect_sensitive_apparel("Smart home gadgets", KW)
    assert result.matched is False
    assert result.matched_keyword is None


def test_detect_empty_vertical() -> None:
    assert detect_sensitive_apparel("", KW) == SAFE
    assert detect_sensitive_apparel("   ", KW) == SAFE
    assert detect_sensitive_apparel(None, KW) == SAFE  # type: ignore[arg-type]


def test_detect_returns_first_matching_keyword() -> None:
    # 'lingerie' appears in the list first; even though 'bra' also matches the
    # text, we report the first found.
    text = "lingerie bra shop"
    result = detect_sensitive_apparel(text, ("bra", "lingerie"))
    assert result.matched is True
    assert result.matched_keyword == "bra"      # iteration order of keywords


def test_detect_empty_keyword_list() -> None:
    assert detect_sensitive_apparel("anything", ()) == SAFE


def test_detect_skips_blank_keywords() -> None:
    # A misconfigured keyword list with blanks must not match every vertical.
    assert detect_sensitive_apparel("anything", ("", "  ", "bra")) == SAFE
    assert detect_sensitive_apparel("bra shop", ("", "bra")).matched is True


# ── append_safety_block ──────────────────────────────────────────────────────


def test_append_safety_block_when_matched() -> None:
    prompt = "Original system prompt."
    safety = SafetyContext(matched=True, matched_keyword="bra")
    result = append_safety_block(prompt, safety, "SAFETY RULES HERE")
    assert "Original system prompt." in result
    assert "SAFETY RULES HERE" in result
    assert result.index("Original") < result.index("SAFETY")


def test_append_safety_block_skipped_when_unmatched() -> None:
    prompt = "Original system prompt."
    result = append_safety_block(prompt, SAFE, "SAFETY RULES HERE")
    assert result == prompt


def test_append_safety_block_skipped_when_block_blank() -> None:
    safety = SafetyContext(matched=True, matched_keyword="bra")
    assert append_safety_block("hi", safety, "") == "hi"
    assert append_safety_block("hi", safety, "   ") == "hi"


def test_append_safety_block_has_visible_separator() -> None:
    safety = SafetyContext(matched=True, matched_keyword="bra")
    result = append_safety_block("A", safety, "B")
    # A clear visual divider keeps the model from blending the original prompt
    # into the safety rules.
    assert "—————" in result or "\n\n" in result
