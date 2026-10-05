"""Авторизация BrokerReport: сессия для веб-интерфейса, токен для API.

Два независимых механизма:

1. **Веб (браузер)** — логин и пароль, состояние в подписанной куке сессии.
   Пароль хранится в виде хеша в файле ``/app/secrets/password_hash.txt``
   (создаётся скриптом ``deploy/set-password.sh``). Для локальной отладки
   можно задать ``APP_PASSWORD`` открытым текстом — но только не на сервере.

2. **API (QUIK)** — заголовок ``X-API-Token``, сверяется с ``API_TOKEN``
   через ``hmac.compare_digest`` (устойчиво к тайминг-атакам).

Переменные окружения
--------------------
``APP_USER``             логин (по умолчанию ``roman``)
``APP_PASSWORD_HASH_FILE`` файл с хешем пароля, основной вариант
                         (по умолчанию ``/app/secrets/password_hash.txt``)
``APP_PASSWORD_HASH``    хеш пароля (werkzeug) в переменной окружения
``APP_PASSWORD``         пароль открытым текстом, только для отладки
``API_TOKEN``            токен для QUIK
``AUTH_DISABLED``        ``true`` — полностью выключить проверку (локальная разработка)
``SESSION_COOKIE_SECURE`` ``true`` — кука только по HTTPS (на сервере включено)
``SESSION_HOURS``        срок жизни сессии в часах (по умолчанию 720 = 30 суток)
``SESSION_EPOCH_FILE``   файл с номером эпохи сессий (отзыв кук при выходе)
``LOGIN_MAX_ATTEMPTS``   попыток входа до временной блокировки (по умолчанию 10)
``LOGIN_WINDOW``         окно подсчёта попыток, секунды (по умолчанию 300)

Безопасность
------------
- Хеш пароля лежит в файле, а не в ``.env``, и вот почему: формат werkzeug —
  ``pbkdf2:sha256:600000$соль$хеш``, а Docker Compose в ``env_file`` считает
  знак ``$`` подстановкой переменной и заменяет ``$соль`` на пустую строку.
  Хеш молча портится, и вход перестаёт работать без единой ошибки в логах.
  Если всё же задаёте хеш через ``.env`` — удваивайте знак: ``$$``.
- ``session`` не хранит ничего, кроме имени пользователя, времени входа и
  CSRF-токена; кука подписана ``FLASK_SECRET``.
- При смене ``FLASK_SECRET`` все сессии разлогиниваются (полезно как «выйти везде»).
- Пароль никогда не пишется в логи; в лог идёт только факт неудачной попытки
  строкой ``AUTH-FAIL`` — по ней можно настроить fail2ban.
"""

import hmac
import logging
import os
import secrets
import threading
import time
from datetime import datetime, timedelta
from functools import wraps

from flask import (Blueprint, current_app, jsonify, redirect, render_template,
                   request, session, url_for)

from app.db import DB_PATH

logger = logging.getLogger(__name__)

# ── Конфигурация из окружения ────────────────────────────────


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ('1', 'true', 'yes', 'on')


# Хеш пароля читается из файла — так знак ``$`` внутри хеша не может быть
# испорчен подстановкой переменных Docker Compose (см. докстроку модуля).
PASSWORD_HASH_FILE = os.environ.get('APP_PASSWORD_HASH_FILE',
                                    '/app/secrets/password_hash.txt')


def _hash_looks_valid(value: str) -> bool:
    """Похож ли хеш на результат werkzeug: pbkdf2:...$соль$хеш."""
    return value.startswith('pbkdf2:') and value.count('$') == 2


def _read_password_hash() -> str:
    """Хеш пароля: сначала файл, при его отсутствии — переменная окружения."""
    try:
        with open(PASSWORD_HASH_FILE, encoding='utf-8') as handle:
            value = handle.read().strip()
    except OSError:
        value = ''
    source = PASSWORD_HASH_FILE
    if not value:
        value = (os.environ.get('APP_PASSWORD_HASH') or '').strip()
        source = 'APP_PASSWORD_HASH'
    if value and not _hash_looks_valid(value):
        logger.error('Хеш пароля (%s) повреждён: знаков «$» — %d, ожидается 2. '
                     'Вход работать не будет. Частая причина — знак $ в .env: '
                     'Docker Compose съедает его как подстановку переменной. '
                     'Перезадайте пароль: sh deploy/set-password.sh',
                     source, value.count('$'))
    return value


