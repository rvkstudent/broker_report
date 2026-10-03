"""Parse broker reports (HTML for Sber, XLSX for VTB, PDF for Gazprombank) and insert data into SQLite."""

import os
import re
from bs4 import BeautifulSoup
from app.db import (get_connection, init_db, get_instrument_lotsize,
                    resolve_report_code, learn_aliases_by_deals,
                    _ref_contract_price, _scale_to_contract)
from app.parser_vtb import parse_vtb_report
from app.parser_mytrades import parse_mytrades
from app.parser_openbroker import parse_openbroker_report
from app.parser_gazprombank_v2 import parse_gazprombank_v2_report
from app.parser_gazprombank_xls import parse_gazprombank_xls_report


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


def parse_report(filepath, force=False):
    """Main entry: parse a broker report (HTML for Sber, XLSX for VTB) and persist to DB.

    force=True — перепарсить отчёт, даже если он уже загружен (например, после
    правок парсера/справочника инструментов). Строки отчёта перезаписываются,
    сделки с теми же номерами корректируются, дублей не появляется.
    """
    init_db()

    # ── Skip already-processed reports ───────────────────────
    # Если файл уже есть в БД и содержит сделки — не обрабатываем повторно.
    fname = os.path.basename(filepath)
    conn = get_connection()
    existing = conn.execute("SELECT id FROM report WHERE filename=?", (fname,)).fetchone()
    if existing:
        rid = existing['id']
        has_data = conn.execute(
            "SELECT COUNT(*) AS cnt FROM trade WHERE report_id=?", (rid,)
        ).fetchone()['cnt']
        if has_data > 0 and not force:
            conn.close()
            print(f'  [skip] {fname} уже обработан (report_id={rid}, сделок={has_data})')
            return rid
        # Если сделок 0 — предыдущий парсинг упал, перезаписываем
        print(f'  [retry] {fname}: перепарсинг (report_id={rid}, предыдущих сделок={has_data})')
    conn.close()

    # Detect file type by extension, name, and content
    fname_lower = fname.lower()
    ext = os.path.splitext(filepath)[1].lower()

    if ext == '.pdf':
        # Gazprombank PDF reports
        return parse_gazprombank_v2_report(filepath)

    if ext in ('.xlsx', '.xls'):
        # my_trades.xlsx — отдельный формат
        if fname_lower == 'my_trades.xlsx':
            return parse_mytrades(filepath)

        # Детектируем брокера по содержимому (первые строки)
        try:
            import pandas as pd
            df_sample = pd.read_excel(filepath, header=None, nrows=10)
            header_text = ''
            for r in range(min(5, df_sample.shape[0])):
                for c in range(min(10, df_sample.shape[1])):
                    v = str(df_sample.iloc[r, c])[:100] if not pd.isna(df_sample.iloc[r, c]) else ''
                    if v:
                        header_text += v + ' '

            if 'Открытие' in header_text or 'БМ-Банк' in header_text:
                return parse_openbroker_report(filepath)

            if 'Ньютон Инвестиции' in header_text or 'Газпромбанк' in header_text:
                return parse_gazprombank_xls_report(filepath)

            # По умолчанию — VTB
            return parse_vtb_report(filepath)
        except Exception:
            return parse_vtb_report(filepath)

    # HTML parser (Sber format)
    with open(filepath, 'r', encoding='utf-8') as f:
        html = f.read()

    soup = BeautifulSoup(html, 'lxml')
    conn = get_connection()
    cur = conn.cursor()

    # ── Extract header info ──────────────────────────────────
    filename = os.path.basename(filepath)
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
        cur.execute("""UPDATE report SET contract=?, investor=?, period_start=?, period_end=?,
                       source_type='broker_report', broker='sber'
                       WHERE id=?""", (contract, investor, period_start, period_end, report_id))
    else:
        cur.execute("""
            INSERT INTO report(filename, contract, investor, period_start, period_end, source_type, broker)
            VALUES (?, ?, ?, ?, ?, 'broker_report', 'sber')
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
    """Parse tables of trades: stocks («Сделки купли/продажи ценных бумаг»)
    and futures («Срочные сделки»). Отчёт — источник истины: существующие сделки
    с тем же номером корректируются (сторона, цена, количество)."""
    tables = {}
    for p_tag in soup.find_all(['p', 'p1']):
        txt = p_tag.get_text()
        if 'Сделки купли/продажи ценных бумаг' in txt and 'stocks' not in tables:
            tables['stocks'] = p_tag.find_next('table')
        if 'Срочные сделки' in txt and 'futures' not in tables:
            tables['futures'] = p_tag.find_next('table')

    if not tables:
        # fallback: таблица с заголовком «Дата заключения»/«Код ЦБ»
        for table in soup.find_all('table'):
            for row in table.find_all('tr'):
                cells = row.find_all('td')
                texts = [c.get_text(strip=True) for c in cells]
                if 'Дата заключения' in texts and 'Код ЦБ' in texts and 'Вид' in texts:
                    tables['stocks'] = table
                    break
            if 'stocks' in tables:
                break
        if not tables:
            return  # нет таблиц сделок

    for kind, table in tables.items():
        if kind == 'futures':
            # Код контракта в отчёте может отличаться от кода в QUIK
            # (Сбер: «MTSI-12.26» ↔ QUIK «MTZ6»). Обучаем алиасы по номерам
            # сделок ДО импорта, чтобы строки без совпадения по номеру тоже
            # попали в правильный инструмент (иначе часть позиции теряется).
            pairs = []
            for r in table.find_all('tr'):
                cs = r.find_all('td')
                if len(cs) < 11:
                    continue
                code = extract_text(cs[3])
                num = extract_text(cs[10])
                if code and num:
                    pairs.append((code, num))
            if pairs:
                learned = learn_aliases_by_deals(cur, pairs)
                if learned:
                    print(f'  [parser] futures code aliases: {learned}')

        for row in table.find_all('tr'):
            cells = row.find_all('td')
            if not cells:
                continue
            txt = row.get_text(strip=True)
            if 'Площадка:' in txt:
                continue
            first_text = cells[0].get_text(strip=True)
            if first_text in ('1', 'Дата заключения', '№ п/п'):
                continue
            if 'row-number' in (cells[0].get('class') or []):
                continue
            if 'Итого' in txt:
                continue
            if len(cells) < 10:
                continue

            if kind == 'futures':
                # Срочные сделки (фьючерсы), 12 колонок:
                #   дата, расчёты, время, код (SBERF), «фьючерс», сторона, кол-во,
                #   цена за ед. (акцию/пункт), комис. брокера, комис. биржи, № сделки, комментарий
                try:
                    trade_date = extract_text(cells[0])
                    settle_date = extract_text(cells[1])
                    trade_time = extract_text(cells[2])
                    report_code = extract_text(cells[3])
                    # Код из отчёта → код QUIK (инструмент — первичный ключ).
                    # Если сопоставления нет — работаем с кодом как есть; при
                    # отсутствии его в справочнике строку пропускаем (как раньше).
                    sec_code = resolve_report_code(cur, report_code)
                    if not cur.execute(
                        "SELECT 1 FROM instrument WHERE sec_code=? AND class_code='SPBFUT'",
                        (sec_code,)).fetchone():
                        continue
                    side = extract_text(cells[5])
                    qty = parse_int(extract_text(cells[6]))
                    price_unit = parse_float(extract_text(cells[7]))
                    broker_fee = parse_float(extract_text(cells[8])) if len(cells) > 8 else 0
                    exchange_fee = parse_float(extract_text(cells[9])) if len(cells) > 9 else 0
                    deal_number = extract_text(cells[10]) if len(cells) > 10 else ''
                    # Цена — стоимость контракта в рублях (как в trade/QUIK).
                    # Отчёты дают её по-разному: SBERF — за акцию (×100),
                    # MTSI-12.26/MTZ6 — сразу за контракт. Выравниваем масштаб
                    # по последним сделкам инструмента.
                    price = _scale_to_contract(price_unit, _ref_contract_price(cur, sec_code))
                    amount = round(qty * price, 2)
                    sec_name, currency = sec_code, 'RUB'
                    nkd, comment, status = 0, '', ''
                except (IndexError, ValueError):
                    continue
            else:
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

            # Отчёт — источник истины: если сделка с таким № уже есть (например, из QUIK,
            # где сторона могла быть определена неверно по флагам), корректируем её значения,
            # иначе вставляем новую строку с source='sber'.
            if deal_number:
                cur.execute("""
                    UPDATE trade SET
                        trade_date=?, settle_date=?, trade_time=?, security_name=?,
                        security_code=?, currency=?, side=?, quantity=?, price=?, amount=?,
                        nkd=?, broker_fee=?, exchange_fee=?, comment=?, status=?
                    WHERE deal_number=? AND deal_number != ''
                """, (trade_date, settle_date, trade_time, sec_name, sec_code, currency,
                      side, qty, price, amount, nkd, broker_fee, exchange_fee, comment, status,
                      deal_number))
                if cur.rowcount == 0:
                    cur.execute("""
                        INSERT OR IGNORE INTO trade(report_id, trade_date, settle_date, trade_time,
                            security_name, security_code, currency, side, quantity, price,
                            amount, nkd, broker_fee, exchange_fee, deal_number, comment, status, source)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (report_id, trade_date, settle_date, trade_time,
                          sec_name, sec_code, currency, side, qty, price,
                          amount, nkd, broker_fee, exchange_fee, deal_number, comment, status, 'sber'))
            else:
                cur.execute("""
                    INSERT OR IGNORE INTO trade(report_id, trade_date, settle_date, trade_time,
                        security_name, security_code, currency, side, quantity, price,
                        amount, nkd, broker_fee, exchange_fee, deal_number, comment, status, source)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (report_id, trade_date, settle_date, trade_time,
                      sec_name, sec_code, currency, side, qty, price,
                      amount, nkd, broker_fee, exchange_fee, deal_number, comment, status, 'sber'))


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
                deal_number, status, source)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (report_id, trade_date, trade_time, sec_name, sec_code, currency,
              side, qty, price1, nkd1, amount1, date1, repo_rate, repo_interest,
              price2, nkd2, amount2, date2, broker_fee, exchange_fee,
              deal_number, status, 'sber'))


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
            INSERT INTO cash_flow(report_id, date, description, currency, credit, debit, source)
            VALUES (?,?,?,?,?,?,?)
        """, (report_id, date, desc, currency, credit, debit, 'sber'))


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
