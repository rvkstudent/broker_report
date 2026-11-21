"""Скринер инструментов: дивидендные акции и облигации с подсветкой кандидатов.

Что даёт страница: таблицу бумаг с ключевыми показателями для выбора и
автоматический скоринг — топ получает бейдж «кандидат», чтобы не сравнивать
сотни строк вручную.

Данные:
* **Облигации** — одним снимком доски MOEX ISS (TQOB/TQCB): цена, YTM,
  дюрация, купон, НКД, оферта, листинг, оборот. Дополнительных запросов нет.
* **Акции** — снимок TQBR отдаёт цену и оборот; дивиденды, P/E, P/B и ROE
  берутся из T-Invest (`GetDividends`, `GetAssetFundamentals`). Без токена
  эти колонки остаются пустыми, остальное работает.

Скоринг прозрачный (формулы — в `_score_bond` / `_score_stock`), все веса
показаны на странице в блоке «Как считается».
"""

from __future__ import annotations

import json
import math
import threading
import time
from datetime import date, datetime

from app.bond_ladder import _get_json, _table, _num
from app.db import get_connection

ISS = 'https://iss.moex.com/iss'

BOND_BOARDS = ('TQOB', 'TQCB')
STOCK_BOARD = 'TQBR'

BOND_MIN_TURNOVER = 1_000_000     # ₽ за день: ниже — не выйти из бумаги
BOND_LIMIT = 400
STOCK_MIN_TURNOVER = 3_000_000
STOCK_LIMIT = 80

CANDIDATES = 10                   # сколько строк подсвечиваем
DIV_ANOMALY = 40.0                # % годовых: выше — почти всегда разовая выплата
DIV_FOR_SCORE = 25.0               # потолок дивдоходности в балле
TTL = 4 * 3600                    # секунд: как часто пересобираем снимок
PAGE_TIMEOUT = 25.0

_lock = threading.Lock()
_refresh_started = False
_last_attempt = 0.0
_RETRY_PAUSE = 600.0              # если сбор упал — не чаще раза в 10 минут


# ── сбор данных: облигации ──────────────────────────────────

def _board_rows(board: str) -> list[dict]:
    """Все бумаги доски одним списком: securities + marketdata.

    ISS отдаёт доску целиком одним ответом (TQCB ≈ 3 000 бумаг, TQOB — 61),
    поэтому постраничный обход не нужен: параметр ``start`` на этом эндпоинте
    игнорируется и вернул бы те же строки.
    """
    doc = _get_json(f'/engines/stock/markets/bonds/boards/{board}/securities.json',
                    {'iss.only': 'securities,marketdata'})
    if not doc:
        return []
    md = {r.get('SECID'): r for r in _table(doc, 'marketdata')}
    seen: set[str] = set()
    rows: list[dict] = []
    for s in _table(doc, 'securities'):
        code = s.get('SECID') or ''
        if code in seen:
            continue
        seen.add(code)
        rows.append({**s, '_md': md.get(code) or {}})
    return rows


def _bond_type(row: dict) -> str:
    unit = str(row.get('FACEUNIT') or 'SUR').upper()
    if unit not in ('SUR', 'RUB', ''):
        return 'Валютная'
    if row.get('_board') == 'TQOB' or 'ОФЗ' in str(row.get('SHORTNAME') or ''):
        return 'ОФЗ'
    return 'Корпоративная'


