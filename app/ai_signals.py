"""AI-сигналы по выбранному списку инструментов.

Пользователь задаёт список бумаг («мой вотчлист»), нажимает кнопку — модель
проходит по списку и по каждой бумаге решает, есть ли сигнал на покупку.
Главное требование: сигнал должен опираться на факты из данных, а не на
«настроение» модели. Поэтому в промпт уходит рыночный контекст:

* цена, доходность за 12 мес, положение в годовом диапазоне (акции);
* YTM, дюрация, спред к ОФЗ, объявленные выплаты (облигации);
* наши правила: акции — только в риск-бюджете, длинные ОФЗ — ставка на ставку,
  цель 30% не прогнозируется.

Генерация идёт в фоне (модель отвечает 20–50 с на бумагу), результат
кэшируется в ``ai_signals`` и показывается на странице.
"""

from __future__ import annotations

import json
import math
import threading
import time
import urllib.request
from datetime import datetime, date, timedelta

from app.advisor import llm_config, STRATEGY_TEXT
from app.bond_ladder import _get_json, _table, _num
from app.db import get_connection

TTL = 6 * 3600             # сигналы живут 6 часов
CONTEXT_TTL = 3600         # рыночный контекст — час
MAX_ITEMS = 20             # с запасом: открытые позиции + ручной список

_state = {'running': False, 'started': 0.0, 'done': 0, 'total': 0,
          'errors': [], 'finished': 0.0}
_context_cache: dict[str, tuple[float, dict]] = {}


# ── вотчлист ────────────────────────────────────────────────

def items() -> list[dict]:
    try:
        conn = get_connection()
        rows = conn.execute(
            'SELECT code, kind, name, added_at FROM ai_watchlist ORDER BY added_at').fetchall()
        conn.close()
    except Exception:  # noqa: BLE001
        return []
    return [dict(r) for r in rows]


def _resolve(code: str, snapshot: dict) -> tuple[dict, str] | None:
    """Найти бумагу в снимке скринера по коду из позиций.

    Позиции приходят то биржевым кодом (SU26243RMFS4), то ISIN, то кодом QUIK
    (26254RMFS). Прямого совпадения может не быть — тогда ищем ISIN через
    справочник bond_ladder (он уже кэширован и при промахе обновляется с ISS).
    """
    found = _find(snapshot, code)
    if found:
        return found
    try:
        from app import bond_ladder
        refs = bond_ladder._load_cache()
        ref = bond_ladder._ref_for(code, refs)
        if not ref:
            bond_ladder.refresh([code])
            ref = bond_ladder._ref_for(code, bond_ladder._load_cache())
        isin = (ref or {}).get('isin')
        if isin:
            found = _find(snapshot, isin)
            if found:
                return found
    except Exception:  # noqa: BLE001
        pass
    # Последний рубеж — сам ISS: так работают фьючерсы, фонды и всё, чего нет
    # в скринере. Сигналы не должны зависеть от фильтров скринера.
    return _iss_facts(code)


