# Rubika Relay CLI

CLI for relaying files through your Rubika Saved Messages.

It supports two operations:

- `send`: zip + encrypt + split (if needed) + upload file parts.
- `receive`: download relay parts, verify SHA-256, and clean up verified messages.

## What it does

1. Authenticates with your Rubika user account (phone + OTP on first run).
2. Persists session to disk and reuses it on later runs.
3. When sending:

- Creates an AES-encrypted zip archive.
- Splits large archives into parts (200 MB per part).
- Sends each part to Saved Messages with caption metadata and SHA-256.

4. When receiving:

- Finds relay messages by caption tag.
- Downloads files to your output directory.
- Verifies each file hash.
- Deletes verified source messages from Saved Messages.

## Requirements

- Python 3.10+
- A Rubika account

## Install

```bash
python -m pip install -r requirements.txt
```

## Usage

Send a file:

```bash
python cli.py send /absolute/or/relative/path/to/file.ext
```

Receive relay files into a folder:

```bash
python cli.py receive --output-dir ./downloads
```

## Data locations

- Session files: `data/sessions/<session-name>.rp`
- Temporary archives/parts: `data/tmp/` (removed automatically after send attempt)

## Notes

- The archive password is printed after a successful `send`; keep it safe.
- If the session is valid, OTP is skipped automatically.