def _bond_row(row: dict, board: str) -> dict | None:
    md = row.get('_md') or {}
    ytm = _num(md.get('YIELD')) or _num(md.get('YIELDATWAPRICE')) or _num(md.get('CLOSEYIELD'))
    matdate = str(row.get('MATDATE') or '')
    if not ytm or not matdate:
        return None
    turnover = _num(md.get('VALTODAY')) or 0.0
    if turnover < BOND_MIN_TURNOVER:
        return None
    # YTM выше 45% на рублёвом рынке — почти всегда артефакт расчёта ISS
    # (амортизация, нестандартный купон), а не реальная доходность.
    if ytm > 45:
        return None
    price = _num(md.get('LAST')) or _num(md.get('LCURRENTPRICE')) or _num(row.get('PREVPRICE'))
    duration_days = _num(md.get('DURATION')) or 0.0
    coupon_period = _num(row.get('COUPONPERIOD')) or 0
    freq = int(round(365 / coupon_period)) if coupon_period else 0
    try:
        years = max((date.fromisoformat(matdate) - date.today()).days / 365.25, 0.0)
    except ValueError:
        years = None

    offer = ''
    for key in ('PUTOPTIONDATE', 'OFFERDATE', 'CALLOPTIONDATE'):
        value = str(row.get(key) or '')
        if value and value != '0000-00-00':
            offer = f'{key[:3].lower()}:{value}'
            break

    return {
        'code': row.get('SECID') or '',
        'isin': row.get('ISIN') or '',
        'name': row.get('SHORTNAME') or row.get('SECNAME') or '',
        'type': _bond_type({**row, '_board': board}),
        'board': board,
        'currency': 'RUB' if str(row.get('FACEUNIT') or 'SUR').upper() in ('SUR', 'RUB', '') else row.get('FACEUNIT'),
        'matdate': matdate,
        'years': round(years, 1) if years is not None else None,
        'ytm': round(ytm, 2),
        'duration': round(duration_days / 365.25, 2) if duration_days else None,
        'price': round(price, 2) if price else None,
        'coupon_pct': _num(row.get('COUPONPERCENT')),
        'coupon_freq': freq,
        'aci': _num(row.get('ACCRUEDINT')),
        'facevalue': _num(row.get('FACEVALUE')),
        'listlevel': int(_num(row.get('LISTLEVEL')) or 0),
        'turnover': round(turnover),
        'trades': int(_num(md.get('NUMTRADES')) or 0),
        'issuesize': _num(row.get('ISSUESIZE')),
        'offer': offer,
        'subtype': row.get('BONDSUBTYPE') or '',
    }


def _bonds() -> tuple[list[dict], list[dict], list[str]]:
    """Рублёвые и валютные выпуски раздельно.

    Валютные не ранжируются вместе с рублёвыми: у ISS доходность таких бумаг
    приходит в разных базах (то в валюте номинала, то пересчитанная в рубли),
    и общий балл получился бы бессмысленным. Их показываем отдельной вкладкой
    по обороту — там важны купон, погашение и ликвидность, а не «лучший балл».
    """
    rub: list[dict] = []
    fx: list[dict] = []
    errors: list[str] = []
    for board in BOND_BOARDS:
        rows = _board_rows(board)
        if not rows:
            errors.append(f'{board}: нет данных')
            continue
        for row in rows:
            item = _bond_row(row, board)
            if not item:
                continue
            (fx if item['currency'] != 'RUB' else rub).append(item)
    rub.sort(key=lambda r: -(r['turnover'] or 0))
    fx.sort(key=lambda r: -(r['turnover'] or 0))
    return rub[:BOND_LIMIT], fx[:100], errors


# ── сбор данных: акции ──────────────────────────────────────

def _shares_snapshot() -> list[dict]:
    doc = _get_json(f'/engines/stock/markets/shares/boards/{STOCK_BOARD}/securities.json',
                    {'iss.only': 'securities,marketdata'})
    if not doc:
        return []
    sec = _table(doc, 'securities')
    md = {r.get('SECID'): r for r in _table(doc, 'marketdata')}
    rows = []
    for s in sec:
        m = md.get(s.get('SECID')) or {}
        turnover = _num(m.get('VALTODAY')) or 0.0
        if turnover < STOCK_MIN_TURNOVER:
            continue
        rows.append({'secid': s.get('SECID'), 'name': s.get('SHORTNAME') or '',
                     'price': _num(m.get('LAST')) or _num(m.get('LCURRENTPRICE')) or _num(s.get('PREVPRICE')),
                     'turnover': turnover,
                     'listlevel': int(_num(s.get('LISTLEVEL')) or 0),
                     'issuesize': _num(s.get('ISSUESIZE'))})
    rows.sort(key=lambda r: -r['turnover'])
    return rows[:STOCK_LIMIT]


