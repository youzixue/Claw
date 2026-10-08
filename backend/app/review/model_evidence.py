"""SELECT-only model evidence for an independently owned local MCP session.

The caller owns the SQLite ``mode=ro`` / ``query_only`` connection, disables
session autoflush and opens an explicit read transaction before calling this
adapter. No engine, transaction, schema initializer or HTTP handler is invoked
here. Missing tables/columns raise OperationalError for the MCP boundary to
report as unavailable, rather than being repaired or disguised as empty data.

Only inspected serialization helpers and the read-only deployment validator
are reused. Stored acceptance/eligibility is evidence, never an instruction to
train, run shadow inference, approve an artifact or change a deployment.
"""

from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.model_lab import _run_payload
from app.models.promotion import (
    PromotionDeploymentEvent,
    PromotionModelArtifact,
    PromotionPredictionRun,
    PromotionShadowEvaluation,
    PromotionShadowRun,
    PromotionTrainingRun,
)
from app.promotion.deployment import (
    _event_payload,
    _loads,
    _validate_deployment_evidence,
)
from app.promotion.versioning import get_promotion_model_identity


# The public names, allowed arguments and bounds match ashare_review_mcp.TOOLS.
_LIMITS = {
    "ashare_prediction_runs": (50, 200),
    "ashare_training_runs": (30, 100),
    "ashare_shadow_runs": (100, 300),
    "ashare_shadow_evaluations": (100, 300),
    "ashare_deployments": None,
}
_ARGUMENTS = {
    "ashare_prediction_runs": {"trade_date", "snapshot_context", "model_version", "limit"},
    "ashare_training_runs": {"target_board", "limit"},
    "ashare_shadow_runs": {"artifact_id", "target_board", "limit"},
    "ashare_shadow_evaluations": {"artifact_id", "target_board", "limit"},
    "ashare_deployments": set(),
}
MODEL_EVIDENCE_TOOLS = frozenset(_ARGUMENTS)


def _validated_arguments(tool_name: str, arguments: dict) -> dict:
    if type(tool_name) is not str or tool_name not in MODEL_EVIDENCE_TOOLS:
        raise ValueError(f"unknown model evidence tool: {tool_name}")
    if type(arguments) is not dict:
        raise ValueError("arguments must be an object")
    if set(arguments) - _ARGUMENTS[tool_name]:
        raise ValueError("unknown tool argument")
    for key, value in arguments.items():
        if key in {"limit", "artifact_id", "target_board"}:
            if type(value) is not int:
                raise ValueError(f"{key} must be integer")
            if key == "target_board" and value not in (1, 2):
                raise ValueError("target_board has an unsupported value")
            if key == "artifact_id" and value < 1:
                raise ValueError("artifact_id outside supported range")
            if key == "limit" and not 1 <= value <= _LIMITS[tool_name][1]:
                raise ValueError("limit outside supported range")
        elif type(value) is not str:
            raise ValueError(f"{key} must be string")
    result = dict(arguments)
    # The previous HTTP query omitted an empty optional date; no implicit date
    # resolution or latest-day substitution belongs at this evidence boundary.
    if result.get("trade_date"):
        try:
            parsed = date.fromisoformat(result["trade_date"])
        except ValueError as exc:
            raise ValueError("trade_date must be YYYY-MM-DD") from exc
        if parsed.isoformat() != result["trade_date"]:
            raise ValueError("trade_date must be YYYY-MM-DD")
        result["trade_date"] = parsed
    if _LIMITS[tool_name] is not None:
        result.setdefault("limit", _LIMITS[tool_name][0])
    return result


async def _read_rows(db: AsyncSession, statement) -> list[SimpleNamespace]:
    # Core column reads do not consult the ORM identity map. Pending/dirty ORM
    # objects must neither leak into evidence nor cause an automatic flush.
    return [SimpleNamespace(**row) for row in (await db.execute(statement)).mappings()]


async def _read_one(db: AsyncSession, model, identity: int):
    table = model.__table__
    rows = await _read_rows(db, select(table).where(table.c.id == identity).limit(1))
    return rows[0] if rows else None


class _DeploymentEvidenceReader:
    """The existing validator only needs SELECT of its frozen evaluation row."""

    def __init__(self, db: AsyncSession):
        self._db = db

    async def get(self, model, identity: int):
        if model is not PromotionShadowEvaluation:
            raise ValueError("unsupported deployment evidence model")
        return await _read_one(self._db, model, identity)


def _fields(row: SimpleNamespace, names: tuple[str, ...], json_fields=()) -> dict:
    payload = {name: getattr(row, name) for name in names}
    for name in json_fields:
        payload[name] = _loads(getattr(row, f"{name}_json"))
    return payload


def _json_ready(value):
    # Match the old HTTP JSON representation (including microseconds), not
    # json.dumps(default=str), which changes datetime separators to spaces.
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    return value


