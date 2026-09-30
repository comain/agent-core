"""Storage for task attachments.

Manual task creation accepts free-form input including images -- a design
sketch, a screenshot of a failure, a photo of a whiteboard. This stores the
bytes and hands back paths the harness can pass to ``opencode run -f``.

Validated live: a PNG attached this way was correctly described by a vision
model through the harness.

Security posture: filenames arrive from a browser upload and are treated as
hostile. Only the basename is kept, it is sanitised to a conservative character
set, and the stored name is prefixed with a content hash -- so a caller cannot
choose where a file lands or overwrite another task's attachment.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Union

#: Types a vision model can read. Anything else is stored but flagged, because
#: silently attaching a .zip to a model wastes a turn.
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_STEM = 60


class AttachmentTooLarge(ValueError):
    """Upload exceeded the configured limit."""


@dataclass(frozen=True)
class Attachment:
    path: Path
    original_name: str
    size: int
    digest: str

    @property
    def is_image(self) -> bool:
        return self.path.suffix.lower() in IMAGE_SUFFIXES


def safe_name(original: str) -> str:
    """Reduce an uploaded filename to something safe to write.

    Path separators and traversal are removed by taking the basename only; the
    result is then restricted to a conservative character set. A name that
    reduces to nothing becomes ``file``.
    """
    base = Path(str(original or "")).name          # strips ../ and any directory
    base = base.replace("\x00", "")
    stem, dot, suffix = base.rpartition(".")
    if not dot:
        stem, suffix = base, ""
    stem = _SAFE.sub("_", stem).strip("._-")[:_MAX_STEM] or "file"
    suffix = _SAFE.sub("", suffix)[:10].lower()
    return f"{stem}.{suffix}" if suffix else stem


class AttachmentStore:
    def __init__(self, root: Union[str, Path], *, max_bytes: int = 20 * 1024 * 1024):
        self.root = Path(root)
        self.max_bytes = max_bytes

    def _dir_for(self, task_ref: str) -> Path:
        # task_ref is also caller-supplied, so it gets the same treatment.
        return self.root / safe_name(task_ref)

    def save(self, *, task_ref: str, filename: str, data: bytes) -> Attachment:
        if len(data) > self.max_bytes:
            raise AttachmentTooLarge(
                f"attachment is {len(data)} bytes, limit is {self.max_bytes}"
            )
        digest = hashlib.sha256(data).hexdigest()[:16]
        target_dir = self._dir_for(task_ref)
        target_dir.mkdir(parents=True, exist_ok=True)
        # Content hash in the stored name: a caller cannot pick the destination,
        # and re-uploading identical bytes is idempotent rather than a clash.
        path = target_dir / f"{digest}-{safe_name(filename)}"
        path.write_bytes(data)
        return Attachment(path=path, original_name=str(filename), size=len(data), digest=digest)

    def list(self, task_ref: str) -> List[Attachment]:
        target_dir = self._dir_for(task_ref)
        if not target_dir.is_dir():
            return []
        out = []
        for path in sorted(target_dir.iterdir()):
            if path.is_file():
                digest = path.name.split("-", 1)[0]
                out.append(Attachment(path=path, original_name=path.name, size=path.stat().st_size, digest=digest))
        return out

    def paths(self, task_ref: str, *, images_only: bool = False) -> List[str]:
        """Paths to hand to the harness as ``attachments=``."""
        return [str(a.path) for a in self.list(task_ref) if a.is_image or not images_only]