def _tinkoff():
    try:
        from app import dashboard_prices
        if not dashboard_prices.enabled():
            return None
        from app.tinkoff_api import TinkoffClient
        return TinkoffClient(dashboard_prices.token())
    except Exception:  # noqa: BLE001
        return None


def _dividends(client, uid: str) -> list[dict]:
    try:
        doc = client._post(
            'tinkoff.public.invest.api.contract.v1.InstrumentsService/GetDividends',
            {'instrumentId': uid})
    except Exception:  # noqa: BLE001
        return []
    out = []
    for item in doc.get('dividends') or []:
        q = item.get('dividendNet') or {}
        try:
            value = float(q.get('units') or 0) + float(q.get('nano') or 0) / 1e9
        except (TypeError, ValueError):
            continue
        if value <= 0:
            continue
        out.append({'value': value,
                    'pay': str(item.get('paymentDate') or '')[:10],
                    'declared': str(item.get('declaredDate') or '')[:10]})
    out.sort(key=lambda d: d['pay'])
    return out


def _fundamentals(client, assets: list[str]) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for i in range(0, len(assets), 100):
        chunk = [a for a in assets[i:i + 100] if a]
        if not chunk:
            continue
        try:
            doc = client._post(
                'tinkoff.public.invest.api.contract.v1.InstrumentsService/GetAssetFundamentals',
                {'assets': chunk})
        except Exception:  # noqa: BLE001
            continue
        for row in doc.get('fundamentals') or []:
            result[row.get('assetUid')] = row
    return result


def _div_stats(dividends: list[dict], price: float | None) -> dict:
    """Дивдоходность за 12 мес, средняя за 3 года и сколько лет платит."""
    if not dividends:
        return {'div12': None, 'div3y': None, 'years_paid': 0, 'last_div': None,
                'last_date': '', 'div_forward': None, 'forward_date': ''}
    today = date.today()
    paid = sorted((d for d in dividends if d['pay'] and d['pay'] <= today.isoformat()),
                  key=lambda d: d['pay'])
    sum12 = sum(d['value'] for d in paid if d['pay'] >= _shift_year(today, -1))
    sum3 = sum(d['value'] for d in paid if d['pay'] >= _shift_year(today, -3))
    years = len({d['pay'][:4] for d in paid})
    last = paid[-1] if paid else None
    # Объявленные, но ещё не выплаченные дивиденды: T-Invest отдаёт их с будущей
    # датой. Это единственный форвардный сигнал из открытых данных — по нему
    # видно, например, что выплата падает (35 ₽ → 7 ₽), хотя «ДД за 12 мес» ещё
    # показывает старую цифру.
    horizon = _shift_year(today, 1)
    future = [d for d in dividends if d['pay'] and today.isoformat() < d['pay'] <= horizon]
    forward = sum(d['value'] for d in future)
    return {
        'div_forward': round(forward / price * 100, 2) if price and forward else None,
        'forward_date': future[0]['pay'] if future else '',
        'div12': round(sum12 / price * 100, 2) if price else None,
        'div3y': round(sum3 / 3 / price * 100, 2) if price else None,
        'years_paid': years,
        'last_div': round(last['value'], 2) if last else None,
        'last_date': last['pay'] if last else '',
    }


def _shift_year(day: date, years: int) -> str:
    try:
        return day.replace(year=day.year + years).isoformat()
    except ValueError:
        return day.replace(year=day.year + years, day=28).isoformat()


def _clean_roe(value: float | None) -> float | None:
    """ROE из T-Invest иногда приходит с мусорным масштабом (−1987%) — режем."""
    if value is None or abs(value) > 300:
        return None
    return round(value, 1)


