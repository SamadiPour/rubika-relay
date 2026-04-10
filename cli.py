from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from relay_cli.auth import login_with_persisted_session
from relay_cli.errors import CliError
from relay_cli.receive import receive_relay_files
from relay_cli.send import send_relay_file

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
SESSION_DIR = DATA_DIR / "sessions"
TMP_DIR = DATA_DIR / "tmp"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rubika file relay CLI.")
    parser.add_argument("--phone", help="Phone number for first-time login.", default=None)
    parser.add_argument("--session-name", default="rubika_user", help="Session name prefix.")

    sub = parser.add_subparsers(dest="command", required=True)

    send_p = sub.add_parser("send", help="Send a file to Saved Messages.")
    send_p.add_argument("file", type=Path, help="Path to the file to send.")

    recv_p = sub.add_parser("receive", help="Download relay files from Saved Messages.")
    recv_p.add_argument("--output-dir", type=Path, default=Path.cwd(), help="Directory to save files (default: CWD).")

    return parser.parse_args()


async def cmd_send(args: argparse.Namespace) -> int:
    client = await login_with_persisted_session(
        session_name=args.session_name,
        session_dir=SESSION_DIR,
        phone_number=args.phone,
    )
    print("Session ready.")

    try:
        message_ids, password = await send_relay_file(client, args.file, TMP_DIR)
        print()
        print(f"Sent {len(message_ids)} part(s) to Saved Messages.")
        print(f"Archive password: {password}")
        return 0
    finally:
        try:
            await client.stop()
        except Exception:
            pass


async def cmd_receive(args: argparse.Namespace) -> int:
    client = await login_with_persisted_session(
        session_name=args.session_name,
        session_dir=SESSION_DIR,
        phone_number=args.phone,
    )
    print("Session ready.")

    try:
        results = await receive_relay_files(client, args.output_dir)
        if results:
            ok = sum(1 for r in results if r["status"] == "ok")
            failed = len(results) - ok
            print()
            print(f"Done. {ok} file(s) verified, {failed} failed.")
        return 0
    finally:
        try:
            await client.stop()
        except Exception:
            pass


def main() -> int:
    args = parse_args()

    try:
        if args.command == "send":
            return asyncio.run(cmd_send(args))
        elif args.command == "receive":
            return asyncio.run(cmd_receive(args))
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
