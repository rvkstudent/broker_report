"""Flask web app for broker report analysis."""

import os
import glob
import threading
import time
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, Response

from app.db import (init_db, get_reports_list, get_report_by_id,
                     get_trade_profit, get_trade_lots, get_open_trades,
                     get_instrument_summary, get_repo_total,
                     save_price, save_prices_batch, get_current_prices,
                     get_my_instruments, save_quik_trades,
                     get_recent_quik_trades, get_quik_positions)
from app.parser import parse_report

flask_app = Flask(__name__, template_folder='templates')
flask_app.secret_key = 'broker-report-secret-key'

REPORTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'reports')
os.makedirs(REPORTS_DIR, exist_ok=True)
_imported_files = set()  # track already-imported filenames


def _auto_import():
    """Import HTML files that haven't been imported yet."""
    imported = 0
    seen = set()
    for fp in sorted(glob.glob(os.path.join(REPORTS_DIR, '*.[Hh][Tt][Mm][Ll]'))):
        if fp.lower() in seen:
            continue
        seen.add(fp.lower())
        fname = os.path.basename(fp)
        if fname in _imported_files:
            continue
        try:
            rid = parse_report(fp)
            print(f'  [auto] ✓ {fname} (id={rid})')
            _imported_files.add(fname)
            imported += 1
        except Exception:
            pass
    return imported


def _watch_folder(interval=60):
    """Background thread: scan for new reports every `interval` seconds."""
    while True:
        time.sleep(interval)
        try:
            cnt = _auto_import()
            if cnt:
                print(f'  [auto] Загружено новых отчётов: {cnt}')
        except Exception:
            pass


def start_watcher(interval=60):
    """Start the background folder watcher (daemon thread)."""
    th = threading.Thread(target=_watch_folder, args=(interval,), daemon=True)
    th.start()


@flask_app.route('/')
def index():
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')

    # Convert HTML date input (YYYY-MM-DD) to DD.MM.YYYY for DB
    def to_dmy(iso):
        if not iso:
            return ''
        parts = iso.split('-')
        if len(parts) == 3:
            return f'{parts[2]}.{parts[1]}.{parts[0]}'
        return iso

    df_dmy = to_dmy(date_from)
    dt_dmy = to_dmy(date_to)

    reports = get_reports_list()
    profit = get_trade_profit(None, df_dmy, dt_dmy)
    lots = get_trade_lots(None, df_dmy, dt_dmy)
    open_trades = get_open_trades(None, df_dmy, dt_dmy)
    instruments = get_instrument_summary(None, df_dmy, dt_dmy)
    repo_total = get_repo_total(None, df_dmy, dt_dmy)

    quik_trades = get_recent_quik_trades(20)
    prices = get_current_prices()
    quik_pos = get_quik_positions()

    # Индикатор соединения с QUIK: данные обновлялись за последние 10 секунд
    quik_connected = False
    if prices:
        from datetime import datetime, timedelta
        for p in prices:
            try:
                ts = datetime.strptime(p['timestamp'], '%Y-%m-%d %H:%M:%S')
                if datetime.now() - ts < timedelta(seconds=10):
                    quik_connected = True
                    break
            except Exception:
                pass

    # Добавляем текущую цену в открытые трейд-сделки для прогноза P&L
    open_trades = list(open_trades)
    price_map = {p['sec_code']: p['price'] for p in prices}
    for o in open_trades:
        o['current_price'] = price_map.get(o['security_code'], 0)

    # Средняя комиссия брокера + биржи (0.0685% от сделки)
    TRADE_FEE_RATE = 0.000685

    # Цвета для значков тикеров (на основе class_code)
    TICKER_COLORS = {
        'TQBR': '#2563eb', 'TQOB': '#ea580c', 'TQTD': '#7c3aed',
        'TQBS': '#059669', '': '#6b7280',
    }

    return render_template('dashboard.html',
                           reports=reports,
                           profit=profit, lots=lots,
                           open_trades=open_trades,
                           instruments=instruments,
                           repo_total=repo_total,
                           trade_fee_rate=TRADE_FEE_RATE,
                           quik_trades=quik_trades,
                           prices=prices,
                           quik_positions=quik_pos,
                           quik_connected=quik_connected,
                           date_from=df_dmy, date_to=dt_dmy,
                           date_from_iso=date_from, date_to_iso=date_to)