def _stocks() -> tuple[list[dict], list[str]]:
    errors: list[str] = []
    rows = _shares_snapshot()
    if not rows:
        return [], ['TQBR: нет данных']

    client = _tinkoff()
    uid_by_ticker: dict[str, dict] = {}
    fundamentals: dict[str, dict] = {}
    divs: dict[str, list[dict]] = {}
    if client is None:
        errors.append('T-Invest недоступен: дивиденды и мультипликаторы не рассчитаны')
    else:
        try:
            doc = client._post(
                'tinkoff.public.invest.api.contract.v1.InstrumentsService/Shares',
                {'instrumentStatus': 'INSTRUMENT_STATUS_BASE'})
            for ins in doc.get('instruments') or []:
                uid_by_ticker[ins.get('ticker') or ''] = ins
        except Exception as exc:  # noqa: BLE001
            errors.append(f'T-Invest Shares: {type(exc).__name__}')
        for r in rows:
            ins = uid_by_ticker.get(r['secid'])
            if not ins:
                continue
            r['uid'] = ins.get('uid')
            r['asset'] = ins.get('assetUid')
            r['lot'] = ins.get('lot')
            divs[r['secid']] = _dividends(client, ins.get('uid') or '')
        fundamentals = _fundamentals(client, [r.get('asset') or '' for r in rows])

    out = []
    for r in rows:
        price = r.get('price')
        history = divs.get(r['secid']) or []
        stats = _div_stats(history, price)
        f = fundamentals.get(r.get('asset') or '') or {}
        out.append({
            'code': r['secid'],
            'name': r['name'],
            'price': round(price, 2) if price else None,
            'div12': stats['div12'],
            'div3y': stats['div3y'],
            'years_paid': stats['years_paid'],
            'last_div': stats['last_div'],
            'last_date': stats['last_date'],
            # История выплат: в модалке и в AI-анализе нужно видеть, разовая
            # выплата была или регулярная (по одной цифре этого не понять).
            'div_forward': stats['div_forward'],
            'forward_date': stats['forward_date'],
            'div_history': [{'value': round(d['value'], 2), 'pay': d['pay']}
                            for d in history[-5:]],
            'pe': round(_num(f.get('peRatioTtm')) or 0, 2) or None,
            'pb': round(_num(f.get('priceToBookTtm')) or 0, 2) or None,
            'roe': _clean_roe(_num(f.get('roe'))),
            'div_ttm_issuer': round(_num(f.get('dividendYieldDailyTtm')) or 0, 2) or None,
            'marketcap': _num(f.get('marketCapitalization')),
            'turnover': round(r['turnover']),
            'listlevel': r['listlevel'],
            'lot': r.get('lot'),
        })
    out.sort(key=lambda r: -(r['turnover'] or 0))
    # Предварительные сигналы (только акции): считаются по минутным данным
    # скринера, без сети, и попадают в тот же снимок.
    try:
        from app import prelim
        signals = prelim.for_tickers([r['code'] for r in out])
        for row in out:
            row['prelim'] = signals.get((row.get('code') or '').upper())
    except Exception:  # noqa: BLE001
        for row in out:
            row['prelim'] = None
    return out, errors


# ── скоринг ─────────────────────────────────────────────────

def _scale(value, low: float, high: float) -> float:
    """Линейная нормировка в 0..1 с обрезкой по краям."""
    if value is None:
        return 0.0
    return max(0.0, min(1.0, (float(value) - low) / (high - low)))


def _ofz_curve(bonds: list[dict]) -> list[tuple[float, float]]:
    """Точки кривой ОФЗ (дюрация → YTM) для расчёта премии корпоратов."""
    return sorted((r['duration'], r['ytm']) for r in bonds
                  if r.get('type') == 'ОФЗ' and r.get('duration') and r.get('ytm'))


def _curve_ytm(points: list[tuple[float, float]], duration: float) -> float | None:
    """YTM безрисковой кривой в точке duration (линейно между соседними ОФЗ)."""
    if not points:
        return None
    for i in range(len(points) - 1):
        x0, y0 = points[i]
        x1, y1 = points[i + 1]
        if x0 <= duration <= x1 and x1 > x0:
            return y0 + (y1 - y0) * (duration - x0) / (x1 - x0)
    return points[0][1] if duration < points[0][0] else points[-1][1]


