"""Проверить, какие QUIK-трейды участвуют в LIFO и их комиссии."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from app.db import get_connection

conn = get_connection()

# QUIK trades in trade table
quik = conn.execute("""
    SELECT security_code, deal_number, side, quantity, amount,
           broker_fee, exchange_fee, broker
    FROM trade
    WHERE source='quik'
    ORDER BY trade_date
""").fetchall()

print(f'QUIK trades in trade: {len(quik)}')
for r in quik:
    print(f"  {r['security_code']:>10} #{r['deal_number']:>15} {r['side']:>8} qty={r['quantity']:>4} amount={r['amount']:>10.2f} fees={r['broker_fee']+r['exchange_fee']:>6.2f} broker={r['broker']}")

# Overlap with sber trades
print()
overlap = conn.execute("""
    SELECT t.security_code, t.deal_number
    FROM trade t
    WHERE t.source='quik' AND t.deal_number != ''
      AND EXISTS (SELECT 1 FROM trade s WHERE s.source='sber' AND s.deal_number=t.deal_number AND s.security_code=t.security_code)
""").fetchall()
print(f'QUIK trades that overlap with sber: {len(overlap)}')
for r in overlap[:10]:
    print(f"  {r['security_code']} #{r['deal_number']}")

conn.close()
