"""AI-разбор рынка по тикеру: направление, крупные игроки, точки входа, уровни.

Кнопка на странице графика. В модель уходят не «картинки», а посчитанные
факты из наших же модулей:

* **горизонтальные объёмы** — профиль по цене (`levels.analyze`): зоны,
  поддержка/сопротивление, перевес покупок/продаж на них, крупные принты;
* **ATR и волатильность** — `volatility.metrics` (ATR в рублях и %, дневная σ,
  фактический размах);
* **всплески объёмов** — крупные сделки (`chartdata.markers`) и дельта
  последних 30 минут (`levels` → `delta`);
* уровни за **3 дня** и **месяц** — отдельные прогоны `levels.analyze` с
  `history_days`, чтобы горизонт не зависел от выбранного на графике периода.

Модель обязана отвечать строго JSON и не выдумывать цены: вход/стоп/цели —
из уровней, которые посчитаны, а не из воздуха.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from datetime import date, datetime

from app.advisor import llm_config, STRATEGY_TEXT
from app.db import get_connection

TTL = 30 * 60              # получаса достаточно: данные внутридневные
_running: dict[str, float] = {}
_failed: dict[str, str] = {}
_lock = threading.Lock()


# ── сбор фактов ─────────────────────────────────────────────

def _levels_block(ticker: str, range_key: str, history_days: int) -> dict | None:
    from app import chartdata, levels
    try:
        from_ns, to_ns, _ = chartdata.resolve_range(range_key)
        lv = levels.analyze(ticker, from_ns, to_ns, history_days=history_days)
    except Exception:  # noqa: BLE001
        return None
    out = {
        'price': lv.get('price'),
        'verdict': lv.get('verdict'),
        'support': lv.get('support'),
        'resistance': lv.get('resistance'),
        'delta': lv.get('delta'),
        'zones': [
            {'price': z.get('price'), 'buy_share': z.get('buy_share'),
             'flow': z.get('flow'), 'large_rub': z.get('large_rub')}
            for z in (lv.get('zones') or [])[:12]
        ],
    }
    return out


def _volatility_block(ticker: str) -> dict | None:
    from app import chartdata, volatility
    try:
        from_ns, to_ns, _ = chartdata.resolve_range('today')
        v = volatility.metrics(ticker, from_ns, to_ns)
    except Exception:  # noqa: BLE001
        return None
    return {k: v.get(k) for k in ('bars', 'price', 'atr_rub', 'atr_pct', 'sigma_day_pct',
                                  'range_pct', 'range_rub', 'volume_rub')}


def _large_prints(ticker: str, limit: int = 8) -> list[dict]:
    from app import chartdata
    try:
        from_ns, to_ns, _ = chartdata.resolve_range('today')
        data = chartdata.markers(ticker, from_ns, to_ns, min_value=1_000_000)
    except Exception:  # noqa: BLE001
        return []
    rows = data.get('markers') if isinstance(data, dict) else data
    rows = rows or []
    out = []
    for row in rows[-limit:]:
        if not isinstance(row, dict):
            continue
        out.append({k: row.get(k) for k in
                    ('time', 'price', 'value', 'side', 'qty', 'text') if row.get(k) is not None})
    return out


def context(ticker: str) -> dict:
    """Все факты для модели одним компактным словарём."""
    ticker = (ticker or '').upper()
    return {
        'ticker': ticker,
        'today': date.today().isoformat(),
        'volatility': _volatility_block(ticker),
        'levels_3d': _levels_block(ticker, '3d', 3),
        'levels_30d': _levels_block(ticker, '30d', 30),
        'large_prints': _large_prints(ticker),
    }


PROMPT = """Ты — трейдер. Дай КОРОТКИЙ торговый план: одна сделка, конкретные цифры.

Главное — ответ на четыре вопроса:
1) куда: лонг / шорт / нейтрально (сейчас, интрадей);
2) по какой цене входить (одна цифра — из уровней в фактах);
3) где стоп и где цель (обе цифры);
4) среднесрочно (на месяц) — покупать / держать / продавать.

