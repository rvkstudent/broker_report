"""Flask web app for broker report analysis."""

import os
import glob
import mimetypes
import re
import secrets
import threading
import time
from datetime import date, datetime, timedelta
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, Response, make_response

from app.db import (init_db, get_reports_list, get_report_by_id,
                     get_trade_profit, get_trade_lots, get_open_trades,
                     get_instrument_summary, get_repo_total,
                     save_price, save_prices_batch, get_current_prices,
                     get_my_instruments, save_quik_trades, save_instruments_batch,
                     get_recent_quik_trades, get_quik_positions,
                     get_trades_list, get_cash_flow_summary)
from app.parser import parse_report
from app.parser_gazprombank_v2 import parse_gazprombank_v2_report
from app.parser_nalog import (parse_nalog_report, get_nalog_summary,
                               get_nalog_years, get_nalog_instruments,
                               get_nalog_tax_summary_by_year,
                               get_nalog_group_summary)
from app.replication import push_async as push_to_cloud
from app import (screener, chartdata, backtest, volatility, levels,
                strategy_levels, signals_cache, signal_days, adaptive)
from app import dashboard_prices as tinkoff_prices
from app.allowed_devices import (is_device_allowed, add_pending_device,
                                  approve_device, reject_device, remove_device,
                                  get_all_devices, toggle_device, get_pending_count,
                                  get_admin_token, DISABLE_AUTH)
from app.auth import register_auth, auth_enabled

flask_app = Flask(__name__, template_folder='templates')
# Шаблоны перечитываем при изменении файла: правки вёрстки не требуют
# перезапуска контейнера (в gunicorn debug выключен, auto_reload по умолчанию глушится).
flask_app.config['TEMPLATES_AUTO_RELOAD'] = True

# Шрифт иконок Bootstrap отдаётся Python'ом как application/octet-stream:
# файла mimetypes.types_map для .woff2 в slim-образе нет. Если рядом появится
# X-Content-Type-Options: nosniff (его добавляет Cloudflare), браузер откажется
# применять шрифт и все иконки bi-* превратятся в квадратики. Регистрируем тип
# явно — это надёжнее, чем рассчитывать на системную таблицу MIME.
mimetypes.add_type('font/woff2', '.woff2')
mimetypes.add_type('font/woff', '.woff')
mimetypes.add_type('font/ttf', '.ttf')

# ── Цены дашборда: QUIK + резервный источник T-Invest ─────────
QUIK_FRESH_SECONDS = 10
# Инструменты с открытыми позициями: Tinkoff подставляет цены только по ним,
# а /api/prices (поллинг каждые 3 с) не пересчитывает позиции на каждый запрос.
_TRACKED_INSTRUMENTS: dict = {}


def _prices_are_fresh(prices, seconds: int = QUIK_FRESH_SECONDS) -> bool:
    """Есть ли среди цен запись не старше ``seconds`` секунд."""
    now = datetime.now()
    for p in prices:
        try:
            ts = datetime.strptime(p['timestamp'], '%Y-%m-%d %H:%M:%S')
        except Exception:
            continue
        if now - ts < timedelta(seconds=seconds):
            return True
    return False


def _position_instruments(open_trades, quik_positions, my_instruments) -> dict:
    """``{sec_code: class_code}`` для инструментов с открытыми позициями."""
    class_map = {i['sec_code']: (i.get('class_code') or '') for i in my_instruments}
    codes = set()
    for o in open_trades or []:
        codes.add(o.get('security_code'))
    for p in quik_positions or []:
        codes.add(p.get('sec_code'))
    return {c: class_map.get(c, '') for c in codes if c}


def _effective_prices(db_prices, instruments=None, quik_connected=None):
    """Цены для дашборда: QUIK, если активен, иначе — T-Invest.

    Возвращает ``(prices, prices_source)``, где ``prices_source`` —
    'quik', 'tinkoff' или 'none'. Tinkoff подставляет только те бумаги, по
    которым пришёл ответ; остальные остаются из БД (пусть и устаревшие).
    """
    db_prices = list(db_prices or [])
    if quik_connected is None:
        quik_connected = _prices_are_fresh(db_prices)
    if quik_connected:
        return db_prices, 'quik'
    if not tinkoff_prices.enabled():
        return db_prices, 'none'

    instruments = instruments if instruments is not None else _TRACKED_INSTRUMENTS
    if not instruments:
        return db_prices, 'none'

    tf_prices = tinkoff_prices.get_prices(instruments)
    if not tf_prices:
        return db_prices, 'none'

    by_code = {p['sec_code']: p for p in db_prices}
    by_code.update(tf_prices)
    merged = sorted(by_code.values(), key=lambda p: p['sec_code'])
    return merged, 'tinkoff'


def _resolve_secret_key() -> str:
    """Ключ подписи сессионной куки.

    Историческое значение по умолчанию лежит в публичном репозитории, поэтому
    с включённой авторизацией его использовать нельзя: зная ключ, можно
    подделать куку и войти без пароля. Если FLASK_SECRET не задан, генерируем
    случайный ключ — сессии не переживут перезапуск, но подделать их нельзя.
    """
    secret = os.environ.get('FLASK_SECRET')
    if secret:
        return secret
    if auth_enabled():
        print('  [WARN] FLASK_SECRET не задан — сгенерирован временный ключ: '
              'сессии сбросятся при перезапуске. Задайте FLASK_SECRET в .env')
        return secrets.token_urlsafe(48)
    return 'broker-report-secret-key'


flask_app.secret_key = _resolve_secret_key()

# Авторизацию подключаем ДО middleware устройств, чтобы проверка входа
# срабатывала первой
register_auth(flask_app)

REPORTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'reports')
os.makedirs(REPORTS_DIR, exist_ok=True)


# ── Jinja фильтры и хелперы для шаблонов ─────────────────────

TICKER_COLORS_MAP = {
    'TQBR': '#2563eb', 'TQOB': '#ea580c', 'TQTD': '#7c3aed',
    'TQBS': '#059669', 'TQCB': '#0891b2', 'TQOD': '#d97706',
}

