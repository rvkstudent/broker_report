"""Database module for broker report analysis."""
import sqlite3
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), 'broker.db')


def _norm_date(d: str) -> str:
    """Convert DD.MM.YYYY or YYYY-MM-DD to YYYYMMDD for comparison."""
    if not d:
        return ''
    d = d.strip()
    if len(d) == 10 and d[2] == '.' and d[5] == '.':
        return d[6:10] + d[3:5] + d[0:2]
    if len(d) == 10 and d[4] == '-':
        return d[0:4] + d[5:7] + d[8:10]
    return d


def _date_where(alias='trade', date_from=None, date_to=None) -> str:
    """Build SQL WHERE snippet for date filtering on trade_date."""
    clauses = []
    params = []
    if date_from:
        clauses.append(f"substr({alias}.trade_date,7,4)||substr({alias}.trade_date,4,2)||substr({alias}.trade_date,1,2) >= ?")
        params.append(_norm_date(date_from))
    if date_to:
        clauses.append(f"substr({alias}.trade_date,7,4)||substr({alias}.trade_date,4,2)||substr({alias}.trade_date,1,2) <= ?")
        params.append(_norm_date(date_to))
    return clauses, params


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Create tables if they don't exist."""
    conn = get_connection()
    cur = conn.cursor()

    cur.executescript("""
        CREATE TABLE IF NOT EXISTS report (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            filename        TEXT NOT NULL,
            contract        TEXT,
            investor        TEXT,
            period_start    TEXT,
            period_end      TEXT,
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(filename)
        );

        CREATE TABLE IF NOT EXISTS trade (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id       INTEGER NOT NULL REFERENCES report(id),
            trade_date      TEXT NOT NULL,
            settle_date     TEXT NOT NULL,
            trade_time      TEXT,
            security_name   TEXT NOT NULL,
            security_code   TEXT,
            currency        TEXT DEFAULT 'RUB',
            side            TEXT NOT NULL CHECK(side IN ('Покупка','Продажа')),
            quantity        INTEGER NOT NULL,
            price           REAL,
            amount          REAL NOT NULL,
            nkd             REAL DEFAULT 0,
            broker_fee      REAL DEFAULT 0,
            exchange_fee    REAL DEFAULT 0,
            deal_number     TEXT,
            comment         TEXT,
            status          TEXT
        );

        CREATE TABLE IF NOT EXISTS repo (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id       INTEGER NOT NULL REFERENCES report(id),
            trade_date      TEXT NOT NULL,
            trade_time      TEXT,
            security_name   TEXT NOT NULL,
            security_code   TEXT,
            currency        TEXT DEFAULT 'RUB',
            side            TEXT NOT NULL,
            quantity        INTEGER NOT NULL,
            price_part1     REAL,
            nkd_part1       REAL DEFAULT 0,
            amount_part1    REAL NOT NULL,
            date_part1      TEXT,
            repo_rate       REAL,
            repo_interest   REAL,
            price_part2     REAL,
            nkd_part2       REAL DEFAULT 0,
            amount_part2    REAL,
            date_part2      TEXT,
            broker_fee      REAL DEFAULT 0,
            exchange_fee    REAL DEFAULT 0,
            deal_number     TEXT,
            status          TEXT
        );

        CREATE TABLE IF NOT EXISTS cash_flow (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id       INTEGER NOT NULL REFERENCES report(id),
            date            TEXT NOT NULL,
            description     TEXT NOT NULL,
            currency        TEXT DEFAULT 'RUB',
            credit          REAL DEFAULT 0,
            debit           REAL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS portfolio (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id       INTEGER NOT NULL REFERENCES report(id),
            security_name   TEXT NOT NULL,
            isin            TEXT,
            currency        TEXT DEFAULT 'RUB',
            qty_start       INTEGER DEFAULT 0,
            price_start     REAL,
            value_start     REAL,
            qty_end         INTEGER DEFAULT 0,
            price_end       REAL,
            value_end       REAL,
            qty_change      INTEGER DEFAULT 0,
            value_change    REAL
        );

        CREATE TABLE IF NOT EXISTS financial_result (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id       INTEGER NOT NULL REFERENCES report(id) UNIQUE,
            income_code     TEXT,
            income_amount   REAL DEFAULT 0,
            expense_code    TEXT,
            expense_amount  REAL DEFAULT 0,
            taxable_amount  REAL DEFAULT 0,
            tax_rate        REAL,
            tax_calculated  REAL DEFAULT 0,
            tax_withheld    REAL DEFAULT 0,
            tax_due         REAL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_trade_report ON trade(report_id);
        CREATE INDEX IF NOT EXISTS idx_repo_report ON repo(report_id);
        CREATE INDEX IF NOT EXISTS idx_cash_report ON cash_flow(report_id);

        CREATE TABLE IF NOT EXISTS current_price (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            sec_code        TEXT NOT NULL,
            class_code      TEXT NOT NULL DEFAULT '',
            price           REAL NOT NULL,
            qty             INTEGER DEFAULT 0,
            value           REAL DEFAULT 0,
            timestamp       TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            UNIQUE(sec_code, class_code)
        );

        CREATE TABLE IF NOT EXISTS quik_trade (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_num       INTEGER,
            sec_code        TEXT NOT NULL,
            class_code      TEXT NOT NULL DEFAULT '',
            price           REAL NOT NULL,
            qty             INTEGER NOT NULL,
            value           REAL,
            accruedint      REAL DEFAULT 0,
            yield           REAL DEFAULT 0,
            settlecode      TEXT,
            reporate        REAL DEFAULT 0,
            repovalue       REAL DEFAULT 0,
            repo2value      REAL DEFAULT 0,
            repoterm        INTEGER DEFAULT 0,
            period          INTEGER DEFAULT 0,
            trade_date      TEXT,
            trade_time      TEXT,
            source          TEXT NOT NULL DEFAULT 'quik',
            created_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            UNIQUE(source, trade_num)
        );

        CREATE INDEX IF NOT EXISTS idx_quik_trade_sec ON quik_trade(sec_code, class_code);
        CREATE INDEX IF NOT EXISTS idx_quik_trade_time ON quik_trade(created_at);
    """)

    # ── Migrations: add source column + unique indexes if missing ──
    for table, idx_name in [('trade', 'idx_trade_source_deal'), ('repo', 'idx_repo_source_deal')]:
        cols = [r[1] for r in cur.execute(f"PRAGMA table_info({table})").fetchall()]
        if 'source' not in cols:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN source TEXT NOT NULL DEFAULT 'report'")

    # Deduplicate rows with same deal_number before creating unique index
    for table in ('trade', 'repo'):
        cur.execute(f"""
            DELETE FROM {table} WHERE id IN (
                SELECT t2.id FROM {table} t2
                INNER JOIN (
                    SELECT MIN(id) AS keep_id, deal_number FROM {table}
                    WHERE deal_number IS NOT NULL AND deal_number != ''
                    GROUP BY deal_number
                    HAVING COUNT(*) > 1
                ) dup ON t2.deal_number = dup.deal_number
                WHERE t2.id != dup.keep_id
            )
        """)
    for table, idx_name in [('trade', 'idx_trade_source_deal'), ('repo', 'idx_repo_source_deal')]:
        cur.execute(f"""
            CREATE UNIQUE INDEX IF NOT EXISTS {idx_name}
            ON {table}(source, deal_number)
            WHERE deal_number IS NOT NULL AND deal_number != ''
        """)

    # Migration for quik_trade: source column + unique on (source, trade_num)
    qk_cols = [r[1] for r in cur.execute("PRAGMA table_info(quik_trade)").fetchall()]
    if 'source' not in qk_cols:
        cur.execute("ALTER TABLE quik_trade ADD COLUMN source TEXT NOT NULL DEFAULT 'quik'")
    if 'side' not in qk_cols:
        cur.execute("ALTER TABLE quik_trade ADD COLUMN side TEXT DEFAULT ''")
    if 'flags' not in qk_cols:
        cur.execute("ALTER TABLE quik_trade ADD COLUMN flags INTEGER DEFAULT 0")
    if 'operation' not in qk_cols:
        cur.execute("ALTER TABLE quik_trade ADD COLUMN operation TEXT DEFAULT ''")
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_quik_trade_source_num
        ON quik_trade(source, trade_num)
        WHERE trade_num IS NOT NULL
    """)

    conn.commit()
    conn.close()


# ── Analytical queries ──────────────────────────────────────────

def get_trade_profit(report_id=None, date_from=None, date_to=None):
    """
    Return per-security realized P&L using LIFO chronological matching.
    Only trades where both buy AND sell happened within this period.
    Opening positions are NOT included.
    """
    lots, _ = _match_trades_lifo(report_id, date_from, date_to)

    from collections import defaultdict
    by_sec = defaultdict(lambda: {
        'buy_qty': 0, 'sell_qty': 0, 'total_buy': 0.0, 'total_sell': 0.0,
        'gross_profit': 0.0, 'total_fees': 0.0,
    })

    for lot in lots:
        code = lot['security_code']
        s = by_sec[code]
        s['security_code'] = code
        s['security_name'] = lot['security_name']
        s['buy_qty'] += lot['qty']
        s['sell_qty'] += lot['qty']
        s['total_buy'] += lot['buy_amount']
        s['total_sell'] += lot['sell_amount']
        s['gross_profit'] += lot['profit']
        s['total_fees'] += lot['buy_fee'] + lot['sell_fee']

    results = []
    for code, s in by_sec.items():
        results.append({
            'security_code': code,
            'security_name': s['security_name'],
            'buy_qty': s['buy_qty'],
            'sell_qty': s['sell_qty'],
            'total_buy': round(s['total_buy'], 2),
            'total_sell': round(s['total_sell'], 2),
            'gross_profit': round(s['gross_profit'], 2),
            'total_fees': round(s['total_fees'], 2),
            'net_profit': round(s['gross_profit'] - s['total_fees'], 2),
        })

    results.sort(key=lambda r: r['net_profit'], reverse=True)
    return results


def get_trade_lots(report_id=None, date_from=None, date_to=None):
    """
    Return individual matched buy→sell lots in chronological order.
    Each lot shows: buy_date, sell_date, qty, buy_price, sell_price, profit, fees.
    """
    lots, _ = _match_trades_lifo(report_id, date_from, date_to)
    return lots


def get_open_trades(report_id=None, date_from=None, date_to=None):
    """
    Buys that have NOT been closed by a sell within this period.
    Returns unmatched buy lots, merged by (security_code, buy_date, buy_price).
    Also includes QUIK OnTrade buys that are not closed by QUIK sells.
    """
    _, unmatched = _match_trades_lifo(report_id, date_from, date_to)

    # Merge consecutive lots with same code, date, and price
    merged = []
    for u in unmatched:
        key = (u['security_code'], u['buy_date'], u['buy_price'])
        if merged and (merged[-1]['security_code'], merged[-1]['buy_date'], merged[-1]['buy_price']) == key:
            merged[-1]['qty'] += u['qty']
            merged[-1]['total_cost'] = round(merged[-1]['qty'] * merged[-1]['buy_price'], 2)
            merged[-1]['fees'] = round(merged[-1]['fees'] + u['fees'], 2)
        else:
            merged.append(dict(u))

    # Добавляем открытые позиции из QUIK OnTrade
    quik_open = _get_quik_open_trades()
    for q in quik_open:
        # Проверяем, нет ли уже такой бумаги в merged (чтобы не дублировать)
        existing = [m for m in merged if m['security_code'] == q['security_code']]
        if not existing:
            merged.append(q)
        else:
            # Если бумага уже есть — объединяем (добавляем к последней группе)
            merged.append(q)

    return merged


def _get_quik_open_trades():
    """
    Get unmatched buy trades from QUIK OnTrade.
    Calculates net position per security (total_bought - total_sold).
    If net > 0, returns one aggregated lot.
    """
    conn = get_connection()
    rows = conn.execute("""
        SELECT sec_code, class_code,
               SUM(CASE WHEN side='buy' THEN qty ELSE 0 END) AS total_buy,
               SUM(CASE WHEN side='sell' THEN qty ELSE 0 END) AS total_sell,
               SUM(CASE WHEN side='buy' THEN value ELSE 0 END) AS buy_value
        FROM quik_trade
        WHERE side IN ('buy', 'sell')
        GROUP BY sec_code, class_code
        HAVING total_buy > total_sell
        ORDER BY sec_code
    """).fetchall()
    conn.close()

    result = []
    for r in rows:
        net_qty = r['total_buy'] - r['total_sell']
        if net_qty <= 0:
            continue
        avg_price = round(r['buy_value'] / r['total_buy'], 2) if r['total_buy'] > 0 else 0
        total_cost = round(net_qty * avg_price, 2)
        result.append({
            'security_code': r['sec_code'],
            'security_name': r['sec_code'],  # будет заменено в app.py
            'qty': net_qty,
            'buy_date': '',
            'buy_price': avg_price,
            'total_cost': total_cost,
            'fees': 0.0,
            'source': 'quik',
        })

    return result


def get_instrument_summary(report_id=None, date_from=None, date_to=None):
    """
    Aggregate summary per instrument from OPEN (unmatched) buy positions.
    Shows total qty, average price, total cost per security.
    """
    from collections import defaultdict
    _, unmatched = _match_trades_lifo(report_id, date_from, date_to)

    by_sec = defaultdict(lambda: {'qty': 0, 'cost': 0.0, 'name': ''})
    for u in unmatched:
        code = u['security_code']
        by_sec[code]['qty'] += u['qty']
        by_sec[code]['cost'] += u['total_cost']
        by_sec[code]['name'] = u['security_name']

    result = []
    for code, data in sorted(by_sec.items(), key=lambda x: x[1]['cost'], reverse=True):
        result.append({
            'security_code': code,
            'security_name': data['name'],
            'qty': data['qty'],
            'avg_price': round(data['cost'] / data['qty'], 2) if data['qty'] > 0 else 0,
            'total_cost': round(data['cost'], 2),
        })
    return result


def get_repo_total(report_id=None, date_from=None, date_to=None):
    """Get total repo costs (interest + fees)."""
    conn = get_connection()
    where_clauses, params = _date_where('repo', date_from, date_to)
    if report_id is not None:
        where_clauses.append("repo.report_id=?")
        params.append(report_id)
    where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"
    r = conn.execute(f"""
        SELECT COALESCE(SUM(repo_interest),0) AS interest,
               COALESCE(SUM(broker_fee),0) AS broker_fees,
               COALESCE(SUM(exchange_fee),0) AS exchange_fees
        FROM repo WHERE {where_sql}
    """, params).fetchone()
    conn.close()
    return {
        'interest': round(r['interest'], 2),
        'broker_fees': round(r['broker_fees'], 2),
        'exchange_fees': round(r['exchange_fees'], 2),
        'total': round(r['interest'] + r['broker_fees'] + r['exchange_fees'], 2),
    }


def _match_trades_lifo(report_id=None, date_from=None, date_to=None):
    """
    Core LIFO matching engine.
    Processes ALL trades in chronological order.
    Deduplicates by deal_number across reports.
    Each sell matches against the MOST RECENT buy (LIFO).
    Returns (matched_lots, unmatched_buys).
    """
    conn = get_connection()
    where_clauses, params = _date_where('trade', date_from, date_to)
    if report_id is not None:
        where_clauses.append("trade.report_id=?")
        params.append(report_id)
    where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"

    trades_raw = conn.execute(f"""
        SELECT id, security_code, security_name, side, quantity, amount,
               broker_fee, exchange_fee, trade_date, trade_time, deal_number
        FROM trade
        WHERE {where_sql}
        ORDER BY trade_date, trade_time, id
    """, params).fetchall()

    # Добавляем QUIK OnTrade сделки в LIFO матчинг
    quik_rows = conn.execute("""
        SELECT sec_code, price, qty, value, side, trade_num,
               trade_date, trade_time
        FROM quik_trade
        WHERE side IN ('buy', 'sell') AND qty > 0
        ORDER BY trade_date, trade_time, id
    """).fetchall()
    conn.close()

    # Deduplicate by deal_number (same trade appears in overlapping reports)
    seen_deals = set()
    trades = []
    for t in trades_raw:
        key = (t['security_code'] or t['security_name'], t['deal_number'])
        if key in seen_deals:
            continue
        seen_deals.add(key)
        trades.append(t)

    # Добавляем QUIK OnTrade сделки в LIFO матчинг
    for q in quik_rows:
        # Пропускаем, если такая сделка уже есть из отчёта (по trade_num)
        dup_key = (q['sec_code'], str(q['trade_num']))
        if dup_key in seen_deals:
            continue
        seen_deals.add(dup_key)

        # Нормализуем в формат, совместимый с trade
        faux = {
            'id': None,
            'security_code': q['sec_code'],
            'security_name': q['sec_code'],
            'side': 'Покупка' if q['side'] == 'buy' else 'Продажа',
            'quantity': q['qty'],
            'amount': q['value'],
            'broker_fee': 0.0,
            'exchange_fee': 0.0,
            'trade_date': q['trade_date'] or '',
            'trade_time': q['trade_time'] or '',
            'deal_number': str(q['trade_num']),
        }
        trades.append(faux)

    from collections import defaultdict
    by_sec = defaultdict(list)
    for t in trades:
        code = t['security_code'] or t['security_name']
        by_sec[code].append(t)

    all_lots = []
    all_unmatched = []

    for code, txns in by_sec.items():
        name = txns[0]['security_name']

        # Process ALL transactions in CHRONOLOGICAL order
        buy_queue = []  # each: [remaining_qty, unit_cost, buy_row, fee_total]

        for t in txns:
            if t['side'] == 'Покупка' and t['quantity'] > 0:
                buy_queue.append([
                    t['quantity'],
                    t['amount'] / t['quantity'],
                    t,
                    t['broker_fee'] + t['exchange_fee']
                ])

            elif t['side'] == 'Продажа' and t['quantity'] > 0:
                remaining = t['quantity']
                sell_unit_price = t['amount'] / t['quantity']
                sell_fee = t['broker_fee'] + t['exchange_fee']

                # LIFO: consume from the END of the queue (most recent buy)
                while remaining > 0 and buy_queue:
                    available = buy_queue[-1][0]
                    cost_per = buy_queue[-1][1]
                    buy_row = buy_queue[-1][2]
                    buy_fee = buy_queue[-1][3]
                    used = min(available, remaining)

                    buy_amount = used * cost_per
                    sell_amount = used * sell_unit_price
                    profit = sell_amount - buy_amount
                    fee_proportion = buy_fee * (used / buy_row['quantity']) if buy_row['quantity'] > 0 else 0

                    all_lots.append({
                        'security_code': code,
                        'security_name': name,
                        'qty': used,
                        'buy_date': buy_row['trade_date'],
                        'buy_price': round(cost_per, 2),
                        'buy_amount': round(buy_amount, 2),
                        'buy_fee': round(fee_proportion, 2),
                        'sell_date': t['trade_date'],
                        'sell_price': round(sell_unit_price, 2),
                        'sell_amount': round(sell_amount, 2),
                        'sell_fee': round(sell_fee * (used / t['quantity']), 2) if t['quantity'] > 0 else 0,
                        'profit': round(profit, 2),
                    })

                    buy_queue[-1][0] -= used
                    if buy_queue[-1][0] <= 0:
                        buy_queue.pop()
                    remaining -= used

        # Remaining in buy queue = unmatched (open) buys
        for lot in buy_queue:
            qty = lot[0]
            if qty <= 0:
                continue
            b = lot[2]
            all_unmatched.append({
                'security_code': code,
                'security_name': name,
                'qty': qty,
                'buy_date': b['trade_date'],
                'buy_price': round(lot[1], 2),
                'total_cost': round(qty * lot[1], 2),
                'fees': round(lot[3], 2),
            })

    return all_lots, all_unmatched


def get_financial_result(report_id=None):
    """Get the financial result from the tax section of the report."""
    conn = get_connection()
    query = """
        SELECT income_code, income_amount, expense_code, expense_amount,
               taxable_amount, tax_rate, tax_calculated, tax_withheld, tax_due,
               (income_amount - expense_amount) AS financial_result
        FROM financial_result
        WHERE ? IS NULL OR report_id=?
    """
    row = conn.execute(query, (report_id, report_id)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_open_positions(report_id=None):
    """Get positions that are still open at period end."""
    conn = get_connection()
    query = """
        SELECT security_name, isin AS security_code, currency,
               qty_end, price_end, value_end
        FROM portfolio
        WHERE qty_end > 0 AND (? IS NULL OR report_id=?)
        ORDER BY value_end DESC
    """
    rows = conn.execute(query, (report_id, report_id)).fetchall()
    conn.close()
    return rows


def get_fees_summary(report_id=None):
    """Aggregate all broker and exchange fees from trades and repo."""
    conn = get_connection()
    query = """
        SELECT 'Торги' AS source,
               COALESCE(SUM(broker_fee),0) AS broker_fees,
               COALESCE(SUM(exchange_fee),0) AS exchange_fees
        FROM trade
        WHERE ? IS NULL OR report_id=?
        UNION ALL
        SELECT 'РЕПО' AS source,
               COALESCE(SUM(broker_fee),0) AS broker_fees,
               COALESCE(SUM(exchange_fee),0) AS exchange_fees
        FROM repo
        WHERE ? IS NULL OR report_id=?
    """
    rows = conn.execute(query, (report_id, report_id, report_id, report_id)).fetchall()
    conn.close()
    return rows


def get_repo_summary(report_id=None):
    """Get repo costs — total interest paid/received."""
    conn = get_connection()
    query = """
        SELECT COUNT(*) AS deals,
               COALESCE(SUM(repo_interest),0) AS total_interest,
               COALESCE(SUM(broker_fee),0) AS broker_fees,
               COALESCE(SUM(exchange_fee),0) AS exchange_fees
        FROM repo
        WHERE ? IS NULL OR report_id=?
    """
    row = conn.execute(query, (report_id, report_id)).fetchone()
    conn.close()
    return row


def get_cash_summary(report_id=None):
    """Cash flow summary grouped by description pattern."""
    conn = get_connection()
    query = """
        SELECT description, SUM(credit) AS total_credit, SUM(debit) AS total_debit
        FROM cash_flow
        WHERE ? IS NULL OR report_id=?
        GROUP BY description
        ORDER BY total_debit DESC
    """
    rows = conn.execute(query, (report_id, report_id)).fetchall()
    conn.close()
    return rows


def get_reports_list():
    conn = get_connection()
    rows = conn.execute("""
        SELECT id, filename, contract, investor, period_start, period_end, created_at
        FROM report ORDER BY created_at DESC
    """).fetchall()
    conn.close()
    return rows


def get_report_by_id(report_id):
    conn = get_connection()
    row = conn.execute("SELECT * FROM report WHERE id=?", (report_id,)).fetchone()
    conn.close()
    return row


def delete_report(report_id):
    conn = get_connection()
    conn.execute("DELETE FROM trade WHERE report_id=?", (report_id,))
    conn.execute("DELETE FROM repo WHERE report_id=?", (report_id,))
    conn.execute("DELETE FROM cash_flow WHERE report_id=?", (report_id,))
    conn.execute("DELETE FROM portfolio WHERE report_id=?", (report_id,))
    conn.execute("DELETE FROM report WHERE id=?", (report_id,))
    conn.commit()
    conn.close()


# ── Current prices ────────────────────────────────────────────

def save_price(sec_code: str, price: float, qty: int = 0, value: float = 0, class_code: str = ''):
    """Upsert current price for a security."""
    conn = get_connection()
    conn.execute("""
        INSERT INTO current_price (sec_code, class_code, price, qty, value, timestamp)
        VALUES (?, ?, ?, ?, ?, datetime('now', 'localtime'))
        ON CONFLICT(sec_code, class_code) DO UPDATE SET
            price = excluded.price,
            qty = excluded.qty,
            value = excluded.value,
            timestamp = datetime('now', 'localtime')
    """, (sec_code, class_code, price, qty, value))
    conn.commit()
    conn.close()


def save_prices_batch(prices: list):
    """Upsert multiple prices in a single transaction.

    Each item: dict with keys sec_code, price, [qty, value, class_code]
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("BEGIN")
    for p in prices:
        cur.execute("""
            INSERT INTO current_price (sec_code, class_code, price, qty, value, timestamp)
            VALUES (?, ?, ?, ?, ?, datetime('now', 'localtime'))
            ON CONFLICT(sec_code, class_code) DO UPDATE SET
                price = excluded.price,
                qty = excluded.qty,
                value = excluded.value,
                timestamp = datetime('now', 'localtime')
        """, (p['sec_code'], p.get('class_code', ''), p['price'], p.get('qty', 0), p.get('value', 0)))
    conn.commit()
    conn.close()


