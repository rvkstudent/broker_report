"""AI-разбор одной бумаги: преимущества, недостатки и пригодность для стратегии.

Зачем: цифры в скринере отвечают на «сколько платят», но не на «устойчиво ли
это». Классическая ловушка — дивдоходность по последней выплате: разовый
дивиденд или изменившаяся дивидендная политика дают красивую цифру, которой
больше не будет. Языковая модель получает факты (историю выплат, метрики,
флаги риска) и явно рассуждает об устойчивости, а не только о величине.

Дизайн:
* вызывается по требованию (кнопка в карточке), а не для всех бумаг сразу;
* ответ кэшируется в ``instrument_ai`` на 7 дней;
* при отсутствии ключа LLM раздел просто не показывается — скринер работает.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from datetime import datetime

from app.advisor import llm_config, STRATEGY_TEXT
from app.db import get_connection

TTL = 7 * 24 * 3600


# ── кэш ─────────────────────────────────────────────────────

def _load(kind: str, code: str) -> dict | None:
    try:
        conn = get_connection()
        row = conn.execute(
            'SELECT payload, updated_at FROM instrument_ai WHERE kind=? AND code=?',
            (kind, code)).fetchone()
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


def _save(kind: str, code: str, data: dict) -> None:
    try:
        conn = get_connection()
        conn.execute(
            'INSERT OR REPLACE INTO instrument_ai (kind, code, payload, updated_at) '
            'VALUES (?, ?, ?, ?)',
            (kind, code, json.dumps(data, ensure_ascii=False),
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


# ── запрос к модели ─────────────────────────────────────────

PROMPT = """Ты — аналитик облигаций и акций для частного портфеля. Ниже факты по ОДНОЙ бумаге.
Оцени её преимущества и недостатки для стратегии и, главное, УСТОЙЧИВОСТЬ ДОХОДНОСТИ.

Правила:
- Дивдоходность по последней выплате часто обманчива: разовый дивиденд, новая дивидендная
  политика, спецвыплата. Если история выплат неровная — прямо скажи, что цифра «за 12 месяцев»
  не повторяема, и приведи, на что ориентироваться (политика эмитента, регулярные выплаты).
- Не выдумывай цифры. Если фактов нет — пиши «нет данных».
- Отдельно проверь, известна ли тебе ДИВИДЕНДНАЯ ПОЛИТИКА эмитента или её таргет
  (например, «выплачивать 24–28 руб. на акцию»). Если знаешь — укажи цифру и пометь,
  что это публичная политика, которую надо сверить с актуальной датой раскрытия:
  твои знания могут быть устаревшими. Если не знаешь — честно напиши «нет данных».
- Для облигаций оцени: спред к ОФЗ (это кредитный риск), дюрацию против горизонта, ликвидность,
  оферту. Высокий купон при низком листинге — красный флаг, а не преимущество.
- Учитывай наши правила: ядро — облигации до погашения; длинные ОФЗ — ставка на снижение ставок;
  акции — только в риск-бюджете; цель 30% годовых не прогнозируется.

СТРАТЕГИЯ:
""" + STRATEGY_TEXT + """

ФАКТЫ (JSON):
{payload}

Верни СТРОГО JSON:
{{"summary": "2–3 предложения по сути",
  "pros": ["до 3 пунктов, каждый с цифрой"],
  "cons": ["до 3 пунктов, каждый с цифрой"],
  "dividend_note": "про устойчивость выплат/купона: повторяема ли, известен ли таргет политики (с оговоркой о дате), что учесть",
  "verdict": "подходит / только для части позиции / не подходит",
  "risks": ["до 3 ключевых рисков"]}}
Без markdown и текста вне JSON."""


def _payload(kind: str, row: dict) -> dict:
    keep = ('code', 'name', 'price', 'ytm', 'duration', 'matdate', 'years',
            'coupon_pct', 'coupon_freq', 'spread', 'type', 'currency',
            'listlevel', 'turnover', 'trades', 'offer', 'div12', 'div3y',
            'years_paid', 'last_div', 'last_date', 'div_history', 'pe', 'pb',
            'roe', 'marketcap', 'flags', 'score', 'reason',
            'div_forward', 'forward_date')
    data = {k: row.get(k) for k in keep if row.get(k) not in (None, '', [])}
    data['reliability'] = (row.get('reliability') or {}).get('score')
    data['reliability_letter'] = (row.get('reliability') or {}).get('letter')
    data['kind'] = 'облигация' if kind in ('bond', 'fx') else 'акция'
    data['today'] = datetime.now().strftime('%Y-%m-%d')
    return data


def _call_llm(payload: dict) -> dict | None:
    cfg = llm_config()
    if not cfg:
        return None
    body = {
        'model': cfg['model'],
        'messages': [{'role': 'user', 'content': PROMPT.format(
            payload=json.dumps(payload, ensure_ascii=False, indent=1))}],
        'temperature': 0.2,
        'max_tokens': cfg['max_tokens'],
    }
    try:
        req = urllib.request.Request(cfg['url'], data=json.dumps(body).encode('utf-8'),
                                     headers=cfg['headers'], method='POST')
        with urllib.request.urlopen(req, timeout=cfg['timeout']) as resp:
            doc = json.loads(resp.read().decode('utf-8'))
    except Exception:  # noqa: BLE001
        return None
    try:
        message = doc['choices'][0]['message']
        content = message.get('content') or ''
        reasoning = message.get('reasoning_content') or ''
    except (KeyError, IndexError, TypeError):
        return None
    parsed = _parse(content) or _parse(reasoning)
    if parsed:
        parsed['model'] = cfg['model']
    return parsed


def _parse(text: str) -> dict | None:
    body = (text or '').strip()
    if body.startswith('```'):
        body = body[3:]
        body = body.split('\n', 1)[-1].rsplit('```', 1)[0]
    start, end = body.find('{'), body.rfind('}')
    if start < 0 or end <= start:
        return None
    try:
        doc = json.loads(body[start:end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(doc, dict) or not doc.get('summary'):
        return None
    def as_list(key: str) -> list[str]:
        value = doc.get(key) or []
        return [str(x)[:300] for x in value][:4] if isinstance(value, list) else []
    return {
        'summary': str(doc.get('summary'))[:800],
        'pros': as_list('pros'),
        'cons': as_list('cons'),
        'dividend_note': str(doc.get('dividend_note') or '')[:600],
        'verdict': str(doc.get('verdict') or '')[:60],
        'risks': as_list('risks'),
    }


def analyze(kind: str, code: str, row: dict | None, refresh: bool = False) -> dict:
    """AI-разбор бумаги: из кэша или свежим запросом к модели."""
    if row is None:
        return {'available': False, 'error': 'нет данных по бумаге'}
    cached = _load(kind, code)
    if not refresh and _fresh(cached):
        cached['available'] = True
        cached['cached'] = True
        return cached
    if not llm_config():
        return {'available': False, 'error': 'нейросеть не настроена'}

    payload = _payload(kind, row)
    started = time.time()
    result = _call_llm(payload)
    if not result:
        return {'available': False, 'error': 'модель не вернула ответ'}
    result['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    result['seconds'] = round(time.time() - started, 1)
    _save(kind, code, result)
    result.update({'available': True, 'cached': False})
    return result