def add_risk_flags(bonds: list[dict]) -> None:
    """Пометки риска: премия к ОФЗ, слишком высокий купон, листинг, ликвидность."""
    points = _ofz_curve(bonds)
    for row in bonds:
        flags = []
        duration = row.get('duration') or 0.0
        base = _curve_ytm(points, duration) if row.get('type') != 'ОФЗ' else None
        row['spread'] = (round(row['ytm'] - base, 2)
                         if base is not None and row.get('ytm') else None)
        if row.get('currency') != 'RUB':
            flags.append('валютная')
        elif row.get('spread') is not None and row['spread'] > 6:
            flags.append(f"премия к ОФЗ {row['spread']:.1f} п.п. — кредитный риск")
        if (row.get('coupon_pct') or 0) > 20:
            flags.append('купон выше 20%')
        if (row.get('listlevel') or 0) >= 3:
            flags.append('листинг 3-го уровня')
        if (row.get('turnover') or 0) < 3_000_000:
            flags.append('низкая ликвидность')
        if row.get('offer'):
            flags.append(f"оферта {row['offer'].split(':')[1]}")
        row['flags'] = flags
        row['risky'] = any(f.startswith(('премия к ОФЗ', 'купон выше')) for f in flags)


def _score_bond(row: dict) -> float:
    """Облигации: доходность + доходность на год дюрации + ликвидность + листинг.

    Сравнивать валютные выпуски с рублёвыми нельзя — у них своя шкала
    доходности (8% в USD ≈ 17% в рублях), поэтому нормировка разная.
    Явно рискованные бумаги (большая премия к ОФЗ или купон > 20%) получают
    штраф: высокая доходность там — плата за кредитный риск, а не за удачу.
    """
    duration = row.get('duration') or 0.0
    ytm = row.get('ytm') or 0.0
    if duration <= 0 or not ytm:
        return 0.0
    eff = ytm / duration
    liquidity = _scale(math.log10(max(row.get('turnover') or 1, 1)) - 5, 0, 3)
    level = 1.0 if row.get('listlevel') == 1 else (0.5 if row.get('listlevel') == 2 else 0.15)
    score = 100 * (0.40 * _scale(ytm, 12, 22)
                   + 0.20 * _scale(eff, 1.5, 5)
                   + 0.25 * liquidity
                   + 0.15 * level)
    if row.get('risky'):
        score -= 15
    return round(max(score, 0.0), 1)


def _score_stock(row: dict) -> float:
    """Акции: дивиденды + оценка + ликвидность + история выплат.

    Дивдоходность (12 мес) — 45%, средняя за 3 года — 20% (устойчивость),
    P/E ниже 12 — 15%, оборот — 10%, число лет выплат — 10%.

    Дивдоходность в балле ограничена ``DIV_FOR_SCORE``: разовая выплата
    (продажа «дочек», спецдивиденд) даёт 100%+ и без потолка вытеснила бы
    из топа все нормальные бумаги.
    """
    div12 = row.get('div12')
    div12 = min(div12, DIV_FOR_SCORE) if div12 is not None else None
    div3y = row.get('div3y')
    div3y = min(div3y, DIV_FOR_SCORE) if div3y is not None else None
    pe_score = 1.0 - _scale(row.get('pe'), 5, 20) if row.get('pe') else 0.35
    liquidity = _scale(math.log10(max(row.get('turnover') or 1, 1)) - 6, 0, 2.5)
    history = _scale(row.get('years_paid'), 0, 5)
    score = 100 * (0.45 * _scale(div12, 0, 15)
                   + 0.20 * _scale(div3y, 0, 12)
                   + 0.15 * pe_score
                   + 0.10 * liquidity
                   + 0.10 * history)
    if (row.get('div12') or 0) > DIV_ANOMALY:
        score -= 12      # разовая выплата — не повод ставить бумагу в топ
    return round(max(score, 0.0), 1)


