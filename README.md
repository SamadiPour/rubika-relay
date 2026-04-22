# Rubika Relay CLI

CLI for relaying files through your Rubika Saved Messages.

It supports three operations:

- `send`: zip + split (if needed) + upload file parts.
- `receive`: download relay parts, verify SHA-256, and clean up verified messages.
- `logout`: clear local session file.

## What it does

1. Authenticates with your Rubika user account (phone + OTP on first run).
2. Persists session to disk and reuses it on later runs.
3. When sending:

- Creates a zip archive.
- Splits large archives into parts (500 MB per part by default, configurable with `--chunk-size`).
- Accepts either a local path or a direct `http(s)` URL as the source.
- Sends each part to Saved Messages with caption metadata and SHA-256.
- Persists upload state per file and resumes from the first unsent part on rerun.

4. When receiving:

- Finds relay messages by caption tag and groups parts by original file.
- Downloads all parts to a temporary work directory.
- Verifies each part hash, rebuilds the archive, extracts it, and writes the restored file with its original name.
- Cleans up temporary chunk/zip files after each restore attempt.
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
rubika-relay send https://example.com/path/to/file.ext
rubika-relay send --fresh file.ext
rubika-relay send --chunk-size 10mb file.ext
rubika-relay send --with-password file.ext

# Receive relay files into the current working directory
rubika-relay receive

# Or receive relay files into a specific folder
rubika-relay receive --output-dir ./downloads

# Logout
rubika-relay logout
```

## Data locations

- Default base directory: `~/.rubika-relay/`
- Session files: `~/.rubika-relay/sessions/<session-name>.rp`
- Default receive output directory: current working directory
- Per-file send state for uploads: `<source-file-name>.relay-state/` next to the source file
    - Contains encrypted archive, split parts, and `send_state.json`
    - Removed automatically after a full successful send
    - Kept after interruptions/failures so reruns can resume

If `--data-dir` or `RUBIKA_RELAY_DATA_DIR` is set, session paths are redirected there.
Per-file send state remains next to the source file so resume data stays with that file.

## Notes

- By default, sent archives are not password-protected.
- If the session is valid, OTP is skipped automatically.
- Resume currently works at part level. If a transient error happens mid-part, only that same part is retried.
- Default upload chunk size is 100 MB.
- `--chunk-size` accepts bytes or units like `10kb`, `10mb`, `10gb`.
