"""
VendorIQ Secure File Storage Utility.

Handles secure storage, file extension validation, size boundary checking,
path traversal defense, and SHA-256 integrity hashing for uploaded quotations.
"""

import hashlib
import os
import re
from pathlib import Path
from typing import Optional, Tuple, Union

ALLOWED_EXTENSIONS = {".pdf", ".xlsx", ".xls"}
MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB

# Magic byte signatures
MAGIC_BYTES = {
    ".pdf": b"%PDF-",
    ".xlsx": b"PK\x03\x04",
    ".xls": b"\xd0\xcf\x11\xe0",
}


def sanitize_filename(filename: str) -> str:
    """
    Sanitize an uploaded filename to strictly prevent directory traversal attacks.
    Removes path separators, null bytes, parent directory tokens (..),
    and non-standard control characters.
    """
    if not filename or not filename.strip():
        return "unnamed_quotation"

    # Normalize slashes and take basename
    clean = filename.replace("\\", "/").strip()
    clean = os.path.basename(clean)

    # Remove null bytes and traversal sequences
    clean = clean.replace("\x00", "").replace("..", "")

    # Keep only safe alphanumeric characters, dashes, underscores, and dots
    clean = re.sub(r"[^a-zA-Z0-9_\-\.]", "_", clean)

    # Collapse repeated underscores
    clean = re.sub(r"_+", "_", clean).strip("._")

    return clean if clean else "unnamed_quotation"


def compute_sha256(content: Union[bytes, str, Path]) -> str:
    """
    Compute SHA-256 cryptographic hash of file content or local file path.
    """
    hasher = hashlib.sha256()
    if isinstance(content, (str, Path)):
        with open(content, "rb") as f:
            while chunk := f.read(65536):
                hasher.update(chunk)
    elif isinstance(content, bytes):
        hasher.update(content)
    else:
        raise ValueError(f"Unsupported content type for SHA-256 computation: {type(content)}")
    return hasher.hexdigest().lower()


def validate_quotation_file(
    filename: str,
    content_bytes: bytes,
    max_size_bytes: int = MAX_FILE_SIZE_BYTES,
) -> Tuple[bool, str]:
    """
    Validate uploaded quotation file extension, size boundary, and magic signature.
    Returns (is_valid: bool, error_message: str).
    """
    if not content_bytes or len(content_bytes) == 0:
        return False, "Uploaded file is empty (0 bytes)."

    if len(content_bytes) > max_size_bytes:
        mb = len(content_bytes) / (1024 * 1024)
        max_mb = max_size_bytes / (1024 * 1024)
        return False, f"File exceeds maximum allowed size of {max_mb:.1f} MB (uploaded: {mb:.2f} MB)."

    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        allowed_str = ", ".join(sorted(ALLOWED_EXTENSIONS))
        return False, f"Unsupported file type '{ext}'. Allowed quotation formats: {allowed_str}."

    # Verify magic signature
    magic = MAGIC_BYTES.get(ext)
    if magic and not content_bytes.startswith(magic):
        return False, f"File content header does not match declared file extension '{ext}'."

    return True, ""


def save_quotation_file(
    brief_id: str,
    supplier_id: str,
    filename: str,
    content_bytes: bytes,
    base_dir: Optional[str] = None,
) -> Tuple[str, str]:
    """
    Securely store an uploaded supplier quotation document on the local filesystem.

    Directory structure:
      <base_dir>/uploads/quotations/<brief_id>/<supplier_id>/<sanitized_filename>

    Guarantees:
      - Path traversal protection (verifies path cannot escape uploads root).
      - Strict validation of file size and extension before writing.
      - Generates SHA-256 hash for document integrity.

    Returns:
      (saved_relative_path, sha256_hash)
    """
    is_valid, err_msg = validate_quotation_file(filename, content_bytes)
    if not is_valid:
        raise ValueError(f"Quotation file validation failed: {err_msg}")

    # Compute SHA-256
    file_hash = compute_sha256(content_bytes)

    # Base directory
    root_dir = Path(base_dir).resolve() if base_dir else Path.cwd().resolve()
    uploads_root = (root_dir / "uploads" / "quotations").resolve()

    # Sanitize components
    safe_brief = sanitize_filename(brief_id)
    safe_supp = sanitize_filename(supplier_id)
    safe_filename = sanitize_filename(filename)

    target_dir = (uploads_root / safe_brief / safe_supp).resolve()

    # Ensure target directory is strictly within uploads_root
    try:
        target_dir.relative_to(uploads_root)
    except ValueError as e:
        raise PermissionError(f"Security Violation: Target path escapes uploads directory: {target_dir}") from e

    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = (target_dir / safe_filename).resolve()

    # Verify final path does not escape
    try:
        target_path.relative_to(uploads_root)
    except ValueError as e:
        raise PermissionError(f"Security Violation: Target file escapes uploads directory: {target_path}") from e

    # Write file
    target_path.write_bytes(content_bytes)

    # Return relative path from root_dir for database storage
    relative_path = str(target_path.relative_to(root_dir)).replace("\\", "/")
    return relative_path, file_hash
