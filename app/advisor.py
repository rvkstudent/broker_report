"""Аналитика и рекомендации по нашей стратегии — правила + опционально LLM.

Два слоя, и это важно понимать:

1. **Правила** (`rules`) — детерминированный движок. Все пороги взяты из
   стратегии, которую мы считаем на этом сайте: иммунизация (D = H),
   концентрация погашений, разброс по годам, риск-бюджет по RVI,
   Келли, доля валюты, достижимость цели по Монте-Карло. Работает всегда,
   не требует сети и не отправляет данные наружу.

2. **Нейросеть** (`llm_recommend`) — необязательный слой: те же данные и
   найденные правила уходят в LLM, которая пишет объяснение и приоритеты
   человеческим языком. Включается ТОЛЬКО если явно заданы
   ``ADVISOR_LLM_URL``, ``ADVISOR_LLM_KEY``, ``ADVISOR_LLM_MODEL``.
   По умолчанию выключена: запрос отправляет состав портфеля внешнему
   сервису, поэтому решение об этом — за владельцем данных, а не за кодом.

Стратегия (напоминание, чтобы рекомендации не расходились с расчётами):
купонная машина как ядро (детерминированный доход), лестница с дюрацией под
горизонт, длинные ОФЗ как опцион на снижение ставок, акции — только в размере
риск-бюджета по RVI, валюта — как хедж, а не как доходность.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request

CACHE_TTL = 900.0          # 15 минут: рекомендации не должны «дёргаться»
CONCENTRATION_LIMIT = 30.0  # % облигаций в одном годе погашения
RISKY_LIMIT = 15.0          # % облигаций с флагами кредитного риска
FX_MIN, FX_MAX = 10.0, 30.0  # % портфеля в валюте
IMMUNIZATION_TOL = 1.0      # лет: допустимый зазор D − H

_cache: dict = {'at': 0.0, 'data': None}
_llm_cache: dict = {'at': 0.0, 'rows': [], 'model': ''}
_llm_running = threading.Event()
_lock = threading.Lock()


# ── контекст: что известно о портфеле ───────────────────────

def build_context(horizon: int = 15, sigma_star: float = 15.0,
                  model: dict | None = None, ladder: dict | None = None,
                  screen: dict | None = None) -> dict:
    """Сжать модели, лестницу и скринер в один профиль для правил и LLM.

    Готовые расчёты можно передать снаружи: страница моделей уже посчитала их
    для своего блока, и второй проход по ISS бьёт по времени открытия страницы.
    """
    from app import models, bond_ladder, instruments

    if model is None:
        try:
            model = models.build(horizon=horizon, sigma_star=sigma_star,
                                 equity_pct=10.0, paths=2000)
        except Exception:  # noqa: BLE001
            model = {'machine': {}, 'immunization': {}, 'rvi': {'available': False},
                     'vol_target': {'available': False}, 'monte_carlo': {'available': False},
                     'kelly': {}, 'bond_mu': 0, 'params': {'horizon': horizon}}

    if ladder is None:
        try:
            ladder = bond_ladder.build()
        except Exception:  # noqa: BLE001
            ladder = {'rows': [], 'ladder': [], 'bond_value': 0}

    if screen is None:
        try:
            screen = instruments.build()
        except Exception:  # noqa: BLE001
            screen = {'bonds': [], 'stocks': [], 'fx_bonds': []}

    positions = ladder.get('rows') or []
    bond_value = sum((r.get('value') or 0) for r in positions)
    fx_value = sum((r.get('value') or 0) for r in positions if r.get('currency') != 'RUB')
    risky_value = sum((r.get('value') or 0) for r in positions
                      if any('кредитный риск' in f or 'купон выше' in f
                             for f in (r.get('flags') or [])))

    years: dict[str, float] = {}
    for row in positions:
        year = (row.get('matdate') or '')[:4]
        if year:
            years[year] = years.get(year, 0.0) + (row.get('value') or 0)

    total_capital = model.get('machine', {}).get('value') or 0
    return {
        'as_of': model.get('as_of'),
        'bond_value': round(bond_value, 0),
        'bond_share_pct': round(bond_value / total_capital * 100, 1) if total_capital else None,
        'fx_share_pct': round(fx_value / total_capital * 100, 1) if total_capital else None,
        'risky_share_pct': round(risky_value / bond_value * 100, 1) if bond_value else None,
        'maturity_years': {y: round(v, 0) for y, v in sorted(years.items())},
        'duration': model.get('immunization', {}).get('duration'),
        'horizon': horizon,
        'gap': model.get('immunization', {}).get('gap'),
        'irr_net': model.get('machine', {}).get('irr_net'),
        'future': model.get('machine', {}).get('future'),
        'rvi': {k: model.get('rvi', {}).get(k) for k in
                ('available', 'current', 'percentile', 'regime', 'median', 'p75')},
        'risk_weight_pct': model.get('vol_target', {}).get('weight'),
        'kelly': model.get('kelly', {}),
        'monte_carlo': {k: model.get('monte_carlo', {}).get(k) for k in
                        ('cagr_p10', 'cagr_p50', 'cagr_p90', 'p_target', 'equity_pct')},
        'bond_candidates': [
            {k: b.get(k) for k in ('code', 'name', 'type', 'ytm', 'duration',
                                   'matdate', 'score', 'turnover', 'flags')}
            for b in (screen.get('bonds') or [])[:8]],
        'stock_candidates': [
            {k: s.get(k) for k in ('code', 'name', 'div12', 'div3y', 'pe', 'roe',
                                   'score', 'flags')}
            for s in (screen.get('stocks') or [])[:8]],
        'bond_metrics': ladder.get('duration_avg'), 'ytm_avg': ladder.get('ytm_avg'),
    }


# ── слой 1: правила ─────────────────────────────────────────

def _rec(rid: str, priority: int, severity: str, title: str,
         why: str, action: str, risk: str = '') -> dict:
    return {'id': rid, 'priority': priority, 'severity': severity, 'title': title,
            'why': why, 'action': action, 'risk': risk, 'source': 'правила'}


def rules(ctx: dict) -> list[dict]:
    """Детерминированные рекомендации по стратегии сайта."""
    out: list[dict] = []

    gap = ctx.get('gap')
    if gap is not None and abs(gap) > IMMUNIZATION_TOL:
        out.append(_rec(
            'immunization', 1, 'action',
            f'Дюрация {ctx["duration"]:.1f} против горизонта {ctx["horizon"]} — портфель не иммунизирован',
            f'Зазор {gap:+.1f} года. При сдвиге ставок на 1 п.п. стоимость облигаций '
            f'сдвигается на {abs(ctx["duration"]):.1f}%, и к горизонту это не компенсируется.',
            ('Удлинить лестницу покупками 2033–2037 (26252, 26244, 26245, 26246, 26250)'
             if gap < 0 else 'Сократить длинный конец: перенести часть 2038–2040 в 2032–2035'),
            'Удлинение увеличивает чувствительность к росту ставок — это плата за иммунизацию.'))

    years = ctx.get('maturity_years') or {}
    total = sum(years.values()) or 0
    if total:
        worst = max(years.items(), key=lambda kv: kv[1])
        share = worst[1] / total * 100
        if share > CONCENTRATION_LIMIT:
            out.append(_rec(
                'concentration', 1, 'action',
                f'{share:.0f}% облигаций погашается в {worst[0]} году',
                f'Порог стратегии — {CONCENTRATION_LIMIT:.0f}%. Всё окно реинвестирования '
                f'собрано в один год: ставка на этот год будет решать весь доход.',
                'Новые поступления и купоны направлять в 2033–2037, не увеличивать 2038+.',
                'Продажа ради разгрузки зафиксирует текущий убыток по длинным ОФЗ.'))

        # дыры между ближайшим и последним годом погашения
        known = sorted(int(y) for y in years if y.isdigit())
        if known:
            missing = [str(y) for y in range(known[0], known[-1] + 1)
                       if str(y) not in years]
            if len(missing) >= 3:
                out.append(_rec(
                    'ladder_gaps', 2, 'watch',
                    f'В лестнице {len(missing)} лет без погашений: {", ".join(missing[:6])}',
                    'Нет поступлений основного долга — реинвестировать придётся по ставке, '
                    'которая будет на рынке, а не по сегодняшней.',
                    'Заполнять дыры по одной бумаге в год: 26252 (2033), 26244 (2034), '
                    '26245 (2035), 26246 (2036), 26250 (2037).',
                    'Короткие выпуски 2033–2037 дают 16,5–17,0% — почти как 2038–2040 при меньшей дюрации.'))

    risky = ctx.get('risky_share_pct')
    if risky is not None and risky > RISKY_LIMIT:
        out.append(_rec(
            'credit_risk', 2, 'watch',
            f'{risky:.0f}% облигационной части помечено как кредитный риск',
            'Это бумаги с премией к кривой ОФЗ больше 6 п.п. или купоном выше 20%: '
            'доходность там — плата за риск эмитента, а не за срок.',
            'Проверить эмитентов; ограничить такие бумаги долей 10–15% и короткой дюрацией.',
            'Дефолт обнуляет и купон, и тело — Монте-Карло этого не учитывает.'))

    fx = ctx.get('fx_share_pct')
    if fx is not None and fx < FX_MIN:
        out.append(_rec(
            'fx_low', 3, 'info',
            f'Валютная доля {fx:.0f}% — хеджа от девальвации нет',
            'Весь капитал в рублях: при ослаблении рубля покупательная способность '
            'в валюте падает вместе с курсом.',
            'Держать 10–20% в валютных выпусках (замещающие/юаневые) — как страховку, '
            'а не как источник доходности.',
            'Доходность валютных бумаг ниже рублёвых: это плата за страховку.'))
    elif fx is not None and fx > FX_MAX:
        out.append(_rec(
            'fx_high', 3, 'watch',
            f'Валютная доля {fx:.0f}% — перекос в одну сторону',
            f'Выше {FX_MAX:.0f}% валютный риск становится основным риском портфеля.',
            'Не наращивать валютные выпуски, новые деньги — в рублёвые ОФЗ.',
            'При укреплении рубля такой портфель проигрывает рынку.'))

    rvi = ctx.get('rvi') or {}
    weight = ctx.get('risk_weight_pct')
    kelly = ctx.get('kelly') or {}
    if rvi.get('available') and weight is not None:
        regime = rvi.get('regime')
        sev = 'watch' if regime in ('страх', 'паника') else 'info'
        out.append(_rec(
            'risk_budget', 2, sev,
            f'RVI {rvi.get("current")} ({regime}) → риск-бюджет акций {weight:.0f}%',
            f'Правило w = σ*/RVI: при страхе выше медианы доля риска автоматически падает. '
            f'Перцентиль {rvi.get("percentile")}%, медиана {rvi.get("median")}, P75 {rvi.get("p75")}.',
            'Держать долю акций в пределах этого бюджета; при RVI > P75 не добавлять, '
            'при RVI < P25 можно поднять к верхней границе.',
            'Наивное «покупай на панике» даёт просадку до −49% — важен размер, а не сигнал.'))

        if kelly and not kelly.get('positive'):
            out.append(_rec(
                'kelly_negative', 2, 'watch',
                'Акции сейчас не дают премии к облигациям (Келли отрицательный)',
                f'Ожидаемая доходность акций (история MCFTR 10,5%) ниже доходности '
                f'облигаций после налогов ({ctx.get("irr_net")}%). Келли: {kelly.get("full")}%.',
                'Не увеличивать долю акций ради доходности: их роль сейчас — диверсификация, '
                'а не рост.',
                'Оценка акций — историческая; при снижении ставок премия может появиться.'))

    mc = ctx.get('monte_carlo') or {}
    p_target = mc.get('p_target')
    if p_target is not None:
        if p_target < 5:
            out.append(_rec(
                'target_30', 1, 'info',
                f'Цель 30% годовых: вероятность {p_target:.1f}%',
                f'Медиана Монте-Карло — {mc.get("cagr_p50")}% годовых, P10 — {mc.get("cagr_p10")}%. '
                '30% достигается только на снижении ставок (дюрация как опцион), а не купонами.',
                'Планировать бюджет по 14–16% после налогов, а 30% считать бонусом '
                'в годы снижения ставок.',
                'Обещать 30% как прогноз нельзя — это ставка на событие.'))
        else:
            out.append(_rec(
                'target_30', 3, 'info',
                f'Цель 30% достижима в {p_target:.1f}% траекторий',
                f'Медиана {mc.get("cagr_p50")}%, P90 {mc.get("cagr_p90")}%.',
                'Следить за просадкой: рост вероятности цели обычно идёт вместе с ростом риска.',
                ''))

    bonds = ctx.get('bond_candidates') or []
    stocks = ctx.get('stock_candidates') or []
    if bonds:
        top = bonds[0]
        out.append(_rec(
            'bond_candidate', 3, 'info',
            f'Лучший кандидат по облигациям: {top.get("name")} ({top.get("ytm")}%, дюрация {top.get("duration")})',
            f'Балл {top.get("score")} по правилу «доходность + ликвидность + листинг». '
            f'Погашение {top.get("matdate")}.',
            'Сравнить с ОФЗ той же дюрации: если премия меньше 1 п.п. — брать ОФЗ.',
            '; '.join(top.get('flags') or []) or 'Явных флагов риска нет.'))
    if stocks:
        top = stocks[0]
        out.append(_rec(
            'stock_candidate', 3, 'info',
            f'Лучший кандидат по дивидендам: {top.get("code")} (ДД {top.get("div12")}%, P/E {top.get("pe")})',
            f'Балл {top.get("score")}: дивиденды {top.get("div12")}% за 12 мес, '
            f'средняя за 3 года {top.get("div3y")}%.',
            'Проверить, что дивиденд не разовый, и входить в пределах риск-бюджета по RVI.',
            '; '.join(top.get('flags') or []) or 'Явных флагов нет.'))

    out.sort(key=lambda r: (r['priority'], r['id']))
    return out


# ── слой 2: нейросеть (опционально) ─────────────────────────

def llm_config() -> dict | None:
    url = (os.environ.get('ADVISOR_LLM_URL') or '').strip()
    key = (os.environ.get('ADVISOR_LLM_KEY') or '').strip()
    model = (os.environ.get('ADVISOR_LLM_MODEL') or '').strip()
    if not (url and model):
        return None
    # Часть шлюзов стоит за Cloudflare и режет запросы без User-Agent (1010).
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json',
               'User-Agent': 'BrokerReport/1.0 (advisor)'}
    if key:
        headers['Authorization'] = f'Bearer {key}'
    # Шлюзы (например, OpenCode Go) требуют идентификатор сессии для маршрутизации.
    session = (os.environ.get('ADVISOR_LLM_SESSION') or '').strip()
    if session:
        headers['x-opencode-session'] = session
    extra = (os.environ.get('ADVISOR_LLM_HEADERS') or '').strip()
    if extra:
        try:
            headers.update({str(k): str(v) for k, v in json.loads(extra).items()})
        except json.JSONDecodeError:
            pass
    return {'url': url, 'key': key, 'model': model, 'headers': headers,
            'max_tokens': int(os.environ.get('ADVISOR_LLM_MAX_TOKENS') or 6000),
            'timeout': float(os.environ.get('ADVISOR_LLM_TIMEOUT') or 90)}


STRATEGY_TEXT = """Стратегия (жёсткие правила, не нарушать):
1) Ядро — облигации до погашения; годовой результат = IRR после налогов (купон и дисконт по НДФЛ 13%). Никаких гарантий доходности.
2) Лестница погашений: дюрация под горизонт (D ≈ H), без концентрации более 30% в одном годе.
3) Длинные ОФЗ — опцион на снижение ставок, а не источник купона.
4) Акции — только в размере риск-бюджета w = σ*/RVI (индекс страха), максимум 25%.
5) Валюта 10–30% — хедж от девальвации, не источник доходности.
6) Цель 30% годовых не прогнозируется: вероятность берётся из Монте-Карло.
7) Запрещено: обещать доходность, советовать кредитное плечо, рекомендовать конкретные суммы покупки без оговорок."""


def _prompt(ctx: dict, found: list[dict]) -> str:
    return (
        'Ты — аналитик личного портфеля. Ниже профиль портфеля и уже найденные '
        'правилами замечания. Напиши 3–6 коротких рекомендаций на русском: '
        'что сделать, почему (с цифрами из профиля) и какой риск.\n\n'
        + STRATEGY_TEXT + '\n\n'
        'ПРОФИЛЬ (JSON):\n' + json.dumps(ctx, ensure_ascii=False, indent=1) + '\n\n'
        'НАЙДЕННЫЕ ПРАВИЛА (JSON):\n' + json.dumps(found, ensure_ascii=False, indent=1) + '\n\n'
        'Верни СТРОГО JSON-массив объектов: '
        '{"title": str, "why": str, "action": str, "risk": str, '
        '"severity": "action"|"watch"|"info"}. '
        'Каждое поле — одна короткая фраза с цифрами. '
        'Не рассуждай: сразу JSON-массив, без markdown и пояснений вне JSON.')


def _parse_json(text: str) -> list[dict] | None:
    body = (text or '').strip()
    if body.startswith('```'):
        body = body.split('```')[1] if '```' in body[3:] else body[3:]
        body = body.split('\n', 1)[-1].rsplit('```', 1)[0]
    start, end = body.find('['), body.rfind(']')
    if start < 0 or end <= start:
        return None
    try:
        rows = json.loads(body[start:end + 1])
    except json.JSONDecodeError:
        return None
    out = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not row.get('title'):
            continue
        out.append({'id': f'llm{len(out)}', 'priority': 2,
                    'severity': row.get('severity') if row.get('severity') in
                    ('action', 'watch', 'info') else 'info',
                    'title': str(row.get('title'))[:200],
                    'why': str(row.get('why') or '')[:600],
                    'action': str(row.get('action') or '')[:600],
                    'risk': str(row.get('risk') or '')[:400],
                    'source': 'нейросеть'})
    return out or None


def llm_recommend(ctx: dict, found: list[dict]) -> list[dict] | None:
    """Запрос к LLM. Возвращает None, если не настроено или произошла ошибка."""
    cfg = llm_config()
    if not cfg:
        return None
    payload = {
        'model': cfg['model'],
        'messages': [{'role': 'user', 'content': _prompt(ctx, found)}],
        'temperature': 0.2,
        'max_tokens': cfg['max_tokens'],
    }
    headers = cfg['headers']
    try:
        req = urllib.request.Request(cfg['url'], data=json.dumps(payload).encode('utf-8'),
                                     headers=headers, method='POST')
        with urllib.request.urlopen(req, timeout=cfg['timeout']) as resp:
            doc = json.loads(resp.read().decode('utf-8'))
    except Exception:  # noqa: BLE001 — сеть/формат: остаёмся на правилах
        return None
    try:
        message = doc['choices'][0]['message']
        content = message.get('content') or ''
        # У reasoning-моделей ответ может лежать в reasoning_content (а content
        # остаться пустым, если упёрлись в лимит токенов) — пробуем оба поля.
        reasoning = message.get('reasoning_content') or ''
    except (KeyError, IndexError, TypeError):
        content, reasoning = '', ''
    return _parse_json(content) or _parse_json(reasoning)


# ── сборка ──────────────────────────────────────────────────

def _llm_worker(ctx: dict, found: list[dict]) -> None:
    """Фоновой вызов нейросети: запрос идёт 20–40 секунд, страница ждать не должна."""
    if _llm_running.is_set():
        return
    _llm_running.set()
    try:
        rows = llm_recommend(ctx, found)
        if rows:
            _llm_cache.update({'at': time.time(), 'rows': rows,
                               'model': (llm_config() or {}).get('model', '')})
    finally:
        _llm_running.clear()


def recommend(force: bool = False, horizon: int = 15, sigma_star: float = 15.0,
              model: dict | None = None, ladder: dict | None = None,
              screen: dict | None = None) -> dict:
    """Рекомендации: правила отдаются сразу, нейросеть догружается в фоне."""
    with _lock:
        if not force and _cache['data'] and time.time() - _cache['at'] < CACHE_TTL:
            data = _cache['data']
            # Фоновая нейросеть могла закончить уже после сборки кэша — тогда
            # подставляем её вывод, не дожидаясь истечения TTL.
            if _llm_cache['rows'] and not data.get('llm'):
                data['llm'] = _llm_cache['rows']
                data['llm_pending'] = False
                data['llm_at'] = time.strftime('%H:%M', time.localtime(_llm_cache['at']))
                _cache['data'] = data
            return data

        ctx = build_context(horizon=horizon, sigma_star=sigma_star,
                            model=model, ladder=ladder, screen=screen)
        found = rules(ctx)
        cfg = llm_config()

        llm_rows = _llm_cache['rows']
        llm_age = time.time() - _llm_cache['at']
        if cfg and (force or not llm_rows or llm_age > CACHE_TTL):
            threading.Thread(target=_llm_worker, args=(ctx, found),
                             name='advisor-llm', daemon=True).start()

        data = {
            'context': ctx,
            'rules': found,
            'llm': llm_rows,
            'llm_enabled': bool(cfg),
            'llm_model': (cfg or {}).get('model', ''),
            'llm_pending': bool(cfg) and not llm_rows,
            'llm_at': (time.strftime('%H:%M', time.localtime(_llm_cache['at']))
                       if llm_rows else ''),
            'as_of': time.strftime('%Y-%m-%d %H:%M:%S'),
        }
        _cache.update({'at': time.time(), 'data': data})
        return data


def warmup() -> None:
    """Прогрев кэшей в фоне: первый заход на страницу не должен ждать сеть."""
    def run():
        try:
            recommend()
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=run, name='advisor-warmup', daemon=True).start()