Правила:
- Цены бери ТОЛЬКО из фактов (support/resistance/zone price/verdict.target).
- Если уровни конфликтуют или данных мало — side = "нейтрально", но всё равно
  укажи, от какой цены смотреть вход и что должно подтвердить сделку.
- Почему — максимум 3 пункта, каждый до 120 символов, без сырых счётчиков
  (никаких hold_rate, «22 пробоя»): пиши вывод, а не статистику.
- Крупные игроки — одна фраза с цифрами (кто и где).
- ЦЕЛЬ — ЗАРАБОТАТЬ. Сделка имеет смысл только если ход до цели заметно больше
  издержек: комиссия ~0,05% на вход и выход, спред и проскальзывание ещё ~0,1%.
  Поэтому ожидаемый ход должен быть не меньше max(0,4%, 0,5×ATR дня) — иначе
  side = "нейтрально" и в why напиши «ход меньше издержек, торговать нечем».
  Цель бери не ближайший шумовой уровень, а следующий значимый: зону с крупным
  объёмом или месячный уровень. Соотношение цель/стоп должно быть ≥ 2:1.
- Не выдумывай цены, не обещай доходность.

СТРАТЕГИЯ (общий контекст, здесь речь о короткой сделке):
""" + STRATEGY_TEXT + """

ФАКТЫ (JSON):
{facts}

Верни СТРОГО JSON:
{{"side": "лонг"|"шорт"|"нейтрально",
  "confidence": 0-100,
  "entry": число|null,
  "stop": число|null,
  "target": число|null,
  "medium_term": "покупать"|"держать"|"продавать",
  "medium_term_why": "одна фраза",
  "big_players": "одна фраза с цифрами",
  "why": ["до 3 коротких пунктов"],
  "risks": ["до 2 коротких пунктов"]}}