APP_USER = os.environ.get('APP_USER', 'roman')
PASSWORD_HASH = _read_password_hash()
PASSWORD_PLAIN = os.environ.get('APP_PASSWORD', '')
API_TOKEN = os.environ.get('API_TOKEN', '')
AUTH_DISABLED = _env_bool('AUTH_DISABLED', False)

SESSION_HOURS = float(os.environ.get('SESSION_HOURS', '720'))
LOGIN_MAX_ATTEMPTS = int(os.environ.get('LOGIN_MAX_ATTEMPTS', '10'))
LOGIN_WINDOW = int(os.environ.get('LOGIN_WINDOW', '300'))

# Максимальная задержка ответа при неверном пароле — тормозит перебор
FAIL_DELAY = float(os.environ.get('LOGIN_FAIL_DELAY', '0.7'))

# Пути, доступные без сессии
PUBLIC_PATHS = ('/login', '/static/', '/favicon', '/healthz')

# Префикс API: авторизуется токеном либо сессией
API_PREFIX = '/api/'


# ── Вспомогательные проверки ─────────────────────────────────


def auth_enabled() -> bool:
    """Нужно ли вообще проверять доступ."""
    if AUTH_DISABLED:
        return False
    return bool(PASSWORD_HASH or PASSWORD_PLAIN)


def api_token_enabled() -> bool:
    """Задан ли токен для QUIK."""
    return bool(API_TOKEN)


def verify_login(user: str, password: str) -> bool:
    """Проверить логин и пароль."""
    if not auth_enabled():
        return False
    if not hmac.compare_digest(user or '', APP_USER):
        return False
    if PASSWORD_HASH:
        from werkzeug.security import check_password_hash
        try:
            return check_password_hash(PASSWORD_HASH, password or '')
        except ValueError:
            logger.error('APP_PASSWORD_HASH повреждён — не удалось проверить пароль')
            return False
    return hmac.compare_digest(password or '', PASSWORD_PLAIN)


def check_api_token(token: str | None) -> bool:
    """Сверить токен API (устойчиво к тайминг-атакам)."""
    if not API_TOKEN:
        # Токен не задан: на сервере это ошибка конфигурации, разрешать нельзя
        return not auth_enabled()
    if not token:
        return False
    return hmac.compare_digest(token, API_TOKEN)


def api_authorized() -> bool:
    """Пускать ли запрос к /api/*.

    Пока пароль не задан (локальный режим), API открыт — иначе QUIK не смог бы
    работать на машине разработчика. Как только пароль задан, нужен токен или
    сессия.
    """
    if not auth_enabled():
        return True
    return is_logged_in() or check_api_token(request_api_token())


def request_api_token() -> str | None:
    """Достать токен из заголовка или из query-параметра."""
    return (request.headers.get('X-API-Token')
            or request.headers.get('Authorization', '').removeprefix('Bearer ').strip()
            or request.args.get('api_token'))


# ── Сессия ───────────────────────────────────────────────────

# Сессия Flask живёт в куке и только подписана, поэтому обычный выход
# очищает куку лишь в браузере: у кого на руках старая кука — тот остаётся
# внутри. Чтобы выход реально отзывал доступ, в куку кладётся номер эпохи,
# а текущее значение лежит в файле на volume. Выход меняет значение в файле,
# и все ранее выданные куки становятся недействительными.

SESSION_EPOCH_FILE = os.environ.get(
    'SESSION_EPOCH_FILE',
    os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), 'session_epoch'))

_epoch_lock = threading.Lock()
_epoch_cache: str | None = None


def _write_epoch(value: str) -> bool:
    try:
        folder = os.path.dirname(SESSION_EPOCH_FILE)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(SESSION_EPOCH_FILE, 'w', encoding='utf-8') as handle:
            handle.write(value + '\n')
        return True
    except OSError as exc:
        logger.warning('Не удалось записать эпоху сессий (%s): %s — '
                       'выход будет действовать только в текущем браузере',
                       SESSION_EPOCH_FILE, exc)
        return False


def session_epoch() -> str:
    """Текущая эпоха сессий, создаётся при первом обращении.

    Значение держится в памяти: если файл недоступен для записи, оно
    останется стабильным в пределах процесса, и доступ не сломается.
    """
    global _epoch_cache
    with _epoch_lock:
        if _epoch_cache:
            return _epoch_cache
        try:
            with open(SESSION_EPOCH_FILE, encoding='utf-8') as handle:
                value = handle.read().strip()
            if value:
                _epoch_cache = value
                return value
        except OSError:
            pass
        value = secrets.token_urlsafe(16)
        _write_epoch(value)
        _epoch_cache = value
        return value


