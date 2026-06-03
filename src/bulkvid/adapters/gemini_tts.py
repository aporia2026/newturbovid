"""Gemini TTS adapter — Vertex AI text-to-speech with multilingual voices.

Uses the official Google Gen AI Python SDK in Vertex AI mode (project
``amit-tts``), targeting ``gemini-2.5-flash-preview-tts`` with
``response_modalities=['audio']``. Voice is selected from a per-language
pool; the response is raw 24 kHz 16-bit mono PCM, which we wrap into a WAV
container in-memory (stdlib ``wave``).

Auth uses application-default credentials from
``GOOGLE_APPLICATION_CREDENTIALS`` (the service account JSON).

Plan: ``_plans/2026-06-02-aporia-bulk-video-tool.md`` §5 (Models), §11 (Cost).
"""

from __future__ import annotations

import io
import wave
from dataclasses import dataclass
from typing import Any

from google import genai
from google.genai import types as gtypes
from google.oauth2 import service_account

from bulkvid.adapters.google_credentials import build_vertex_credentials_info
from bulkvid.config import Settings, get_settings
from bulkvid.logging import get_logger

_log = get_logger("tts")


# ── Pricing ──────────────────────────────────────────────────────────────────
# Verified plan §11 2026-06-02 (Vertex AI Gemini TTS). Refresh before release.
# Placeholder rate; the real bill on the first deploy locks this in.
COST_GEMINI_TTS_PER_CHAR_USD = 0.000_001    # ~$1 per million characters


# ── Audio format constants (Gemini TTS returns 24kHz mono 16-bit PCM) ────────
GEMINI_TTS_SAMPLE_RATE_HZ = 24_000
GEMINI_TTS_CHANNELS = 1
GEMINI_TTS_SAMPLE_WIDTH_BYTES = 2


# ── Voice catalog ────────────────────────────────────────────────────────────
# Gemini 2.5 voices are multilingual — the model auto-detects the input text's
# language and speaks it. The mapping here picks a voice with a tone that
# suits each language; admin panel overrides this in Phase 5.

GEMINI_VOICE_POOL = (
    "Kore", "Aoede", "Charon", "Fenrir", "Leda", "Orus", "Puck", "Zephyr",
)

DEFAULT_VOICE = "Kore"

VOICE_BY_LANGUAGE: dict[str, str] = {
    "en": "Kore",
    "he": "Aoede",
    "ar": "Charon",
    "fr": "Leda",
    "es": "Puck",
    "de": "Zephyr",
    "it": "Orus",
    "pt": "Fenrir",
}


def pick_voice(language: str, override: str | None = None) -> str:
    """Pick a Gemini voice for the given language."""
    if override and override in GEMINI_VOICE_POOL:
        return override
    return VOICE_BY_LANGUAGE.get((language or "").lower(), DEFAULT_VOICE)


# Target-market country -> English accent. The article drives LANGUAGE
# (plan goal #4); the campaign COUNTRY drives the accent/dialect. Gemini TTS
# has no locale parameter, so accent is steered via the prompt.
_ENGLISH_ACCENT_BY_COUNTRY: dict[str, str] = {
    "gb": "British", "uk": "British", "united kingdom": "British",
    "britain": "British", "great britain": "British", "england": "British",
    "scotland": "Scottish", "wales": "Welsh",
    "us": "American", "usa": "American", "united states": "American", "america": "American",
    "au": "Australian", "australia": "Australian",
    "ca": "Canadian", "canada": "Canadian",
    "ie": "Irish", "ireland": "Irish",
    "nz": "New Zealand", "new zealand": "New Zealand",
    "in": "Indian", "india": "Indian",
    "za": "South African", "south africa": "South African",
}


