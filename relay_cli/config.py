"""Relay configuration constants."""

MAX_PART_SIZE = 100 * 1024 * 1024
FILE_CHUNK_SIZE = 1024 * 1024  # 1 MB read buffer for hashing and streaming
MAX_RETRIES = 5
RETRY_BASE_DELAY_SECONDS = 2.0
RETRY_MAX_DELAY_SECONDS = 30.0
RETRY_JITTER_SECONDS = 0.75
RELAY_TAG = "[relay]"
SEND_STATE_FILE = "send_state.json"

# Default number of parts to upload/download concurrently.
MAX_PARALLEL_UPLOADS = 4
MAX_PARALLEL_DOWNLOADS = 4
