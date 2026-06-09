"""Avatar catalog — read-through cache for the TikTok Symphony list.

The ``/admin/avatars`` page auto-fetches from TikTok on every load and
writes the result into this cache via ``replace_catalog``. If a
subsequent fetch fails (network blip, 403, endpoint moved), the page
falls back to ``load_catalog`` and shows the last good list with the
error visible — so a transient TikTok failure never blanks the page.

Stored as a JSON array in the settings store under a single key.

Plan: ``_plans/2026-06-09-video-with-avatar-tab.md`` §Auto-fetch cache.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass

from bulkvid.logging import get_logger
from bulkvid.orchestrator.settings_store import SettingsStore

_log = get_logger("avatar_catalog")


SETTING_TIKTOK_AVATAR_CATALOG = "tiktok_avatar_catalog"

# Avatar IDs are short alphanumerics (TikTok format observed: 8-40 chars,
# alphanumerics + dashes + underscores). Cap aggressively to keep one
# bad paste from poisoning the catalog with multi-MB junk.
_ID_MAX_LEN = 64
_NAME_MAX_LEN = 80
_PREVIEW_URL_MAX_LEN = 500
_GENDER_VALUES: frozenset[str] = frozenset({"", "female", "male", "neutral"})
_ID_PATTERN = re.compile(r"^[A-Za-z0-9_\-]+$")


@dataclass(frozen=True)
class CatalogAvatar:
    """One operator-curated avatar entry."""

    avatar_id: str
    name: str
    gender: str           # "" | "female" | "male" | "neutral"
    preview_url: str
    notes: str = ""


def _decode(raw: str) -> list[CatalogAvatar]:
    if not raw.strip():
        return []
    try:
        items = json.loads(raw)
    except json.JSONDecodeError as e:
        _log.warning("avatar_catalog_parse_failed", error=str(e), preview=raw[:80])
        return []
    if not isinstance(items, list):
        return []
    out: list[CatalogAvatar] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        avatar_id = str(it.get("avatar_id") or "").strip()
        if not avatar_id:
            continue
        out.append(
            CatalogAvatar(
                avatar_id=avatar_id,
                name=str(it.get("name") or "").strip(),
                gender=str(it.get("gender") or "").strip().lower(),
                preview_url=str(it.get("preview_url") or "").strip(),
                notes=str(it.get("notes") or "").strip(),
            )
        )
    return out


def _encode(entries: list[CatalogAvatar]) -> str:
    return json.dumps([asdict(e) for e in entries], ensure_ascii=False)


async def load_catalog(store: SettingsStore) -> list[CatalogAvatar]:
    """Return every catalog entry, in insertion order. Empty list on
    first run or after a parse failure (logged)."""
    raw = await store.get(SETTING_TIKTOK_AVATAR_CATALOG, default="")
    return _decode(raw or "")


async def replace_catalog(
    store: SettingsStore,
    entries: list[dict[str, str]],
    *,
    updated_by: str = "auto-fetch",
) -> None:
    """Overwrite the catalog with the given entries. Used as a
    read-through cache: the /admin/avatars page calls TikTok's list
    endpoint and pipes the result here. Invalid entries are silently
    skipped (defensive — a bad TikTok payload shouldn't corrupt the
    cache)."""
    valid: list[CatalogAvatar] = []
    for it in entries:
        avatar_id = str(it.get("avatar_id") or "").strip()
        if not avatar_id or not _ID_PATTERN.match(avatar_id):
            continue
        if len(avatar_id) > _ID_MAX_LEN:
            continue
        valid.append(
            CatalogAvatar(
                avatar_id=avatar_id,
                name=str(it.get("name") or "").strip()[:_NAME_MAX_LEN],
                gender=str(it.get("gender") or "").strip().lower(),
                preview_url=str(it.get("preview_url") or "").strip()[:_PREVIEW_URL_MAX_LEN],
                notes=str(it.get("notes") or "").strip()[:240],
            )
        )
    await store.set(
        SETTING_TIKTOK_AVATAR_CATALOG, _encode(valid), updated_by=updated_by,
    )
    _log.info(
        "avatar_catalog_replaced",
        accepted=len(valid),
        proposed=len(entries),
    )


def _validate_input(
    *, avatar_id: str, name: str, gender: str, preview_url: str,
) -> str | None:
    """Return ``None`` if the input is OK, else a short error string."""
    if not avatar_id:
        return "avatar_id is required"
    if len(avatar_id) > _ID_MAX_LEN:
        return f"avatar_id too long (max {_ID_MAX_LEN} chars)"
    if not _ID_PATTERN.match(avatar_id):
        return "avatar_id must be alphanumeric + dashes/underscores only"
    if len(name) > _NAME_MAX_LEN:
        return f"name too long (max {_NAME_MAX_LEN} chars)"
    if gender not in _GENDER_VALUES:
        return f"gender must be one of {sorted(_GENDER_VALUES)}"
    if preview_url and not preview_url.startswith(("http://", "https://")):
        return "preview_url must start with http:// or https://"
    if len(preview_url) > _PREVIEW_URL_MAX_LEN:
        return f"preview_url too long (max {_PREVIEW_URL_MAX_LEN} chars)"
    return None


async def add_avatar(
    store: SettingsStore,
    *,
    avatar_id: str,
    name: str,
    gender: str,
    preview_url: str,
    notes: str = "",
    updated_by: str = "admin",
) -> str | None:
    """Append a new avatar to the catalog (or update an existing entry
    with the same ``avatar_id``). Returns ``None`` on success, else a
    short error string suitable for showing inline on the admin form."""
    avatar_id = avatar_id.strip()
    name = name.strip()
    gender = gender.strip().lower()
    preview_url = preview_url.strip()
    notes = notes.strip()

    err = _validate_input(
        avatar_id=avatar_id,
        name=name,
        gender=gender,
        preview_url=preview_url,
    )
    if err is not None:
        return err

    entries = await load_catalog(store)
    new = CatalogAvatar(
        avatar_id=avatar_id,
        name=name,
        gender=gender,
        preview_url=preview_url,
        notes=notes[:240],
    )
    # Upsert: same avatar_id replaces in-place to keep ordering stable.
    replaced = False
    for i, existing in enumerate(entries):
        if existing.avatar_id == avatar_id:
            entries[i] = new
            replaced = True
            break
    if not replaced:
        entries.append(new)
    await store.set(
        SETTING_TIKTOK_AVATAR_CATALOG, _encode(entries), updated_by=updated_by
    )
    _log.info(
        "avatar_catalog_upsert",
        avatar_id=avatar_id,
        replaced=replaced,
        total=len(entries),
    )
    return None


async def delete_avatar(
    store: SettingsStore,
    *,
    avatar_id: str,
    updated_by: str = "admin",
) -> bool:
    """Remove the avatar with ``avatar_id`` from the catalog. Returns
    True if anything was removed."""
    avatar_id = avatar_id.strip()
    if not avatar_id:
        return False
    entries = await load_catalog(store)
    new_entries = [e for e in entries if e.avatar_id != avatar_id]
    if len(new_entries) == len(entries):
        return False
    await store.set(
        SETTING_TIKTOK_AVATAR_CATALOG, _encode(new_entries),
        updated_by=updated_by,
    )
    _log.info(
        "avatar_catalog_deleted", avatar_id=avatar_id, total=len(new_entries),
    )
    return True