Без markdown и текста вне JSON."""


def _call(ticker: str, facts: dict) -> dict | None:
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
    atr = (facts.get('volatility') or {}).get('atr_pct')
    data = _parse(text, atr_hint=atr)
    if data:
        data['model'] = cfg['model']
        data['ticker'] = ticker
    return data


def _num(value) -> float | None:
    try:
        number = float(str(value).replace(',', '.').replace(' ', ''))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _parse(text: str, atr_hint: float | None = None) -> dict | None:
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

    side = str(doc.get('side') or '').strip().lower()
    if side not in ('лонг', 'шорт', 'нейтрально'):
        return None
    entry = _num(doc.get('entry'))
    stop = _num(doc.get('stop'))
    target = _num(doc.get('target'))

    # Профит и риск считаем сами: это арифметика, модели тут доверять нечего.
    metrics: dict = {}
    if entry and target:
        move = (target / entry - 1) * 100
        if side == 'шорт':
            move = -move
        metrics['profit_pct'] = round(move, 2)
    if entry and stop:
        risk = (entry / stop - 1) * 100 if stop else None
        if side == 'шорт':
            risk = -risk if risk else risk
        metrics['risk_pct'] = round(abs(risk), 2) if risk is not None else None
    if metrics.get('profit_pct') and metrics.get('risk_pct'):
        metrics['rr'] = round(metrics['profit_pct'] / metrics['risk_pct'], 1)

    # Издержки: ~0,05% комиссия на вход и выход + ~0,1% спред/проскальзывание.
    COSTS_PCT = 0.2
    MIN_NET_PCT = 0.4
    atr_pct = atr_hint or 0.0
    min_move = max(MIN_NET_PCT, round(atr_pct * 0.5, 2)) if atr_pct else MIN_NET_PCT
    if metrics.get('profit_pct') is not None:
        metrics['costs_pct'] = COSTS_PCT
        metrics['net_profit_pct'] = round(metrics['profit_pct'] - COSTS_PCT, 2)
        metrics['min_move_pct'] = min_move
        tradeable = (side != 'нейтрально'
                     and metrics['net_profit_pct'] >= MIN_NET_PCT
                     and metrics['profit_pct'] >= min_move
                     and (metrics.get('rr') or 0) >= 2)
        metrics['tradeable'] = tradeable
        if not tradeable and side != 'нейтрально':
            # Сделка не окупает издержки — честнее сказать «нет сделки».
            side = 'нейтрально'
            doc['_downgraded'] = True

    def short_list(key: str, limit: int) -> list[str]:
        return [str(x)[:200] for x in (doc.get(key) or [])][:limit]

    try:
        confidence = int(float(doc.get('confidence') or 0))
    except (TypeError, ValueError):
        confidence = 0
    medium = str(doc.get('medium_term') or '').strip().lower()
    if medium not in ('покупать', 'держать', 'продавать'):
        medium = ''
    why = short_list('why', 3)
    if doc.get('_downgraded'):
        why = ['Ожидаемый ход меньше издержек — сделки нет'] + why[:2]
    return {
        'direction': side,
        'confidence': max(0, min(100, confidence)),
        'entry': entry,
        'stop': stop,
        'target': target,
        'metrics': metrics,
        'medium_term': medium,
        'medium_term_why': str(doc.get('medium_term_why') or '')[:200],
        'big_players': str(doc.get('big_players') or '')[:300],
        'summary': str(doc.get('why') and ' '.join(short_list('why', 3)) or '')[:600],
        'why': why,
        'risks': short_list('risks', 2),
    }


# ── кэш и фоновый запуск ────────────────────────────────────

def _load(ticker: str) -> dict | None:
    try:
        conn = get_connection()
        row = conn.execute('SELECT payload, updated_at FROM chart_ai WHERE ticker=?',
                           (ticker,)).fetchone()
        conn.close()
    except Exception:  # noqa: BLE001
        return None
    if not row:
        return None
    try:
        data = json.loads(row['payload'] or '{}')
    except json.JSONDecodeError:
        return None
    data['updated_at'] = row['updated_at'] or ''
    return data


def _save(ticker: str, data: dict) -> None:
    try:
        conn = get_connection()
        conn.execute('INSERT OR REPLACE INTO chart_ai (ticker, payload, updated_at) '
                     'VALUES (?, ?, ?)',
                     (ticker, json.dumps(data, ensure_ascii=False),
                      datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()
        conn.close()
    except Exception:  # noqa: BLE001
        pass


def _fresh(data: dict | None) -> bool:
    if not data or not data.get('updated_at'):
        return False
    try:
        age = datetime.now() - datetime.strptime(data['updated_at'], '%Y-%m-%d %H:%M:%S')
    except ValueError:
        return False
    return age.total_seconds() < TTL


def _worker(ticker: str) -> None:
    try:
        facts = context(ticker)
        result = _call(ticker, facts)
        if result:
            result['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            _save(ticker, result)
            _failed.pop(ticker, None)
        else:
            _failed[ticker] = 'модель не вернула ответ'
    except Exception as exc:  # noqa: BLE001
        _failed[ticker] = f'{type(exc).__name__}: {exc}'
    finally:
        with _lock:
            _running.pop(ticker, None)


def start(ticker: str) -> None:
    ticker = (ticker or '').upper()
    with _lock:
        if ticker in _running:
            return
        _failed.pop(ticker, None)
        _running[ticker] = time.time()
    threading.Thread(target=_worker, args=(ticker,), name=f'chart-ai-{ticker}',
                     daemon=True).start()


def status(ticker: str) -> dict:
    ticker = (ticker or '').upper()
    if ticker in _running:
        return {'available': False, 'pending': True,
                'seconds': round(time.time() - _running[ticker])}
    cached = _load(ticker)
    if _fresh(cached):
        cached = dict(cached or {})
        cached['available'] = True
        cached['cached'] = True
        return cached
    error = _failed.get(ticker)
    if error:
        return {'available': False, 'error': error}
    return {'available': False, 'idle': True}