def _iss_facts(code: str) -> tuple[dict, str] | None:
    """Любой инструмент с MOEX ISS — независимо от скринера.

    Скринер намеренно узкий (ликвидные акции и облигации), но сигналы нужны и
    по фьючерсам, фондам и прочим позициям. Здесь берём паспорт бумаги и
    рыночные данные с нужного рынка ISS.
    """
    key = (code or '').strip().upper()
    if not key:
        return None
    doc = _get_json('/securities/%s.json' % key)
    desc = {r.get('name'): r.get('value') for r in _table(doc, 'description')}
    if not desc:
        # Код может быть торговым псевдонимом (RURMARG, LQDT): ищем по строке.
        found = _table(_get_json('/securities.json', {'q': key}), 'securities')
        exact = [r for r in found
                 if key in (str(r.get('secid') or '').upper(),
                            str(r.get('isin') or '').upper(),
                            str(r.get('regnumber') or '').upper())]
        if not exact:
            return None
        secid = exact[0].get('secid') or key
        doc = _get_json('/securities/%s.json' % secid)
        desc = {r.get('name'): r.get('value') for r in _table(doc, 'description')}
        if not desc:
            return None
        desc.setdefault('SHORTNAME', exact[0].get('shortname') or key)
        desc.setdefault('TYPE', exact[0].get('type') or '')
        desc.setdefault('GROUP', exact[0].get('group') or '')
    sec_type = str(desc.get('TYPE') or '').lower()
    group = str(desc.get('GROUP') or '')
    name = desc.get('SHORTNAME') or desc.get('SECNAME') or key

    if 'futures' in sec_type or 'forts' in group:
        md_doc = _get_json('/engines/futures/markets/forts/securities/%s.json' % key)
        md = (_table(md_doc, 'marketdata') or [{}])[0]
        price = _num(md.get('LAST')) or _num(md.get('SETTLEPRICE'))
        expiry = ''
        for field in ('LASTTRADEDATE', 'LASTDELIVERYDATE', 'EXPIRATIONDATE'):
            value = str(desc.get(field) or '')
            if value and value != '0000-00-00':
                expiry = value
                break
        if not expiry:
            # у срочного рынка дата часто только в имени: «MTSI-12.26»
            suffix = str(name).split('-')[-1]
            if '.' in suffix:
                month, year = suffix.split('.')[:2]
                expiry = f'20{year}-{int(month):02d} (из названия контракта)'
        return {
            'code': key,
            'name': name,
            'kind': 'future',
            'type': 'фьючерс',
            'asset_code': desc.get('ASSETCODE') or '',
            'expiry': expiry,
            'price': price,
            'settle': _num(md.get('SETTLEPRICE')),
            'turnover': _num(md.get('VALTODAY')),
            'trades': _num(md.get('NUMTRADES')),
            'open_position': _num(md.get('OPENPOSITION')),
            'lot': _num(desc.get('LOTVOLUME')),
        }, 'future'

    if 'bond' in sec_type or 'bond' in group:
        price_pct = _num(desc.get('PREVPRICE'))
        return {
            'code': key,
            'name': name,
            'type': 'облигация',
            'currency': 'RUB' if str(desc.get('FACEUNIT') or 'SUR').upper() in ('SUR', 'RUB', '') else desc.get('FACEUNIT'),
            'matdate': desc.get('MATDATE') or '',
            'coupon_pct': _num(desc.get('COUPONPERCENT')),
            'price': price_pct,
        }, ('fx' if str(desc.get('FACEUNIT') or 'SUR').upper() not in ('SUR', 'RUB', '') else 'bond')

    if 'share' in sec_type or 'stock' in group or 'etf' in group or 'fund' in sec_type:
        md_doc = _get_json('/engines/stock/markets/shares/securities/%s.json' % key)
        md = (_table(md_doc, 'marketdata') or [{}])[0]
        return {
            'code': key,
            'name': name,
            'type': 'акция/фонд',
            'price': _num(md.get('LAST')) or _num(md.get('LCURRENTPRICE')),
            'turnover': _num(md.get('VALTODAY')),
            'trades': _num(md.get('NUMTRADES')),
        }, 'stock'
    return None


def _fallback_row(code: str) -> tuple[dict, str] | None:
    """Факты для бумаги, которой нет в скринере (неликвидна или вне досок).

    Открытая позиция важнее ликвидности: если бумаги нет в снимке скринера,
    берём её из лестницы (bond_ladder), где есть YTM, дюрация и купон.
    """
    try:
        from app import bond_ladder
        data = bond_ladder.build()
    except Exception:  # noqa: BLE001
        return None
    key = (code or '').upper()
    for row in data.get('rows') or []:
        if key not in ((row.get('code') or '').upper(), (row.get('isin') or '').upper()):
            continue
        currency = row.get('currency') or 'RUB'
        return {
            'code': row.get('code') or key,
            'name': row.get('name') or key,
            'ytm': row.get('ytm'),
            'duration': row.get('duration'),
            'matdate': row.get('matdate'),
            'years': row.get('years'),
            'coupon_pct': row.get('coupon_pct'),
            'coupon_freq': row.get('coupon_freq'),
            'price': row.get('price_pct'),
            'value': row.get('value'),
            'currency': currency,
            'type': row.get('kind'),
        }, ('fx' if currency != 'RUB' else 'bond')
    return None


