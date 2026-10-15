"""Artifact production and byte-range reading, shared by human and tool callers."""
from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path

from agilecode.application.cancellation import owned_to_thread
from agilecode.core.types import AccessDenied, AgileCodeError, Conflict, now, uid


class ArtifactService:
    def __init__(self, store, root: Path | None = None):
        self.store = store
        self.root = Path(root) if root is not None else store.path.parent / "artifacts"

    async def create(self, ticket_id: str, name: str, *, suffix=".log", identity: str | None = None,
                     content: bytes | None = None) -> dict:
        identity = identity or uid()
        if not suffix.startswith(".") or any(c in suffix for c in "/\\"):
            raise AgileCodeError("Invalid artifact suffix.")
        if not identity or any(c in identity for c in "/\\") or identity in {".", ".."}:
            raise AgileCodeError("Invalid artifact identity.")
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{identity}{suffix}"
        prior = await self.store.one("SELECT * FROM artifacts WHERE id=?", (identity,))
        if prior and (prior["ticket_id"] != ticket_id or prior["name"] != name or Path(prior["path"]) != path):
            raise Conflict("Artifact identity is already bound to different content or ownership.")
        if path.is_symlink():
            raise AccessDenied("Artifact path must not be a symlink.")
        # Deterministic large-result identities can recover after file creation
        # succeeded but its database receipt was lost. Never overwrite different
        # bytes, and never adopt an unknown live producer's output.
        if path.exists():
            if content is None or path.read_bytes() != content:
                raise Conflict("Artifact identity already has different or live producer content.")
        else:
            with path.open("xb") as handle:
                if content is not None:
                    handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        if prior is None:
            await self.store.insert("artifacts", {"id": identity, "ticket_id": ticket_id, "name": name,
                                                   "path": str(path), "size": 0, "complete": 0, "created": now()})
        if content is not None:
            await self.finalize(identity)
        return await self.store.get("artifacts", identity)

    async def finalize(self, artifact_id: str, *, capture_truncated=False) -> dict:
        artifact = await self.store.get("artifacts", artifact_id)
        def stat_and_hash():
            sha = hashlib.sha256()
            size = 0
            with Path(artifact["path"]).open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    size += len(chunk)
                    sha.update(chunk)
            return size, sha.hexdigest()
        size, content_hash = await asyncio.to_thread(stat_and_hash)
        await self.store.update("artifacts", artifact_id, size=size, hash=content_hash, complete=1,
                                capture_truncated=int(capture_truncated or artifact.get("capture_truncated", 0)))
        return await self.store.get("artifacts", artifact_id)

    async def read(self, artifact_id: str, offset=0, limit=100000, *, ticket_id: str | None = None) -> dict:
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise AgileCodeError("Artifact offset must be a nonnegative byte offset.")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise AgileCodeError("Artifact limit must be a positive byte count.")
        artifact = await self.store.get("artifacts", artifact_id)
        if ticket_id is not None and artifact["ticket_id"] != ticket_id:
            raise AccessDenied("Artifact belongs to another ticket.")
        def read():
            with Path(artifact["path"]).open("rb") as handle:
                handle.seek(offset)
                data = handle.read(limit)
                # The observed total refers to the same open file, even if a
                # producer appends after the requested bytes have been read.
                total = os.fstat(handle.fileno()).st_size
            return data, total
        data, total = await owned_to_thread(read)
        remaining = offset + len(data) < total
        capture_truncated = bool(artifact.get("capture_truncated", 0))
        complete = bool(artifact["complete"])
        return {"artifact_id": artifact_id, "content": data.decode(errors="replace"),
                "offset": offset, "bytes": len(data), "total_bytes": total,
                "next_offset": offset + len(data), "remaining": remaining,
                "producer_complete": complete, "capture_truncated": capture_truncated,
                # Existing tool aliases have one meaning for every caller.
                "complete": complete, "partial": remaining or offset > 0 or capture_truncated}