def save_quik_trades(trades: list):
    """Save QUIK trades (OnAllTrade data) to SQLite in a batch."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("BEGIN")
    for t in trades:
        # Parse datetime from QUIK if provided
        trade_date = None
        trade_time = None
        dt = t.get('datetime')
        if dt and isinstance(dt, dict):
            y = dt.get('year', 0) or 0
            m = dt.get('month', 0) or 0
            d = dt.get('day', 0) or 0
            hh = dt.get('hour', 0) or 0
            mm = dt.get('min', 0) or 0
            ss = dt.get('sec', 0) or 0
            if y > 0 and m > 0 and d > 0:
                trade_date = f"{d:02d}.{m:02d}.{y:04d}"
                trade_time = f"{hh:02d}:{mm:02d}:{ss:02d}"
        elif dt and isinstance(dt, str):
            import re
            m = re.match(r'^(\d{2}\.\d{2}\.\d{4})\s*(\d{2}:\d{2}:\d{2})', dt)
            if m:
                trade_date = m.group(1)
                trade_time = m.group(2)

        # Fallback: если datetime не передан, берём trade_date/trade_time напрямую из JSON
        if not trade_date:
            trade_date = t.get('trade_date')
        if not trade_time:
            trade_time = t.get('trade_time')

        # Определяем сторону сделки: приоритет — operation (OnTrade),
        #   затем flags (OnAllTrade), затем явное side из JSON.
        #   OnTrade: operation='B' (buy) / 'S' (sell)
        #   OnAllTrade: flags & 0x02 (bid) → buy, flags & 0x01 (offer) → sell
        flags = t.get('flags', 0) or 0
        operation = t.get('operation', '') or ''
        side = t.get('side', '')
        if operation == 'B':
            side = 'buy'
        elif operation == 'S':
            side = 'sell'
        elif not side and flags:
            if flags & 0x02:
                side = 'buy'
            elif flags & 0x01:
                side = 'sell'

        # QUIK OnTrade передаёт qty в лотах. Фактическое количество
        # акций = value / price (price — за 1 акцию).
        price = t['price']
        qty = t['qty']
        t_val = t.get('value', 0) or 0
        if price > 0:
            actual_qty = int(round(t_val / price))
            if actual_qty > 0:
                qty = actual_qty

        cur.execute("""
            INSERT INTO quik_trade
                (trade_num, sec_code, class_code, price, qty, value,
                 accruedint, yield, settlecode,
                 reporate, repovalue, repo2value, repoterm, period,
                 trade_date, trade_time, source, side, flags, operation)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'quik', ?, ?, ?)
            ON CONFLICT(source, trade_num) DO UPDATE SET
                side=COALESCE(NULLIF(quik_trade.side, ''), excluded.side),
                flags=COALESCE(NULLIF(quik_trade.flags, 0), excluded.flags),
                operation=COALESCE(NULLIF(quik_trade.operation, ''), excluded.operation),
                price=excluded.price, qty=excluded.qty, value=excluded.value,
                trade_date=excluded.trade_date, trade_time=excluded.trade_time
        """, (
            t.get('trade_num'), t.get('sec_code'), t.get('class_code', ''),
            price, qty, t_val,
            t.get('accruedint', 0), t.get('yield', 0), t.get('settlecode', ''),
            t.get('repolate', 0), t.get('repovalue', 0), t.get('repo2value', 0),
            t.get('repoterm', 0), t.get('period', 0),
            trade_date, trade_time,
            side, flags, operation
        ))
    conn.commit()
    conn.close()


def get_current_prices():
    """Get all current instrument prices."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT sec_code, class_code, price, qty, value, timestamp
        FROM current_price
        ORDER BY sec_code
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_my_instruments():
    """Get distinct securities from the trade table (user's instruments)."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT DISTINCT security_code AS sec_code, security_name AS sec_name
        FROM trade
        WHERE security_code IS NOT NULL AND security_code != ''
        ORDER BY security_code
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_quik_positions():
    """Aggregate QUIK positions from OnTrade data.
       side='buy' → +qty, side='sell' → -qty.
       Legacy records without side are excluded (can't determine direction).
    """
    conn = get_connection()
    rows = conn.execute("""
        SELECT sec_code, class_code,
               SUM(CASE WHEN side='sell' THEN -qty ELSE qty END) AS net_qty,
               SUM(CASE WHEN side='sell' THEN 0 ELSE value END) AS buy_value
        FROM quik_trade
        WHERE side IN ('buy', 'sell')
        GROUP BY sec_code, class_code
        HAVING net_qty > 0
        ORDER BY sec_code
    """).fetchall()
    conn.close()
    result = []
    for r in rows:
        avg_price = round(r['buy_value'] / r['net_qty'], 2) if r['net_qty'] > 0 else 0
        result.append({
            'sec_code': r['sec_code'],
            'class_code': r['class_code'],
            'qty': r['net_qty'],
            'avg_price': avg_price,
            'total_cost': round(r['buy_value'], 2),
        })
    return result


def get_recent_quik_trades(limit: int = 20):
    """Get recent QUIK trades for display."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT id, trade_num, sec_code, class_code, price, qty, value,
               trade_date, trade_time, created_at
        FROM quik_trade
        ORDER BY created_at DESC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_price_history(sec_code: str = None, limit: int = 100):
    """Get price history from the log table if available, or current snapshot."""
    # For now returns current prices; can be extended with a history table later
    conn = get_connection()
    if sec_code:
        rows = conn.execute("""
            SELECT sec_code, class_code, price, qty, value, timestamp
            FROM current_price
            WHERE sec_code = ?
            ORDER BY sec_code
        """, (sec_code,)).fetchall()
    else:
        rows = conn.execute("""
            SELECT sec_code, class_code, price, qty, value, timestamp
            FROM current_price
            ORDER BY sec_code
        """).fetchall()
    conn.close()
    return [dict(r) for r in rows]
