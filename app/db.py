"""Database module for broker report analysis."""
import sqlite3
import os
import sys
from datetime import datetime

def _get_db_dir() -> str:
    """Get cross-platform user data directory for BrokerReport."""
    if sys.platform == 'win32':
        base = os.environ.get('APPDATA', os.path.expanduser('~'))
    elif sys.platform == 'darwin':
        base = os.path.expanduser('~/Library/Application Support')
    else:
        base = os.environ.get('XDG_DATA_HOME', os.path.expanduser('~/.local/share'))
    db_dir = os.path.join(base, 'BrokerReport')
    os.makedirs(db_dir, exist_ok=True)
    return db_dir

DB_PATH = os.path.join(_get_db_dir(), 'broker.db')


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


def _date_where(alias='trade', date_from=None, date_to=None, date_col='trade_date'):
    """Build SQL WHERE snippet for date filtering.
    date_col — имя колонки с датой (trade_date, date, ...).
    """
    clauses = []
    params = []
    if date_from:
        clauses.append(f"substr({alias}.{date_col},7,4)||substr({alias}.{date_col},4,2)||substr({alias}.{date_col},1,2) >= ?")
        params.append(_norm_date(date_from))
    if date_to:
        clauses.append(f"substr({alias}.{date_col},7,4)||substr({alias}.{date_col},4,2)||substr({alias}.{date_col},1,2) <= ?")
        params.append(_norm_date(date_to))
    return clauses, params


def _source_where(alias='trade', broker=None):
    """Build SQL WHERE snippet for broker source filtering.
    broker='all' or None → no filter.
    broker='sber' → only source='sber'.
    broker='vtb' → only source='vtb'.
    broker='gazprombank' → source in ('gazprombank', 'gazprombank_v2').
    """
    clauses = []
    params = []
    if broker and broker != 'all':
        if broker == 'gazprombank':
            clauses.append(f"{alias}.source IN (?, ?)")
            params.extend(['gazprombank', 'gazprombank_v2'])
        else:
            clauses.append(f"{alias}.source=?")
            params.append(broker)
    return clauses, params


