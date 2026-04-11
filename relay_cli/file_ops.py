from __future__ import annotations

import hashlib
import secrets
import string
from pathlib import Path

import pyzipper
from pyzipper.zipfile_aes import AESZipInfo

from .config import MAX_PART_SIZE


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def remove_file_safely(file_path: Path) -> None:
    try:
        file_path.unlink(missing_ok=True)
    except OSError:
        pass


def _random_string(length: int, alphabet: str = string.ascii_lowercase + string.digits) -> str:
    return "".join(secrets.choice(alphabet) for _ in range(length))


def create_encrypted_zip(
    source_file: Path,
    output_dir: Path,
    *,
    with_password: bool = False,
) -> tuple[Path, str | None]:
    ensure_dir(output_dir)
    archive_name = _random_string(6)
    zip_path = output_dir / f"{archive_name}.zip"
    password = _random_string(16) if with_password else None

    # Fixed timestamp so the archive hash depends only on file content.
    entry_info = AESZipInfo(source_file.name, date_time=(2020, 1, 1, 0, 0, 0))
    entry_info.compress_type = pyzipper.ZIP_DEFLATED
    file_data = source_file.read_bytes()

    if password:
        with pyzipper.AESZipFile(
            zip_path, "w",
            compression=pyzipper.ZIP_DEFLATED,
            encryption=pyzipper.WZ_AES,
        ) as zf:
            zf.setpassword(password.encode())
            zf.writestr(entry_info, file_data)
    else:
        with pyzipper.AESZipFile(
            zip_path,
            "w",
            compression=pyzipper.ZIP_DEFLATED,
        ) as zf:
            zf.writestr(entry_info, file_data)

    return zip_path, password


def split_file(file_path: Path, max_size: int = MAX_PART_SIZE, *, part_stem: str | None = None) -> list[Path]:
    if max_size <= 0:
        raise ValueError("max_size must be a positive integer")

    file_size = file_path.stat().st_size
    stem = part_stem or file_path.stem

    if file_size <= max_size:
        part_path = file_path.parent / f"{stem}.001"
        file_path.rename(part_path)
        return [part_path]

    # Keep part count constrained by max_size, then balance bytes across parts.
    part_count = (file_size + max_size - 1) // max_size
    balanced_part_size = (file_size + part_count - 1) // part_count

    parts: list[Path] = []
    part_num = 0
    with file_path.open("rb") as fh:
        while True:
            chunk = fh.read(balanced_part_size)
            if not chunk:
                break
            part_num += 1
            part_path = file_path.parent / f"{stem}.{part_num:03d}"
            part_path.write_bytes(chunk)
            parts.append(part_path)

    return parts


def sha256_hash(file_path: Path) -> str:
    h = hashlib.sha256()
    with file_path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
