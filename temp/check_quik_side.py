"""Check quik_trade side/flags distribution."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from app.db import get_connection
conn = get_connection()

# Side distribution
r = conn.execute('SELECT side, COUNT(*) as c FROM quik_trade GROUP BY side').fetchall()
print('Side distribution:')
for x in r:
    print(f'  side={x["side"]!r}: {x["c"]}')

# Flags for empty side
r2 = conn.execute('SELECT flags, COUNT(*) as c FROM quik_trade WHERE side IS NULL OR side="" GROUP BY flags ORDER BY c DESC').fetchall()
print('\nFlags for empty side:')
for x in r2[:10]:
    print(f'  flags={x["flags"]}: {x["c"]}')

# Check if flags have standard bits
r3 = conn.execute("""
    SELECT 
        CASE 
            WHEN (flags & 1) = 1 THEN 'has_bit0'
            WHEN flags = 0 THEN 'flags_zero'
            ELSE 'other_flags'
        END AS flag_group,
        COUNT(*) as c
    FROM quik_trade 
    WHERE side IS NULL OR side="" 
    GROUP BY flag_group
""").fetchall()
print('\nFlag groups for empty side:')
for x in r3:
    print(f'  {x["flag_group"]}: {x["c"]}')

# Also check operation
r4 = conn.execute("SELECT operation, COUNT(*) as c FROM quik_trade WHERE side IS NULL OR side='' GROUP BY operation").fetchall()
print('\nOperation for empty side:')
for x in r4:
    print(f'  operation={x["operation"]!r}: {x["c"]}')

# operation_type
r5 = conn.execute("SELECT operation_type, COUNT(*) as c FROM quik_trade WHERE side IS NULL OR side='' GROUP BY operation_type").fetchall()
print('\noperation_type for empty side:')
for x in r5:
    print(f'  op_type={x["operation_type"]}: {x["c"]}')

conn.close()