def position_items(snapshot: dict | None = None) -> list[dict]:
    """Открытые позиции портфеля — источник сигналов по умолчанию.

    Для каждой позиции тянем факт владения (количество, средняя цена, вложено):
    без него модель не может отличить «купить» от «докупать/сокращать».
    Фьючерсы и фонды в скринер не попадают и в список не включаются.
    """
    from app.db import get_instrument_summary
    snapshot = snapshot or _snapshot()
    try:
        positions = get_instrument_summary(None, '', '', 'all') or []
    except Exception:  # noqa: BLE001
        return []

    out: list[dict] = []
    by_key: dict[str, int] = {}
    for pos in positions:
        code = (pos.get('security_code') or '').strip().upper()
        if not code:
            continue
        found = _resolve(code, snapshot) or _fallback_row(code)
        if not found:
            continue
        row, kind = found
        key = (row.get('code') or code).upper()
        # Одна бумага может лежать двумя кодами (26254RMFS и RU000A10D533) —
        # это одна позиция, количества складываем, иначе размер занижен.
        if key in by_key:
            merged = out[by_key[key]]['position']
            qty = (merged.get('qty') or 0) + (pos.get('qty') or 0)
            cost = (merged.get('cost') or 0) + (pos.get('total_cost') or 0)
            merged['qty'] = qty
            merged['cost'] = cost
            merged['avg_price'] = round(cost / qty, 2) if qty else merged.get('avg_price')
            continue
        out.append({
            'code': row.get('code') or code,
            'kind': kind,
            'name': row.get('name') or pos.get('security_name') or '',
            'source': 'позиция',
            'row': row,
            'position': {
                'qty': pos.get('qty'),
                'avg_price': pos.get('avg_price'),
                'cost': pos.get('total_cost'),
            },
        })
    return out


def effective_items(snapshot: dict | None = None) -> list[dict]:
    """Что обрабатываем: открытые позиции + ручной список (без дублей)."""
    snapshot = snapshot or _snapshot()
    items_out: list[dict] = []
    seen: set[str] = set()
    for item in position_items(snapshot):
        seen.add(item['code'].upper())
        items_out.append(item)
    for item in items():
        code = (item.get('code') or '').upper()
        if code in seen:
            continue
        seen.add(code)
        found = _resolve(code, snapshot) or _fallback_row(code)
        if not found:
            continue
        row, kind = found
        items_out.append({**item, 'code': row.get('code') or code, 'kind': kind,
                          'row': row,
                          'name': row.get('name') or item.get('name') or '',
                          'source': 'список'})
    return items_out[:MAX_ITEMS]