@flask_app.template_filter('money')
def format_money(value):
    """Формат числа: 1 234.56 → '1 234.56'"""
    if value is None or value == 0:
        return '0'
    try:
        return f'{value:,.2f}'.replace(',', ' ')
    except (ValueError, TypeError):
        return str(value)


@flask_app.template_filter('basename')
def filename_only(value):
    """Только имя файла, без пути и каталогов.

    В отчётах QUIK в поле filename встречается полный путь
    (/Users/.../reports/foo.html). В списке отчётов каталог — шум, а на
    телефоне он ещё и съедает всю ширину строки под многоточие, поэтому
    в шаблоне показываем имя, а полный путь — в атрибуте title.
    """
    name = str(value or '').replace('\\', '/')
    return name.rsplit('/', 1)[-1]


@flask_app.template_filter('pl_total')
def format_pl_total(value):
    """Итого P&L («финрез»): прибыль — числом, убыток — в скобках.

    В отчётности убыток принято брать в скобки, а не писать минус. Плюс
    тоже не пишем: знак и так виден по цвету (см. .profit-positive /
    .profit-negative в base.html). Скобка не должна отрываться от числа —
    за это отвечает .finrez-cell в base.html.
    """
    try:
        num = float(value or 0)
    except (TypeError, ValueError):
        return '—'
    if num < 0:
        return f'({-num:,.2f})'
    return f'{num:,.2f}'


@flask_app.template_filter('short_date')
def format_short_date(value):
    """Короткая дата: 2026-09-01 и 01.09.2026 → 01.09.26.

    В карточном виде на телефоне подпись «Дата покупки» и значение делят
    около 160 px. Полный год — лишние два символа, из-за которых значение
    уезжало на вторую строку обрывком, поэтому год — двумя цифрами.
    """
    raw = str(value or '').strip().replace('\u00a0', ' ')
    if not raw:
        return '—'
    # Брокерские отчёты отдают dd.mm.yyyy, налоговая и парсеры — yyyy-mm-dd
    match = re.match(r'^(\d{4})-(\d{2})-(\d{2})', raw)
    if match:
        year, month, day = match.groups()
        return f'{day}.{month}.{year[2:]}'
    match = re.match(r'^(\d{1,2})\.(\d{1,2})\.(\d{4})$', raw)
    if match:
        day, month, year = match.groups()
        return f'{day.zfill(2)}.{month.zfill(2)}.{year[2:]}'
    return raw


def ticker_color(sec_code):
    """Цвет для значка тикера на основе class_code или хеша кода."""
    if not sec_code:
        return '#6b7280'
    # По первой букве кода
    colors = ['#2563eb', '#ea580c', '#7c3aed', '#059669', '#0891b2',
              '#d97706', '#be123c', '#1a8c39', '#0d5e8a', '#9333ea']
    idx = sum(ord(c) for c in sec_code) % len(colors)
    return colors[idx]


flask_app.jinja_env.globals['ticker_color'] = ticker_color


def _auto_import():
    """Import HTML/XLSX files from reports/ that haven't been imported yet."""
    imported = 0
    from app.db import get_connection
    conn = get_connection()
    # Нормализуем: только basename в нижнем регистре — чтобы Mac/Windows пути не дублировались
    known = {os.path.basename(r['filename']).lower()
             for r in conn.execute("SELECT filename FROM report").fetchall()}
    conn.close()
    patterns = [
        os.path.join(REPORTS_DIR, '*.[Hh][Tt][Mm][Ll]'),
        os.path.join(REPORTS_DIR, '*.[Xx][Ll][Ss][Xx]'),
        os.path.join(REPORTS_DIR, '*.[Xx][Ll][Ss]'),
        os.path.join(REPORTS_DIR, '*.[Pp][Dd][Ff]'),
    ]
    for pattern in patterns:
        for fp in sorted(glob.glob(pattern)):
            if os.path.basename(fp).lower() in known:
                continue
            try:
                rid = parse_report(fp)
                imported += 1
            except Exception as e:
                print(f'  [auto] ✗ {os.path.basename(fp)}: {e}')
    if imported:
        push_to_cloud()
    return imported


def _watch_folder(interval=60):
    """Background thread: scan for new reports every `interval` seconds."""
    while True:
        time.sleep(interval)
        try:
            cnt = _auto_import()
            if cnt:
                print(f'  [auto] Загружено новых отчётов: {cnt}')
                push_to_cloud()
        except Exception:
            pass


def start_watcher(interval=60):
    """Start the background folder watcher (daemon thread)."""
    th = threading.Thread(target=_watch_folder, args=(interval,), daemon=True)
    th.start()


# ── Авторизация устройств ────────────────────────────────────

def _get_device_id() -> str | None:
    """Извлечь device_id из заголовка, куки или аргумента."""
    return (request.headers.get('X-Device-ID')
            or request.cookies.get('device_id')
            or request.args.get('device_id'))


def _get_admin_token_from_request() -> str:
    """Извлечь админ-токен из заголовка, куки или аргумента."""
    return (request.headers.get('X-Admin-Token')
            or request.cookies.get('admin_token')
            or request.form.get('admin_token'))


@flask_app.before_request
def _check_device_auth():
    """Middleware: проверяет, разрешён ли доступ устройству.

    Правила:
    - Если DISABLE_AUTH=True → пропускаем все запросы (локальная разработка)
    - /admin* → проверяем админ-токен
    - Статика, фавикон → пропускаем
    - Всё остальное → проверяем device_id
    """
    if DISABLE_AUTH:
        return None

    # Разрешённые пути без авторизации
    public_paths = ('/static/', '/favicon')
    if request.path.startswith(public_paths):
        return None

    # Админ-роуты проверяются отдельно внутри обработчиков
    if request.path.startswith('/admin'):
        return None

    # Для /api/device-register — доступ без device_id (регистрация нового)
    if request.path == '/api/device-register' and request.method == 'POST':
        return None

    # Проверяем device_id
    device_id = _get_device_id()
    if not device_id:
        # Если нет device_id — возвращаем страницу с предложением авторизоваться
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Требуется авторизация устройства. Передайте X-Device-ID заголовок.'}), 401
        return render_template('device_auth.html', device_id=None, error=None), 401

    if not is_device_allowed(device_id):
        # Автоматически добавляем устройство в список ожидания
        add_pending_device(device_id)
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Устройство не авторизовано. Запрос отправлен администратору.'}), 403
        return render_template('device_auth.html',
                               device_id=device_id,
                               error='Устройство не авторизовано. Запрос на доступ отправлен администратору.'), 403

    return None


