"""ИИ-оценка портфеля: преимущества, недостатки, прогноз и ребалансировка.

Кнопка на странице роста портфеля. В модель уходят только посчитанные факты:
состав позиций, дюрация и концентрация по годам, IRR после налогов, индекс
страха (RVI) и распределение Монте-Карло, а также лучшие кандидаты скринера.
Модель не считает доходность сама — она объясняет и предлагает действия.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from datetime import datetime

from app.advisor import llm_config, STRATEGY_TEXT
from app.db import get_connection

TTL = 60 * 60
_running = {'at': 0.0}
_failed: dict[str, str] = {}
_lock = threading.Lock()


def context() -> dict:
    """Факты по текущему портфелю для промпта."""
    from app import models, bond_ladder, instruments
    from app.db import get_instrument_summary

    bond_ladder_rows: list[dict] = []
    try:
        bond_ladder_rows = bond_ladder.build().get('rows') or []
    except Exception:  # noqa: BLE001
        bond_ladder_rows = []

    # Снимок скринера знает SECID и тип бумаги — по нему и классифицируем
    # позиции: иначе рублёвые ОФЗ попадали в «акции».
    screen: dict = {}
    try:
        screen = instruments.build()
    except Exception:  # noqa: BLE001
        screen = {}
    bond_meta: dict[str, dict] = {}
    for bucket in ('bonds', 'fx_bonds'):
        for row in screen.get(bucket) or []:
            for key in ((row.get('code') or '').upper(), (row.get('isin') or '').upper()):
                if key:
                    bond_meta[key] = row
    stock_codes = {(s.get('code') or '').upper() for s in (screen.get('stocks') or [])}

    # Облигации из лестницы: там есть валюта, тип и текущая стоимость —
    # без этого модель не видит валютный хедж и пишет «валюты нет».
    bond_by_code: dict[str, dict] = {}
    try:
        for row in (bond_ladder_rows or []):
            for key in ((row.get('code') or '').upper(), (row.get('isin') or '').upper()):
                if key:
                    bond_by_code[key] = row
    except Exception:  # noqa: BLE001
        pass

    positions = []
    try:
        for pos in get_instrument_summary(None, '', '', 'all') or []:
            if (pos.get('qty') or 0) <= 0:
                continue
            code = (pos.get('security_code') or '').upper()
            meta = bond_meta.get(code)
            bond = bond_by_code.get(code)
            if meta:
                currency = meta.get('currency') or 'RUB'
                kind = 'валютная облигация' if currency != 'RUB' else 'облигация'
            elif code in stock_codes:
                currency, kind = 'RUB', 'акция'
            elif bond:
                currency = bond.get('currency') or 'RUB'
                kind = 'валютная облигация' if currency != 'RUB' else 'облигация'
            else:
                digits = ''.join(ch for ch in code if ch.isdigit())
                guess = bond_by_code.get(digits) if digits else None
                currency = (guess or {}).get('currency') or 'RUB'
                kind = 'облигация' if (guess or code[:2] in ('SU', 'RU')) else 'акция'
            value = ((meta or {}).get('value') or (bond or {}).get('value')
                     or pos.get('total_cost') or 0)
            positions.append({'code': pos.get('security_code'),
                              'name': pos.get('security_name'),
                              'qty': pos.get('qty'),
                              'avg_price': pos.get('avg_price'),
                              'cost': pos.get('total_cost'),
                              'value': round(float(value or 0), 0),
                              'currency': currency,
                              'kind': kind})
    except Exception:  # noqa: BLE001
        pass

    # Доли по классам и валютам — прямо в фактах, чтобы модель не догадывалась.
    total = sum(p['value'] for p in positions) or 1.0
    fx_bonds = sum(p['value'] for p in positions if p['currency'] != 'RUB')
    stocks = sum(p['value'] for p in positions if p['kind'] == 'акция')
    rub_bonds = sum(p['value'] for p in positions
                    if p['currency'] == 'RUB' and p['kind'] != 'акция')
    allocation = {
        'total_value': round(total, 0),
        'bonds_rub_pct': round(rub_bonds / total * 100, 1),
        'bonds_fx_pct': round(fx_bonds / total * 100, 1),
        'stocks_pct': round(stocks / total * 100, 1),
        'fx_share_pct': round(fx_bonds / total * 100, 1),
        'fx_positions': [{'code': p['code'], 'name': p['name'],
                          'currency': p['currency'], 'value': p['value'],
                          'share_pct': round(p['value'] / total * 100, 1)}
                         for p in positions if p['currency'] != 'RUB'],
        'has_fx_hedge': fx_bonds > 0,
    }
    for p in positions:
        p['share_pct'] = round(p['value'] / total * 100, 1)
    positions.sort(key=lambda p: -(p['value'] or 0))
    positions = [{k: p[k] for k in ('code', 'name', 'kind', 'currency',
                                    'value', 'share_pct')} for p in positions[:15]]

    try:
        model = models.build(horizon=15, sigma_star=15.0, equity_pct=10.0, paths=2000)
    except Exception:  # noqa: BLE001
        model = {}

    try:
        ladder = bond_ladder.build()
    except Exception:  # noqa: BLE001
        ladder = {}
    years: dict[str, float] = {}
    for row in ladder.get('rows') or []:
        year = (row.get('matdate') or '')[:4]
        if year:
            years[year] = round(years.get(year, 0.0) + (row.get('value') or 0), 0)

    return {
        'today': datetime.now().strftime('%Y-%m-%d'),
        'positions': positions,
        'allocation': allocation,
        'bonds': {'value': ladder.get('bond_value'),
                  'duration_avg': ladder.get('duration_avg'),
                  'ytm_avg': ladder.get('ytm_avg'),
                  'by_year': dict(sorted(years.items()))},
        'irr_net_pct': (model.get('machine') or {}).get('irr_net'),
        'future_15y': (model.get('machine') or {}).get('future'),
        'immunization': {k: (model.get('immunization') or {}).get(k)
                         for k in ('duration', 'horizon', 'gap', 'immunized')},
        'rvi': {k: (model.get('rvi') or {}).get(k)
                for k in ('current', 'percentile', 'regime', 'median')},
        'risk_weight_pct': (model.get('vol_target') or {}).get('weight'),
        'kelly': model.get('kelly'),
        'monte_carlo': {k: (model.get('monte_carlo') or {}).get(k)
                        for k in ('p10', 'p50', 'p90', 'cagr_p10', 'cagr_p50',
                                  'cagr_p90', 'p_target')},
        'bond_candidates': [{k: b.get(k) for k in ('code', 'name', 'ytm', 'duration',
                                                   'matdate', 'score', 'reliability')}
                            for b in (screen.get('bonds') or [])[:6]],
        'stock_candidates': [{k: s.get(k) for k in ('code', 'name', 'div12', 'div_forward',
                                                     'pe', 'score')}
                             for s in (screen.get('stocks') or [])[:5]],
    }


PROMPT = """Ты — портфельный аналитик. Ниже факты по портфелю частного инвестора.
Дай честную оценку и конкретные предложения.