def get_connection():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Create tables if they don't exist."""
    conn = get_connection()
    cur = conn.cursor()

    # Каждая таблица создаётся отдельно — если одна упадёт,
    # остальные останутся
    ddl = [
        """CREATE TABLE IF NOT EXISTS report (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            filename        TEXT NOT NULL,
            contract        TEXT,
            investor        TEXT,
            period_start    TEXT,
            period_end      TEXT,
            source_type     TEXT DEFAULT '',
            broker          TEXT DEFAULT '',
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(filename)
        )""",
        """CREATE TABLE IF NOT EXISTS trade (
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
        )""",
        """CREATE TABLE IF NOT EXISTS repo (
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
        )""",
        """CREATE TABLE IF NOT EXISTS cash_flow (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id       INTEGER NOT NULL REFERENCES report(id),
            date            TEXT NOT NULL,
            description     TEXT NOT NULL,
            currency        TEXT DEFAULT 'RUB',
            credit          REAL DEFAULT 0,
            debit           REAL DEFAULT 0
        )""",
        """CREATE TABLE IF NOT EXISTS portfolio (
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
        )""",
        """CREATE TABLE IF NOT EXISTS financial_result (
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
        )""",
        """CREATE INDEX IF NOT EXISTS idx_trade_report ON trade(report_id)""",
        """CREATE INDEX IF NOT EXISTS idx_repo_report ON repo(report_id)""",
        """CREATE INDEX IF NOT EXISTS idx_cash_report ON cash_flow(report_id)""",
        """CREATE TABLE IF NOT EXISTS current_price (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            sec_code        TEXT NOT NULL,
            class_code      TEXT NOT NULL DEFAULT '',
            price           REAL NOT NULL,
            qty             INTEGER DEFAULT 0,
            value           REAL DEFAULT 0,
            timestamp       TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            UNIQUE(sec_code, class_code)
        )""",
        """CREATE TABLE IF NOT EXISTS quik_trade (
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
        )""",
        """CREATE INDEX IF NOT EXISTS idx_quik_trade_sec ON quik_trade(sec_code, class_code)""",
        """CREATE INDEX IF NOT EXISTS idx_quik_trade_time ON quik_trade(created_at)""",
        """CREATE TABLE IF NOT EXISTS nalog (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            year            TEXT NOT NULL,
            instrument_name TEXT,
            instrument_code TEXT,
            side            TEXT,
            deal_date       TEXT,
            deal_number     TEXT,
            fnc_code        TEXT,
            price           REAL,
            quantity        INTEGER,
            amount          REAL,
            currency        TEXT DEFAULT 'RUB',
            income          REAL DEFAULT 0,
            expense         REAL DEFAULT 0,
            source_file     TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS nalog_tax_summary (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            year            TEXT NOT NULL,
            broker_income   REAL DEFAULT 0,
            broker_taxable  REAL DEFAULT 0,
            broker_tax_calc REAL DEFAULT 0,
            broker_tax_paid REAL DEFAULT 0,
            broker_tax_due  REAL DEFAULT 0,
            depositary_income REAL DEFAULT 0,
            depositary_taxable REAL DEFAULT 0,
            total_income    REAL DEFAULT 0,
            total_taxable   REAL DEFAULT 0,
            source_file     TEXT
        )""",
    ]
    for d in ddl:
        try:
            cur.execute(d)
        except Exception as e:
            print(f'  [init_db] warning: {e}')

    conn.commit()

    # ── Migration: add broker column to quik_trade ──
    qk_cols = [r[1] for r in cur.execute("PRAGMA table_info(quik_trade)").fetchall()]
    if 'broker' not in qk_cols:
        cur.execute("ALTER TABLE quik_trade ADD COLUMN broker TEXT DEFAULT ''")
    if 'account' not in qk_cols:
        cur.execute("ALTER TABLE quik_trade ADD COLUMN account TEXT DEFAULT ''")

    # ── Migrations: add source column + unique indexes if missing ──
    for table in ('trade', 'repo', 'cash_flow', 'portfolio'):
        cols = [r[1] for r in cur.execute(f"PRAGMA table_info({table})").fetchall()]
        if 'source' not in cols:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN source TEXT NOT NULL DEFAULT 'sber'")

    # Migrate existing rows with old default 'report' → 'sber'
    for table in ('trade', 'repo', 'cash_flow', 'portfolio'):
        cur.execute(f"UPDATE {table} SET source='sber' WHERE source='report'")

    # Deduplicate rows with same (source, deal_number) before creating unique index
    for table in ('trade', 'repo'):
        cur.execute(f"""
            DELETE FROM {table} WHERE id IN (
                SELECT t2.id FROM {table} t2
                INNER JOIN (
                    SELECT MIN(id) AS keep_id, source, deal_number FROM {table}
                    WHERE deal_number IS NOT NULL AND deal_number != ''
                    GROUP BY source, deal_number
                    HAVING COUNT(*) > 1
                ) dup ON t2.deal_number = dup.deal_number AND t2.source = dup.source
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
    if 'operation_type' not in qk_cols:
        cur.execute("ALTER TABLE quik_trade ADD COLUMN operation_type INTEGER DEFAULT -1")
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_quik_trade_source_num
        ON quik_trade(source, trade_num)
        WHERE trade_num IS NOT NULL
    """)

    # Migration: fix empty side for QUIK trades that have flags
    # flags & 0x02 → buy, flags & 0x01 → sell
    cur.execute("""
        UPDATE quik_trade
        SET side = CASE
            WHEN (flags & 2) = 2 THEN 'buy'
            WHEN (flags & 1) = 1 THEN 'sell'
            ELSE side
        END
        WHERE side IS NULL OR side = ''
    """)

    # ── Instrument reference table ──────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS instrument (
            sec_code    TEXT NOT NULL,
            class_code  TEXT NOT NULL DEFAULT '',
            lotsize     INTEGER DEFAULT 1,
            min_step    REAL DEFAULT 0.01,
            short_name  TEXT DEFAULT '',
            full_name   TEXT DEFAULT '',
            updated_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            PRIMARY KEY (sec_code, class_code)
        )
    """)

    # ── Migration: add QUIK metadata columns to trade table ──
    trade_cols = [r[1] for r in cur.execute("PRAGMA table_info(trade)").fetchall()]
    for col_name, col_type, col_default in [
        ('account', "TEXT DEFAULT ''", "''"),
        ('operation_type', 'INTEGER DEFAULT -1', '-1'),
        ('flags', 'INTEGER DEFAULT 0', '0'),
        ('operation', "TEXT DEFAULT ''", "''"),
        ('class_code', "TEXT DEFAULT ''", "''"),
        ('broker', "TEXT DEFAULT ''", "''"),
    ]:
        if col_name not in trade_cols:
            cur.execute(f"ALTER TABLE trade ADD COLUMN {col_name} {col_type}")

    # ── Migration: перенос QUIK-трейдов из quik_trade в trade ──
    # Создаём псевдо-отчёт для QUIK, если ещё нет
    cur.execute("SELECT id FROM report WHERE filename='_quik_ontrade_'")
    qr = cur.fetchone()
    if qr:
        quik_report_id = qr['id']
    else:
        cur.execute("""
            INSERT INTO report(filename, contract, investor, period_start, period_end)
            VALUES ('_quik_ontrade_', 'QUIK', 'QUIK OnTrade', '', '')
        """)
        quik_report_id = cur.lastrowid

    # Сколько QUIK-трейдов уже перенесено?
    already = cur.execute(
        "SELECT COUNT(*) AS cnt FROM trade WHERE source='quik'"
    ).fetchone()['cnt']

    if already == 0:
        quik_rows = conn.execute("""
            SELECT trade_num, sec_code, class_code, price, qty, value,
                   accruedint, side, trade_date, trade_time,
                   broker, account, flags, operation, operation_type
            FROM quik_trade
            WHERE side IN ('buy', 'sell')
              AND qty > 0
              AND (repovalue IS NULL OR repovalue = 0)
            ORDER BY trade_date, trade_time, id
        """).fetchall()
        inserted = 0
        skipped = 0
        for q in quik_rows:
            trade_date = q['trade_date'] or ''
            trade_time = q['trade_time'] or ''
            side = 'Покупка' if q['side'] == 'buy' else 'Продажа'
            amount = q['value'] or 0.0
            nkd = q['accruedint'] or 0.0
            broker = q['broker'] or ''
            deal_number = str(q['trade_num']) if q['trade_num'] is not None else ''

            # Дедупликация по (source, deal_number)
            if deal_number:
                existing = cur.execute(
                    "SELECT id FROM trade WHERE source=? AND deal_number=?",
                    ('quik', deal_number)
                ).fetchone()
                if existing:
                    skipped += 1
                    continue

            cur.execute("""
                INSERT INTO trade
                    (report_id, trade_date, settle_date, trade_time,
                     security_name, security_code, class_code,
                     currency, side, quantity, price, amount, nkd,
                     broker_fee, exchange_fee, deal_number,
                     source, broker, account,
                     flags, operation, operation_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        0, 0, ?, 'quik', ?, ?, ?, ?, ?)
            """, (
                quik_report_id,
                trade_date, trade_date, trade_time,
                q['sec_code'], q['sec_code'], q['class_code'] or '',
                'RUB', side, q['qty'], q['price'], amount, nkd,
                deal_number,
                broker, q['account'] or '',
                q['flags'] or 0, q['operation'] or '', q['operation_type'] or -1,
            ))
            inserted += 1
        if inserted > 0 or skipped > 0:
            print(f'  [migrate] QUIK: {inserted} inserted, {skipped} skipped (already in trade)')

    # Уникальный индекс для QUIK-трейдов в trade
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_trade_quik_deal
        ON trade(source, deal_number)
        WHERE source='quik' AND deal_number IS NOT NULL AND deal_number != ''
    """)

    # ── Очистка B/S-дублей в trade ──
    for prefix in ('B', 'S'):
        conn.execute(f"""
            DELETE FROM trade WHERE id IN (
                SELECT dup.id FROM trade dup
                INNER JOIN trade orig ON orig.deal_number = SUBSTR(dup.deal_number, 2)
                    AND orig.source = dup.source
                    AND orig.security_code = dup.security_code
                    AND orig.side = dup.side
                WHERE dup.deal_number LIKE '{prefix}%'
                  AND dup.source = 'vtb'
            )
        """)

    # ── Нормализация кодов ОФЗ в trade ──
    conn.execute("""
        UPDATE trade SET security_code = 'SU26241RMFS8'
        WHERE security_code IN ('26241RMFS', 'RU000A105FZ9')
    """)
    conn.execute("""
        UPDATE trade SET security_code = 'SU26243RMFS4'
        WHERE security_code = '26243RMFS'
    """)
    for old, new in [('SU26244RMFS2', '26244RMFS'), ('SU26245RMFS9', '26245RMFS'),
                     ('SU26246RMFS7', '26246RMFS'), ('SU26247RMFS5', '26247RMFS'),
                     ('SU26248RMFS3', '26248RMFS'), ('SU26249RMFS1', '26249RMFS')]:
        conn.execute("UPDATE trade SET security_code=? WHERE security_code=?", (new, old))

    # ── Дедупликация cash_flow ──
    conn.execute("DELETE FROM cash_flow WHERE id NOT IN ("
                 "SELECT MIN(id) FROM cash_flow GROUP BY date, description, credit, debit)")
    conn.execute("DELETE FROM cash_flow WHERE date='Дата'")

    # ── Migration: add broker and source_type to report ──
    report_cols = [r[1] for r in cur.execute("PRAGMA table_info(report)").fetchall()]
    if 'broker' not in report_cols:
        cur.execute("ALTER TABLE report ADD COLUMN broker TEXT DEFAULT ''")
    if 'source_type' not in report_cols:
        cur.execute("ALTER TABLE report ADD COLUMN source_type TEXT DEFAULT ''")

    # Set broker for existing reports
    # Сначала чистим осиротевшие записи (без данных) — чтобы автоимпорт их пересоздал
    cur.execute("""
        DELETE FROM report WHERE id NOT IN (
            SELECT DISTINCT report_id FROM trade
            UNION SELECT DISTINCT report_id FROM cash_flow
            UNION SELECT DISTINCT report_id FROM repo
        ) AND filename != '_quik_ontrade_'
    """)
    # openbroker и my_trades приравниваем к ВТБ
    cur.execute("""
        UPDATE report SET broker='vtb', source_type='broker_report'
        WHERE (broker = '' OR broker IS NULL)
          AND (LOWER(filename) LIKE '%open%' OR LOWER(filename) LIKE '%broker%'
               OR LOWER(filename) LIKE '%my_trade%')
    """)
    # Остальные — по source из торгов/cash_flow
    cur.execute("""
        UPDATE report SET broker = (
            SELECT COALESCE(
                (SELECT source FROM trade WHERE trade.report_id = report.id
                 AND source != '' AND source != 'quik' LIMIT 1),
                (SELECT source FROM cash_flow WHERE cash_flow.report_id = report.id
                 AND source != '' AND source != 'quik' LIMIT 1),
                ''
            )
        ) WHERE broker = '' OR broker IS NULL
    """)
    cur.execute("""
        UPDATE report SET source_type='broker_report'
        WHERE (source_type = '' OR source_type IS NULL)
          AND broker IN ('sber', 'vtb')
    """)

    # Migration: openbroker и my_trades → vtb в trade.source
    # Сбрасываем уникальный индекс, обновляем source, пересоздаём
    cur.execute("DROP INDEX IF EXISTS idx_trade_source_deal")
    cur.execute("""
        UPDATE trade SET source='vtb'
        WHERE source IN ('openbroker', 'my_trades')
    """)
    # Дедупликация перед созданием индекса
    cur.execute("""
        DELETE FROM trade WHERE id NOT IN (
            SELECT MIN(id) FROM trade
            WHERE deal_number IS NOT NULL AND deal_number != ''
            GROUP BY source, deal_number
        ) AND deal_number IS NOT NULL AND deal_number != ''
    """)
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_trade_source_deal
        ON trade(source, deal_number)
        WHERE deal_number IS NOT NULL AND deal_number != ''
    """)

    # Fix _quik_ontrade_ pseudo-report — у него нет реального брокера
    cur.execute("UPDATE report SET broker='' WHERE filename='_quik_ontrade_'")

    # ── Таблица маппинга типов операций cash_flow → категории ──
    cur.execute("""
        CREATE TABLE IF NOT EXISTS cash_flow_category (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            source      TEXT NOT NULL,          -- 'sber' | 'vtb'
            pattern     TEXT NOT NULL,           -- подстрока для поиска в description
            match_mode  TEXT NOT NULL DEFAULT 'contains',  -- 'contains' | 'prefix'
            category    TEXT NOT NULL,           -- Дивиденд, Купон, Налог, Пополнение, Вывод ДС, Операционные
            priority    INTEGER NOT NULL DEFAULT 0,  -- меньше = выше приоритет
            comment     TEXT DEFAULT ''
        )
    """)
    # Очищаем старые данные при перезапуске (перезаписываем актуальные)
    cur.execute("DELETE FROM cash_flow_category")
    seed_mapping = [
        # ── SBER ──
        ('sber', 'Выплата дивидендов',        'contains', 'Дивиденд',     1, 'Дивиденды с удержанным налогом'),
        ('sber', 'Выплата купонов',           'contains', 'Купон',        1, 'Купонный доход по облигациям'),
        ('sber', 'Зачисление',                'prefix',   'Пополнение',   2, 'Пополнение счёта / бонусы / акции'),
        ('sber', 'Списание д/с. Налог',       'prefix',   'Налог',        1, 'Налог на доходы физ.лиц'),
        ('sber', 'Списание д/с',              'prefix',   'Вывод ДС',     3, 'Вывод денежных средств'),
        ('sber', 'Комиссия',                  'prefix',   'Операционные', 0, 'Комиссии биржи и брокера'),
        ('sber', 'Сделка от',                 'prefix',   'Операционные', 0, 'Расчёты по сделкам'),
        # ── VTB ──
        ('vtb', 'Вознаграждение',             'prefix',   'Операционные', 0, 'Комиссии брокера'),
        ('vtb', 'Дивиденды —',                'prefix',   'Дивиденд',     1, 'Дивиденды по акциям'),
        ('vtb', 'Дивиденды ',                 'prefix',   'Дивиденд',     1, 'Дивиденды (альт. формат)'),
        ('vtb', 'Зачисление денежных средств','prefix',   'Пополнение',   1, 'Пополнение счёта'),
        ('vtb', 'Купонный доход —',           'prefix',   'Купон',        1, 'Купонный доход по облигациям'),
        ('vtb', 'НДФЛ —',                     'prefix',   'Налог',        1, 'Налог на доходы'),
        ('vtb', 'Перевод денежных средств',   'prefix',   'Пополнение',   2, 'Перевод между субсчетами'),
        ('vtb', 'Сальдо расчетов',            'prefix',   'Операционные', 0, 'Технические сальдо (расчеты)'),
        ('vtb', 'Сальдо расчётов',            'prefix',   'Операционные', 0, 'Технические сальдо (расчёты)'),
        ('vtb', 'Списание денежных средств —','prefix',   'Вывод ДС',     2, 'Вывод денежных средств'),
    ]
    for src, pat, mode, cat, prio, comment in seed_mapping:
        cur.execute(
            "INSERT INTO cash_flow_category(source, pattern, match_mode, category, priority, comment) VALUES (?,?,?,?,?,?)",
            (src, pat, mode, cat, prio, comment)
        )

    conn.commit()
    conn.close()


# ── Analytical queries ──────────────────────────────────────────

def get_trade_profit(report_id=None, date_from=None, date_to=None, broker=None):
    """
    Return per-security realized P&L using LIFO chronological matching.
    Only trades where both buy AND sell happened within this period.
    Opening positions are NOT included.
    """
    lots, _ = _match_trades_lifo(report_id, date_from, date_to, broker)

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


def get_trade_lots(report_id=None, date_from=None, date_to=None, broker=None):
    """
    Return individual matched buy→sell lots in chronological order.
    Each lot shows: buy_date, sell_date, qty, buy_price, sell_price, profit, fees.
    """
    lots, _ = _match_trades_lifo(report_id, date_from, date_to, broker)
    return lots


def get_open_trades(report_id=None, date_from=None, date_to=None, broker=None):
    """
    Buys that have NOT been closed by a sell within this period.
    Returns unmatched buy lots, merged by (security_code, buy_date, buy_price).

    ВНИМАНИЕ: дата фильтрует только продажи (чтобы определить, какие покупки
    были закрыты в периоде), но сами покупки показываются ВСЕ независимо от
    даты — открытая позиция должна быть видна всегда, даже если куплена давно.

    Все сделки в единой таблице trade (source='sber'/'vtb'/'quik').
    QUIK-трейды участвуют в LIFO-матчинге вместе со своим брокером.
    """
    _, unmatched = _match_trades_lifo(report_id, None, None, broker)

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

    return merged


def get_instrument_summary(report_id=None, date_from=None, date_to=None, broker=None):
    """
    Aggregate summary per instrument from OPEN (unmatched) buy positions.
    Shows total qty, average price, total cost per security.
    """
    from collections import defaultdict
    _, unmatched = _match_trades_lifo(report_id, date_from, date_to, broker)

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


def get_repo_total(report_id=None, date_from=None, date_to=None, broker=None):
    """Get total repo costs (interest + fees)."""
    conn = get_connection()
    where_clauses, params = _date_where('repo', date_from, date_to)
    src_clauses, src_params = _source_where('repo', broker)
    where_clauses.extend(src_clauses)
    params.extend(src_params)
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


# ── Кэш маппинга типов операций cash_flow → категории ─────────
_cf_category_cache = None

def _load_cf_category_mapping():
    """Загружает маппинг (source, pattern, match_mode) → category из БД."""
    global _cf_category_cache
    if _cf_category_cache is not None:
        return _cf_category_cache
    conn = get_connection()
    rows = conn.execute("""
        SELECT source, pattern, match_mode, category, priority
        FROM cash_flow_category
        ORDER BY priority, id
    """).fetchall()
    conn.close()
    _cf_category_cache = rows
    return rows


def _categorize_cash_flow(description: str, source: str) -> str:
    """Определяет категорию cash_flow по маппингу из БД.

    Сначала проверяет точные pattern'ы из таблицы cash_flow_category
    (prefix — начало строки, contains — вхождение подстроки).
    Если не найдено — fallback на старые keyword'ы.
    Возвращает категорию или 'Прочее'.
    """
    mapping = _load_cf_category_mapping()
    desc_lower = description.lower()

    # 1. Проверяем по маппингу из БД
    for row in mapping:
        if row['source'] != source:
            continue
        pat = row['pattern']
        match_mode = row['match_mode']
        if match_mode == 'prefix':
            if desc_lower.startswith(pat.lower()):
                return row['category']
        else:  # contains
            if pat.lower() in desc_lower:
                return row['category']

    # 2. Fallback: старые keyword'ы (для обратной совместимости)
    FALLBACK = [
        ('Дивиденд',     ['Дивиденд', 'дивиденд']),
        ('Купон',        ['Купон', 'Выплата купонов', 'купонный']),
        ('Налог',        ['НДФЛ', 'Уплата налога', 'Оплата налога', 'Списание задолженности по налогу',
                          'Налог на доходы', 'Налог удержан']),
        ('Пополнение',   ['Зачисление денежных средств', 'Зачисление д/с', 'Перевод денежных средств',
                          'Пополнение']),
        ('Вывод ДС',     ['Списание д/с — Вывод', 'Списание денежных средств — Вывод',
                          'Списание д/с', 'Списание денежных средств']),
    ]
    for cat, keywords in FALLBACK:
        if any(kw.lower() in desc_lower for kw in keywords):
            return cat
    return 'Прочее'


def get_cash_flow_summary(report_id=None, date_from=None, date_to=None, broker=None):
    """Get non-operational cash flow grouped by category.

    Исключаются операционные движения (Сделка, Комиссия) —
    они уже учтены в trade-расчётах. Остаются только:
    Налог, Дивиденд, Купон, Пополнение, Вывод ДС, Переводы.
    """
    conn = get_connection()
    where_clauses, params = _date_where('cash_flow', date_from, date_to, date_col='date')
    if report_id is not None:
        where_clauses.append("cash_flow.report_id=?")
        params.append(report_id)
    src_clauses, src_params = _source_where('cash_flow', broker)
    where_clauses.extend(src_clauses)
    params.extend(src_params)
    where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"
    rows = conn.execute(f"""
        SELECT description, source,
               COALESCE(SUM(credit),0) AS credits,
               COALESCE(SUM(debit),0) AS debits
        FROM cash_flow WHERE {where_sql}
        GROUP BY description, source
        ORDER BY description
    """, params).fetchall()
    conn.close()

    result = {}
    for row in rows:
        desc = row['description']
        src = row['source']
        amount = row['credits'] - row['debits']

        cat = _categorize_cash_flow(desc, src)

        # Пропускаем операционные движения
        if cat == 'Операционные':
            continue

        result[cat] = {'amount': round(result.get(cat, {'amount': 0})['amount'] + amount, 2)}
    return result


def _normalize_sec_code(code: str) -> str:
    """Normalize bond security codes to prevent phantom open positions.

    ОФЗ облигации могут иметь несколько кодов:
      - 26241RMFS / SU26241RMFS8 (старый/новый тикер)
      - RU000A105FZ9 (ISIN)
    Приводим все к единому виду для корректного LIFO-матчинга.
    """
    if not code:
        return code
    # ISIN → ticker mapping (ОФЗ)
    isin_map = {
        'RU000A105FZ9': 'SU26241RMFS8',  # ОФЗ 26241
    }
    if code in isin_map:
        return isin_map[code]
    # Старый тикер без SU → новый с SU
    # SU26243RMFS4 → остаётся, 26243RMFS → SU26243RMFS4
    # Определяем по длине: старый = 9-10 символов (только цифры+RMFS)
    # новый = с префиксом SU (11+ символов)
    if not code.startswith('SU') and code.endswith('RMFS') and len(code) <= 10:
        # Ищем соответствующий код с SU
        # Формат: было 26241RMFS, стало SU26241RMFS8
        # Извлекаем номер облигации (26241) и добавляем SU + младшая цифра
        import re
        m = re.match(r'(\d+)(RMFS)', code)
        if m:
            num = m.group(1)
            # Определяем последнюю цифру: 8 для 26241, 4 для 26243, 2 для 26244 и т.д.
            suffix_map = {'26241': '8', '26243': '4', '26244': '2', '26245': '9',
                         '26246': '7', '26247': '5', '26248': '3', '26249': '1',
                         '26223': '6', '26236': '8', '26237': '6', '26238': '4',
                         '26234': '8', '26219': '5', '26226': '5', '26229': '1'}
            suffix = suffix_map.get(num, '')
            if suffix:
                return f'SU{num}RMFS{suffix}'
    return code


def _run_lifo(trades, name_map):
    """Run LIFO matching on a list of trades for a single broker.

    Args:
        trades: list of dicts with keys (security_code, security_name, side,
                quantity, amount, broker_fee, exchange_fee, trade_date,
                trade_time, deal_number, source)
        name_map: dict {security_code: security_name}

    Returns: (matched_lots, unmatched_buys)
    """
    from collections import defaultdict
    by_sec = defaultdict(list)
    for t in trades:
        code = _normalize_sec_code(t['security_code'] or t['security_name'])
        by_sec[code].append(t)

    all_lots = []
    all_unmatched = []

    for code, txns in by_sec.items():
        name = name_map.get(code, txns[0]['security_name'])
        buy_queue = []

        for t in txns:
            if t['side'] == 'Покупка' and t['quantity'] > 0:
                buy_queue.append([
                    t['quantity'],
                    t['amount'] / t['quantity'],
                    t,
                    (t['broker_fee'] or 0) + (t['exchange_fee'] or 0)
                ])

            elif t['side'] == 'Продажа' and t['quantity'] > 0:
                remaining = t['quantity']
                sell_unit_price = t['amount'] / t['quantity']
                sell_fee = (t['broker_fee'] or 0) + (t['exchange_fee'] or 0)

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

                    lot_source = buy_row['source'] if buy_row['source'] else t['source']
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
                        'source': lot_source,
                    })

                    buy_queue[-1][0] -= used
                    if buy_queue[-1][0] <= 0:
                        buy_queue.pop()
                    remaining -= used

        # Финальный sweep: если unmatched остались и с одной и с другой
        # стороны — матчим по FIFO (самые старые покупки с самыми старыми
        # продажами) чтобы избежать артефактов LIFO-порядка.
        if buy_queue:
            total_buy = sum(t['quantity'] for t in txns if t['side'] == 'Покупка')
            total_sell = sum(t['quantity'] for t in txns if t['side'] == 'Продажа')
            # unmatched_sell = общее кол-во продаж, не нашедших пару
            matched_qty = sum(l['qty'] for l in all_lots if l['security_code'] == code)
            unmatched_sell = total_sell - matched_qty
            if unmatched_sell > 0:
                # Есть потерянные продажи — матчим buy_queue с ними по FIFO
                # Сортируем unmatched покупки по дате (FIFO)
                unsold_buys = sorted(buy_queue, key=lambda x: x[2]['trade_date'] + (x[2]['trade_time'] or ''))
                remaining_sell = unmatched_sell
                for lot in unsold_buys:
                    qty = lot[0]
                    if qty <= 0 or remaining_sell <= 0:
                        break
                    used = min(qty, remaining_sell)
                    b = lot[2]
                    sell_price = lot[1]  # по цене покупки (profit=0)
                    all_lots.append({
                        'security_code': code,
                        'security_name': name,
                        'qty': used,
                        'buy_date': b['trade_date'],
                        'buy_price': round(lot[1], 2),
                        'buy_amount': round(used * lot[1], 2),
                        'buy_fee': round(lot[3] * (used / b['quantity']), 2) if b['quantity'] > 0 else 0,
                        'sell_date': b['trade_date'],
                        'sell_price': round(lot[1], 2),
                        'sell_amount': round(used * lot[1], 2),
                        'sell_fee': 0,
                        'profit': 0.0,
                        'source': b['source'] if b['source'] else '',
                    })
                    lot[0] -= used
                    remaining_sell -= used
                # Очищаем пустые
                buy_queue = [lot for lot in buy_queue if lot[0] > 0]
            elif total_buy == total_sell and not unmatched_sell:
                # Нетто-позиция закрыта — всё сматчилось, чистим остатки
                fifo_queue = sorted(buy_queue, key=lambda x: x[2]['trade_date'] + (x[2]['trade_time'] or ''))
                for lot in fifo_queue:
                    qty = lot[0]
                    if qty <= 0:
                        continue
                    b = lot[2]
                    all_lots.append({
                        'security_code': code,
                        'security_name': name,
                        'qty': qty,
                        'buy_date': b['trade_date'],
                        'buy_price': round(lot[1], 2),
                        'buy_amount': round(qty * lot[1], 2),
                        'buy_fee': round(lot[3], 2),
                        'sell_date': b['trade_date'],
                        'sell_price': round(lot[1], 2),
                        'sell_amount': round(qty * lot[1], 2),
                        'sell_fee': 0,
                        'profit': 0.0,
                        'source': b['source'] if b['source'] else '',
                    })
                buy_queue.clear()

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
                'source': b['source'] if b['source'] else '',
            })

    return all_lots, all_unmatched


def _match_trades_lifo(report_id=None, date_from=None, date_to=None, broker=None):
    """
    Core LIFO matching engine.

    Все сделки в единой таблице trade. QUIK-трейды имеют source='quik'
    и broker='sber'/'vtb'.

    Правила матчинга:
    - broker='sber' или 'vtb': source = выбранный брокер + QUIK (с этим broker)
    - broker=None или 'all': каждый источник матчится отдельно:
        source='sber' + QUIK(c broker='sber')
        source='vtb'  + QUIK(c broker='vtb')
        source='quik' (без broker или с неизвестным broker) — отдельно

    Returns (matched_lots, unmatched_buys).
    """
    conn = get_connection()

    if broker and broker != 'all':
        if broker == 'gazprombank':
            source_groups = [('gazprombank', 'gazprombank_v2')]
        else:
            source_groups = [(broker,)]
    else:
        source_groups = [
            ('sber',),
            ('vtb',),
            ('gazprombank', 'gazprombank_v2'),
        ]

    all_lots = []
    all_unmatched = []
    name_map = {}

    for src_group in source_groups:
        where_clauses, params = _date_where('trade', date_from, date_to)

        # Группа источников, которые матчатся вместе (один брокер)
        placeholders = ','.join('?' * len(src_group))
        if broker and broker != 'all':
            where_clauses.append(f"(trade.source IN ({placeholders}) OR (trade.source='quik' AND trade.broker=?))")
            params.extend(src_group)
            params.append(broker)
        else:
            where_clauses.append(f"(trade.source IN ({placeholders}) OR (trade.source='quik' AND trade.broker IN ({placeholders})))")
            params.extend(src_group)
            params.extend(src_group)

        if report_id is not None:
            where_clauses.append("(trade.report_id=? OR trade.source='quik')")
            params.append(report_id)

        where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"

        trades_raw = conn.execute(f"""
            SELECT id, security_code, security_name, side, quantity, amount,
                   broker_fee, exchange_fee, trade_date, trade_time, deal_number, source
            FROM trade
            WHERE {where_sql}
            ORDER BY substr(trade_date,7,4)||substr(trade_date,4,2)||substr(trade_date,1,2), trade_time, LENGTH(deal_number), deal_number
        """, params).fetchall()

        trades = []
        seen_deals = set()
        for t in trades_raw:
            key = (t['security_code'] or t['security_name'], t['deal_number'])
            if key in seen_deals:
                continue
            seen_deals.add(key)
            trades.append(t)
            code = t['security_code'] or t['security_name']
            if code not in name_map:
                name_map[code] = t['security_name']

        lots, unmatched = _run_lifo(trades, name_map)
        all_lots.extend(lots)
        all_unmatched.extend(unmatched)

    # QUIK-трейды без пометки брокера — отдельно
    if not broker or broker == 'all':
        quik_nobroker = conn.execute(f"""
            SELECT id, security_code, security_name, side, quantity, amount,
                   broker_fee, exchange_fee, trade_date, trade_time, deal_number, source
            FROM trade
            WHERE source='quik' AND (broker IS NULL OR broker='')
        """, []).fetchall()
        if quik_nobroker:
            q_trades = []
            for t in quik_nobroker:
                code = t['security_code'] or t['security_name']
                if code not in name_map:
                    name_map[code] = t['security_name']
                q_trades.append(t)
            q_lots, q_unmatched = _run_lifo(q_trades, name_map)
            all_lots.extend(q_lots)
            all_unmatched.extend(q_unmatched)

    conn.close()
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
        SELECT id, filename, contract, investor, period_start, period_end, created_at,
               COALESCE(source_type, '') AS source_type,
               COALESCE(broker, '') AS broker
        FROM report ORDER BY created_at DESC
    """).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        # Для старых записей без source_type — определяем по расширению
        if not d.get('source_type'):
            fn = d['filename'].lower()
            if fn.endswith('.xlsx') or fn.endswith('.xls'):
                d['source_type'] = 'broker_report'
            else:
                d['source_type'] = 'broker_report'
        if not d.get('broker'):
            fn = d['filename'].lower()
            if fn.endswith('.xlsx') or fn.endswith('.xls'):
                d['broker'] = 'vtb'
            else:
                d['broker'] = 'sber'
        result.append(d)
    return result


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

