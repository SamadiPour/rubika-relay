from __future__ import annotations

from pathlib import Path
from typing import Optional

from rubpy import Client
from rubpy import exceptions as rubpy_exceptions

from .errors import CliError
from .file_ops import ensure_dir

AUTH_ERRORS = (
    rubpy_exceptions.InvalidInput,
    rubpy_exceptions.CodeIsExpired,
    rubpy_exceptions.TooRequests,
    rubpy_exceptions.NotRegistered,
    rubpy_exceptions.NoConnection,
    rubpy_exceptions.InvalidAuth,
)


def _normalize_phone(phone: str) -> str:
    stripped = "".join(ch for ch in phone if ch.isdigit())
    if stripped.startswith("0"):
        return f"98{stripped[1:]}"
    if stripped.startswith("98"):
        return stripped
    if stripped.startswith("9") and len(stripped) == 10:
        return f"98{stripped}"
    return stripped


def _validate_phone(phone: str) -> None:
    if len(phone) < 11:
        raise CliError("Phone number format looks invalid.")


async def login_with_persisted_session(
    session_name: str,
    session_dir: Path,
    phone_number: Optional[str],
) -> Client:
    ensure_dir(session_dir)
    session_base = session_dir / session_name
    client = Client(name=str(session_base), display_welcome=False)
    client.name = "Chrome"

    session_file = Path(f"{session_base}.rp")
    effective_phone = _normalize_phone(phone_number) if phone_number else None

    if effective_phone:
        _validate_phone(effective_phone)

    try:
        # If session exists, start() should reuse it and skip OTP.
        if session_file.exists():
            await client.start()
        else:
            if not effective_phone:
                raw_phone = input("Phone number (e.g. 98xxxxxxxxxx): ").strip()
                effective_phone = _normalize_phone(raw_phone)
                _validate_phone(effective_phone)
            await client.start(phone_number=effective_phone)

        await _ensure_client_guid(client)

        return client

    except AUTH_ERRORS as exc:
        await _safe_disconnect(client)
        raise CliError(f"Authentication failed: {exc}") from exc
    except Exception as exc:
        await _safe_disconnect(client)
        raise CliError(f"Unexpected login error: {exc}") from exc


async def _safe_disconnect(client: Client) -> None:
    try:
        await client.stop()
    except Exception:
        pass


async def _ensure_client_guid(client: Client) -> None:
    if getattr(client, "guid", None):
        return

    me = await client.get_me()
    guid = getattr(getattr(me, "user", None), "user_guid", None)
    if not guid:
        raise CliError("Authentication succeeded but user GUID is unavailable.")

    client.guid = str(guid)


def clear_local_session(session_name: str, session_dir: Path) -> bool:
    """Remove the persisted session file for a session name.

    Returns True when a session file existed and was removed.
    """
    ensure_dir(session_dir)
    session_file = session_dir / f"{session_name}.rp"
    existed = session_file.exists()

    if not existed:
        return False

    try:
        session_file.unlink()
    except OSError as exc:
        raise CliError(f"Failed to remove session file: {exc}") from exc

    return True