def _stock_flags(row: dict) -> list[str]:
    """Пометки, по которым бумагу надо смотреть глазами, а не по баллу."""
    flags = []
    last, fwd = row.get('last_div'), row.get('div_forward')
    if last and fwd is not None and row.get('price'):
        forward_rub = row['price'] * fwd / 100
        if forward_rub < last * 0.7:
            flags.append(f"объявлена выплата {forward_rub:.2f} ₽ против {last:.2f} ₽ — "
                         f"ДД за 12 мес не повторится")
    if (row.get('div12') or 0) > DIV_ANOMALY:
        flags.append(f"аномальная ДД {row['div12']:.0f}% — вероятно разовая выплата")
    if row.get('pe') and row['pe'] < 1:
        flags.append('P/E ниже 1 — проверить отчётность')
    if row.get('roe') is not None and row['roe'] < 0:
        flags.append('отрицательный ROE')
    return flags


# ── рейтинг надёжности ──────────────────────────────────────

RELIABILITY_LABELS = (
    (8.5, 'A', 'высокая'),
    (7.0, 'B', 'выше средней'),
    (5.5, 'C', 'средняя'),
    (1.0, 'D', 'низкая'),
)


def _reliability_label(score: float) -> tuple[str, str]:
    for threshold, letter, text in RELIABILITY_LABELS:
        if score >= threshold:
            return letter, text
    return 'D', 'низкая'


def reliability_bond(row: dict) -> dict:
    """Надёжность облигации, 1–10 — наша оценка, а не рейтинг агентства.

    Рейтингов АКРА/Эксперт РА в открытом API нет (это платные данные), поэтому
    считаем прокси из того, что видно рынку. Факторы:

    * база по типу: ОФЗ — государство, валютная — эмитент + курс, корпоративная — ниже;
    * **спред к кривой ОФЗ** — главный рыночный индикатор кредитного риска:
      чем больше премия, тем ниже надёжность (до −3 баллов при 12 п.п.);
    * уровень листинга MOEX: 1-й — допущен к торгам без ограничений;
    * ликвидность (оборот): из бумаги должно быть можно выйти;
    * размер выпуска — крупные выпуски обычно качественнее;
    * близкая оферта и купон выше 20% — минус: доходность может не реализоваться.
    """
    reasons: list[str] = []
    kind = row.get('type')
    score = {'ОФЗ': 9.5, 'Валютная': 7.0}.get(kind, 6.5)
    reasons.append(f'{kind}: база {score:.1f}')

    spread = row.get('spread')
    if spread is not None and kind != 'ОФЗ':
        penalty = min(max(spread, 0.0), 12.0) / 12.0 * 3.0
        score -= penalty
        reasons.append(f'спред к ОФЗ {spread:+.1f} п.п. → −{penalty:.1f}')

    level = row.get('listlevel') or 0
    if level == 1:
        score += 0.5
        reasons.append('листинг 1 → +0.5')
    elif level >= 3:
        score -= 1.0
        reasons.append(f'листинг {level} → −1.0')

    turnover = row.get('turnover') or 0
    liq = _scale(math.log10(max(turnover, 1)) - 5, 0, 3) * 0.8
    score += liq
    reasons.append(f'оборот {turnover/1e6:.1f} млн → +{liq:.1f}')

    size = row.get('issuesize') or 0
    if size:
        add = _scale(math.log10(size) - 5, 0, 3) * 0.4
        score += add
        reasons.append(f'выпуск {size/1e6:.0f} млн шт → +{add:.1f}')

    offer = row.get('offer') or ''
    if offer:
        day = offer.split(':')[-1]
        try:
            years_to_offer = (date.fromisoformat(day) - date.today()).days / 365.25
            if 0 <= years_to_offer <= 1.5:
                score -= 0.5
                reasons.append(f'оферта через {years_to_offer:.1f} г. → −0.5')
        except ValueError:
            pass

    if (row.get('coupon_pct') or 0) > 20:
        score -= 1.0
        reasons.append('купон выше 20% → −1.0')

    score = round(max(1.0, min(10.0, score)), 1)
    letter, text = _reliability_label(score)
    return {'score': score, 'letter': letter, 'text': text, 'reasons': reasons}


