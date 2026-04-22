# Rubika Relay CLI

CLI for relaying files through your Rubika Saved Messages.

It currently supports four commands:

- `send`: zip, split, upload, and resume interrupted uploads.
- `list`: list relay files in Saved Messages with pagination.
- `receive`: download relay parts, verify SHA-256, resume, and restore or save the archive.
- `logout`: clear the persisted local session file.

## What it does

1. Authenticates with your Rubika user account (phone + OTP on first run).
2. Persists session to disk and reuses it on later runs.
3. Supports optional HTTP(S)/SOCKS proxy routing for all Rubika traffic.
4. When sending:

- Creates a zip archive.
- Splits large archives into parts (100 MB per part by default, configurable with `--chunk-size`).
- Accepts either a local path or a direct `http(s)` URL as the source.
- Downloads URL sources to a temporary directory and cleans that up after a successful upload.
- Sends parts to Saved Messages with caption metadata and SHA-256.
- Supports parallel uploads (default: 4 workers).
- Persists upload state per file and resumes from the first unsent part on rerun.
- Prints an upload manifest table with `Part`, `File`, `SHA256`, and `Message ID`.
- Automatically removes the local `.relay-state` directory after a fully successful upload.

5. When receiving:

- Finds relay messages by caption tag and groups parts by original file.
- Supports paginated listing of relay files before downloading.
- Downloads parts to a deterministic work directory so interrupted receives can resume.
- Verifies each part hash before assembly.
- Retries hash-mismatched parts with longer backoff before failing the receive.
- Rebuilds the archive and either extracts it to the original file name or saves the raw ZIP with `--skip-unzip`.
- Supports parallel downloads (default: 4 workers).
- Prints a download manifest table with `Part`, `File`, `SHA256`, and `Message ID` for comparison with the upload manifest.
- Deletes verified source messages from Saved Messages unless `--keep` is used.

## Requirements

- Python 3.10+
- A Rubika account

## Install

```bash
python -m pip install .

# Or for development:
python -m pip install -e .
```

## Global Options

All commands support these top-level options:

- `--phone`: phone number for first-time login.
- `--session-name`: session file prefix. Default: `rubika_user`.
- `--data-dir`: base directory for persisted session storage.
- `--proxy`: optional HTTP(S)/SOCKS proxy URL for all Rubika requests.

Environment variables:

- `RUBIKA_RELAY_DATA_DIR`: fallback for `--data-dir`.
- `RUBIKA_RELAY_PROXY`: fallback for `--proxy`.

## Usage

```bash
# Send a file
rubika-relay send /absolute/or/relative/path/to/file.ext
rubika-relay send https://example.com/path/to/file.ext
rubika-relay send --fresh file.ext
rubika-relay send --chunk-size 10mb file.ext
rubika-relay send --with-password file.ext
rubika-relay send --parallel 8 file.ext

# List relay files with pagination
rubika-relay list
rubika-relay list --page 2 --page-size 25

# Receive relay files into the current working directory
rubika-relay receive

# Or receive relay files into a specific folder
rubika-relay receive --output-dir ./downloads
rubika-relay receive --fresh
rubika-relay receive --parallel 8
rubika-relay receive --skip-unzip
rubika-relay receive --keep

# Logout
rubika-relay logout
```

## Command Reference

### `send`

Uploads a local file or direct `http(s)` URL source to Saved Messages.

Flags:

- `--fresh`: discard any existing local send state and rebuild the ZIP/parts from scratch.
- `--chunk-size SIZE`: override the default 100 MB part size. Supports raw bytes or units like `10mb`.
- `--with-password`: create a password-protected ZIP archive before upload.
- `--parallel N`: upload up to `N` parts concurrently. Default: `4`.

Behavior:

- Prints an upload manifest table after success.
- Removes local send state after a fully successful upload.

### `list`

Lists relay files currently discoverable in Saved Messages.

Flags:

- `--page N`: 1-based results page. Default: `1`.
- `--page-size N`: number of listed files per page. Default: `25`.

### `receive`

Downloads relay files from Saved Messages, verifies hashes, and restores the final output.

Flags:

- `--output-dir DIR`: destination directory for restored files or saved ZIP archives.
- `--keep`: do not delete verified source messages after download.
- `--fresh`: discard cached partial receive state under `.relay-recv/` and start clean.
- `--skip-unzip`: save the rebuilt ZIP archive instead of extracting it.
- `--parallel N`: download up to `N` parts concurrently. Default: `4`.

Behavior:

- Resumes from previously verified on-disk parts in `<output-dir>/.relay-recv/...`.
- Retries download-side hash mismatches several times with increasing wait before failing a part.
- Prints a download manifest table after successful verification.

### `logout`

Removes the persisted Rubika session file so the next run requires login again.

## Data locations

- Default base directory: `~/.rubika-relay/`
- Session files: `~/.rubika-relay/sessions/<session-name>.rp`
- Default receive output directory: current working directory
- Per-file send state for uploads: `<source-file-name>.relay-state/` next to the source file
    - Contains encrypted archive, split parts, and `send_state.json`
    - Kept after interruptions/failures so reruns can resume
    - Removed automatically after a fully successful upload
- Per-transfer receive state for downloads: `<output-dir>/.relay-recv/`
    - Contains verified downloaded parts for resume
    - Removed automatically after a fully successful restore or saved archive
    - Removed manually for a fresh receive with `rubika-relay receive --fresh`

If `--data-dir` or `RUBIKA_RELAY_DATA_DIR` is set, session paths are redirected there.
Per-file send state remains next to the source file so resume data stays with that file.

## Notes

- By default, sent archives are not password-protected.
- If the session is valid, OTP is skipped automatically.
- Resume works at part level for both send and receive.
- If a transient error happens mid-part, only that same part is retried.
- Default upload chunk size is 100 MB.
- Default upload/download parallelism is 4 workers.
- `--chunk-size` accepts bytes or units like `10kb`, `10mb`, `10gb`.
- `receive --skip-unzip` writes the assembled archive as a `.zip` instead of extracting it.
- Download-side hash mismatches are retried with longer waits before the receive fails.
- When duplicate uploads exist for the same part, receive prefers the newest message encountered while paging Saved Messages.
