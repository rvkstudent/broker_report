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
MAX_ITEMS = 12             # больше за раз не гоняем: модель дорогая и медленная

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


def add_item(code: str, kind: str = 'auto') -> tuple[bool, str]:
    code = (code or '').strip().upper()
    if not code:
        return False, 'пустой код'
    if len(items()) >= MAX_ITEMS:
        return False, f'в списке уже {MAX_ITEMS} бумаг — это предел'
    snapshot = _snapshot()
    found = _find(snapshot, code)
    if not found:
        return False, f'«{code}» не найден среди ликвидных бумаг скринера'
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
    for kind, bucket in (('bond', 'bonds'), ('fx', 'fx_bonds'), ('stock', 'stocks')):
        for row in snapshot.get(bucket) or []:
            if (row.get('code') or '').upper() == key:
                return row, kind
    return None


def _history(secid: str, days: int = 400) -> list[tuple[str, float]]:
    """Дневные закрытия с ISS (для диапазона и динамики)."""
    cached = _context_cache.get('hist:' + secid)
    if cached and time.time() - cached[0] < CONTEXT_TTL:
        return cached[1]
    frm = (date.today() - timedelta(days=days)).isoformat()
    doc = _get_json('/history/engines/stock/markets/shares/securities/%s.json' % secid,
                    {'history.columns': 'TRADEDATE,CLOSE', 'from': frm})
    rows = [(_num(r.get('CLOSE')), str(r.get('TRADEDATE') or ''))
            for r in _table(doc, 'history')]
    series = [(d, v) for v, d in rows if v and d]
    series.sort()
    _context_cache['hist:' + secid] = (time.time(), series)
    return series


def market_context(row: dict, kind: str) -> dict:
    """Что рынок говорит о бумаге прямо сейчас (компактно, для промпта)."""
    ctx = {'today': date.today().isoformat()}
    if kind == 'stock':
        series = _history(row.get('code') or '')
        if series:
            closes = [v for _, v in series]
            last = closes[-1]
            year = closes[-250:] if len(closes) > 250 else closes
            hi, lo = max(year), min(year)
            ctx.update({
                'last_price': round(last, 2),
                'year_high': round(hi, 2),
                'year_low': round(lo, 2),
                'from_high_pct': round((last / hi - 1) * 100, 1) if hi else None,
                'from_low_pct': round((last / lo - 1) * 100, 1) if lo else None,
                'change_12m_pct': (round((last / closes[-250] - 1) * 100, 1)
                                   if len(closes) > 250 else None),
                'volatility_pct': (round(_stdev(
                    [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes[-250:]))])
                    * math.sqrt(252) * 100, 1) if len(closes) > 20 else None),
            })
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
        found = _find(snapshot, item['code'])
        if not found:
            _state['errors'].append(f"{item['code']}: нет в снимке")
            _state['done'] += 1
            continue
        row, kind = found
        facts = {k: v for k, v in row.items() if v not in (None, '', [])}
        facts['kind'] = 'облигация' if kind in ('bond', 'fx') else 'акция'
        try:
            facts['market'] = market_context(row, kind)
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


def generate_async() -> tuple[bool, str]:
    """Запустить генерацию в фоне. Возвращает (запущено, сообщение)."""
    if _state['running']:
        return False, 'генерация уже идёт'
    watch = items()
    if not watch:
        return False, 'список пуст — добавьте бумаги'
    if not llm_config():
        return False, 'нейросеть не настроена'
    snapshot = _snapshot()
    threading.Thread(target=_worker, args=(watch, snapshot), name='ai-signals',
                     daemon=True).start()
    return True, f'генерация запущена: {len(watch)} бумаг(и)'


def cached() -> list[dict]:
    """Сохранённые сигналы для бумаг из вотчлиста (свежие сверху)."""
    watch = {i['code']: i for i in items()}
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
                llm_enabled=bool(llm_config()),
                model=(llm_config() or {}).get('model', ''),
                ttl_hours=TTL // 3600)