def reliability_stock(row: dict) -> dict:
    """Устойчивость акции, 1–10 — тоже наша оценка по доступным метрикам.

    Смысл: бумага платит дивиденды регулярно, компания прибыльна, оценка не
    аномальна, акция ликвидна. Это не кредитный рейтинг и не прогноз цены.
    """
    reasons: list[str] = []
    score = 6.0
    reasons.append('база 6.0')

    years = min(row.get('years_paid') or 0, 6)
    add = years / 6 * 1.2
    score += add
    reasons.append(f'выплат {years} лет → +{add:.1f}')

    roe = row.get('roe')
    if roe is None:
        reasons.append('ROE нет данных')
    elif roe < 0:
        score -= 1.0
        reasons.append('ROE отрицательный → −1.0')
    else:
        add = min(roe, 40.0) / 40.0 * 1.0
        score += add
        reasons.append(f'ROE {roe:.0f}% → +{add:.1f}')

    pe = row.get('pe')
    if pe is None:
        reasons.append('P/E нет данных')
    elif pe < 1:
        score -= 0.8
        reasons.append(f'P/E {pe:.1f} < 1 → −0.8')
    elif pe <= 12:
        score += 0.8
        reasons.append(f'P/E {pe:.1f} → +0.8')
    elif pe <= 25:
        score += 0.3
        reasons.append(f'P/E {pe:.1f} → +0.3')
    else:
        score -= 0.4
        reasons.append(f'P/E {pe:.1f} → −0.4')

    pb = row.get('pb')
    if pb:
        if 0 < pb <= 3:
            score += 0.5
            reasons.append(f'P/B {pb:.1f} → +0.5')
        else:
            score -= 0.3
            reasons.append(f'P/B {pb:.1f} → −0.3')

    if (row.get('listlevel') or 0) == 1:
        score += 0.5
        reasons.append('листинг 1 → +0.5')

    turnover = row.get('turnover') or 0
    liq = _scale(math.log10(max(turnover, 1)) - 6, 0, 2.5) * 0.8
    score += liq
    reasons.append(f'оборот {turnover/1e6:.0f} млн → +{liq:.1f}')

    score = round(max(1.0, min(10.0, score)), 1)
    letter, text = _reliability_label(score)
    return {'score': score, 'letter': letter, 'text': text, 'reasons': reasons}


def _reason_bond(row: dict) -> str:
    parts = [f"YTM {row['ytm']:.1f}%"]
    if row.get('duration'):
        parts.append(f"дюрация {row['duration']:.1f}")
    parts.append(f"купон {row['coupon_pct']:.1f}%" if row.get('coupon_pct') else 'без купона')
    return ' · '.join(parts)


def _reason_stock(row: dict) -> str:
    parts = []
    if row.get('div12') is not None:
        parts.append(f"ДД {row['div12']:.1f}%")
    if row.get('pe'):
        parts.append(f"P/E {row['pe']:.1f}")
    if row.get('roe'):
        parts.append(f"ROE {row['roe']:.0f}%")
    return ' · '.join(parts) or 'нет данных по дивидендам'


def mark_candidates(bonds: list[dict], stocks: list[dict],
                    fx_bonds: list[dict] | None = None) -> None:
    """Проставить score/кандидатов спискам (мутирует на месте)."""
    if fx_bonds:
        add_risk_flags(fx_bonds)
        for row in fx_bonds:
            row['score'] = None
            row['candidate'] = False
            row['reason'] = _reason_bond(row)
            row['reliability'] = reliability_bond(row)
    add_risk_flags(bonds)
    for row in bonds:
        row['score'] = _score_bond(row)
        row['reason'] = _reason_bond(row)
        row['reliability'] = reliability_bond(row)
    for row in stocks:
        row['flags'] = _stock_flags(row)
        row['score'] = _score_stock(row)
        row['reason'] = _reason_stock(row)
        row['reliability'] = reliability_stock(row)
    for rows in (bonds, stocks):
        rows.sort(key=lambda r: -(r['score'] or 0))
        for i, row in enumerate(rows):
            row['candidate'] = i < CANDIDATES


# ── кэш ─────────────────────────────────────────────────────

