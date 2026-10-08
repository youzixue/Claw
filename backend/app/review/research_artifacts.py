"""Bounded readers for already-published research files. Never publish or replay."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[3]
ARTIFACT_ROOT = ROOT / "outputs" / "paper_research"
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
IDENTITY = re.compile(r"paper-research-(\d{8}T\d{12})-([a-f0-9]{16})\.json")


def artifact_catalog(day, *, as_of, root=None):
    directory = Path(root or ARTIFACT_ROOT).resolve()
    candidates = sorted(directory.glob(f"paper-research-{day:%Y%m%d}T*.json"), reverse=True)
    items, failures = [], []
    # The name supplies a publication clock, not a reconstructed historical ready_at.
    from datetime import datetime
    for path in candidates[:32]:
        match = IDENTITY.fullmatch(path.name)
        if not match:
            continue
        at = datetime.strptime(match.group(1), "%Y%m%dT%H%M%S%f")
        if at > as_of:
            continue
        size = path.stat().st_size
        if path.is_symlink() or not path.resolve().is_relative_to(directory):
            failures.append({"artifact_id": path.name, "reason": "unsafe_path"})
            continue
        items.append({"artifact_id": path.name, "as_of": at.isoformat(),
                      "bytes": size, "readable_within_budget": size <= MAX_ARTIFACT_BYTES,
                      "availability": "file_observed_now_not_historical_ready_at"})
    return {"items": items, "invalid": failures, "catalog_truncated": len(candidates) > 32}


def read_artifact(day, *, as_of, artifact_id=None, section="summary",
                  account_name=None, code=None, cursor=0, limit=50, root=None):
    directory = Path(root or ARTIFACT_ROOT).resolve()
    catalog = artifact_catalog(day, as_of=as_of, root=directory)
    if not artifact_id:
        artifact_id = next((r["artifact_id"] for r in catalog["items"]
                            if r["readable_within_budget"]), None)
    if not artifact_id:
        return {"status": "unavailable", "reason": "no_bounded_published_artifact",
                "trade_date": day.isoformat(), "catalog": catalog}
    if not IDENTITY.fullmatch(artifact_id) or artifact_id not in {r["artifact_id"] for r in catalog["items"]}:
        raise ValueError("artifact identity must match the requested day and cutoff")
    path = directory / artifact_id
    with path.open("rb") as stream:
        raw = stream.read(MAX_ARTIFACT_BYTES + 1)
    if len(raw) > MAX_ARTIFACT_BYTES:
        return {"status": "unavailable", "reason": "artifact_byte_budget", "catalog": catalog}
    digest = hashlib.sha256(raw).hexdigest()
    if digest[:16] != IDENTITY.fullmatch(artifact_id).group(2):
        raise ValueError("research artifact hash mismatch")
    data = json.loads(raw)
    if data.get("as_of") != next(r["as_of"] for r in catalog["items"] if r["artifact_id"] == artifact_id):
        raise ValueError("research artifact clock mismatch")
    result = {"status": "ok", "artifact_id": artifact_id, "sha256": digest,
              "as_of": data["as_of"], "schema": data.get("schema"),
              "read_only": True, "report_availability": data.get("report_availability"),
              "catalog": catalog}
    sections = data.get("sections", {})
    if section == "summary":
        summary = {}
        for name, container in sections.items():
            report = container.get("report") or {}
            summary[name] = {"status": container.get("status"),
                "metadata": {k: v for k, v in report.items() if k in {
                    "report_version", "dataset_sha256", "parallel_dataset_sha256", "report_data_hash",
                    "summary", "comparison_contract", "policy", "warnings", "warning",
                    "start_date", "end_date", "as_of_at", "read_only", "archive_availability"}},
                "accounts": report.get("paired_accounts", report.get("accounts", [])),
                "item_counts": {k: len(v) for k, v in report.items() if isinstance(v, list)}}
        result["sections"] = summary
        return result
    if section not in {"signal_portfolio", "post_exit"}:
        raise ValueError("unsupported research section")
    container = sections.get(section, {})
    report = container.get("report") or {}
    key = "signals" if section == "signal_portfolio" else "cycles"
    all_rows = report.get(key, [])
    rows = [r for r in all_rows
            if (not account_name or r.get("account_name") == account_name)
            and (not code or r.get("code") == code)]
    result.update(section=section, section_status=container.get("status"),
                  total_unfiltered=len(all_rows), total_matching=len(rows),
                  cursor=cursor, items=rows[cursor:cursor+limit],
                  next_cursor=cursor+limit if cursor+limit < len(rows) else None,
                  truncation="paged", no_executed_return_implied=True)
    return result
