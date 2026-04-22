from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path

from relay_cli.auth import clear_local_session, login_with_persisted_session, safe_disconnect
from relay_cli.config import MAX_PART_SIZE, MAX_PARALLEL_DOWNLOADS, MAX_PARALLEL_UPLOADS
from relay_cli.errors import CliError
from relay_cli.receive import receive_relay_files
from relay_cli.send import send_relay_file

ENV_DATA_DIR = "RUBIKA_RELAY_DATA_DIR"

_CHUNK_SIZE_MULTIPLIERS = {
    "k": 1024,
    "kb": 1024,
    "m": 1024 * 1024,
    "mb": 1024 * 1024,
    "g": 1024 * 1024 * 1024,
    "gb": 1024 * 1024 * 1024,
}


def default_data_dir() -> Path:
    # Use a user-level directory so the installed CLI works independently of repo location.
    return Path.home() / ".rubika-relay"


def resolve_data_dir(arg_data_dir: Path | None) -> Path:
    if arg_data_dir is not None:
        return arg_data_dir.expanduser().resolve()

    env_data_dir = os.getenv(ENV_DATA_DIR)
    if env_data_dir:
        return Path(env_data_dir).expanduser().resolve()

    return default_data_dir().resolve()


def parse_chunk_size(value: str) -> int:
    raw = value.strip().lower()
    match = re.fullmatch(r"(\d+)\s*([kmg]b?)?", raw)
    if not match:
        raise argparse.ArgumentTypeError(
            "Invalid chunk size. Use bytes or units like 10kb, 10mb, 10gb."
        )

    amount = int(match.group(1))
    if amount <= 0:
        raise argparse.ArgumentTypeError("Chunk size must be a positive number.")

    unit = match.group(2)
    multiplier = _CHUNK_SIZE_MULTIPLIERS.get(unit, 1)
    return amount * multiplier


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rubika file relay CLI.")
    parser.add_argument("--phone", help="Phone number for first-time login.", default=None)
    parser.add_argument("--session-name", default="rubika_user", help="Session name prefix.")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help=(
            "Base directory for session storage. "
            f"Defaults to ${ENV_DATA_DIR} or {default_data_dir()}."
        ),
    )

    sub = parser.add_subparsers(dest="command", required=True)

    send_p = sub.add_parser("send", help="Send a file to Saved Messages.")
    send_p.add_argument("file", help="Path to the file to send, or a full direct http(s) URL.")
    send_p.add_argument(
        "--fresh",
        action="store_true",
        help="Ignore existing local send state and start upload preparation from scratch.",
    )
    send_p.add_argument(
        "--chunk-size",
        type=parse_chunk_size,
        default=None,
        metavar="SIZE",
        help=(
            "Override upload chunk size (examples: 104857600, 100mb, 1gb). "
            f"Default is {MAX_PART_SIZE // (1024 * 1024)}mb."
        ),
    )
    send_p.add_argument(
        "--with-password",
        action="store_true",
        help="Protect the generated ZIP with a random password (disabled by default).",
    )
    send_p.add_argument(
        "--parallel",
        type=int,
        default=MAX_PARALLEL_UPLOADS,
        metavar="N",
        help=(
            "Number of parts to upload concurrently "
            f"(default: {MAX_PARALLEL_UPLOADS}). Use 1 to disable parallelism."
        ),
    )

    recv_p = sub.add_parser("receive", help="Download relay files from Saved Messages.")
    recv_p.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help=(
            "Directory to save restored files "
            "(default: current working directory)."
        ),
    )
    recv_p.add_argument(
        "--keep",
        action="store_true",
        help="Keep messages in Saved Messages after downloading (do not delete them).",
    )
    recv_p.add_argument(
        "--parallel",
        type=int,
        default=MAX_PARALLEL_DOWNLOADS,
        metavar="N",
        help=(
            "Number of parts to download concurrently "
            f"(default: {MAX_PARALLEL_DOWNLOADS}). Use 1 to disable parallelism."
        ),
    )

    sub.add_parser("logout", help="Clear local session file.")

    return parser.parse_args()


def _session_dir_for(data_dir: Path) -> Path:
    return data_dir / "sessions"


async def cmd_send(args: argparse.Namespace) -> int:
    data_dir = resolve_data_dir(args.data_dir)
    session_dir = _session_dir_for(data_dir)

    client = await login_with_persisted_session(
        session_name=args.session_name,
        session_dir=session_dir,
        phone_number=args.phone,
    )

    try:
        message_ids, password = await send_relay_file(
            client,
            args.file,
            fresh=args.fresh,
            with_password=args.with_password,
            chunk_size=args.chunk_size,
            parallel=args.parallel,
        )
        print()
        print(f"Sent {len(message_ids)} part(s) to Saved Messages.")
        if password:
            print(f"Archive password: {password}")
        else:
            print("Archive password: (none)")
        return 0
    finally:
        await safe_disconnect(client)


async def cmd_receive(args: argparse.Namespace) -> int:
    data_dir = resolve_data_dir(args.data_dir)
    session_dir = _session_dir_for(data_dir)
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir is not None
        else Path.cwd().resolve()
    )

    client = await login_with_persisted_session(
        session_name=args.session_name,
        session_dir=session_dir,
        phone_number=args.phone,
    )

    try:
        results = await receive_relay_files(client, output_dir, keep=args.keep, parallel=args.parallel)
        if results:
            ok = sum(1 for r in results if r["status"] == "ok")
            failed = len(results) - ok
            print()
            print(f"Done. {ok} file(s) verified, {failed} failed.")
        return 0
    finally:
        await safe_disconnect(client)


async def cmd_logout(args: argparse.Namespace) -> int:
    data_dir = resolve_data_dir(args.data_dir)
    session_dir = _session_dir_for(data_dir)

    removed_session = clear_local_session(args.session_name, session_dir)

    print("Local logout completed.")
    if removed_session:
        print(f"Removed session: {args.session_name}.rp")
    else:
        print("No session file found to remove.")

    print("Next command run will require login (OTP) again.")
    return 0


def main() -> int:
    args = parse_args()

    try:
        if args.command == "send":
            return asyncio.run(cmd_send(args))
        if args.command == "receive":
            return asyncio.run(cmd_receive(args))
        if args.command == "logout":
            return asyncio.run(cmd_logout(args))
        return 2
    except KeyboardInterrupt:
        print("Interrupted.")
        return 130
    except CliError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Unexpected error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