def _is_bond_class(class_code: str) -> bool:
    """Detect if a class_code represents bonds (price in % of nominal)."""
    return class_code in ('TQOB', 'TQCB', 'TQOD') or class_code.startswith('TQO')


def save_price(sec_code: str, price: float, qty: int = 0, value: float = 0, class_code: str = ''):
    """Upsert current price for a security.

    For bonds (class_code TQOB/TQCB), QUIK sends price as % of nominal.
    We convert to ruble price = value / qty for correct P&L calculation.
    """
    import sqlite3 as _sqlite3
    conn = _sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("CREATE TABLE IF NOT EXISTS current_price ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "sec_code TEXT NOT NULL, class_code TEXT NOT NULL DEFAULT '',"
        "price REAL NOT NULL, qty INTEGER DEFAULT 0, value REAL DEFAULT 0,"
        "timestamp TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),"
        "UNIQUE(sec_code, class_code))")
    # Convert bond % price to ruble price
    if _is_bond_class(class_code) and qty > 0 and value > 0:
        price = round(value / qty, 2)
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
    For bonds (TQOB/TQCB), converts % price to ruble price = value / qty.
    """
    import sqlite3 as _sqlite3
    conn = _sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = _sqlite3.Row
    # Гарантируем, что таблица существует (быстро, если уже есть)
    conn.execute("CREATE TABLE IF NOT EXISTS current_price ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "sec_code TEXT NOT NULL, class_code TEXT NOT NULL DEFAULT '',"
        "price REAL NOT NULL, qty INTEGER DEFAULT 0, value REAL DEFAULT 0,"
        "timestamp TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),"
        "UNIQUE(sec_code, class_code))")
    cur = conn.cursor()
    cur.execute("BEGIN")
    for p in prices:
        price = p['price']
        class_code = p.get('class_code', '')
        qty = p.get('qty', 0)
        value = p.get('value', 0)
        if _is_bond_class(class_code) and qty > 0 and value > 0:
            price = round(value / qty, 2)
        cur.execute("""
            INSERT INTO current_price (sec_code, class_code, price, qty, value, timestamp)
            VALUES (?, ?, ?, ?, ?, datetime('now', 'localtime'))
            ON CONFLICT(sec_code, class_code) DO UPDATE SET
                price = excluded.price,
                qty = excluded.qty,
                value = excluded.value,
                timestamp = datetime('now', 'localtime')
        """, (p['sec_code'], class_code, price, qty, value))
    conn.commit()
    conn.close()


# ── Карта счетов → брокер ─────────────────────────────────────
# Ключ — номер счёта (account) из QUIK OnTrade.
# Значение — 'sber' или 'vtb'. Добавьте свои счета.
ACCOUNT_BROKER_MAP = {
    # Заполнить позже: 'номер_счёта': 'sber' or 'vtb'
    # Список счетов можно получить через GET /api/accounts
}


def _resolve_broker(t: dict) -> str:
    """Определить брокера по счёту (account) из QUIK.

    Приоритет:
    1. Явное поле broker из JSON (если не пустое)
    2. Маппинг account → broker из ACCOUNT_BROKER_MAP
    3. settlecode (код расчётов) — часто содержит код брокера
    4. Пустая строка (неизвестный)
    """
    broker = (t.get('broker') or '').strip()
    if broker:
        return broker

    account = (t.get('account') or '').strip()
    if account and account in ACCOUNT_BROKER_MAP:
        return ACCOUNT_BROKER_MAP[account]

    settlecode = (t.get('settlecode') or '').strip()
    # Часто settlecode содержит код брокера: 'Y0'/'Y1'/'N0'/'N1' и т.д.
    # Если понадобится — можно добавить маппинг settlecode → broker

    return ''


def save_quik_trades(trades: list):
    """Save QUIK trades (OnAllTrade data) to SQLite in a batch.

    Пишет в единую таблицу trade (source='quik'), а также дублирует
    в quik_trade (для обратной совместимости при откате).

    Firebase = source of truth: после каждого батча триггерим push.
    """
    conn = get_connection()
    cur = conn.cursor()

    # Получаем или создаём псевдо-отчёт для QUIK
    cur.execute("SELECT id FROM report WHERE filename='_quik_ontrade_'")
    qr = cur.fetchone()
    if qr:
        quik_report_id = qr['id']
    else:
        cur.execute("""
            INSERT INTO report(filename, contract, investor, period_start, period_end)
            VALUES ('_quik_ontrade_', 'QUIK', 'QUIK OnTrade', '', '')
        """)
        quik_report_id = cur.lastrowid

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
        else:
            # Приоритет для OnTrade: operation_type → flags → side от Lua
            # operation_type: 0 = покупка, 1 = продажа, -1 = неизвестно
            op_type = t.get('operation_type', -1)
            if op_type == 0:
                side = 'buy'
            elif op_type == 1:
                side = 'sell'
            elif flags:
                # OnTrade: бит 0 (0x01) = 1 → покупка, 0 → продажа
                # Проверяем, что flags содержит хотя бы один стандартный бит
                # (0x01-0x20). Если только нестандартные (0x40+), как у SBMM
                # при неторговых операциях — не доверяем, оставляем side от Lua.
                if flags & 0x3F:
                    if flags & 0x01:
                        side = 'buy'
                    else:
                        side = 'sell'
                # иначе флаги нестандартные — оставляем side от Lua как есть

        # QUIK OnTrade передаёт qty в лотах. Фактическое количество
        # акций = value / price (price — за 1 акцию), но если известен
        # lotsize из таблицы instrument — умножаем qty на lotsize.
        price = t['price']
        qty = t['qty']
        t_val = t.get('value', 0) or 0
        if price > 0:
            actual_qty = int(round(t_val / price))
            if actual_qty > 0:
                qty = actual_qty

        # Если в БД есть lotsize, используем для контроля (может отличаться
        # от value/price для некоторых инструментов)
        lotsize = get_instrument_lotsize(t.get('sec_code', ''), t.get('class_code', ''))
        if lotsize > 1 and qty == 1 and t_val > 0:
            pass  # уже исправлено выше через actual_qty

        account = (t.get('account') or '').strip()
        broker = _resolve_broker(t)
        op_type = t.get('operation_type', -1)
        sec_code = t.get('sec_code', '')
        class_code = t.get('class_code', '')
        deal_number = str(t.get('trade_num', ''))
        side_ru = 'Покупка' if side == 'buy' else 'Продажа'

        # ── Пишем в trade (единая таблица) ──
        cur.execute(f"""
            INSERT INTO trade
                (report_id, trade_date, settle_date, trade_time,
                 security_name, security_code, class_code,
                 currency, side, quantity, price, amount, nkd,
                 broker_fee, exchange_fee, deal_number,
                 source, broker, account,
                 flags, operation, operation_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    0, 0, ?, 'quik', ?, ?, ?, ?, ?)
            ON CONFLICT(source, deal_number) WHERE source='quik' AND deal_number IS NOT NULL AND deal_number != ''
            DO UPDATE SET
                side=COALESCE(NULLIF(trade.side, ''), excluded.side),
                price=excluded.price, quantity=excluded.quantity, amount=excluded.amount,
                trade_date=excluded.trade_date, trade_time=excluded.trade_time,
                broker=COALESCE(NULLIF(trade.broker, ''), excluded.broker),
                account=COALESCE(NULLIF(trade.account, ''), excluded.account),
                flags=COALESCE(NULLIF(trade.flags, 0), excluded.flags),
                operation=COALESCE(NULLIF(trade.operation, ''), excluded.operation),
                operation_type=COALESCE(NULLIF(trade.operation_type, -1), excluded.operation_type)
        """, (
            quik_report_id,
            trade_date, trade_date, trade_time,
            sec_code, sec_code, class_code,
            'RUB', side_ru, qty, price, t_val,
            t.get('accruedint', 0),
            deal_number,
            broker, account,
            flags, operation, op_type,
        ))

        # ── Также дублируем в quik_trade (для обратной совместимости) ──
        cur.execute("""
            INSERT INTO quik_trade
                (trade_num, sec_code, class_code, price, qty, value,
                 accruedint, yield, settlecode,
                 reporate, repovalue, repo2value, repoterm, period,
                 trade_date, trade_time, source, side, flags, operation,
                 broker, account, operation_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'quik',
                    ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, trade_num) DO UPDATE SET
                side=COALESCE(NULLIF(quik_trade.side, ''), excluded.side),
                flags=COALESCE(NULLIF(quik_trade.flags, 0), excluded.flags),
                operation=COALESCE(NULLIF(quik_trade.operation, ''), excluded.operation),
                operation_type=COALESCE(NULLIF(quik_trade.operation_type, -1), excluded.operation_type),
                price=excluded.price, qty=excluded.qty, value=excluded.value,
                trade_date=excluded.trade_date, trade_time=excluded.trade_time,
                broker=COALESCE(NULLIF(quik_trade.broker, ''), excluded.broker),
                account=COALESCE(NULLIF(quik_trade.account, ''), excluded.account)
        """, (
            t.get('trade_num'), sec_code, class_code,
            price, qty, t_val,
            t.get('accruedint', 0), t.get('yield', 0), t.get('settlecode', ''),
            t.get('repolate', 0), t.get('repovalue', 0), t.get('repo2value', 0),
            t.get('repoterm', 0), t.get('period', 0),
            trade_date, trade_time,
            side, flags, operation, broker, account, op_type
        ))
    conn.commit()
    conn.close()

    # Firebase = source of truth: пушим после каждого батча
    try:
        from app.replication import push as push_to_cloud
        push_to_cloud()
    except Exception:
        pass  # Firebase недоступен — не фатально, данные есть локально


def get_current_prices():
    """Get latest price per instrument (deduplicated by sec_code)."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT sec_code, class_code, price, qty, value, timestamp
        FROM (
            SELECT *,
                   ROW_NUMBER() OVER (PARTITION BY sec_code ORDER BY timestamp DESC) AS rn
            FROM current_price
        ) ranked
        WHERE rn = 1
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