def add_item(code: str, kind: str = 'auto') -> tuple[bool, str]:
    code = (code or '').strip().upper()
    if not code:
        return False, 'пустой код'
    if len(items()) >= MAX_ITEMS:
        return False, f'в списке уже {MAX_ITEMS} бумаг — это предел'
    snapshot = _snapshot()
    found = _find(snapshot, code) or _resolve(code, snapshot)
    if not found:
        return False, f'«{code}» не найден ни в скринере, ни в справочнике MOEX ISS'
    row, real_kind = found
    try:
        conn = get_connection()
        conn.execute('INSERT OR REPLACE INTO ai_watchlist (code, kind, name, added_at) '
                     'VALUES (?, ?, ?, ?)',
                     (row.get('code'), real_kind, row.get('name') or '',
                      datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()
        conn.close()
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    return True, f'{row.get("name") or code} добавлен в список'


def remove_item(code: str) -> None:
    try:
        conn = get_connection()
        conn.execute('DELETE FROM ai_watchlist WHERE code=?', ((code or '').upper(),))
        conn.execute('DELETE FROM ai_signals WHERE code=?', ((code or '').upper(),))
        conn.commit()
        conn.close()
    except Exception:  # noqa: BLE001
        pass


# ── рыночный контекст ───────────────────────────────────────

def _snapshot() -> dict:
    from app import instruments
    try:
        return instruments.build()
    except Exception:  # noqa: BLE001
        return {'bonds': [], 'fx_bonds': [], 'stocks': []}


def _find(snapshot: dict, code: str) -> tuple[dict, str] | None:
    key = code.upper()
    buckets = (('bond', 'bonds'), ('fx', 'fx_bonds'), ('stock', 'stocks'))
    for kind, bucket in buckets:
        for row in snapshot.get(bucket) or []:
            if (row.get('code') or '').upper() == key:
                return row, kind
    # Код QUIK и код ISS не совпадают: «26254RMFS» против «SU26254RMFS1».
    # Сравниваем по номеру выпуска — это надёжно для ОФЗ и не путает бумаги.
    digits = ''.join(ch for ch in key if ch.isdigit())
    if len(digits) >= 4:
        for kind, bucket in buckets:
            for row in snapshot.get(bucket) or []:
                row_code = (row.get('code') or '').upper()
                if digits in row_code:
                    return row, kind
    return None


_HISTORY_MARKETS = (
    '/history/engines/stock/markets/shares/securities/%s.json',
    '/history/engines/futures/markets/forts/securities/%s.json',
)

_PRICE_BOARDS = (
    ('/engines/stock/markets/shares/boards/TQBR/securities/%s.json',),
    ('/engines/stock/markets/shares/boards/TQTF/securities/%s.json',),
    ('/engines/stock/markets/shares/boards/TQIF/securities/%s.json',),
    ('/engines/stock/markets/bonds/boards/TQOB/securities/%s.json',),
    ('/engines/stock/markets/bonds/boards/TQCB/securities/%s.json',),
    ('/engines/futures/markets/forts/securities/%s.json',),
)


def current_price(code: str) -> float | None:
    """Актуальная цена с MOEX ISS (marketdata), а не из истории.

    История ISS отдаётся с 2011 года, если не передать ``from``, и «последняя»
    строка такой выборки — цена давно прошедшего дня. Поэтому текущую цену
    берём из marketdata нужной доски, а историю используем только для
    диапазона и волатильности.
    """
    key = (code or '').upper()
    cached = _context_cache.get('price:' + key)
    if cached and time.time() - cached[0] < 600:
        return cached[1]
    price = None
    for (template,) in _PRICE_BOARDS:
        doc = _get_json(template % key)
        rows = _table(doc, 'marketdata')
        if not rows:
            continue
        row = rows[0]
        price = (_num(row.get('LAST')) or _num(row.get('LCURRENTPRICE'))
                 or _num(row.get('SETTLEPRICE')) or _num(row.get('MARKETPRICE')))
        if price:
            break
    if price:
        _context_cache['price:' + key] = (time.time(), price)
    return price


def _history(secid: str, days: int = 400) -> list[tuple[str, float]]:
    """Дневные закрытия за последний год (окно задаём через ``from``).

    Параметр ``start`` листает с самого начала истории (2011 год), поэтому
    без ``from`` выборка получается «древней». Фильтруем по дате ещё раз.
    """
    cached = _context_cache.get('hist:' + secid)
    if cached and time.time() - cached[0] < CONTEXT_TTL:
        return cached[1]
    frm = (date.today() - timedelta(days=days)).isoformat()
    series: list[tuple[str, float]] = []
    for template in _HISTORY_MARKETS:
        collected: list[tuple[str, float]] = []
        start = 0
        for _ in range(6):
            doc = _get_json(template % secid,
                            {'history.columns': 'TRADEDATE,CLOSE',
                             'from': frm, 'start': start})
            rows = _table(doc, 'history')
            if not rows:
                break
            for r in rows:
                value = _num(r.get('CLOSE'))
                day = str(r.get('TRADEDATE') or '')
                if value and day and day >= frm:
                    collected.append((day, value))
            start += len(rows)
            if len(rows) < 100:
                break
        if collected:
            series = collected
            break
    series.sort()
    _context_cache['hist:' + secid] = (time.time(), series)
    return series


def market_context(row: dict, kind: str) -> dict:
    """Что рынок говорит о бумаге прямо сейчас (компактно, для промпта).

    Текущая цена — из marketdata ISS; история — только для диапазона,
    динамики за 12 мес и волатильности.
    """
    ctx = {'today': date.today().isoformat()}
    if kind in ('stock', 'future'):
        code = row.get('code') or ''
        series = _history(code)
        closes = [v for _, v in series]
        # Текущая цена — из marketdata; история нужна для диапазона.
        last = current_price(code) or row.get('price') or (closes[-1] if closes else None)
        if closes and last and (closes[-1] / last > 2 or last / closes[-1] > 2):
            # история явно из другого периода — не используем её вовсе
            series, closes = [], []
        if closes:
            year = closes[-250:] if len(closes) > 250 else closes
            hi, lo = max(year), min(year)
            ctx.update({
                'last_price': round(last, 2) if last else None,
                'year_high': round(hi, 2),
                'year_low': round(lo, 2),
                'from_high_pct': round((last / hi - 1) * 100, 1) if last and hi else None,
                'from_low_pct': round((last / lo - 1) * 100, 1) if last and lo else None,
                'change_12m_pct': (round((last / closes[0] - 1) * 100, 1)
                                   if last and closes and len(closes) > 60 else None),
                'volatility_pct': (round(_stdev(
                    [closes[i] / closes[i - 1] - 1 for i in range(1, len(year))])
                    * math.sqrt(252) * 100, 1) if len(year) > 20 else None),
            })
        elif last:
            ctx['last_price'] = round(last, 2)
    return ctx


def _stdev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))