# ── Админ-панель управления устройствами ─────────────────────

@flask_app.route('/admin', methods=['GET', 'POST'])
def admin_panel():
    """Панель администратора: управление устройствами."""
    admin_token = _get_admin_token_from_request()
    error = None
    success = None

    if request.method == 'POST':
        action = request.form.get('action', '')
        admin_token = request.form.get('admin_token', admin_token)

        if action == 'approve':
            device_id = request.form.get('device_id', '').strip()
            result = approve_device(device_id, admin_token)
            if result.get('success'):
                success = f'Устройство {device_id} разрешено'
            else:
                error = result.get('error', 'Ошибка')

        elif action == 'reject':
            device_id = request.form.get('device_id', '').strip()
            result = reject_device(device_id, admin_token)
            if result.get('success'):
                success = f'Устройство {device_id} отклонено'
            else:
                error = result.get('error', 'Ошибка')

        elif action == 'remove':
            device_id = request.form.get('device_id', '')
            result = remove_device(device_id, admin_token)
            if result.get('success'):
                success = f'Устройство {device_id} удалено'
            else:
                error = result.get('error', 'Ошибка удаления')

        elif action == 'toggle':
            device_id = request.form.get('device_id', '')
            enabled = request.form.get('enabled') == '1'
            result = toggle_device(device_id, enabled, admin_token)
            if result.get('success'):
                status = 'включено' if enabled else 'отключено'
                success = f'Устройство {device_id} {status}'
            else:
                error = result.get('error', 'Ошибка')

    store = get_all_devices(admin_token) if admin_token else {'approved': [], 'pending': []}
    approved = store.get('approved', [])
    pending = store.get('pending', [])
    is_auth = bool(approved or pending or (admin_token and get_admin_token() == admin_token))

    return render_template('admin.html',
                           devices=approved,
                           pending_devices=pending,
                           admin_token=admin_token,
                           is_auth=is_auth,
                           error=error,
                           success=success)


@flask_app.route('/api/admin/devices', methods=['GET'])
def api_admin_devices():
    """API: список устройств (требует admin_token)."""
    admin_token = _get_admin_token_from_request()
    store = get_all_devices(admin_token)
    if not store.get('approved') and not store.get('pending') and admin_token != get_admin_token():
        return jsonify({'error': 'Неверный токен администратора'}), 403
    return jsonify(store)


@flask_app.route('/api/device-register', methods=['POST'])
def api_device_register():
    """API: регистрация нового устройства администратором."""
    data = request.get_json(silent=True) or {}
    device_id = data.get('device_id', '').strip()
    admin_token = data.get('admin_token', '') or _get_admin_token_from_request()

    if data.get('action') == 'approve':
        result = approve_device(device_id, admin_token)
    elif data.get('action') == 'reject':
        result = reject_device(device_id, admin_token)
    else:
        result = approve_device(device_id, admin_token)

    if result.get('success'):
        return jsonify(result)
    return jsonify(result), 403


@flask_app.route('/device/status')
def device_status():
    """Страница статуса устройства — показывает device_id."""
    device_id = _get_device_id()
    allowed = is_device_allowed(device_id) if device_id else False
    return render_template('device_status.html',
                           device_id=device_id,
                           allowed=allowed,
                           auth_enabled=not DISABLE_AUTH)


# ── Список сделок ────────────────────────────────────────────

@flask_app.route('/trades')
def trades_view():
    """Страница со списком всех сделок с фильтрацией и пагинацией."""
    sec_code = request.args.get('sec_code', '').strip()
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')
    source = request.args.get('source', '')
    sort = request.args.get('sort', 'date_desc')
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)

    # Конвертация ISO → DD.MM.YYYY
    def to_dmy(iso):
        if not iso:
            return ''
        parts = iso.split('-')
        if len(parts) == 3:
            return f'{parts[2]}.{parts[1]}.{parts[0]}'
        return iso

    df_dmy = to_dmy(date_from)
    dt_dmy = to_dmy(date_to)

    trades, total = get_trades_list(
        security_code=sec_code,
        date_from=df_dmy,
        date_to=dt_dmy,
        source=source,
        page=page,
        per_page=per_page,
        sort=sort,
    )

    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)

    # Список инструментов для автокомплита/фильтра
    instruments = get_my_instruments()
    sec_codes = sorted(set(i['sec_code'] for i in instruments if i.get('sec_code')))

    return render_template('trades.html',
                           trades=trades,
                           total=total,
                           page=page,
                           per_page=per_page,
                           total_pages=total_pages,
                           sec_code=sec_code,
                           date_from=date_from,
                           date_to=date_to,
                           source=source,
                           sort=sort,
                           sec_codes=sec_codes)


NALOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'reports', 'nalog')
os.makedirs(NALOG_DIR, exist_ok=True)

# Отметки времени уже разобранных налоговых файлов: путь -> mtime.
# Нужны, чтобы не перечитывать xlsx на каждый запрос страницы «Налоги».
_nalog_imported: dict[str, float] = {}