def save_instruments_batch(instruments: list):
    """Upsert instrument reference data (lotsize, min_step, etc.) from QUIK."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("BEGIN")
    for inst in instruments:
        cur.execute("""
            INSERT INTO instrument (sec_code, class_code, lotsize, min_step,
                                    short_name, full_name, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))
            ON CONFLICT(sec_code, class_code) DO UPDATE SET
                lotsize = COALESCE(NULLIF(excluded.lotsize, 0), instrument.lotsize),
                min_step = COALESCE(NULLIF(excluded.min_step, 0), instrument.min_step),
                short_name = COALESCE(NULLIF(excluded.short_name, ''), instrument.short_name),
                full_name = COALESCE(NULLIF(excluded.full_name, ''), instrument.full_name),
                updated_at = datetime('now', 'localtime')
        """, (
            inst['sec_code'], inst.get('class_code', ''),
            int(inst.get('lotsize', 1)),
            float(inst.get('min_step', 0.01)),
            inst.get('short_name', ''),
            inst.get('full_name', ''),
        ))
    conn.commit()
    conn.close()


def get_instrument_lotsize(sec_code: str, class_code: str = '') -> int:
    """Get lot size for a security from the instrument reference."""
    conn = get_connection()
    r = conn.execute("""
        SELECT lotsize FROM instrument
        WHERE sec_code=? AND class_code=?
    """, (sec_code, class_code)).fetchone()
    conn.close()
    if r and r['lotsize'] and r['lotsize'] > 1:
        return r['lotsize']
    return 1


def get_quik_positions():
    """Aggregate QUIK positions from OnTrade data (читает из trade, source='quik').
       side='Покупка' → +qty, side='Продажа' → -qty.
    """
    conn = get_connection()
    rows = conn.execute("""
        SELECT security_code AS sec_code, class_code,
               SUM(CASE WHEN side='Продажа' THEN -quantity ELSE quantity END) AS net_qty,
               SUM(CASE WHEN side='Продажа' THEN 0 ELSE amount END) AS buy_value
        FROM trade
        WHERE source='quik'
          AND side IN ('Покупка', 'Продажа')
          AND quantity > 0
        GROUP BY security_code, class_code
        HAVING net_qty > 0
        ORDER BY security_code
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
    """Get recent QUIK trades for display (читает из trade, source='quik')."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT id, deal_number AS trade_num, security_code AS sec_code,
               class_code, price, quantity AS qty, amount AS value,
               trade_date, trade_time
        FROM trade
        WHERE source='quik'
        ORDER BY id DESC
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


# ── Список сделок с фильтрацией и пагинацией ─────────────────

_TRADE_SORT_OPTIONS = {
    'date': "substr(t.trade_date,7,4)||substr(t.trade_date,4,2)||substr(t.trade_date,1,2)",
    'date_desc': "substr(t.trade_date,7,4)||substr(t.trade_date,4,2)||substr(t.trade_date,1,2) DESC",
    'amount': "t.amount DESC",
    'amount_asc': "t.amount",
    'code': "t.security_code",
    'code_desc': "t.security_code DESC",
}

def get_trades_list(security_code: str = '',
                    date_from: str = '',
                    date_to: str = '',
                    source: str = '',
                    page: int = 1,
                    per_page: int = 50,
                    sort: str = 'date_desc') -> tuple[list[dict], int]:
    """Получить список сделок с фильтрацией и пагинацией.

    Возвращает (trades, total_count).
    """
    conn = get_connection()
    where = []
    params = []

    # Фильтр по коду инструмента
    if security_code:
        where.append("(t.security_code LIKE ? OR t.security_name LIKE ?)")
        params.extend([f'%{security_code}%', f'%{security_code}%'])

    # Фильтр по датам
    if date_from:
        where.append("substr(t.trade_date,7,4)||substr(t.trade_date,4,2)||substr(t.trade_date,1,2) >= ?")
        params.append(_norm_date(date_from))
    if date_to:
        where.append("substr(t.trade_date,7,4)||substr(t.trade_date,4,2)||substr(t.trade_date,1,2) <= ?")
        params.append(_norm_date(date_to))

    # Фильтр по источнику
    if source and source != 'all':
        where.append("t.source = ?")
        params.append(source)

    where_sql = " AND ".join(where) if where else "1=1"

    # Сортировка
    order_sql = _TRADE_SORT_OPTIONS.get(sort, "substr(t.trade_date,7,4)||substr(t.trade_date,4,2)||substr(t.trade_date,1,2) DESC")

    # Считаем общее количество (все сделки в одной таблице trade)
    total = conn.execute(f"""
        SELECT COUNT(*) as cnt FROM trade t WHERE {where_sql}
    """, params).fetchone()['cnt']

    # Пагинация
    offset = (page - 1) * per_page

    # Все сделки из единой таблицы trade (source='sber'/'vtb'/'quik')
    rows = conn.execute(f"""
        SELECT
            t.id,
            t.source AS source_type,
            t.source AS broker,
            t.trade_date,
            COALESCE(t.settle_date, '') AS settle_date,
            COALESCE(t.trade_time, '') AS trade_time,
            t.security_name,
            t.security_code,
            t.currency,
            t.side,
            t.quantity,
            t.price,
            t.amount,
            t.nkd,
            t.broker_fee,
            t.exchange_fee,
            (t.broker_fee + t.exchange_fee) AS total_fee,
            t.deal_number,
            COALESCE(t.comment, '') AS comment,
            CAST(CASE WHEN t.source='quik' THEN t.deal_number ELSE NULL END AS INTEGER) AS trade_num,
            COALESCE(r.filename, '') AS report_filename,
            COALESCE(t.account, '') AS account,
            COALESCE(t.operation_type, -1) AS operation_type
        FROM trade t
        LEFT JOIN report r ON r.id = t.report_id
        WHERE {where_sql}
        ORDER BY {order_sql}
        LIMIT ? OFFSET ?
    """, params + [per_page, offset]).fetchall()

    conn.close()
    return [dict(r) for r in rows], total