@flask_app.route('/upload', methods=['POST'])
def upload():
    """Parse an HTML report file from the reports directory."""
    files = request.files.getlist('files')
    if not files or files[0].filename == '':
        # Try file path from form
        filepath = request.form.get('filepath', '')
        if filepath and os.path.exists(filepath):
            try:
                rid = parse_report(filepath)
                flash(f'Отчёт {os.path.basename(filepath)} загружен (id={rid})', 'success')
            except Exception as e:
                flash(f'Ошибка: {e}', 'danger')
            return redirect(url_for('index'))
        # Scan for HTML files in the current directory
        found = False
        for fp in glob.glob(os.path.join(REPORTS_DIR, '*.html')):
            try:
                rid = parse_report(fp)
                flash(f'Загружен: {os.path.basename(fp)} (id={rid})', 'success')
                found = True
            except Exception as e:
                flash(f'Ошибка {os.path.basename(fp)}: {e}', 'danger')
        if not found:
            flash('HTML-файлы не найдены', 'warning')
        return redirect(url_for('index'))

    # Handle uploaded files
    for f in files:
        if f.filename:
            save_path = os.path.join(REPORTS_DIR, f.filename)
            f.save(save_path)
            try:
                rid = parse_report(save_path)
                flash(f'Загружен: {f.filename} (id={rid})', 'success')
            except Exception as e:
                flash(f'Ошибка {f.filename}: {e}', 'danger')
    return redirect(url_for('index'))


@flask_app.route('/report/<int:report_id>', methods=['GET', 'POST'])
def report_view(report_id):
    if request.method == 'POST':
        delete_report(report_id)
        flash('Отчёт удалён', 'info')
        return redirect(url_for('index'))

    r = get_report_by_id(report_id)
    if not r:
        flash('Отчёт не найден', 'danger')
        return redirect(url_for('index'))

    profit = get_trade_profit(report_id)
    lots = get_trade_lots(report_id)
    open_trades = get_open_trades(report_id)
    instruments = get_instrument_summary(report_id)
    repo_total = get_repo_total(report_id)

    return render_template('report.html',
                           report=r,
                           profit=profit, lots=lots,
                           open_trades=open_trades,
                           instruments=instruments,
                           repo_total=repo_total)


@flask_app.route('/api/report/<int:report_id>/profit')
def api_profit(report_id):
    return jsonify([dict(row) for row in get_trade_profit(report_id)])


@flask_app.route('/api/report/<int:report_id>/open')
def api_open(report_id):
    return jsonify([dict(row) for row in get_open_positions(report_id)])


# ── Trade API (from QUIK) ─────────────────────────────────────

@flask_app.route('/api/trade', methods=['POST'])
def api_trade():
    """Receive trades from QUIK OnAllTrade callback.

    JSON body (batch):
        {"trades": [
            {"trade_num": 123, "sec_code": "SBER", "class_code": "TQBR",
             "price": 312.5, "qty": 100, "value": 31250.0, ...}
        ]}
    """
    data = request.get_json(silent=True)
    if not data or 'trades' not in data or not isinstance(data['trades'], list):
        return jsonify({'error': 'Invalid JSON, expected {"trades": [...]}'}), 400

    if not data['trades']:
        return jsonify({'error': 'Empty trades list'}), 400

    save_quik_trades(data['trades'])

    # Also update current prices from trade data
    for t in data['trades']:
        if t.get('sec_code') and t.get('price'):
            save_price(
                sec_code=t['sec_code'],
                price=float(t['price']),
                qty=int(t.get('qty', 0)),
                value=float(t.get('value', 0)),
                class_code=t.get('class_code', '')
            )

    return jsonify({'status': 'ok', 'count': len(data['trades'])}), 200