@flask_app.route('/nalog')
def nalog_view():
    """Страница налоговых отчётов со сводной таблицей (инструменты × годы)."""
    # Автоимпорт новых файлов. Разбирать xlsx на каждый заход нельзя: это
    # чтение и разбор мегабайтов на каждый запрос. Держим отметки времени
    # разобранных файлов и трогаем только изменившиеся.
    if os.path.exists(NALOG_DIR):
        for fname in sorted(os.listdir(NALOG_DIR)):
            if not fname.lower().endswith('.xlsx'):
                continue
            fp = os.path.join(NALOG_DIR, fname)
            try:
                mtime = os.path.getmtime(fp)
            except OSError:
                continue
            if _nalog_imported.get(fp) == mtime:
                continue
            try:
                parse_nalog_report(fp)
                _nalog_imported[fp] = mtime
            except Exception:
                pass

    years = get_nalog_years()
    flat = get_nalog_summary()
    instruments = get_nalog_instruments()

    # Строим pivot: { instrument_code: { year: { deals, income, amount, pos, neg } } }
    pivot = {}
    for r in flat:
        code = r['instrument_code'] or r['instrument_name']
        if code not in pivot:
            pivot[code] = {
                'name': r['instrument_name'],
                'code': r['instrument_code'],
                'years': {}
            }
        pivot[code]['years'][r['year']] = {
            'deals': r['deals'],
            'income': r['total_income'],
            'amount': r['total_amount'],
            'pos': r['positive_deals'],
            'neg': r['negative_deals'],
        }

    # Преобразуем список налоговой сводки в dict {year: data}
    tax_dict = {t['year']: t for t in get_nalog_tax_summary_by_year()}

    # Итоги по году — используем broker_result (фин. результат из сводки отчёта)
    # Базу для итогов всегда строим по сделкам, а официальный broker_result
    # накладываем сверху, если сводка по этому году есть.
    #
    # Так страница не падает и не пустует, когда детальные строки пришли из
    # Firebase, а xlsx со сводкой ещё не загружен: раньше year_totals в этом
    # случае оставался пустым, шаблон обращался к yt.income и отдавал 500.
    year_totals = {}
    for r in flat:
        t = year_totals.setdefault(r['year'], {
            'deals': 0, 'income': 0.0, 'amount': 0.0, 'pos': 0, 'neg': 0,
        })
        t['deals'] += r['deals'] or 0
        t['income'] += r['total_income'] or 0
        t['amount'] += r['total_amount'] or 0
        t['pos'] += r['positive_deals'] or 0
        t['neg'] += r['negative_deals'] or 0

    # Годы, у которых все строки без названия инструмента, в flat не попадают,
    # но в списке years есть — заводим их явно, чтобы ключ существовал всегда.
    for y in years:
        year_totals.setdefault(y, {
            'deals': 0, 'income': 0.0, 'amount': 0.0, 'pos': 0, 'neg': 0,
        })

    for y, td in tax_dict.items():
        t = year_totals.setdefault(y, {
            'deals': 0, 'income': 0.0, 'amount': 0.0, 'pos': 0, 'neg': 0,
        })
        t['income'] = td.get('broker_result', t['income'])

    # Группировка ОФЗ / Прочие → {grp: {year: {data}}}
    group_summary = get_nalog_group_summary()
    groups = {'ОФЗ': {}, 'Прочие': {}, 'Всего': {}}
    for r in group_summary:
        groups[r['grp']][r['year']] = {
            'deals': r['deals'],
            'total_income': r['total_income'],
            'total_profit': r['total_profit'],
            'total_loss': r['total_loss'],
        }
    # Добавляем «Всего» (сумма ОФЗ + Прочие)
    for grp in ('ОФЗ', 'Прочие'):
        for y, d in groups[grp].items():
            if y not in groups['Всего']:
                groups['Всего'][y] = {'deals': 0, 'total_income': 0.0, 'total_profit': 0.0, 'total_loss': 0.0}
            for k in ('deals', 'total_income', 'total_profit', 'total_loss'):
                groups['Всего'][y][k] += d[k]

    return render_template('nalog.html',
                           years=years,
                           pivot=pivot,
                           year_totals=year_totals,
                           tax_summary=tax_dict,
                           groups=groups)


def _default_period():
    """Период по умолчанию: с 1-го числа прошлого месяца по сегодня.

    Без фильтра дашборд тянул всю историю сделок — лишние мегабайты разметки
    и сотни миллисекунд на агрегаты. На практике смотрят текущий и предыдущий
    месяц, поэтому при первом заходе показываем именно их.
    """
    today = date.today()
    first_of_prev_month = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    return first_of_prev_month.isoformat(), today.isoformat()


def _period_args():
    """Период из query-строки с учётом значения по умолчанию.

    Различаем два случая:
      * параметров в адресе нет вовсе — первый заход, берём период по умолчанию;
      * параметры пришли пустыми (?date_from=&date_to=) — пользователь очистил
        поля и хочет видеть всё; пустая строка и означает «без фильтра».

    Без этого различия кнопка «сбросить» возвращала бы период по умолчанию
    и показать всю историю было бы нельзя.
    """
    if 'date_from' not in request.args and 'date_to' not in request.args:
        return _default_period()
    return request.args.get('date_from', ''), request.args.get('date_to', '')


