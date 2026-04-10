# Rubika Relay CLI

CLI for relaying files through your Rubika Saved Messages.

It supports three operations:

- `send`: zip + encrypt + split (if needed) + upload file parts.
- `receive`: download relay parts, verify SHA-256, and clean up verified messages.
- `logout`: clear local session file.

## What it does

1. Authenticates with your Rubika user account (phone + OTP on first run).
2. Persists session to disk and reuses it on later runs.
3. When sending:

- Creates an AES-encrypted zip archive.
- Splits large archives into parts (100 MB per part).
- Sends each part to Saved Messages with caption metadata and SHA-256.
- Persists upload state per file and resumes from the first unsent part on rerun.

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
python -m pip install .

# Or for development:
python -m pip install -e .
```

## Usage

```bash
# Send a file
rubika-relay send /absolute/or/relative/path/to/file.ext

# Force a fresh upload state (ignore resume state)
rubika-relay send --fresh /absolute/or/relative/path/to/file.ext

# Receive relay files into a folder
rubika-relay receive --output-dir ./downloads

# Logout
rubika-relay logout
```

## Data locations

- Default base directory: `~/.rubika-relay/`
- Session files: `~/.rubika-relay/sessions/<session-name>.rp`
- Per-file send state for uploads: `<source-file-name>.relay-state/` next to the source file
  - Contains encrypted archive, split parts, and `send_state.json`
  - Removed automatically after a full successful send
  - Kept after interruptions/failures so reruns can resume

If `--data-dir` or `RUBIKA_RELAY_DATA_DIR` is set, session paths are redirected there.
Per-file send state remains next to the source file so resume data stays with that file.

## Notes

- The archive password is printed after a successful `send`; keep it safe.
- If the session is valid, OTP is skipped automatically.
- Resume currently works at part level. If a transient error happens mid-part, only that same part is retried.
