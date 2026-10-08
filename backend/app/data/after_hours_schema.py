"""Pure DDL shared by model creation and the explicit migration; no runtime DB access."""

SQLITE_INSERT_GUARD = """
CREATE TRIGGER IF NOT EXISTS after_hours_no_replace
BEFORE INSERT ON stock_after_hours_observation
WHEN EXISTS (
    SELECT 1 FROM stock_after_hours_observation old
    WHERE old.id = NEW.id OR (
        old.code = NEW.code AND old.trade_date = NEW.trade_date
        AND old.stage = NEW.stage AND old.source = NEW.source
        AND old.source_version = NEW.source_version AND old.content_hash = NEW.content_hash
    )
)
BEGIN
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM stock_after_hours_observation old
        WHERE old.code = NEW.code AND old.trade_date = NEW.trade_date
        AND old.stage = NEW.stage AND old.source = NEW.source
        AND old.source_version = NEW.source_version AND old.content_hash = NEW.content_hash
        AND old.payload_json = NEW.payload_json AND old.protocol_version = NEW.protocol_version
        AND old.quality_status = NEW.quality_status
    ) THEN RAISE(IGNORE)
    ELSE RAISE(ABORT, 'after-hours evidence is append-only') END;
END
"""

POSTGRES_GUARD_FUNCTION = """
CREATE OR REPLACE FUNCTION claw_after_hours_forbid_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'after-hours evidence is append-only';
END;
$$
"""

POSTGRES_GUARD_TRIGGER = """
CREATE TRIGGER after_hours_no_mutation
BEFORE UPDATE OR DELETE ON stock_after_hours_observation
FOR EACH ROW EXECUTE FUNCTION claw_after_hours_forbid_mutation()
"""