@flask_app.route('/')
def index():
    date_from, date_to = _period_args()
    broker = request.args.get('broker', 'all')

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
    profit = get_trade_profit(None, df_dmy, dt_dmy, broker)
    # Лоты не считаем: блок «Детали» грузит их отдельно через /api/lots,
    # а полный список — это тысячи строк и сотни миллисекунд на запрос.
    open_trades = get_open_trades(None, df_dmy, dt_dmy, broker)
    instruments = get_instrument_summary(None, df_dmy, dt_dmy, broker)

    # Набор кодов инструментов, у которых есть открытая позиция
    open_codes = set(i['security_code'] for i in instruments) if instruments else set()

    repo_total = get_repo_total(None, df_dmy, dt_dmy, broker)

    quik_trades = get_recent_quik_trades(20)
    db_prices = get_current_prices()
    quik_pos = get_quik_positions(broker)
    my_instruments = get_my_instruments()

    # Индикатор соединения с QUIK: данные обновлялись за последние 10 секунд
    quik_connected = _prices_are_fresh(db_prices)

    # Цены для дашборда: QUIK, а если он молчит — резерв из T-Invest.
    global _TRACKED_INSTRUMENTS
    price_instruments = _position_instruments(open_trades, quik_pos, my_instruments)
    if price_instruments:
        _TRACKED_INSTRUMENTS = dict(price_instruments)
    prices, prices_source = _effective_prices(db_prices, price_instruments, quik_connected)

    # Добавляем текущую цену в открытые трейд-сделки для прогноза P&L
    open_trades = list(open_trades)
    price_map = {p['sec_code']: p['price'] for p in prices}

    # Подставляем названия для QUIK-сделок (у них только sec_code)
    name_map = {i['sec_code']: i['sec_name'] for i in my_instruments}
    my_price_codes = sorted({i['sec_code'] for i in my_instruments})
    for o in open_trades:
        o['current_price'] = price_map.get(o['security_code'], 0)
        if o.get('source') == 'quik' and o['security_code'] in name_map:
            o['security_name'] = name_map[o['security_code']]

    # Карта прогнозного P&L по открытым позициям на инструмент (уже с current_price!)
    TRADE_FEE_RATE = 0.000685
    forecast_map = {}
    if open_trades:
        for o in open_trades:
            code = o['security_code']
            cur = o.get('current_price', 0)
            if cur <= 0:
                # Нет текущей цены — прогноз не считаем (иначе -стоимость позиции
                # превращается в ложный «убыток -100%» и раздувает итог).
                continue
            gross = (cur - o['buy_price']) * o['qty']
            buy_fee = o['total_cost'] * TRADE_FEE_RATE
            sell_fee = cur * o['qty'] * TRADE_FEE_RATE
            net = gross - buy_fee - sell_fee
            forecast_map[code] = forecast_map.get(code, 0) + net

    # Реализованные строки: прогноз по открытым позициям выносим в отдельные
    # строки ниже (с пометкой is_open), чтобы покупки без продаж были видны
    # прямо в таблице «Прибыль» и не дублировали прогноз в итогах.
    from collections import defaultdict
    for p in profit:
        p['forecast_pl'] = 0.0
        p['total_pl'] = round(p['net_profit'], 2)
        p['is_open'] = False

    # Открытые позиции (куплено, нет продажи) — отдельные строки с пометкой.
    open_agg = defaultdict(lambda: {'qty': 0, 'name': ''})
    for o in open_trades:
        code = o['security_code']
        open_agg[code]['qty'] += o['qty']
        open_agg[code]['name'] = o['security_name']
    for code, d in open_agg.items():
        if d['qty'] <= 0:
            continue
        forecast = round(forecast_map.get(code, 0), 2)
        profit.append({
            'security_code': code,
            'security_name': d['name'],
            'buy_qty': d['qty'],
            'sell_qty': 0,
            'gross_profit': 0,
            'total_fees': 0,
            'net_profit': 0,
            'forecast_pl': forecast,
            'total_pl': forecast,
            'is_open': True,
        })

    # Движение денежных средств
    cash_flow = get_cash_flow_summary(None, df_dmy, dt_dmy, broker)

    # Цвета для значков тикеров (на основе class_code)
    TICKER_COLORS = {
        'TQBR': '#2563eb', 'TQOB': '#ea580c', 'TQTD': '#7c3aed',
        'TQBS': '#059669', '': '#6b7280',
    }

    return render_template('dashboard.html',
                           reports=reports,
                           profit=profit,
                           open_trades=open_trades,
                           instruments=instruments,
                           open_codes=open_codes,
                           repo_total=repo_total,
                           trade_fee_rate=TRADE_FEE_RATE,
                           price_map=price_map,
                           quik_trades=quik_trades,
                           prices=prices,
                           my_price_codes=my_price_codes,
                           quik_positions=quik_pos,
                           quik_connected=quik_connected,
                           prices_source=prices_source,
                           cash_flow=cash_flow,
                           date_from=df_dmy, date_to=dt_dmy,
                           date_from_iso=date_from, date_to_iso=date_to,
                           broker=broker)


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
                push_to_cloud()
            except Exception as e:
                flash(f'Ошибка: {e}', 'danger')
            return redirect(url_for('index'))
        # Scan for HTML/XLSX files in the current directory
        found = False
        for ext in ('*.html', '*.htm', '*.xlsx', '*.xls', '*.pdf'):
            for fp in glob.glob(os.path.join(REPORTS_DIR, ext)):
                try:
                    rid = parse_report(fp)
                    flash(f'Загружен: {os.path.basename(fp)} (id={rid})', 'success')
                    found = True
                except Exception as e:
                    flash(f'Ошибка {os.path.basename(fp)}: {e}', 'danger')
        if found:
            push_to_cloud()
        if not found:
            flash('Файлы отчётов не найдены', 'warning')
        return redirect(url_for('index'))

    # Handle uploaded files
    uploaded = 0
    for f in files:
        if f.filename:
            save_path = os.path.join(REPORTS_DIR, f.filename)
            f.save(save_path)
            try:
                rid = parse_report(save_path)
                flash(f'Загружен: {f.filename} (id={rid})', 'success')
                uploaded += 1
            except Exception as e:
                flash(f'Ошибка {f.filename}: {e}', 'danger')
    if uploaded:
        push_to_cloud()
    return redirect(url_for('index'))


@flask_app.route('/report/<int:report_id>', methods=['GET', 'POST'])
def report_view(report_id):
    if request.method == 'POST':
        delete_report(report_id)
        flash('Отчёт удалён', 'info')
        push_to_cloud()
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

    # НЕ перезаписываем current_price ценой сделки: цена покупки/продажи —
    # это не текущая рыночная цена (особенно для фьючерсов SPBFUT, где цена
    # контракта отличается). Рыночные цены приходят отдельно через /api/price.

    return jsonify({'status': 'ok', 'count': len(data['trades'])}), 200


# ── Accounts API (список счетов из QUIK) ──────────────────────

@flask_app.route('/api/accounts', methods=['GET'])
def api_accounts():
    """Get distinct accounts from QUIK trades (для настройки ACCOUNT_BROKER_MAP)."""
    from app.db import get_connection
    conn = get_connection()
    rows = conn.execute("""
        SELECT DISTINCT account, broker,
               COUNT(*) AS trade_count,
               MAX(created_at) AS last_seen
        FROM quik_trade
        WHERE account IS NOT NULL AND account != ''
        GROUP BY account, broker
        ORDER BY account
    """).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


# ── Detail lots API ───────────────────────────────────────────

@flask_app.route('/api/lots', methods=['GET'])
def api_lots():
    """Строки блока «Детали» на дашборде.

    Лотов набираются тысячи, и в HTML страницы они давали больше двух
    мегабайт, хотя блок по умолчанию свёрнут. Отдаём их отдельно и
    подгружаем при первом раскрытии.
    """
    def to_dmy(iso):
        parts = (iso or '').split('-')
        if len(parts) == 3:
            return f'{parts[2]}.{parts[1]}.{parts[0]}'
        return iso or ''

    df_dmy = to_dmy(request.args.get('date_from', ''))
    dt_dmy = to_dmy(request.args.get('date_to', ''))
    broker = request.args.get('broker', 'all')

    lots = get_trade_lots(None, df_dmy, dt_dmy, broker)
    instruments = get_instrument_summary(None, df_dmy, dt_dmy, broker)
    open_codes = set(i['security_code'] for i in instruments) if instruments else set()

    return render_template('_lots_rows.html', lots=lots, open_codes=open_codes)