async def _deployment_state(db: AsyncSession) -> dict:
    # Even a legacy/empty lane must not masquerade as an available deployment
    # ledger if its required registry/evidence tables have never been created.
    for model in (PromotionModelArtifact, PromotionShadowEvaluation):
        await db.execute(select(model.__table__).limit(0))
    table = PromotionDeploymentEvent.__table__
    lanes = []
    for target in (1, 2):
        rows = await _read_rows(
            db, select(table).where(table.c.target_board == target)
            .order_by(table.c.id.desc()).limit(1),
        )
        event = rows[0] if rows else None
        artifact = (
            await _read_one(db, PromotionModelArtifact, event.artifact_id)
            if event is not None and event.artifact_id is not None else None
        )
        payload = _event_payload(event, artifact)
        payload["target_board"] = target
        integrity_error = None
        if event is not None and event.artifact_id is not None and artifact is None:
            integrity_error = "deployment artifact is missing; legacy fallback is active"
        elif event is not None and artifact is not None:
            try:
                # Verified call graph: SELECT one evaluation, validate local
                # artifact bytes and compare frozen policy; no ensure or writes.
                await _validate_deployment_evidence(
                    _DeploymentEvidenceReader(db), event=event,
                    artifact=artifact, target_board=target,
                )
            except OperationalError:
                raise
            except Exception as exc:
                integrity_error = f"deployment artifact validation failed: {exc}"
        payload["integrity_ok"] = integrity_error is None
        payload["effective_active"] = bool(payload.get("active")) and integrity_error is None
        if integrity_error:
            payload["integrity_error"] = integrity_error
            payload["effective_model_version"] = get_promotion_model_identity().active_model_version
        else:
            payload["effective_model_version"] = payload["active_model_version"]
        lanes.append(payload)
    events = await _read_rows(db, select(table).order_by(table.c.id.desc()).limit(100))
    return {
        "lanes": lanes,
        "events": [
            {**_fields(row, (
                "id", "event_key", "target_board", "action", "deployment_mode",
                "artifact_id", "evidence_evaluation_id", "from_model_version",
                "to_model_version", "operator", "reason",
            ), ("metadata",)), "created_at": row.created_at.isoformat(timespec="seconds")}
            for row in events
        ],
        "automatic_promotion": False,
    }


async def read_model_evidence(db: AsyncSession, *, tool_name: str, arguments: dict) -> dict:
    """Read one of the five MCP contracts without initializing or modifying it.

    OperationalError is deliberately not converted to a successful empty result.
    This function does not begin/commit/rollback a transaction or enable/disable
    query_only; the independent local MCP session is responsible for those guards.
    """
    args = _validated_arguments(tool_name, arguments)
    with db.no_autoflush:
        if tool_name == "ashare_deployments":
            return _json_ready(await _deployment_state(db))
        if tool_name == "ashare_prediction_runs":
            table = PromotionPredictionRun.__table__
            statement = select(table)
            if args.get("trade_date"):
                statement = statement.where(table.c.reference_trade_date == args["trade_date"])
            for key in ("snapshot_context", "model_version"):
                if args.get(key, "").strip():
                    statement = statement.where(table.c[key] == args[key].strip())
            rows = await _read_rows(db, statement.order_by(table.c.as_of_at.desc()).limit(args["limit"]))
            return {"count": len(rows), "runs": [_run_payload(row) for row in rows]}
        if tool_name == "ashare_training_runs":
            model, output_key, order_key = PromotionTrainingRun, "training_runs", "started_at"
            names = (
                "id", "training_key", "target_board", "status", "model_version",
                "feature_version", "data_version", "artifact_id", "error_message",
                "started_at", "completed_at",
            )
            json_fields = ("config", "dataset", "metrics", "acceptance")
        elif tool_name == "ashare_shadow_runs":
            model, output_key, order_key = PromotionShadowRun, "shadow_runs", "reference_trade_date"
            names = (
                "id", "shadow_key", "prediction_run_id", "artifact_id", "target_board",
                "snapshot_context", "reference_trade_date", "as_of_at",
                "champion_model_version", "challenger_model_version", "status",
                "candidate_count", "created_at",
            )
            json_fields = ("metadata",)
        else:
            model, output_key, order_key = PromotionShadowEvaluation, "evaluations", "created_at"
            names = (
                "id", "evaluation_key", "artifact_id", "target_board", "snapshot_context",
                "challenger_model_version", "label_version", "outcome_end_date",
                "evaluated_shadow_run_count", "trade_day_count", "sample_count",
                "positive_count", "decision", "created_at",
            )
            json_fields = ("metrics", "acceptance", "metadata")
        table = model.__table__
        statement = select(table)
        for key in ("target_board", "artifact_id"):
            if key in args:
                statement = statement.where(table.c[key] == args[key])
        statement = statement.order_by(table.c[order_key].desc())
        if tool_name == "ashare_shadow_runs":
            statement = statement.order_by(table.c.id.desc())
        rows = await _read_rows(db, statement.limit(args["limit"]))
        return _json_ready({
            "count": len(rows),
            output_key: [_fields(row, names, json_fields) for row in rows],
        })