def revoke_sessions():
    """Отозвать всё выданные ранее сессии: выход и смена пароля."""
    global _epoch_cache
    with _epoch_lock:
        value = secrets.token_urlsafe(16)
        _write_epoch(value)
        _epoch_cache = value


def login_user(user: str):
    """Открыть сессию."""
    session.clear()
    session['user'] = user
    session['login_at'] = datetime.now().isoformat()
    session['csrf'] = secrets.token_urlsafe(32)
    session['epoch'] = session_epoch()
    session.permanent = True


def logout_user():
    """Закрыть сессию."""
    session.clear()


def current_user() -> str | None:
    """Имя вошедшего пользователя или None (с учётом срока жизни)."""
    user = session.get('user')
    if not user:
        return None
    if session.get('epoch') != session_epoch():
        # Кука выдана до последнего выхода или смены пароля
        logout_user()
        return None
    try:
        login_at = datetime.fromisoformat(session.get('login_at', ''))
    except ValueError:
        logout_user()
        return None
    if datetime.now() - login_at > timedelta(hours=SESSION_HOURS):
        logout_user()
        return None
    return user


def is_logged_in() -> bool:
    return current_user() is not None


# ── CSRF ─────────────────────────────────────────────────────


def csrf_token() -> str:
    """Токен для формы; создаётся при первом обращении."""
    token = session.get('csrf')
    if not token:
        token = secrets.token_urlsafe(32)
        session['csrf'] = token
    return token


def check_csrf() -> bool:
    """Проверить CSRF-токен в форме или заголовке."""
    expected = session.get('csrf')
    if not expected:
        return False
    got = (request.form.get('csrf_token')
           or request.headers.get('X-CSRF-Token'))
    return bool(got) and hmac.compare_digest(got, expected)


# ── Защита от перебора пароля ────────────────────────────────


_failures: dict[str, list[float]] = {}
_failures_lock = threading.Lock()


def _client_ip() -> str:
    """IP клиента (с учётом ProxyFix за Cloudflare)."""
    return request.remote_addr or 'unknown'


def login_locked_out() -> bool:
    """Слишком много неудачных попыток с этого IP?"""
    now = time.time()
    ip = _client_ip()
    with _failures_lock:
        stamps = [t for t in _failures.get(ip, []) if now - t < LOGIN_WINDOW]
        _failures[ip] = stamps
    return len(stamps) >= LOGIN_MAX_ATTEMPTS


def record_login_failure():
    """Зафиксировать неудачную попытку и записать её в лог."""
    ip = _client_ip()
    with _failures_lock:
        _failures.setdefault(ip, []).append(time.time())
    logger.warning('AUTH-FAIL ip=%s user=%s path=%s ua=%s',
                   ip, request.form.get('username', ''), request.path,
                   (request.headers.get('User-Agent') or '')[:80])


def clear_login_failures():
    """Сбросить счётчик после успешного входа."""
    with _failures_lock:
        _failures.pop(_client_ip(), None)


# ── Декораторы ───────────────────────────────────────────────


def login_required(view):
    """Доступ только для вошедших; для API — JSON 401 вместо редиректа."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not auth_enabled() or is_logged_in():
            return view(*args, **kwargs)
        if request.path.startswith(API_PREFIX):
            return jsonify({'error': 'Требуется авторизация'}), 401
        return redirect(url_for('auth.login', next=request.path))
    return wrapper


def api_token_required(view):
    """Доступ по токену API (для QUIK) либо по сессии."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if api_authorized():
            return view(*args, **kwargs)
        logger.warning('API-FAIL ip=%s path=%s (неверный или отсутствующий токен)',
                       _client_ip(), request.path)
        return jsonify({'error': 'Неверный или отсутствующий X-API-Token'}), 401
    return wrapper


# ── Before-request: единая точка входа ───────────────────────