# ── Instruments API ───────────────────────────────────────────

@flask_app.route('/api/instruments', methods=['GET', 'POST'])
def api_instruments():
    if request.method == 'POST':
        """Save instrument reference data (lotsize, min_step, etc.) from QUIK."""
        data = request.get_json(silent=True)
        if not data or 'instruments' not in data or not isinstance(data['instruments'], list):
            return jsonify({'error': 'Invalid JSON, expected {"instruments": [...]}'}), 400
        if not data['instruments']:
            return jsonify({'error': 'Empty instruments list'}), 400
        save_instruments_batch(data['instruments'])
        return jsonify({'status': 'ok', 'count': len(data['instruments'])}), 200

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
    """Get all current instrument prices (QUIK, с резервом из T-Invest)."""
    prices, _source = _effective_prices(get_current_prices())
    return jsonify(prices), 200


@flask_app.route('/api/quik-trades', methods=['GET'])
def api_quik_trades():
    """Get recent QUIK trades."""
    limit = request.args.get('limit', 20, type=int)
    trades = get_recent_quik_trades(limit)
    return jsonify(trades), 200


@flask_app.route('/api/quik-connected', methods=['GET'])
def api_quik_connected():
    """Поток цен: QUIK активен, а если нет — используется резерв T-Invest."""
    prices = get_current_prices()
    connected = _prices_are_fresh(prices)
    tinkoff = (not connected and tinkoff_prices.enabled()
               and tinkoff_prices.cached_count() > 0)
    return jsonify({
        'connected': connected,
        'tinkoff': bool(tinkoff),
        'source': 'quik' if connected else ('tinkoff' if tinkoff else 'none'),
    }), 200


@flask_app.route('/api/forecast', methods=['GET'])
def api_forecast():
    """Get forecast P&L for open positions based on current prices.

    Учитывает фильтр по брокеру и датам (query-параметры broker, date_from,
    date_to), чтобы прогноз на дашборде совпадал с выбранным фильтром.
    """
    broker = request.args.get('broker', 'all')
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')
    global _TRACKED_INSTRUMENTS

    # Конвертация HTML date (YYYY-MM-DD) → DD.MM.YYYY для БД
    def to_dmy(iso):
        if not iso:
            return ''
        parts = iso.split('-')
        if len(parts) == 3:
            return f'{parts[2]}.{parts[1]}.{parts[0]}'
        return iso

    # date_from не передаём: открытые позиции считаются по всей истории до
    # date_to (иначе позиция, открытая до начала фильтра, пропадала из прогноза).
    open_trades = list(get_open_trades(None, None, to_dmy(date_to), broker))

    db_prices = get_current_prices()
    quik_connected = _prices_are_fresh(db_prices)
    instruments = {o['security_code']: _TRACKED_INSTRUMENTS.get(o['security_code'], '')
                   for o in open_trades}
    if instruments:
        _TRACKED_INSTRUMENTS = dict(instruments)
    prices, _source = _effective_prices(db_prices, instruments, quik_connected)
    price_map = {p['sec_code']: p['price'] for p in prices}

    TRADE_FEE_RATE = 0.000685
    forecast_map = {}
    if open_trades:
        for o in open_trades:
            code = o['security_code']
            cur = price_map.get(code, 0)
            if cur <= 0:
                # Нет текущей цены — прогноз не считаем (иначе -стоимость позиции
                # превращается в ложный «убыток -100%» и раздувает итог).
                continue
            gross = (cur - o['buy_price']) * o['qty']
            buy_fee = o['total_cost'] * TRADE_FEE_RATE
            sell_fee = cur * o['qty'] * TRADE_FEE_RATE
            net = gross - buy_fee - sell_fee
            forecast_map[code] = forecast_map.get(code, 0) + net

    result = {}
    for code, val in forecast_map.items():
        result[code] = {
            'forecast': round(val, 2),
            'current_price': price_map.get(code, 0),
            'net_profit': None,  # заполняется на клиенте из HTML
        }
    return jsonify(result), 200


@flask_app.route('/screener')
def screener_view():
    """Скринер акций по объёмному анализу обезличенных сделок."""
    return render_template('screener.html',
                           status=screener.collector_status(),
                           signals=screener.SIGNAL_LABELS,
                           universe=screener.universe_snapshot())