# Common country codes -> full names, so the directive reads "spoken in Poland"
# rather than "spoken in PL". Full-name inputs pass through unchanged.
_COUNTRY_NAME: dict[str, str] = {
    "pl": "Poland", "mx": "Mexico", "es": "Spain", "fr": "France",
    "de": "Germany", "it": "Italy", "pt": "Portugal", "br": "Brazil",
    "il": "Israel", "sa": "Saudi Arabia", "ae": "the UAE", "nl": "the Netherlands",
    "se": "Sweden", "no": "Norway", "dk": "Denmark", "fi": "Finland",
    "tr": "Turkey", "gr": "Greece", "ro": "Romania", "cz": "Czechia",
}


def accent_directive(language: str, country: str) -> str:
    """Return a prompt directive steering the accent to the target market.

    English maps the country to a specific accent (British for the UK, etc.).
    Other languages get a generic "regional dialect of <country>" nudge, with
    country codes expanded to names. Returns "" when there's nothing useful.
    """
    c = (country or "").strip().lower()
    if not c:
        return ""
    if (language or "").lower().startswith("en"):
        accent = _ENGLISH_ACCENT_BY_COUNTRY.get(c)
        return f"Speak in a natural {accent} English accent." if accent else ""
    label = _COUNTRY_NAME.get(c, country.strip())
    return f"Use the natural regional accent and dialect spoken in {label}."


# ── Errors ───────────────────────────────────────────────────────────────────


class GeminiTTSError(RuntimeError):
    """Base class for Gemini TTS errors."""


class GeminiTTSNoAudioError(GeminiTTSError):
    """Response did not include any inline audio data."""


# ── Result ───────────────────────────────────────────────────────────────────


@dataclass
class TTSResult:
    wav_bytes: bytes                  # complete WAV-wrapped audio
    voice: str
    language: str
    duration_seconds: float           # derived from PCM length
    character_count: int
    cost_usd: float


# ── PCM → WAV helper ─────────────────────────────────────────────────────────


