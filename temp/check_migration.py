"""Проверить миграцию QUIK -> trade."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from app.db import get_connection, init_db

init_db()
conn = get_connection()

cols = [r[1] for r in conn.execute('PRAGMA table_info(trade)').fetchall()]
print('trade columns:', cols)

cnt = conn.execute("SELECT COUNT(*) AS cnt FROM trade WHERE source='quik'").fetchone()['cnt']
print(f'QUIK trades in trade: {cnt}')

cnt2 = conn.execute('SELECT COUNT(*) AS cnt FROM quik_trade').fetchone()['cnt']
print(f'Rows in quik_trade table: {cnt2}')

# Check broker values
brokers = conn.execute("SELECT DISTINCT broker, COUNT(*) as c FROM trade WHERE source='quik' GROUP BY broker").fetchall()
print('QUIK trades by broker:')
for r in brokers:
    print(f'  broker={r["broker"]!r}: {r["c"]}')

conn.close()