def _load(kind: str) -> tuple[list[dict], str, str]:
    try:
        conn = get_connection()
        row = conn.execute('SELECT payload, source, updated_at FROM instrument_screen WHERE kind=?',
                           (kind,)).fetchone()
        conn.close()
    except Exception:  # noqa: BLE001
        return [], '', ''
    if not row:
        return [], '', ''
    try:
        return json.loads(row['payload'] or '[]'), row['source'] or '', row['updated_at'] or ''
    except json.JSONDecodeError:
        return [], '', ''


def _save(kind: str, rows: list[dict], source: str, updated_at: str) -> None:
    try:
        conn = get_connection()
        conn.execute(
            'INSERT OR REPLACE INTO instrument_screen (kind, payload, source, updated_at) '
            'VALUES (?, ?, ?, ?)',
            (kind, json.dumps(rows, ensure_ascii=False), source, updated_at))
        conn.commit()
        conn.close()
    except Exception:  # noqa: BLE001
        pass


def refresh(force: bool = False) -> None:
    """Пересобрать оба снимка и записать в кэш."""
    global _last_attempt
    with _lock:
        _last_attempt = time.monotonic()
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        bonds, fx_bonds, bond_errors = _bonds()
        mark_candidates(bonds, [], fx_bonds)
        _save('bond', bonds, '; '.join(bond_errors), now)
        _save('bond_fx', fx_bonds, '; '.join(bond_errors), now)

        stocks, stock_errors = _stocks()
        mark_candidates([], stocks)
        _save('stock', stocks, '; '.join(stock_errors), now)


def _stale(updated_at: str) -> bool:
    if not updated_at:
        return True
    try:
        age = datetime.now() - datetime.strptime(updated_at, '%Y-%m-%d %H:%M:%S')
    except ValueError:
        return True
    return age.total_seconds() > TTL


def _refresh_async() -> None:
    def run():
        try:
            refresh()
        except Exception:  # noqa: BLE001 — фон не должен ронять приложение
            pass

    threading.Thread(target=run, name='instrument-screen', daemon=True).start()


def build() -> dict:
    """Снимок для страницы: сначала кэш, при устаревании — фоновое обновление."""
    bonds, bond_src, bond_at = _load('bond')
    fx_bonds, _, _ = _load('bond_fx')
    stocks, stock_src, stock_at = _load('stock')

    updated = max(filter(None, (bond_at, stock_at)), default='')
    need_refresh = (not bonds and not stocks) or _stale(updated)
    if need_refresh:
        fresh_enough = (time.monotonic() - _last_attempt) < _RETRY_PAUSE
        if not fresh_enough:
            if not bonds and not stocks:
                # Первый заход: показывать нечего — собираем синхронно.
                try:
                    refresh()
                except Exception:  # noqa: BLE001
                    pass
                bonds, bond_src, bond_at = _load('bond')
                fx_bonds, _, _ = _load('bond_fx')
                stocks, stock_src, stock_at = _load('stock')
                updated = max(filter(None, (bond_at, stock_at)), default='')
            else:
                _refresh_async()

    mark_candidates(bonds, stocks, fx_bonds)   # пересчёт score на актуальных данных
    return {
        'bonds': bonds,
        'fx_bonds': fx_bonds,
        'stocks': stocks,
        'updated_at': updated,
        'as_of': date.today().isoformat(),
        'errors': [e for e in (bond_src, stock_src) if e],
        'candidates': CANDIDATES,
        'bond_min_turnover': BOND_MIN_TURNOVER,
        'stock_min_turnover': STOCK_MIN_TURNOVER,
    }


def start_background_refresh(interval: float = TTL) -> None:
    """Фоновое обновление снимка (аналог signals_cache)."""
    global _refresh_started
    if _refresh_started:
        return
    _refresh_started = True

    def loop():
        time.sleep(5)
        while True:
            try:
                if _stale(_load('bond')[2]) or _stale(_load('stock')[2]):
                    refresh()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(max(interval, 300))

    threading.Thread(target=loop, daemon=True, name='instrument-screen-refresh').start()