def _gate():
    """Пропустить или завернуть запрос.

    Возвращает None, если запрос можно пропустить дальше.
    """
    if not auth_enabled():
        return None

    path = request.path

    # Статика, страница входа, healthcheck — без проверок
    if path.startswith(PUBLIC_PATHS):
        return None

    # API: токен либо сессия
    if path.startswith(API_PREFIX):
        if request.method == 'OPTIONS':
            return None
        if api_authorized():
            return None
        if path == '/api/healthz':
            return None
        logger.warning('API-FAIL ip=%s path=%s method=%s',
                       _client_ip(), path, request.method)
        return jsonify({'error': 'Неверный или отсутствующий X-API-Token'}), 401

    # Всё остальное — только с сессией
    if not is_logged_in():
        if request.accept_mimetypes.best == 'application/json':
            return jsonify({'error': 'Требуется авторизация'}), 401
        return redirect(url_for('auth.login', next=path))

    # Защита форм от подделки запроса (для HTML-форм, не для JSON API)
    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
        if not check_csrf():
            logger.warning('CSRF-FAIL ip=%s path=%s', _client_ip(), path)
            return render_template('login.html', error=None,
                                   message='Сессия устарела, войдите заново.'), 400

    return None


# ── Роуты ────────────────────────────────────────────────────

auth_bp = Blueprint('auth', __name__)


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    """Страница входа."""
    next_url = request.args.get('next') or request.form.get('next') or '/'
    # Открытый редирект не допускаем: только внутренние пути
    if not next_url.startswith('/') or next_url.startswith('//'):
        next_url = '/'

    if not auth_enabled():
        # Авторизация выключена (локальный режим) — сразу на главную
        return redirect(next_url)

    if request.method == 'GET':
        if is_logged_in():
            return redirect(next_url)
        return render_template('login.html', error=None, next=next_url)

    if login_locked_out():
        logger.warning('AUTH-LOCKOUT ip=%s', _client_ip())
        return render_template(
            'login.html', error='Слишком много попыток. Повторите через 5 минут.',
            next=next_url), 429

    username = (request.form.get('username') or '').strip()
    password = request.form.get('password') or ''

    # CSRF на форме входа: токен живёт в сессии анонимного пользователя
    if not check_csrf():
        record_login_failure()
        time.sleep(FAIL_DELAY)
        return render_template('login.html', error='Форма устарела, попробуйте снова.',
                               next=next_url), 400

    if verify_login(username, password):
        clear_login_failures()
        login_user(username)
        logger.info('AUTH-OK user=%s ip=%s', username, _client_ip())
        return redirect(next_url)

    record_login_failure()
    time.sleep(FAIL_DELAY)
    return render_template('login.html', error='Неверный логин или пароль.',
                           next=next_url), 401


@auth_bp.route('/logout', methods=['POST'])
def logout():
    """Выход: отозвать все сессии и вернуться на страницу входа."""
    logout_user()
    revoke_sessions()
    logger.info('AUTH-OUT ip=%s', _client_ip())
    return redirect(url_for('auth.login'))


@auth_bp.route('/healthz')
def healthz():
    """Проверка живости для Docker/monitoring."""
    return jsonify({'status': 'ok'}), 200


# ── Подключение к приложению ─────────────────────────────────


def register_auth(flask_app):
    """Настроить куки, прокси-заголовки, роуты и фильтр запросов."""
    from werkzeug.middleware.proxy_fix import ProxyFix

    forwards = int(os.environ.get('TRUSTED_PROXY_COUNT', '1'))
    if forwards > 0:
        flask_app.wsgi_app = ProxyFix(flask_app.wsgi_app, x_for=forwards,
                                      x_proto=forwards, x_host=forwards)

    flask_app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=_env_bool('SESSION_COOKIE_SECURE', False),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=SESSION_HOURS),
    )

    flask_app.register_blueprint(auth_bp)
    flask_app.before_request(_gate)

    flask_app.jinja_env.globals['csrf_token'] = csrf_token
    flask_app.jinja_env.globals['current_user'] = current_user
    flask_app.jinja_env.globals['auth_enabled'] = auth_enabled

    if not auth_enabled():
        if AUTH_DISABLED:
            logger.warning('Авторизация ВЫКЛЮЧЕНА принудительно '
                           '(AUTH_DISABLED=true) — режим локальной разработки')
        else:
            logger.warning('Авторизация ВЫКЛЮЧЕНА: пароль не задан (нет файла %s '
                           'и переменной APP_PASSWORD_HASH). Задайте пароль '
                           'командой: sh deploy/set-password.sh',
                           PASSWORD_HASH_FILE)
    elif not api_token_enabled():
        logger.warning('API_TOKEN не задан — QUIK не сможет отправлять данные '
                       'на этот сервер (см. deploy/init-secrets.sh)')
    return flask_app
