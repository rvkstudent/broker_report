"""Parse broker HTML reports and insert data into SQLite."""

import re
from bs4 import BeautifulSoup
from app.db import get_connection, init_db


def parse_float(s):
    """Parse a Russian-format number string to float."""
    if s is None:
        return 0.0
    s = s.strip()
    if not s or s in ('', '-', '—', '&nbsp;'):
        return 0.0
    # Remove non-breaking spaces, thin spaces, regular spaces
    s = s.replace('\xa0', '').replace('&nbsp;', '').replace(' ', '')
    s = s.replace(',', '.')
    # Handle + prefix
    s = s.lstrip('+')
    try:
        return float(s)
    except ValueError:
        return 0.0


def parse_int(s):
    s = s.strip().replace('\xa0', '').replace('&nbsp;', '').replace(' ', '')
    try:
        return int(s)
    except ValueError:
        return 0


def extract_text(cell):
    return cell.get_text(strip=True)


def parse_report(filepath):
    """Main entry: parse an HTML broker report and persist to DB."""
    init_db()

    with open(filepath, 'r', encoding='utf-8') as f:
        html = f.read()

    soup = BeautifulSoup(html, 'lxml')
    conn = get_connection()
    cur = conn.cursor()

    # ── Extract header info ──────────────────────────────────
    filename = filepath.split('\\')[-1]
    contract = ''
    investor = ''
    period_start = ''
    period_end = ''

    h3 = soup.find('h3')
    if h3:
        txt = h3.get_text('\n')
        m = re.search(r'за период с\s+(\S+)\s+по\s+(\S+)', txt)
        if m:
            period_start = m.group(1)
            period_end = m.group(2)

    # Find investor / contract
    for p in soup.find_all('p'):
        txt = p.get_text()
        m = re.search(r'Инвестор:\s*(.+?)$', txt, re.M)
        if m:
            investor = m.group(1).strip()
        m2 = re.search(r'Договор\s+(\S+)', txt)
        if m2:
            contract = m2.group(1)

    # Upsert report — get or create
    cur.execute("SELECT id FROM report WHERE filename=?", (filename,))
    existing = cur.fetchone()
    if existing:
        report_id = existing['id']
        # Clear old data for this report before re-parsing
        for tbl in ('trade', 'repo', 'cash_flow', 'portfolio', 'financial_result'):
            cur.execute(f"DELETE FROM {tbl} WHERE report_id=?", (report_id,))
        cur.execute("""UPDATE report SET contract=?, investor=?, period_start=?, period_end=?
                       WHERE id=?""", (contract, investor, period_start, period_end, report_id))
    else:
        cur.execute("""
            INSERT INTO report(filename, contract, investor, period_start, period_end)
            VALUES (?, ?, ?, ?, ?)
        """, (filename, contract, investor, period_start, period_end))
        cur.execute("SELECT id FROM report WHERE filename=?", (filename,))
        report_id = cur.fetchone()['id']

    # ── Parse trades (Сделки купли/продажи) ────────────────
    _parse_trades(soup, cur, report_id)

    # ── Parse repo (Сделки РЕПО) ────────────────────────────
    _parse_repo(soup, cur, report_id)

    # ── Parse cash flow (Движение денежных средств) ─────────
    _parse_cash_flow(soup, cur, report_id)

    # ── Parse portfolio (Портфель ценных бумаг) ─────────────
    _parse_portfolio(soup, cur, report_id)

    # ── Parse financial result (Налоговый раздел) ──────────
    _parse_financial_result(soup, cur, report_id)

    conn.commit()
    conn.close()
    return report_id


def _find_table_by_header(soup, header_text):
    """Find the first <table> whose preceding <p> or text contains header_text."""
    tables = soup.find_all('table')
    for table in tables:
        prev = table.find_previous(['p', 'p1', 'b', 'br'])
        if prev:
            txt = prev.get_text()
            if header_text.lower() in txt.lower():
                return table
    return None