# ── сигнал по одной бумаге ──────────────────────────────────

PROMPT = """Ты — риск-менеджер частного портфеля. По одной бумаге реши: есть ли
СИГНАЛ НА ПОКУПКУ прямо сейчас, с учётом фактов и рыночного контекста.

Правила:
- Сигнал «покупать» только если факты его поддерживают: доходность/оценка/положение
  в диапазоне/устойчивость выплат. Если данных мало или картина смешанная — «ждать».
  Если бумага противоречит стратегии — «не покупать».
- Не выдумывай цены и уровни: вход/цель/стоп считай от last_price или цены из фактов.
  Если не можешь обосновать цель — оставь пустую строку, а не придумывай.
- Дивдоходность по последней выплате может быть разовой: смотри объявленные выплаты
  и историю, а не только цифру за 12 мес.
- Для облигаций: высокая премия к ОФЗ — это кредитный риск, а не «выгодно».
- Если kind = "фьючерс": это срочный контракт, а не покупка актива. Обязательно учти:
  дату экспирации (в фактах expiry; если нет — скажи, что неизвестна), гарантийное
  обеспечение (плечо), базовый актив (asset_code) и открытый интерес. «Покупать» значит
  занять длинную позицию по контракту; перенос на следующий контракт — отдельное решение.
  Не переноси купонные и дивидендные аргументы на фьючерс.

СТРАТЕГИЯ:
""" + STRATEGY_TEXT + """

ФАКТЫ (JSON):
{facts}

Верни СТРОГО JSON:
{{"signal": "покупать"|"ждать"|"не покупать",
  "confidence": 0-100,
  "entry": "условие входа, например «до 170 ₽» или пустая строка",
  "target": "цель или пустая строка",
  "stop": "где ошибка отменяет идею, или пустая строка",
  "horizon": "например «6–12 мес»",
  "why": ["2–3 причины с цифрами"],
  "risks": ["1–3 риска"]}}
Без markdown и текста вне JSON."""


def _call(item: dict, facts: dict) -> dict | None:
    cfg = llm_config()
    if not cfg:
        return None
    body = {'model': cfg['model'],
            'messages': [{'role': 'user',
                          'content': PROMPT.format(
                              facts=json.dumps(facts, ensure_ascii=False, indent=1))}],
            'temperature': 0.15, 'max_tokens': cfg['max_tokens']}
    try:
        req = urllib.request.Request(cfg['url'], data=json.dumps(body).encode('utf-8'),
                                     headers=cfg['headers'], method='POST')
        with urllib.request.urlopen(req, timeout=cfg['timeout']) as resp:
            doc = json.loads(resp.read().decode('utf-8'))
        message = doc['choices'][0]['message']
        text = message.get('content') or message.get('reasoning_content') or ''
    except Exception:  # noqa: BLE001
        return None
    data = _parse(text)
    if data:
        data['model'] = cfg['model']
    return data


def _parse(text: str) -> dict | None:
    body = (text or '').strip()
    if body.startswith('```'):
        body = body[3:].split('\n', 1)[-1].rsplit('```', 1)[0]
    start, end = body.find('{'), body.rfind('}')
    if start < 0 or end <= start:
        return None
    try:
        doc = json.loads(body[start:end + 1])
    except json.JSONDecodeError:
        return None
    signal = str(doc.get('signal') or '').strip().lower()
    if signal not in ('покупать', 'ждать', 'не покупать'):
        return None
    def lst(key: str) -> list[str]:
        value = doc.get(key) or []
        return [str(x)[:300] for x in value][:4] if isinstance(value, list) else []
    try:
        confidence = int(float(doc.get('confidence') or 0))
    except (TypeError, ValueError):
        confidence = 0
    return {'signal': signal, 'confidence': max(0, min(100, confidence)),
            'entry': str(doc.get('entry') or '')[:120],
            'target': str(doc.get('target') or '')[:120],
            'stop': str(doc.get('stop') or '')[:120],
            'horizon': str(doc.get('horizon') or '')[:60],
            'why': lst('why'), 'risks': lst('risks')}


