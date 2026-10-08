"""SQLite guards for future receipt-backed fixed-price resource consumption.

No global foreign_keys/recursive_triggers setting is assumed. Receipt and economic
identity guards are installed together only after the receipt table exists.
PostgreSQL consumption is deliberately unsupported until separately verified.
"""

FULL_PROTOCOL = "after_hours_receipt_resources_v1_20261002"
PARTIAL_PROTOCOL = "after_hours_partial_receipt_resources_v1_20261002"


def _partial_book_quantity_sql():
    # JSON coordinates are structural constraints, NOT source/permit/hash authority.
    payload = "CASE WHEN json_valid(NEW.payload_json) THEN NEW.payload_json ELSE '{}' END"
    value = lambda key: f"json_extract({payload}, '$.{key}')"
    kind = lambda key: f"json_type({payload}, '$.{key}')"
    integers = ("fragment_index", "original_quantity", "cumulative_before",
                "cumulative_after", "remaining_quantity_after", "account_numeric_id",
                "trade_fill_id", "paper_trade_id")
    texts = ("protocol_version", "scope_key", "allocation_id", "order_id")
    typed = " AND ".join([*(f"{kind(key)}='integer'" for key in integers),
                         *(f"{kind(key)}='text'" for key in texts)])
    typed += (f" AND json_type({payload})='object' AND "
              f"(SELECT count(*)=count(DISTINCT key) FROM json_each({payload}))")
    same = "r.scope_key=NEW.scope_key AND r.order_id=NEW.order_id"
    history_count = f"(SELECT count(*) FROM paper_after_hours_resource_receipt r WHERE {same})"
    history_quantity = f"""(SELECT coalesce(sum(pf.quantity),0)
        FROM paper_after_hours_resource_receipt r JOIN trade_fill pf ON pf.id=r.trade_fill_id
        WHERE {same})"""
    partial = f"""
        NEW.protocol_version='{PARTIAL_PROTOCOL}' AND json_valid(NEW.payload_json)
        AND length(CAST(NEW.payload_json AS BLOB))<=2097152 AND {typed}
        AND {value("protocol_version")}=NEW.protocol_version
        AND {value("scope_key")}=NEW.scope_key
        AND {value("allocation_id")}=NEW.allocation_id
        AND {value("order_id")}=NEW.order_id
        AND {value("account_numeric_id")}=s.account_numeric_id
        AND {value("trade_fill_id")}=f.id AND {value("paper_trade_id")}=t.id
        AND {kind("request_id")}='text' AND length({value("request_id")})=35
        AND length(CAST({value("request_id")} AS BLOB))=35
        AND substr({value("request_id")},1,4)='afp-'
        AND substr({value("request_id")},5) NOT GLOB '*[^0-9a-f]*'
        AND f.fill_id='fill-' || {value("request_id")}
        AND {kind("fixed_price")} IN ('integer','real')
        AND {value("fixed_price")}=f.price AND f.price>0 AND f.price-f.price=0
        AND s.checked_at>=NEW.recorded_at
        AND typeof(o.quantity)='integer' AND o.quantity>=100 AND o.quantity%100=0
        AND typeof(f.quantity)='integer' AND f.quantity>=100 AND f.quantity%100=0
        AND {value("original_quantity")}=o.quantity
        AND {value("fragment_index")}={history_count}+1
        AND {value("cumulative_before")}={history_quantity}
        AND {value("cumulative_before")}>=0
        AND {value("cumulative_after")}={value("cumulative_before")}+f.quantity
        AND {value("cumulative_after")}<=o.quantity
        AND {value("remaining_quantity_after")}=o.quantity-{value("cumulative_after")}
        AND typeof(o.filled_quantity)='integer' AND o.filled_quantity={value("cumulative_before")}
        AND o.status IN ('submitted','partial')
        AND o.created_at<=t.trade_time AND date(t.trade_time)=s.trade_date
        AND NOT EXISTS (SELECT 1 FROM paper_after_hours_resource_receipt r
            WHERE r.order_id=NEW.order_id AND (r.scope_key<>NEW.scope_key
            OR r.protocol_version<>'{PARTIAL_PROTOCOL}'))
        AND NOT EXISTS (SELECT 1 FROM paper_after_hours_resource_receipt r
            LEFT JOIN trade_fill pf ON pf.id=r.trade_fill_id
            LEFT JOIN paper_trade_log pt ON pt.id=r.paper_trade_id
            WHERE {same} AND (pf.id IS NULL OR pt.id IS NULL
            OR r.recorded_at>NEW.recorded_at OR pf.filled_at<>r.recorded_at
            OR pt.trade_time<>r.recorded_at OR pf.quantity<>pt.amount
            OR pt.account_id<>s.account_numeric_id OR pf.order_id<>NEW.order_id
            OR pf.price<>f.price OR pt.price<>t.price))
        AND NOT EXISTS (SELECT 1 FROM trade_fill pf
            LEFT JOIN paper_after_hours_resource_receipt r ON r.trade_fill_id=pf.id
            WHERE pf.order_id=NEW.order_id AND pf.id<>NEW.trade_fill_id
            AND (r.id IS NULL OR r.order_id<>NEW.order_id OR r.scope_key<>NEW.scope_key
            OR r.protocol_version<>'{PARTIAL_PROTOCOL}'))
    """
    full = f"""NEW.protocol_version='{FULL_PROTOCOL}' AND f.quantity=o.quantity
        AND NOT EXISTS (SELECT 1 FROM paper_after_hours_resource_receipt r
                        WHERE r.order_id=NEW.order_id)"""
    return f"t.amount=f.quantity AND (({full}) OR ({partial}))"


