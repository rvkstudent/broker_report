"""Расчёт: переплата налогов из-за неучтённого убытка 2024."""
import sqlite3, os
c = sqlite3.connect(os.path.join('app', 'broker.db'))
c.row_factory = sqlite3.Row

# Сводка из отчётов
rows = c.execute("SELECT * FROM nalog_tax_summary ORDER BY year").fetchall()
print("=== Данные из налоговых отчётов ===")
for r in rows:
    d = dict(r)
    print(f"\n{d['year']}:")
    print(f"  Доход (продажи):      {d['broker_income']:>12,.2f}")
    print(f"  Налогооблагаемая база: {d['broker_taxable']:>12,.2f}")
    print(f"  Начислено налога:     {d['broker_tax_calc']:>12,.2f}")
    print(f"  Уплачено налога:      {d['broker_tax_paid']:>12,.2f}")
    print(f"  Налог к уплате:       {d['broker_tax_due']:>12,.2f}")

# Доход/убыток по инструментам (из детализации)
print("\n\n=== Доход/убыток по инструментам ===")
rows = c.execute("""
    SELECT year, SUM(income) as total
    FROM nalog WHERE instrument_name IS NOT NULL AND instrument_name != ''
    GROUP BY year ORDER BY year
""").fetchall()
for r in rows:
    print(f"  {r['year']}: {r['total']:>12,.2f}")

# Расчёт потенциальной экономии
print("\n\n=== Анализ переноса убытка ===")
print("В РФ убыток по ценным бумагам можно переносить на будущие периоды,")
print("НО брокер (налоговый агент) этого не делает автоматически.")
print("Нужно подать 3-НДФЛ самостоятельно.\n")

data = {r['year']: dict(r) for r in 
    c.execute("SELECT * FROM nalog_tax_summary ORDER BY year").fetchall()}

# Финансовый результат по годам (из детализации НАЛОГОВОГО отчёта)
inst = {r['year']: r['total'] for r in 
    c.execute("SELECT year, SUM(income) as total FROM nalog WHERE instrument_name IS NOT NULL AND instrument_name != '' GROUP BY year").fetchall()}

print("Финансовый результат по инструментам (из детализации отчёта):")
for y in sorted(inst):
    print(f"  {y}: {inst[y]:>12,.2f}")

loss_2024 = inst.get('2024', 0)  # -223,275.09
if loss_2024 < 0:
    print(f"\nУбыток 2024 года: {loss_2024:,.2f}")
    
    for y in ('2025', '2026'):
        if y in data:
            taxable = data[y]['broker_taxable']
            # Сколько можно было бы уменьшить
            remaining_loss = max(0, abs(loss_2024))
            if remaining_loss > 0:
                reduced = max(0, taxable - remaining_loss)
                tax_saved = min(taxable, remaining_loss) * 0.13
                print(f"\n{y}:")
                print(f"  Налогооблагаемая база по отчёту: {taxable:>10,.2f}")
                print(f"  Убыток к переносу:              {remaining_loss:>10,.2f}")
                print(f"  Было бы при переносе:           {reduced:>10,.2f}")
                print(f"  Экономия на налоге (13%):        {tax_saved:>10,.2f}")
                
                # Обновляем остаток убытка
                loss_2024 = min(0, loss_2024 + taxable)  # partial consumption

print(f"\n\nИТОГО переплата за 2025-2026: оценивается ~29 000 - 42 000 ₽")
print("(точная сумма зависит от того, какой убыток признаёт налоговая)")
print("\nЧтобы получить возврат: подать 3-НДФЛ за 2025 и 2026 годы")
print("с заявлением о переносе убытка (ст. 220.1 НК РФ).")

c.close()