# ── Instruments API ───────────────────────────────────────────

@flask_app.route('/api/instruments', methods=['GET'])
def api_instruments():
    """Get list of user's instruments (from trade history)."""
    instruments = get_my_instruments()
    return jsonify(instruments), 200


# ── Price API ─────────────────────────────────────────────────

@flask_app.route('/api/price', methods=['POST'])
def api_price():
    """Receive current instrument price from QUIK or other sources.

    JSON body (single):
        {"sec_code": "SBER", "price": 250.12, "qty": 100, "class_code": "TQBR"}

    JSON body (batch):
        {"prices": [
            {"sec_code": "SBER", "price": 250.12, "qty": 100, "class_code": "TQBR"},
            {"sec_code": "GAZP", "price": 150.50, "qty": 50, "class_code": "TQBR"}
        ]}
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'Invalid JSON'}), 400

    # Batch mode
    if 'prices' in data and isinstance(data['prices'], list):
        if not data['prices']:
            return jsonify({'error': 'Empty prices list'}), 400
        save_prices_batch(data['prices'])
        return jsonify({'status': 'ok', 'count': len(data['prices'])}), 200

    # Single mode
    sec_code = data.get('sec_code')
    price = data.get('price')
    if not sec_code or price is None:
        return jsonify({'error': 'sec_code and price are required'}), 400

    save_price(
        sec_code=sec_code,
        price=float(price),
        qty=int(data.get('qty', 0)),
        value=float(data.get('value', 0)),
        class_code=data.get('class_code', '')
    )
    return jsonify({'status': 'ok', 'sec_code': sec_code, 'price': float(price)}), 200


@flask_app.route('/api/prices', methods=['GET'])
def api_prices():
    """Get all current instrument prices."""
    prices = get_current_prices()
    return jsonify(prices), 200


@flask_app.route('/api/quik-trades', methods=['GET'])
def api_quik_trades():
    """Get recent QUIK trades."""
    limit = request.args.get('limit', 20, type=int)
    trades = get_recent_quik_trades(limit)
    return jsonify(trades), 200


@flask_app.route('/api/quik-connected', methods=['GET'])
def api_quik_connected():
    """Check if QUIK data is flowing (price update within last 10s)."""
    prices = get_current_prices()
    from datetime import datetime, timedelta
    for p in prices:
        try:
            ts = datetime.strptime(p['timestamp'], '%Y-%m-%d %H:%M:%S')
            if datetime.now() - ts < timedelta(seconds=10):
                return jsonify({'connected': True}), 200
        except Exception:
            pass
    return jsonify({'connected': False}), 200


TICKER_LOGO_COLORS = {
    'SBER': '#1a8c39', 'GAZP': '#0d5e8a', 'MOEX': '#7c3aed',
    'MTSS': '#e75614', 'AFKS': '#2563eb', 'RAGR': '#0891b2',
    'LQDT': '#d97706', 'RU000A10C3M0': '#be123c',
}

@flask_app.route('/api/logo/<ticker>', methods=['GET'])
def api_logo(ticker):
    """Generate ticker icon as SVG (local, no external calls)."""
    color = TICKER_LOGO_COLORS.get(ticker.upper(), '#6b7280')
    letter = ticker[0].upper() if ticker else '?'
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">
        <rect width="16" height="16" rx="3" fill="{color}"/>
        <text x="8" y="11" text-anchor="middle" fill="white" font-size="9" font-weight="600" font-family="Arial,sans-serif">{letter}</text>
    </svg>'''
    return Response(svg, mimetype='image/svg+xml',
                    headers={'Cache-Control': 'public, max-age=86400'})


if __name__ == '__main__':
    init_db()
    flask_app.run(host='127.0.0.1', port=5000, debug=True)