@flask_app.route('/api/screener')
def api_screener():
    """Метрики скринера. Параметры: window, min_turnover, signal, limit."""
    window = request.args.get('window', 'session')
    try:
        min_turnover = float(request.args.get('min_turnover', 0) or 0)
    except ValueError:
        min_turnover = 0.0
    limit = max(1, min(request.args.get('limit', 200, type=int), 500))
    wanted = {s.strip() for s in (request.args.get('signal') or '').split(',') if s.strip()}

    rows = screener.compute_metrics(window=window, min_turnover=min_turnover)
    if wanted:
        rows = [r for r in rows if r['signal'] in wanted]

    # Волатильность считаем по минуткам того же окна: одна SQL-выборка на
    # весь универсум, поэтому таблица не начинает тормозить.
    try:
        vol_from, vol_to, _ = screener._window_bounds(window)
        vols = volatility.universe(vol_from, vol_to, interval=1)
    except Exception:
        vols = {}
    # Сигналы стратегии за сегодня — из фонового кэша (тяжёлый расчёт)
    try:
        signal_map, signals_at = signals_cache.snapshot()
    except Exception:
        signal_map, signals_at = {}, 0.0
    for row in rows:
        info = signal_map.get(row['ticker'], {})
        row['signal_count'] = int(info.get('count', 0))
        row['signal_taken'] = int(info.get('taken', 0))
        row['signal_skipped'] = int(info.get('skipped', 0))
        row['signal_last'] = info.get('last')
        row['signal_kinds'] = info.get('kinds', [])

    for row in rows:
        vol = vols.get(row['ticker'], {})
        row['vol_day_pct'] = vol.get('sigma_day_pct', 0.0)
        row['vol_range_rub'] = vol.get('range_rub', 0.0)
        row['vol_atr_rub'] = vol.get('atr_rub', 0.0)
        row['vol_range_pct'] = vol.get('range_pct', 0.0)

    # Качество прошлых сигналов и действующие индивидуальные параметры.
    try:
        quality = adaptive.quality_map()
    except Exception:
        quality = {}
    for row in rows:
        info = quality.get(row['ticker']) or {}
        row['quality_trades'] = int(info.get('trades', 0))
        row['quality_win_rate'] = float(info.get('win_rate', 0.0))
        row['quality_pf'] = float(info.get('profit_factor', 0.0))
        row['quality_net'] = float(info.get('net', 0.0))
        try:
            row['adaptive_params'] = adaptive.summary(row['ticker'])
        except Exception:
            row['adaptive_params'] = {}

    # Бумаги с сигналами стратегии — наверху, внутри группы по оценке.
    # Сначала принятые сигналы (их можно торговать), потом остальные.
    rows.sort(key=lambda r: (-r.get('signal_taken', 0),
                             -r.get('signal_count', 0), -r.get('score', 0)))

    return jsonify({
        'window': rows[0]['window'] if rows else window,
        'count': len(rows),
        'rows': rows[:limit],
        'status': screener.collector_status(),
        'signals_at': signals_at,
        'signals_total': sum(r.get('signal_count', 0) for r in rows),
    })


@flask_app.route('/api/screener/status')
def api_screener_status():
    return jsonify(screener.collector_status())


@flask_app.route('/api/screener/<ticker>')
def api_screener_instrument(ticker):
    """Детализация: метрики, лента сделок и дельта по минутам."""
    window = request.args.get('window', 'session')
    limit = max(10, min(request.args.get('limit', 150, type=int), 500))
    detail = screener.instrument_detail(ticker, window=window, limit=limit)
    if detail is None:
        return jsonify({'error': 'Инструмент не найден в универсуме'}), 404
    return jsonify(detail)


@flask_app.route('/api/screener/refresh-universe', methods=['POST'])
def api_screener_refresh_universe():
    """Пересобрать универсум топ-N по обороту (в фоне, до минуты)."""
    if not screener.is_enabled():
        return jsonify({'error': 'Скринер выключен: задайте TINKOFF_TOKEN'}), 400
    if not screener.request_universe_refresh():
        return jsonify({'ok': False, 'message': 'Обновление уже выполняется'}), 202
    return jsonify({'ok': True, 'message': 'Обновление универсума запущено'}), 202



# ── График инструмента и бэктест ─────────────────────────────


@flask_app.route('/chart')
@flask_app.route('/chart/<ticker>')
def chart_view(ticker=None):
    """График свечей с маркерами сделок и панелью бэктеста."""
    if not ticker:
        rows = screener.universe_snapshot()
        first = next((r['ticker'] for r in rows if r['selected']), 'SBER')
        return redirect(url_for('chart_view', ticker=first))
    return render_template('chart.html',
                           ticker=ticker.upper(),
                           strategies=strategy_levels.catalog())


def _chart_range():
    range_key = request.args.get('range', '3d')
    interval = request.args.get('interval', 5, type=int)
    if interval not in chartdata.INTERVALS:
        interval = 5
    from_ns, to_ns, _ = chartdata.resolve_range(range_key)
    return from_ns, to_ns, range_key, interval


@flask_app.route('/api/chart/<ticker>')
def api_chart(ticker):
    """Свечи, объём, дельта и VWAP за выбранный диапазон."""
    from_ns, to_ns, range_key, interval = _chart_range()
    try:
        data = chartdata.series(ticker, from_ns, to_ns, interval)
        data['volatility'] = volatility.metrics(ticker, from_ns, to_ns, interval)
    except chartdata.ChartError as exc:
        return jsonify({'error': str(exc)}), 400
    data['range'] = range_key
    return jsonify(data)


@flask_app.route('/api/signals/<ticker>')
def api_signals(ticker):
    """Сигналы на покупку по уровням за торговые дни диапазона.

    Считаются по каждому дню отдельно на минутных барах (``app.signal_days``):
    сигнал дня X одинаков при любом диапазоне и таймфрейме графика, поэтому
    историю сигналов можно сравнивать между днями и оценивать стратегию.
    """
    from_ns, to_ns, range_key, _interval = _chart_range()
    payload = {k: request.args.get(k) for k in
               ('warmup_min', 'approach_pct', 'break_margin_pct', 'confirm_delta',
                'volume_factor', 'take_profit_pct', 'stop_loss_pct', 'max_hold_min')}
    payload = {k: v for k, v in payload.items() if v is not None}
    # По умолчанию — индивидуальные параметры инструмента; явные параметры в
    # запросе (manual=1 или любое значение) включают ручной режим.
    manual = request.args.get('manual') == '1' or bool(payload)
    signals, zones = signal_days.signals_for_range(
        ticker, from_ns, to_ns, payload if manual else None)
    return jsonify({
        'ticker': ticker.upper(),
        'params': strategy_levels._resolve(payload),
        'signals': signals,
        'markers': strategy_levels.signal_markers(signals),
        'zones': [{'price': z['price'], 'low': z['low'], 'high': z['high'],
                   'side': z['side'], 'origin': z.get('origin')} for z in zones],
        'count': len(signals),
        'range_key': range_key,
    })


@flask_app.route('/api/levels/<ticker>')
def api_levels(ticker):
    """Профиль объёма по цене, зоны поддержки/сопротивления и вердикт."""
    from_ns, to_ns, range_key, _ = _chart_range()
    try:
        buckets = int(request.args.get('buckets', 80))
    except (TypeError, ValueError):
        buckets = 80
    try:
        large = float(request.args.get('large_rub') or 0) or \
            screener.get_config().large_trade_rub
    except ValueError:
        large = screener.get_config().large_trade_rub
    try:
        days = max(0, min(int(request.args.get('days', 0) or 0), 180))
    except (TypeError, ValueError):
        days = 0
    try:
        data = levels.analyze(ticker, from_ns, to_ns, buckets=buckets, large_rub=large,
                              history_days=days)
    except levels.LevelsError as exc:
        return jsonify({'error': str(exc)}), 400
    data['range_key'] = range_key
    return jsonify(data)


