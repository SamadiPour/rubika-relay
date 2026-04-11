from __future__ import annotations

import hashlib
import secrets
import string
from pathlib import Path

import pyzipper

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

    if password:
        with pyzipper.AESZipFile(
            zip_path, "w",
            compression=pyzipper.ZIP_DEFLATED,
            encryption=pyzipper.WZ_AES,
        ) as zf:
            zf.setpassword(password.encode())
            zf.write(source_file, source_file.name)
    else:
        with pyzipper.AESZipFile(
            zip_path,
            "w",
            compression=pyzipper.ZIP_DEFLATED,
        ) as zf:
            zf.write(source_file, source_file.name)

    return zip_path, password


def split_file(file_path: Path, max_size: int = MAX_PART_SIZE) -> list[Path]:
    file_size = file_path.stat().st_size
    if file_size <= max_size:
        return [file_path]

    parts: list[Path] = []
    part_num = 0
    with file_path.open("rb") as fh:
        while True:
            chunk = fh.read(max_size)
            if not chunk:
                break
            part_num += 1
            part_path = file_path.parent / f"{file_path.stem}.part{part_num:02d}"
            part_path.write_bytes(chunk)
            parts.append(part_path)

    return parts


def sha256_hash(file_path: Path) -> str:
    h = hashlib.sha256()
    with file_path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