# ── генерация ───────────────────────────────────────────────

def _save(code: str, payload: dict) -> None:
    try:
        conn = get_connection()
        conn.execute('INSERT OR REPLACE INTO ai_signals (code, payload, updated_at) '
                     'VALUES (?, ?, ?)',
                     (code, json.dumps(payload, ensure_ascii=False),
                      datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()
        conn.close()
    except Exception:  # noqa: BLE001
        pass


def _worker(watch: list[dict], snapshot: dict) -> None:
    _state.update({'running': True, 'started': time.time(), 'done': 0,
                   'total': len(watch), 'errors': []})
    for item in watch:
        found = (item.get('row'), item.get('kind')) if item.get('row') else _find(snapshot, item['code'])
        if not found or not found[0]:
            _state['errors'].append(f"{item['code']}: нет данных")
            _state['done'] += 1
            continue
        row, kind = found
        facts = {k: v for k, v in row.items() if v not in (None, '', [])}
        facts['kind'] = ('облигация' if kind in ('bond', 'fx')
                         else 'фьючерс' if kind == 'future' else 'акция')
        facts['source'] = ('скринер' if kind in ('bond', 'fx', 'stock') and row.get('score') is not None
                           else 'MOEX ISS')
        if item.get('position'):
            facts['position'] = item['position']   # уже в портфеле: qty/средняя/вложено
        try:
            facts['market'] = market_context(row, kind)
        except Exception:  # noqa: BLE001
            pass
        if kind == 'future':
            facts['instrument_type'] = 'фьючерс'
            asset = row.get('asset_code') or ''
            if asset:
                facts['underlying'] = asset
                try:
                    sub = market_context({'code': asset}, 'stock')
                    if sub.get('last_price'):
                        facts['underlying_market'] = sub
                except Exception:  # noqa: BLE001
                    pass
        result = _call(item, facts)
        if result:
            result.update({'code': item['code'], 'name': item.get('name') or row.get('name'),
                           'kind': kind,
                           'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')})
            _save(item['code'], result)
        else:
            _state['errors'].append(f"{item['code']}: модель не ответила")
        _state['done'] += 1
    _state.update({'running': False, 'finished': time.time()})


def generate_async(codes: list[str] | None = None) -> tuple[bool, str]:
    """Запустить генерацию в фоне.

    ``codes`` — какие бумаги считать (галочки на странице). Без него берём всё:
    открытые позиции плюс ручной список.
    """
    if _state['running']:
        return False, 'генерация уже идёт'
    snapshot = _snapshot()
    watch = effective_items(snapshot)
    if codes:
        wanted = {c.strip().upper() for c in codes if c and c.strip()}
        watch = [w for w in watch if w['code'].upper() in wanted]
    if not watch:
        return False, 'нечего обрабатывать: нет ни открытых позиций, ни бумаг в списке'
    if not llm_config():
        return False, 'нейросеть не настроена'
    threading.Thread(target=_worker, args=(watch, snapshot), name='ai-signals',
                     daemon=True).start()
    return True, f'генерация запущена: {len(watch)} бумаг(и)'


def cached() -> list[dict]:
    """Сохранённые сигналы для бумаг из вотчлиста (свежие сверху)."""
    watch = {i['code'].upper(): i for i in effective_items()}
    try:
        conn = get_connection()
        rows = conn.execute('SELECT code, payload, updated_at FROM ai_signals').fetchall()
        conn.close()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for row in rows:
        if row['code'] not in watch:
            continue
        try:
            data = json.loads(row['payload'] or '{}')
        except json.JSONDecodeError:
            continue
        data['code'] = row['code']
        data['name'] = watch[row['code']].get('name') or data.get('name') or ''
        data['updated_at'] = row['updated_at']
        data['kind'] = watch[row['code']].get('kind') or data.get('kind')
        out.append(data)
    order = {'покупать': 0, 'ждать': 1, 'не покупать': 2}
    out.sort(key=lambda r: (order.get(r.get('signal'), 3), -(r.get('confidence') or 0)))
    return out


def status() -> dict:
    return dict(_state,
                positions=len(position_items()),
                manual=len(items()),
                llm_enabled=bool(llm_config()),
                model=(llm_config() or {}).get('model', ''),
                ttl_hours=TTL // 3600)