@flask_app.route('/api/volatility/<ticker>')
def api_volatility(ticker):
    """Волатильность инструмента: абсолютная (₽) и относительная (%)."""
    from_ns, to_ns, _, interval = _chart_range()
    try:
        data = volatility.metrics(ticker, from_ns, to_ns, interval)
    except chartdata.ChartError as exc:
        return jsonify({'error': str(exc)}), 400
    return jsonify(data)


@flask_app.route('/api/chart/<ticker>/markers')
def api_chart_markers(ticker):
    """Маркеры сделок: крупные принты или все (с ограничением)."""
    from_ns, to_ns, _, _ = _chart_range()
    mode = request.args.get('mode', 'large')
    if mode not in ('large', 'all'):
        mode = 'large'
    try:
        min_value = float(request.args.get('min_value') or 0)
    except ValueError:
        min_value = 0.0
    if min_value <= 0:
        min_value = screener.get_config().large_trade_rub
    try:
        return jsonify(chartdata.markers(ticker, from_ns, to_ns, min_value, mode))
    except chartdata.ChartError as exc:
        return jsonify({'error': str(exc)}), 400


@flask_app.route('/api/chart/<ticker>/tape')
def api_chart_tape(ticker):
    from_ns, to_ns, _, _ = _chart_range()
    limit = max(10, min(request.args.get('limit', 100, type=int), 500))
    return jsonify({'items': chartdata.tape(ticker, from_ns, to_ns, limit)})


@flask_app.route('/api/backtest/scan', methods=['POST'])
def api_backtest_scan():
    """Проверка стратегии по универсуму: in-sample / out-of-sample.

    Отвечает на вопрос «эффективна ли стратегия», а не «что было по одной
    бумаге»: один набор параметров на всём периоде, сравнение двух половин.
    """
    payload = request.get_json(silent=True) or {}
    strategy = payload.get('strategy') or strategy_levels.KEY
    range_key = payload.get('range') or 'today'
    try:
        interval = int(payload.get('interval', 1))
    except (TypeError, ValueError):
        interval = 1
    if interval not in chartdata.INTERVALS:
        interval = 1
    try:
        top = max(1, min(int(payload.get('top', 20)), 200))
    except (TypeError, ValueError):
        top = 20

    def _num(value, default):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    notional = max(1000.0, _num(payload.get('notional'), 100_000.0))
    fee_pct = max(0.0, min(_num(payload.get('fee_pct'), 0.05), 1.0))
    try:
        split = min(0.9, max(0.1, _num(payload.get('split'), 0.6)))
    except (TypeError, ValueError):
        split = 0.6

    tickers = [t['ticker'] for t in screener.universe_snapshot() if t['selected']][:top]
    if not tickers:
        return jsonify({'error': 'Универсум пуст — сначала соберите данные'}), 400

    from_ns, to_ns, _ = chartdata.resolve_range(range_key)
    try:
        result = backtest.scan(tickers, from_ns, to_ns, strategy, payload.get('params'),
                               interval=interval, notional=notional, fee_pct=fee_pct,
                               split=split)
    except (chartdata.ChartError, backtest.BacktestError) as exc:
        return jsonify({'error': str(exc)}), 400
    return jsonify(result)


@flask_app.route('/api/backtest/<ticker>', methods=['POST'])
def api_backtest(ticker):
    """Прогнать intraday-стратегию по минуткам инструмента."""
    payload = request.get_json(silent=True) or {}
    strategy = payload.get('strategy') or strategy_levels.KEY
    range_key = payload.get('range') or request.args.get('range', 'today')
    interval = payload.get('interval', 1)
    try:
        interval = int(interval)
    except (TypeError, ValueError):
        interval = 1
    if interval not in chartdata.INTERVALS:
        interval = 1
    from_ns, to_ns, _ = chartdata.resolve_range(range_key)

    def _num(value, default):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    notional = max(1000.0, _num(payload.get('notional'), 100_000.0))
    fee_pct = max(0.0, min(_num(payload.get('fee_pct'), 0.05), 1.0))
    try:
        if strategy == strategy_levels.KEY:
            # Бэктест считаем по дням на минутках — так же, как сигналы.
            # Иначе на 5-минутках/3 днях пороги другие и «сделки» исчезают,
            # хотя скринер показывает принятый сигнал.
            days = signal_days.day_results(ticker, from_ns, to_ns,
                                           payload.get('params'))
            signals = sorted((s for d in days for s in d['signals']),
                             key=lambda s: s['time'])
            trades = sorted((t for d in days for t in d['trades']),
                            key=lambda t: t['exit_time'])
            running = 0.0
            equity = []
            for trade in trades:
                running += trade['pnl']
                equity.append({'time': trade['exit_time'], 'value': round(running, 2)})
            metrics = strategy_levels._metrics(trades, running, notional)
            metrics['skipped_rr'] = sum(
                (d.get('metrics') or {}).get('skipped_rr', 0) for d in days)
            last = days[-1] if days else None
            result = {
                'ticker': ticker.upper(),
                'strategy': strategy_levels.KEY,
                'label': strategy_levels.LABEL,
                'params': last['params'] if last else strategy_levels._resolve(payload.get('params')),
                'interval': 1,
                'notional': notional,
                'fee_pct': fee_pct,
                'bars': sum(d.get('bars', 0) for d in days),
                'signals': signals,
                'trades': trades,
                'equity': equity,
                'metrics': metrics,
                'stop_enabled': last.get('stop_enabled') if last else True,
                'no_stop_tickers': last.get('no_stop_tickers', []) if last else [],
                'markers': strategy_levels.signal_markers(signals),
            }
        else:
            result = backtest.run(ticker, from_ns, to_ns, strategy, payload.get('params'),
                                  interval=interval, notional=notional, fee_pct=fee_pct)
    except (chartdata.ChartError, backtest.BacktestError, levels.LevelsError) as exc:
        return jsonify({'error': str(exc)}), 400
    return jsonify(result)


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