def wrap_pcm_to_wav(
    pcm: bytes,
    *,
    sample_rate: int = GEMINI_TTS_SAMPLE_RATE_HZ,
    channels: int = GEMINI_TTS_CHANNELS,
    sample_width: int = GEMINI_TTS_SAMPLE_WIDTH_BYTES,
) -> bytes:
    """Wrap raw PCM bytes into a WAV container (stdlib ``wave``)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


def pcm_duration_seconds(
    pcm: bytes,
    *,
    sample_rate: int = GEMINI_TTS_SAMPLE_RATE_HZ,
    channels: int = GEMINI_TTS_CHANNELS,
    sample_width: int = GEMINI_TTS_SAMPLE_WIDTH_BYTES,
) -> float:
    """Compute audio duration from raw PCM byte length."""
    bytes_per_second = sample_rate * channels * sample_width
    if bytes_per_second == 0:
        return 0.0
    return len(pcm) / bytes_per_second


# ── Client ───────────────────────────────────────────────────────────────────


class GeminiTTSClient:
    """Async Gemini TTS via Vertex AI.

    The underlying ``google.genai.Client`` is constructed lazily so unit tests
    can inject a fake (avoids hitting Vertex AI auth at import time).
    """

    DEFAULT_MODEL = "gemini-2.5-flash-preview-tts"

    def __init__(
        self,
        project: str,
        location: str = "us-central1",
        model: str = DEFAULT_MODEL,
        credentials_info: dict[str, Any] | None = None,
        client: Any | None = None,
    ) -> None:
        if not project:
            raise ValueError("GeminiTTSClient requires a Vertex AI project")
        self._project = project
        self._location = location
        self._model = model
        self._credentials_info = credentials_info
        self._client = client  # injected (tests) or built lazily

    def _ensure_client(self) -> Any:
        if self._client is None:
            kwargs: dict[str, Any] = {
                "vertexai": True,
                "project": self._project,
                "location": self._location,
            }
            # If env-var inline credentials are configured, build a
            # service-account Credentials object and pass it in. Otherwise
            # the SDK falls back to ADC (works when GOOGLE_APPLICATION_CREDENTIALS
            # points at a JSON file on disk).
            if self._credentials_info is not None:
                # Vertex AI uses the cloud-platform scope. Without explicit
                # scopes the auth handshake rejects with "invalid_scope".
                kwargs["credentials"] = (
                    service_account.Credentials.from_service_account_info(
                        self._credentials_info,
                        scopes=["https://www.googleapis.com/auth/cloud-platform"],
                    )
                )
            self._client = genai.Client(**kwargs)
        return self._client

    async def synthesize(
        self,
        text: str,
        language: str,
        voice: str | None = None,
        style_prompt: str | None = None,
        country: str = "",
    ) -> TTSResult:
        """Generate speech audio. Returns a WAV-wrapped ``TTSResult``.

        ``country`` steers the accent to the target market (e.g. UK -> British
        English) — the article still determines ``language``.
        """
        if not text or not text.strip():
            raise ValueError("synthesize requires non-empty text")

        chosen_voice = pick_voice(language, override=voice)
        client = self._ensure_client()

        # Prepend an accent directive (from country) then the per-row style
        # direction, as soft instructions Gemini TTS picks up from the prompt.
        accent = accent_directive(language, country)
        prefix = " ".join(p for p in (accent, (style_prompt or "").strip()) if p)
        prompt_text = f"{prefix}\n\n{text.strip()}" if prefix else text.strip()

        config = gtypes.GenerateContentConfig(
            response_modalities=["audio"],
            speech_config=gtypes.SpeechConfig(
                voice_config=gtypes.VoiceConfig(
                    prebuilt_voice_config=gtypes.PrebuiltVoiceConfig(
                        voice_name=chosen_voice,
                    )
                )
            ),
        )

        _log.info(
            "tts_synthesize",
            model=self._model,
            voice=chosen_voice,
            language=language,
            country=country or "",
            accent=accent or "default",
            text_chars=len(text),
            has_style=bool(style_prompt),
        )

        response = await client.aio.models.generate_content(
            model=self._model,
            contents=prompt_text,
            config=config,
        )

        pcm = _extract_audio_bytes(response)
        if not pcm:
            raise GeminiTTSNoAudioError(
                f"Gemini TTS returned no audio for voice={chosen_voice}"
            )

        wav = wrap_pcm_to_wav(pcm)
        duration = pcm_duration_seconds(pcm)
        char_count = len(text)
        cost = round(char_count * COST_GEMINI_TTS_PER_CHAR_USD, 6)

        _log.info(
            "tts_synthesize_ok",
            voice=chosen_voice,
            language=language,
            duration_seconds=round(duration, 2),
            character_count=char_count,
            cost_usd=cost,
            wav_bytes=len(wav),
        )

        return TTSResult(
            wav_bytes=wav,
            voice=chosen_voice,
            language=language,
            duration_seconds=duration,
            character_count=char_count,
            cost_usd=cost,
        )


# ── Audio extraction (defensive — Gen AI response shape varies) ──────────────


def _extract_audio_bytes(response: Any) -> bytes:
    """Pull the first audio payload out of a generate_content response.

    Gemini returns one or more parts on ``response.candidates[0].content.parts``;
    audio lives in ``part.inline_data.data`` with mime ``audio/L16`` (raw PCM).
    """
    try:
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return b""
        content = getattr(candidates[0], "content", None)
        parts = getattr(content, "parts", None) or []
        for part in parts:
            inline = getattr(part, "inline_data", None)
            if inline is None:
                continue
            data = getattr(inline, "data", None)
            if data:
                return data
    except Exception as e:    # defensive: response shapes drift between SDK versions
        _log.error("tts_extract_audio_error", error=str(e))
    return b""


def build_client_from_settings(settings: Settings | None = None) -> GeminiTTSClient:
    s = settings or get_settings()
    if not s.VERTEX_AI_PROJECT_ID:
        raise ValueError("VERTEX_AI_PROJECT_ID is empty; cannot build GeminiTTSClient")
    return GeminiTTSClient(
        project=s.VERTEX_AI_PROJECT_ID,
        location=s.VERTEX_AI_LOCATION,
        credentials_info=build_vertex_credentials_info(s),
    )
