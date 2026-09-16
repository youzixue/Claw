"""Independent research archive. No DB, collectors, production quality gate or approved vendors.

A ReviewedSourcePolicy is executable, reviewed protocol parsing, NOT a manifest
claim. Empty policy is deliberately the production default: existing mutable
tables/source labels alone cannot produce formal evidence.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Mapping
from types import MappingProxyType

MATERIAL_SCHEMA = "promotion_daily_material_v1"
MAX_BYTES = 32 * 1024 * 1024


def clock(value):
    if isinstance(value, str):
        if len(value) < 19 or value[10] not in {"T", " "}:
            return None
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    return value if isinstance(value, datetime) and value.tzinfo is None else None


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def decode(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError("nonfinite JSON")))


@dataclass(frozen=True)
class ReviewedSourcePolicy:
    policy_id: str = "no_reviewed_daily_source_v1"
    # Key source:source_version -> trusted parser of actual raw bytes.
    # Parser must verify finality/universe protocol, not echo user boolean claims.
    validators: Mapping[str, Callable[[bytes], dict]] = field(default_factory=dict)

    def __post_init__(self):
        if not self.policy_id or any(not callable(v) for v in self.validators.values()):
            raise ValueError("reviewed policy requires a version and parser callables")
        object.__setattr__(self, "validators", MappingProxyType(dict(self.validators)))


class MaterialArchive:
    def __init__(self, root, *, policy=None, clock=datetime.now):
        self.root = Path(root).absolute()
        self.policy = policy or ReviewedSourcePolicy()
        self._clock = clock
        self._safe(self.root)

    def now(self):
        result = clock(self._clock())
        if result is None:
            raise ValueError("archive requires naive Asia/Shanghai actual clock")
        return result

    def _safe(self, path):
        for part in (path, *path.parents):
            if part.is_symlink():
                raise ValueError("archive symlink forbidden")
        return path

    def put(self, raw: bytes, *, area="objects"):
        if area not in {"objects", "ready"} or not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_BYTES:
            raise ValueError("invalid archive area or size")
        sha = digest(raw)
        relative = f"{area}/{sha}.blob"
        directory = self._safe(self.root / area)
        directory.mkdir(parents=True, exist_ok=True)
        destination = self._safe(self.root / relative)
        ref = {"path": relative, "sha256": sha, "size": len(raw)}
        fd, temp = tempfile.mkstemp(prefix=".pending-", dir=directory)
        linked = False
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temp, destination)  # atomic no-replace; never overwrite revisions
                linked = True
            except FileExistsError:
                if self.read_bytes(ref) != raw:
                    raise ValueError("archive identity collision")
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except BaseException:
            if linked:
                destination.unlink(missing_ok=True)  # unsuccessful publication is not ready
            raise
        finally:
            os.unlink(temp)
        return ref

    def _path(self, ref):
        if not isinstance(ref, dict) or set(ref) != {"path", "sha256", "size"}:
            raise ValueError("invalid material ref")
        sha, size = ref["sha256"], ref["size"]
        if (not isinstance(sha, str) or not re.fullmatch("[a-f0-9]{64}", sha)
                or isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= MAX_BYTES
                or ref["path"] not in {f"objects/{sha}.blob", f"ready/{sha}.blob"}):
            raise ValueError("invalid ref path/hash/size")
        return self._safe(self.root / ref["path"])

    def read_bytes(self, ref):
        path = self._path(ref)
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size != ref["size"]:
                raise ValueError("material size or type mismatch")
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) != ref["size"] or digest(raw) != ref["sha256"]:
            raise ValueError("material physical SHA mismatch")
        return raw

    def archive(self, raw: bytes, metadata: dict):
        """Seal observed-now bytes, including unverified/retrospective material.

        No caller-supplied receive/archive clocks are used as historical facts.
        Source clocks can only come from a reviewed parser on read().
        """
        at = self.now().isoformat()
        raw_ref = self.put(raw)
        document = {"schema_version": MATERIAL_SCHEMA, "raw_ref": raw_ref,
                    "source": str(metadata.get("source") or ""),
                    "source_version": str(metadata.get("source_version") or ""),
                    "provenance": str(metadata.get("provenance") or "observed_now"),
                    "received_at": at, "observed_at": at, "archive_created_at": self.now().isoformat(),
                    "first_available_at": None}
        # Raw bytes are already durable; envelope publication is checked by read.
        return self.put(encode(document))

    def read(self, ref):
        document = decode(self.read_bytes(ref))
        if not isinstance(document, dict) or document.get("schema_version") != MATERIAL_SCHEMA:
            raise ValueError("material schema mismatch")
        raw = self.read_bytes(document["raw_ref"])
        if not all(isinstance(document.get(k), str) for k in ("source", "source_version")):
            raise ValueError("invalid_source_identity_structure")
        key = document["source"] + ":" + document["source_version"]
        parser = self.policy.validators.get(key)
        reasons = []
        parsed = None
        if document.get("provenance") != "forward_response":
            reasons.append("observed_now_not_forward_evidence")
        if parser is None:
            reasons.append("source_protocol_unreviewed")
        else:
            parsed = parser(raw)
            # Re-encode validates owned JSON and rejects NaN/unsupported payload.
            parsed = decode(encode(parsed))
            if not isinstance(parsed, dict):
                raise ValueError("source parser must return object")
        row_refs = []
        if isinstance(parsed, dict) and isinstance(parsed.get("rows"), list):
            row_refs = [{"code": row.get("code"), "raw_ref": dict(document["raw_ref"]),
                         "row_index": index, "normalized_row_sha256": digest(encode(row))}
                        for index, row in enumerate(parsed["rows"]) if isinstance(row, dict)]
        return {"ref": dict(ref), "manifest": document, "parsed": parsed, "row_refs": row_refs,
                "status": "blocked" if reasons else "verified_protocol",
                "reasons": reasons, "policy_id": self.policy.policy_id}

    def available_at(self, ref):
        # Filesystem publication time is an additional lower bound. Never round
        # down microseconds to make a same-second publication appear earlier.
        return datetime.fromtimestamp(self._path(ref).stat().st_mtime)

    def publish(self, payload):
        ready_ref = self.put(encode(payload))
        published = self.now().isoformat()  # AFTER materialized payload is durable
        receipt = {"schema_version": "promotion_daily_ready_receipt_v1",
                   "ready_ref": ready_ref, "published_at": published,
                   "policy_id": self.policy.policy_id}
        return self.put(encode(receipt), area="ready")