Требования:
- pros / cons: по 2–4 пункта, каждый с цифрой из фактов (дюрация, IRR, концентрация,
  доля акций/валюты, RVI, Монте-Карло).
- expected_return: база/плохой/хороший сценарий в % годовых после налогов — из
  Монте-Карло (cagr_p10/p50/p90), не из головы.
- rebalancing: 2–4 конкретных действия (что купить/продать/не наращивать), каждое
  с обоснованием и привязкой к цифрам. Суммы не выдумывай — говори в долях или
  рублях из фактов (например, «перевести ~200 тыс. ₽ из 2038 в 2033–2037»).
- Валюту бери из allocation.fx_share_pct и fx_positions (там перечислены
  валютные бумаги, например замещающие облигации). Если fx_share_pct > 0 —
  хедж ЕСТЬ, назови его долю и эмитента; нельзя писать «валюты нет».
- Недостаток формулируй только если fx_share_pct ниже 10% (мало) или выше 30%
  (перекос).
- Не обещай доходность и не советуй плечо.
- Будь краток: summary ≤ 400 символов, каждый пункт pros/cons/risks ≤ 120 символов,
  action/why ≤ 140 символов. Длинные ответы обрываются по лимиту токенов.

СТРАТЕГИЯ:
""" + STRATEGY_TEXT + """