def _parse_trades(soup, cur, report_id):
    """Parse the сделки купли/продажи ценных бумаг table."""
    # Find the trade table - it has a specific header
    for p_tag in soup.find_all(['p', 'p1']):
        if 'Сделки купли/продажи ценных бумаг' in p_tag.get_text():
            table = p_tag.find_next('table')
            break
    else:
        # fallback: find table with header row containing "Дата заключения" and "Код ЦБ"
        for table in soup.find_all('table'):
            rows = table.find_all('tr')
            for row in rows:
                cells = row.find_all('td')
                texts = [c.get_text(strip=True) for c in cells]
                if 'Дата заключения' in texts and 'Код ЦБ' in texts and 'Вид' in texts:
                    break
            else:
                continue
            break
        else:
            return  # no trade table found

    rows = table.find_all('tr')
    in_data = False
    platform = ''

    for row in rows:
        cells = row.find_all('td')
        if not cells:
            continue

        # Check for platform row
        txt = row.get_text(strip=True)
        if 'Площадка:' in txt:
            platform = txt.replace('Площадка:', '').strip()
            continue

        # Skip header rows
        first_text = cells[0].get_text(strip=True)
        if first_text in ('1', 'Дата заключения', '№ п/п'):
            continue
        if 'row-number' in (cells[0].get('class') or []):
            continue
        if 'Итого' in txt:
            continue

        # Ensure we have enough columns
        if len(cells) < 10:
            continue

        try:
            trade_date = extract_text(cells[0])
            settle_date = extract_text(cells[1])
            trade_time = extract_text(cells[2])
            sec_name = extract_text(cells[3])
            sec_code = extract_text(cells[4])
            currency = extract_text(cells[5])
            side = extract_text(cells[6])
            qty = parse_int(extract_text(cells[7]))
            price = parse_float(extract_text(cells[8]))
            amount = parse_float(extract_text(cells[9]))
            nkd = parse_float(extract_text(cells[10])) if len(cells) > 10 else 0
            broker_fee = parse_float(extract_text(cells[11])) if len(cells) > 11 else 0
            exchange_fee = parse_float(extract_text(cells[12])) if len(cells) > 12 else 0
            deal_number = extract_text(cells[13]) if len(cells) > 13 else ''
            comment = extract_text(cells[14]) if len(cells) > 14 else ''
            status = extract_text(cells[15]) if len(cells) > 15 else ''
        except (IndexError, ValueError):
            continue

        if not trade_date or not sec_name or not side:
            continue
        if side not in ('Покупка', 'Продажа'):
            continue

        cur.execute("""
            INSERT OR IGNORE INTO trade(report_id, trade_date, settle_date, trade_time,
                security_name, security_code, currency, side, quantity, price,
                amount, nkd, broker_fee, exchange_fee, deal_number, comment, status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (report_id, trade_date, settle_date, trade_time,
              sec_name, sec_code, currency, side, qty, price,
              amount, nkd, broker_fee, exchange_fee, deal_number, comment, status))


def _parse_repo(soup, cur, report_id):
    """Parse the сделки РЕПО table."""
    for p_tag in soup.find_all(['p', 'p1']):
        if 'Сделки РЕПО' in p_tag.get_text():
            table = p_tag.find_next('table')
            break
    else:
        return

    rows = table.find_all('tr')
    for row in rows:
        cells = row.find_all('td')
        if not cells:
            continue
        txt = row.get_text(strip=True)
        if 'СпецРЕПО' in txt or 'Площадка:' in txt:
            continue
        if 'row-number' in (cells[0].get('class') or []):
            continue
        if 'Итого' in txt:
            continue
        if extract_text(cells[0]) in ('1', 'Дата заключения'):
            continue

        if len(cells) < 16:
            continue

        try:
            trade_date = extract_text(cells[0])
            trade_time = extract_text(cells[1])
            sec_name = extract_text(cells[2])
            sec_code = extract_text(cells[3])
            currency = extract_text(cells[4])
            side = extract_text(cells[5])
            qty = parse_int(extract_text(cells[6]))
            price1 = parse_float(extract_text(cells[7]))
            nkd1 = parse_float(extract_text(cells[8]))
            amount1 = parse_float(extract_text(cells[9]))
            date1 = extract_text(cells[10])
            repo_rate = parse_float(extract_text(cells[11]))
            repo_interest = parse_float(extract_text(cells[12]))
            price2 = parse_float(extract_text(cells[13]))
            nkd2 = parse_float(extract_text(cells[14]))
            amount2 = parse_float(extract_text(cells[15]))
            date2 = extract_text(cells[16]) if len(cells) > 16 else ''
            # skip cols 17-19 (settle qty, margin, etc.)
            broker_fee = parse_float(extract_text(cells[19])) if len(cells) > 19 else 0
            exchange_fee = parse_float(extract_text(cells[20])) if len(cells) > 20 else 0
            deal_number = extract_text(cells[21]) if len(cells) > 21 else ''
            status = extract_text(cells[22]) if len(cells) > 22 else ''
        except (IndexError, ValueError):
            continue

        if not trade_date or not sec_name:
            continue

        cur.execute("""
            INSERT OR IGNORE INTO repo(report_id, trade_date, trade_time, security_name,
                security_code, currency, side, quantity, price_part1, nkd_part1,
                amount_part1, date_part1, repo_rate, repo_interest, price_part2,
                nkd_part2, amount_part2, date_part2, broker_fee, exchange_fee,
                deal_number, status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (report_id, trade_date, trade_time, sec_name, sec_code, currency,
              side, qty, price1, nkd1, amount1, date1, repo_rate, repo_interest,
              price2, nkd2, amount2, date2, broker_fee, exchange_fee,
              deal_number, status))


def _parse_cash_flow(soup, cur, report_id):
    """Parse the движение денежных средств table."""
    for p_tag in soup.find_all(['p', 'p1']):
        if 'Движение денежных средств за период' in p_tag.get_text():
            table = p_tag.find_next('table')
            break
    else:
        return

    rows = table.find_all('tr')
    for row in rows:
        cells = row.find_all('td')
        if not cells:
            continue
        txt = row.get_text(strip=True)
        if 'row-number' in (cells[0].get('class') or []):
            continue
        if 'Итого' in txt:
            continue
        if extract_text(cells[0]) in ('1', 'Дата'):
            continue
        if len(cells) < 6:
            continue

        date = extract_text(cells[0])
        desc = extract_text(cells[2])
        currency = extract_text(cells[3])
        credit = parse_float(extract_text(cells[4]))
        debit = parse_float(extract_text(cells[5]))

        if not date or not desc:
            continue

        cur.execute("""
            INSERT INTO cash_flow(report_id, date, description, currency, credit, debit)
            VALUES (?,?,?,?,?,?)
        """, (report_id, date, desc, currency, credit, debit))


def _parse_financial_result(soup, cur, report_id):
    """Parse the tax / financial result section (ИТОГОВЫЙ ФИНАНСОВЫЙ РЕЗУЛЬТАТ)."""
    # Find the table after "ИТОГОВЫЙ ФИНАНСОВЫЙ РЕЗУЛЬТАТ"
    for p_tag in soup.find_all(['p', 'p1']):
        txt = p_tag.get_text()
        if 'ИТОГОВЫЙ ФИНАНСОВЫЙ РЕЗУЛЬТАТ' in txt:
            table = p_tag.find_next('table')
            break
    else:
        return

    rows = table.find_all('tr')
    income_total = 0.0
    expense_total = 0.0
    tax_rate = None
    first_tax_rate = None
    tax_calc = 0.0
    tax_withheld = 0.0
    tax_due = 0.0

    for row in rows:
        cells = row.find_all('td')
        txt = row.get_text(strip=True).replace('\xa0', '').replace(' ', '')
        if not cells:
            continue

        # First data row: income and expense totals
        if len(cells) >= 8 and not row.find_parent('table', class_='table-header'):
            val0 = parse_float(extract_text(cells[0]))
            val5 = parse_float(extract_text(cells[5]))
            if val0 > 0:
                income_total = val0
            if val5 > 0:
                expense_total = val5

        # Tax rate rows: "Ставка X.XX%"
        if 'Ставка' in row.get_text():
            m = re.search(r'Ставка\s+([\d.]+)%', row.get_text())
            if m:
                # Store rate, will be used for next data row
                tax_rate = float(m.group(1))
            continue  # skip to next row (data row after rate)

        # Data rows under tax rate — taxable amount, tax calculated, withheld, due
        if tax_rate is not None and len(cells) >= 5:
            vals = [parse_float(extract_text(c)) for c in cells[:5]]
            if vals[1] > 0:  # taxable amount in column 1
                if first_tax_rate is None:
                    first_tax_rate = tax_rate
                tax_calc += vals[2] if len(vals) > 2 else 0
                tax_withheld += vals[3] if len(vals) > 3 else 0
                tax_due += vals[4] if len(vals) > 4 else 0
            # Reset rate after processing this rate's data
            tax_rate = None

    # Also try to read from the first tax table (I. ДОХОДЫ И РАСХОДЫ без переноса убытка)
    income_code = '1530'
    expense_code = '201'
    taxable_amount = 0.0
    income_amt = 0.0
    expense_amt = 0.0

    for p_tag in soup.find_all(['p', 'p1']):
        if 'ДОХОДЫ И РАСХОДЫ на' in p_tag.get_text() and 'без переноса' in p_tag.get_text():
            tbl = p_tag.find_next('table')
            if tbl:
                for r in tbl.find_all('tr'):
                    tds = r.find_all('td')
                    if len(tds) >= 4:
                        code = extract_text(tds[1])
                        if code == '1530':
                            income_amt = parse_float(extract_text(tds[2]))
                            taxable_amount = parse_float(extract_text(tds[3]))
                            expense_amt = parse_float(extract_text(tds[5])) if len(tds) > 5 else 0
                        elif code == '1537':
                            # Убыток по РЕПО
                            pass
            break

    if income_total == 0:
        income_total = income_amt
    if expense_total == 0:
        expense_total = expense_amt

    cur.execute("""
        INSERT INTO financial_result
            (report_id, income_code, income_amount, expense_code, expense_amount,
             taxable_amount, tax_rate, tax_calculated, tax_withheld, tax_due)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (report_id, income_code, income_total, expense_code, expense_total,
          taxable_amount, first_tax_rate, tax_calc, tax_withheld, tax_due))


def _parse_portfolio(soup, cur, report_id):
    """Parse the Портфель Ценных Бумаг table."""
    for p_tag in soup.find_all(['p', 'p1']):
        if 'Портфель Ценных Бумаг' in p_tag.get_text():
            table = p_tag.find_next('table')
            break
    else:
        return

    rows = table.find_all('tr')
    for row in rows:
        cells = row.find_all('td')
        if not cells:
            continue
        txt = row.get_text(strip=True)
        if 'row-number' in (cells[0].get('class') or []):
            continue
        if 'Итого' in txt or 'Площадка:' in txt or 'Портфель' in txt:
            continue
        if extract_text(cells[0]) in ('1', 'Наименование', ''):
            continue

        # Columns: name, isin, currency, qty_start, nominal, price_start, value_start, nkd_start,
        #          qty_end, nominal_end, price_end, value_end, nkd_end, qty_change, value_change, ...
        if len(cells) < 15:
            continue

        try:
            name = extract_text(cells[0])
            isin = extract_text(cells[1])
            currency = extract_text(cells[2])
            qty_start = parse_int(extract_text(cells[3]))
            price_start = parse_float(extract_text(cells[5]))
            value_start = parse_float(extract_text(cells[6]))
            qty_end = parse_int(extract_text(cells[8]))
            price_end = parse_float(extract_text(cells[10]))
            value_end = parse_float(extract_text(cells[11]))
            qty_change = parse_int(extract_text(cells[13]))
            value_change = parse_float(extract_text(cells[14]))
        except (IndexError, ValueError):
            continue

        if not name:
            continue

        cur.execute("""
            INSERT INTO portfolio(report_id, security_name, isin, currency,
                qty_start, price_start, value_start, qty_end, price_end,
                value_end, qty_change, value_change)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (report_id, name, isin, currency,
              qty_start, price_start, value_start, qty_end, price_end,
              value_end, qty_change, value_change))
