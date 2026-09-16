"""Explicit bounded raw-response research capture, using the existing source route.

No scheduler, business DB or model ready write. Old bars in a response become
observed-now evidence only. This CLI does not recover provider finality/identity.
"""
import argparse
import asyncio
from datetime import date, datetime
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))


def validate(args):
    root = Path(args.archive_root).absolute()
    outputs = (ROOT / "outputs").resolve()
    if (any(p.is_symlink() for p in (root, *root.parents))
            or not root.resolve().is_relative_to(outputs) or root.resolve() == outputs
            or root.exists() or not root.parent.is_dir()):
        raise ValueError("archive must be a new directory under existing project outputs, without symlinks")
    if args.source not in {"eastmoney_fund", "ths_kline"}:
        raise ValueError("unsupported source")
    codes = tuple(args.codes or ())
    if args.source == "eastmoney_fund" and codes:
        raise ValueError("fund source uses the existing full pagination, not a supplied universe")
    if args.source == "ths_kline" and (not 1 <= len(codes) <= 3 or len(set(codes)) != len(codes)
            or any(not isinstance(code, str) or re.fullmatch(r"[0-9]{6}", code) is None for code in codes)):
        raise ValueError("one to three distinct six-digit probe codes required, not a full universe")
    return root.resolve(), codes


def _parsed_date_diagnostic(rows, requested_date):
    """Only parsed date presence; no price/finality/identity or universe approval."""
    if rows is None:
        return {"parsed_result_status": "no_result", "parsed_row_count": None,
                "latest_parsed_date": None, "requested_date_row_count": None,
                "invalid_parsed_date_count": None}
    if type(rows) is not list:
        raise ValueError("unexpected_parsed_result_type")
    dates, invalid = [], 0
    for row in rows:
        value = row.get("trade_date") if type(row) is dict else None
        try:
            parsed = date.fromisoformat(value) if type(value) is str else None
        except ValueError:
            parsed = None
        if parsed is None or parsed.isoformat() != value:
            invalid += 1
        else:
            dates.append(value)
    return {"parsed_result_status": "returned", "parsed_row_count": len(rows),
            "latest_parsed_date": max(dates) if dates else None,
            "requested_date_row_count": dates.count(requested_date),
            "invalid_parsed_date_count": invalid}


async def run(args):
    root, codes = validate(args)
    # Exclusive reservation occurs before requests; a racing caller cannot share a batch.
    root.mkdir(mode=0o700)
    from app.data.source_capture import ResponseCapture
    capture = ResponseCapture()
    operation_status, error_type = "returned", None
    parsed_rows = 0
    requested_date = capture.started_at.date().isoformat()
    probe_rows = [{"code": code, "request_status": "not_started"} for code in codes]
    try:
        if args.source == "eastmoney_fund":
            from app.data.sources.eastmoney_source import EastMoneySource
            parsed_rows = len(await EastMoneySource(response_capture=capture).get_individual_fund_flow("今日"))
        else:
            from app.data.sources.ths_kline_source import ThsKlineSource
            source = ThsKlineSource(response_capture=capture)
            # Original year route, one attempt per code. No cookie/proxy/CDN policy change.
            # A three-code transport probe is not a daily universe producer.
            async with asyncio.timeout(90):
                for probe in probe_rows:
                    probe.update(request_status="started", requested_at=datetime.now().isoformat())
                    rows = await source._fetch_kline(
                        probe["code"], f"{capture.started_at.year}.js", max_attempts=1)
                    diagnostic = _parsed_date_diagnostic(rows, requested_date)
                    probe.update(diagnostic, request_status="returned",
                                 returned_at=datetime.now().isoformat())
                    parsed_rows += diagnostic["parsed_row_count"] or 0
                    await asyncio.sleep(source.rate_limit)
    except asyncio.CancelledError:
        # Do not spawn a thread after cancellation or reinterpret it as a complete batch.
        raise
    except Exception as exc:
        operation_status, error_type = "failed", type(exc).__name__
        for probe in probe_rows:
            if probe["request_status"] == "started":
                probe.update(request_status="failed", error_type=error_type,
                             returned_at=datetime.now().isoformat())
    result = await asyncio.to_thread(capture.seal, root,
                                    operation_status=operation_status, error_type=error_type)
    summary = {"archive_root": str(root), "report_ref": result["report_ref"],
        "status": result["report"]["status"], "operation_status": operation_status,
        "error_type": error_type, "parsed_rows_diagnostic_only": parsed_rows,
        "response_count_retained": result["report"]["response_count_retained"],
        "capture_errors": result["report"]["capture_errors"], "formal_ready": False,
        "business_db_opened": False, "scheduled": False}
    if args.source == "ths_kline":
        for probe in probe_rows:
            probe["retained_http_200_response_count"] = sum(
                r.source == "ths_kline" and r.request_key[0] == probe["code"]
                for r in capture.responses)
        missing = [p["code"] for p in probe_rows if p.get("requested_date_row_count") != 1]
        invalid = [p["code"] for p in probe_rows if p.get("invalid_parsed_date_count") != 0]
        # Three successful annual downloads need not contain today's daily bars.
        # Keep unstarted/failed/missing/duplicate targets in the selected denominator.
        summary["probe_date_coverage"] = {
            "schema": "source_probe_date_coverage_v1", "requested_date": requested_date,
            "scope": "explicit_selected_codes_not_market_universe",
            "requested_code_count": len(codes), "probes": probe_rows,
            "codes_without_unique_requested_date_row": missing,
            "codes_with_invalid_or_unknown_parsed_dates": invalid,
            "requested_date_coverage_status": (
                "present_unverified" if not missing and not invalid else "partial_or_unavailable"),
            "price_finality_and_basis_verified": False,
            "historical_first_availability_verified": False,
        }
        # Persist the selected denominator and failure diagnostics next to the
        # immutable transport receipt, not just in transient console output.
        from app.promotion.modeling.daily_materials import MaterialArchive, encode
        archive = MaterialArchive(root)
        summary["probe_ref"] = await asyncio.to_thread(archive.put, encode(summary))
    print(json.dumps(summary, ensure_ascii=False, allow_nan=False))
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=["eastmoney_fund", "ths_kline"], required=True)
    parser.add_argument("--code", dest="codes", action="append")
    parser.add_argument("--archive-root", type=Path, required=True)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