ФАКТЫ (JSON):
{facts}

Верни СТРОГО JSON:
{{"summary": "3–4 предложения",
  "pros": ["..."], "cons": ["..."],
  "expected_return": {{"base": "X%", "bad": "Y%", "good": "Z%", "note": "чем обосновано"}},
  "rebalancing": [{{"action": "что сделать", "why": "почему, с цифрами", "size": "доля/сумма"}}],
  "risks": ["1–3 ключевых риска"]}}
Без markdown и текста вне JSON."""


def _call(facts: dict) -> dict | None:
    cfg = llm_config()
    if not cfg:
        return None
    body = {'model': cfg['model'],
            'messages': [{'role': 'user',
                          'content': PROMPT.format(facts=json.dumps(facts, ensure_ascii=False, indent=1))}],
            'temperature': 0.2,
            'max_tokens': max(int(cfg['max_tokens']), 12000)}
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
    if not doc.get('summary'):
        return None

    def lst(key: str, limit: int = 5) -> list[str]:
        return [str(x)[:400] for x in (doc.get(key) or [])][:limit]

    reb = []
    for item in (doc.get('rebalancing') or [])[:5]:
        if isinstance(item, dict):
            reb.append({k: str(item.get(k) or '')[:300] for k in ('action', 'why', 'size')})
        elif isinstance(item, str):
            reb.append({'action': item[:300], 'why': '', 'size': ''})
    return {'summary': str(doc.get('summary'))[:1200],
            'pros': lst('pros'), 'cons': lst('cons'),
            'expected_return': doc.get('expected_return') or {},
            'rebalancing': reb,
            'risks': lst('risks', 4)}


# ── кэш и фон ───────────────────────────────────────────────

def _load() -> dict | None:
    try:
        conn = get_connection()
        row = conn.execute("SELECT payload, updated_at FROM portfolio_ai WHERE id=1").fetchone()
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


def _save(data: dict) -> None:
    try:
        conn = get_connection()
        conn.execute('INSERT OR REPLACE INTO portfolio_ai (id, payload, updated_at) VALUES (1, ?, ?)',
                     (json.dumps(data, ensure_ascii=False),
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


def _worker() -> None:
    try:
        result = _call(context())
        if result:
            result['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            _save(result)
            _failed.pop('ai', None)
        else:
            _failed['ai'] = 'модель не вернула ответ'
    except Exception as exc:  # noqa: BLE001
        _failed['ai'] = f'{type(exc).__name__}: {exc}'
    finally:
        _running['at'] = 0.0


def start() -> None:
    with _lock:
        if _running['at']:
            return
        _failed.pop('ai', None)
        _running['at'] = time.time()
    threading.Thread(target=_worker, name='portfolio-ai', daemon=True).start()


def status() -> dict:
    if _running['at']:
        return {'available': False, 'pending': True,
                'seconds': round(time.time() - _running['at'])}
    error = _failed.get('ai')
    cached = _load()
    if _fresh(cached):
        cached = dict(cached or {})
        cached['available'] = True
        cached['cached'] = True
        if error:
            # Показываем прошлую оценку, но честно говорим, что пересчёт не удался.
            cached['stale'] = True
            cached['error'] = error
        return cached
    if error:
        return {'available': False, 'error': error}
    return {'available': False, 'idle': True}