def sqlite_guards(*, include_partial=True):
    statements = [
        """CREATE TRIGGER IF NOT EXISTS af_resource_scope_no_delete
        BEFORE DELETE ON paper_after_hours_resource_scope
        BEGIN SELECT RAISE(ABORT, 'fixed-price resource scope cannot be deleted'); END""",
        """CREATE TRIGGER IF NOT EXISTS af_resource_scope_no_replace
        BEFORE INSERT ON paper_after_hours_resource_scope WHEN EXISTS
        (SELECT 1 FROM paper_after_hours_resource_scope WHERE scope_key=NEW.scope_key
         OR (account_numeric_id=NEW.account_numeric_id AND code=NEW.code AND trade_date=NEW.trade_date))
        BEGIN SELECT RAISE(ABORT, 'fixed-price scope cannot be replaced or reset'); END""",
    ]
    identity = ("scope_key", "account_numeric_id", "account_name", "code", "trade_date",
                "source", "source_version", "session_id")
    immutable = " OR ".join(f"NEW.{key} IS NOT OLD.{key}" for key in identity)
    statements.append(f"""CREATE TRIGGER IF NOT EXISTS af_resource_scope_monotonic
        BEFORE UPDATE ON paper_after_hours_resource_scope WHEN
        {immutable} OR NEW.revision != OLD.revision+1
        OR NEW.terminal_sequence < OLD.terminal_sequence
        OR NEW.source_quote_at < OLD.source_quote_at
        OR NEW.source_available_at < OLD.source_available_at
        OR NEW.source_available_at < NEW.source_quote_at
        OR NEW.checked_at < OLD.checked_at
        OR (NEW.terminal_sequence = OLD.terminal_sequence
            AND NEW.lifecycle_prefix_hash IS NOT OLD.lifecycle_prefix_hash)
        BEGIN SELECT RAISE(ABORT, 'fixed-price scope identity or watermark changed'); END""")
    for action in ("UPDATE", "DELETE"):
        statements.append(f"""CREATE TRIGGER IF NOT EXISTS af_resource_receipt_no_{action.lower()}
            BEFORE {action} ON paper_after_hours_resource_receipt
            BEGIN SELECT RAISE(ABORT, 'fixed-price consumption is append-only'); END""")
    book_quantity = (_partial_book_quantity_sql() if include_partial
                     else "t.amount=f.quantity AND f.quantity=o.quantity")
    statements += [
        """CREATE TRIGGER IF NOT EXISTS af_resource_receipt_no_replace
        BEFORE INSERT ON paper_after_hours_resource_receipt WHEN EXISTS
        (SELECT 1 FROM paper_after_hours_resource_receipt WHERE id=NEW.id
         OR allocation_id=NEW.allocation_id OR trade_fill_id=NEW.trade_fill_id
         OR paper_trade_id=NEW.paper_trade_id)
        BEGIN SELECT RAISE(ABORT, 'fixed-price consumption cannot be replaced'); END""",
        f"""CREATE TRIGGER IF NOT EXISTS af_resource_receipt_book_binding
        BEFORE INSERT ON paper_after_hours_resource_receipt WHEN NOT EXISTS
        (SELECT 1 FROM paper_after_hours_resource_scope s
         JOIN trade_fill f ON f.id=NEW.trade_fill_id
         JOIN paper_trade_log t ON t.id=NEW.paper_trade_id
         JOIN trade_order o ON o.order_id=NEW.order_id
         JOIN paper_account a ON a.id=s.account_numeric_id
         WHERE s.scope_key=NEW.scope_key AND a.account_name=s.account_name
         AND a.status='active' AND t.account_id=a.id AND o.account_id=a.account_name
         AND o.order_type='after_hours_fixed' AND o.broker='paper' AND f.broker='paper'
         AND f.order_id=o.order_id AND f.broker_trade_id=CAST(t.id AS TEXT)
         AND t.code=s.code AND f.code=s.code AND o.code=s.code
         AND f.side=t.trade_type AND o.side=t.trade_type
         AND {book_quantity}
         AND t.price=f.price AND t.commission IS f.commission AND t.tax IS f.tax
         AND t.realized_pnl IS f.realized_pnl AND t.trade_time=f.filled_at
         AND f.trade_date=s.trade_date AND o.trade_date=s.trade_date
         AND NEW.recorded_at=f.filled_at
         AND s.revision=(SELECT count(*)+1 FROM paper_after_hours_resource_receipt r
                         WHERE r.scope_key=s.scope_key))
        BEGIN SELECT RAISE(ABORT, 'fixed-price consumption missing book or CAS binding'); END""",
    ]
    # Receipt deletion or identity changes cannot silently release consumed shares,
    # even when SQLite foreign_keys and recursive_triggers are OFF.
    guards = {
        "trade_fill": ("f", "trade_fill_id", ("id", "fill_id", "order_id", "broker",
            "external_order_id", "code", "side", "price", "quantity", "commission", "tax",
            "realized_pnl", "broker_trade_id", "raw_json", "decision_round_id",
            "fill_round_id", "trade_date", "filled_at")),
        "paper_trade_log": ("t", "paper_trade_id", ("id", "account_id", "code", "trade_type",
            "price", "amount", "trade_time", "commission", "tax", "signal_id",
            "realized_pnl", "strategy_version", "decision_round_id", "fill_round_id")),
        "trade_order": ("o", "order_id", ("id", "order_id", "broker", "account_id", "code",
            "side", "order_type", "price", "quantity", "strategy_version", "signal_id",
            "decision_round_id", "decision_at", "trade_date", "idempotency_key")),
        "paper_account": ("a", None, ("id", "account_name")),
    }
    for table, (alias, link, fields) in guards.items():
        if link is None:
            old_link = "EXISTS (SELECT 1 FROM paper_after_hours_resource_scope WHERE account_numeric_id=OLD.id)"
            new_link = "EXISTS (SELECT 1 FROM paper_after_hours_resource_scope WHERE account_numeric_id=NEW.id)"
        elif table == "trade_order":
            old_link = "EXISTS (SELECT 1 FROM paper_after_hours_resource_receipt WHERE order_id=OLD.order_id)"
            new_link = """EXISTS (SELECT 1 FROM paper_after_hours_resource_receipt r
                JOIN trade_order o ON o.order_id=r.order_id WHERE o.id=NEW.id OR o.order_id=NEW.order_id
                OR o.idempotency_key=NEW.idempotency_key)"""
        else:
            old_link = f"EXISTS (SELECT 1 FROM paper_after_hours_resource_receipt WHERE {link}=OLD.id)"
            new_link = f"EXISTS (SELECT 1 FROM paper_after_hours_resource_receipt WHERE {link}=NEW.id)"
            if table == "trade_fill":
                new_link = """EXISTS (SELECT 1 FROM paper_after_hours_resource_receipt r
                    JOIN trade_fill f ON f.id=r.trade_fill_id WHERE f.id=NEW.id OR f.fill_id=NEW.fill_id)"""
        differences = " OR ".join(f"NEW.{key} IS NOT OLD.{key}" for key in fields)
        # UPDATE OR REPLACE can implicitly delete a bound destination row even
        # when the updated OLD row is unbound and recursive_triggers is OFF.
        # Check both source and every unique destination key; unchanged economic
        # identity still permits ordinary non-economic annotations.
        for action, condition in (("DELETE", old_link), ("INSERT", new_link),
                                  ("UPDATE", f"(({old_link}) OR ({new_link})) AND ({differences})")):
            statements.append(f"""CREATE TRIGGER IF NOT EXISTS af_bound_{table}_no_{action.lower()}
                BEFORE {action} ON {table} WHEN {condition}
                BEGIN SELECT RAISE(ABORT, 'fixed-price bound economic identity is immutable'); END""")
    return statements


def install_sqlite_guards(target, connection, **kw):
    if connection.dialect.name == "sqlite":
        for statement in sqlite_guards():
            connection.exec_driver_sql(statement)
