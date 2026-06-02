import os
from pathlib import Path
from django.core.exceptions import ImproperlyConfigured
from django.core.management.utils import get_random_secret_key
from dotenv import load_dotenv

# Загружаем .env
load_dotenv()


def _is_remote_db_host(host: str) -> bool:
    """True for non-local DB hosts where SSL is usually required."""
    return bool(host and host not in ("localhost", "127.0.0.1"))


def _normalize_render_db_host(host: str) -> str:
    """
    Normalize DB host for Render deployments.
    Some misconfigured envs contain short internal IDs like `dpg-...-a`,
    while Django/psycopg expects a resolvable FQDN.
    """
    normalized = (host or "").strip()
    if not normalized:
        return normalized

    # Already an IP/FQDN/local host.
    if "." in normalized or normalized in ("localhost", "127.0.0.1"):
        return normalized

    if normalized.startswith("dpg-"):
        region = (os.environ.get("RENDER_REGION") or "").strip().lower()
        if not region:
            # Render default region fallback for this deployment.
            region = "oregon"
        if region:
            # Render internal host can appear as short id: dpg-...-a
            # Build canonical regional hostname used by Render Postgres.
            return f"{normalized}.{region}-postgres.render.com"

        # Try known fallbacks from common env names first.
        for key in ("DB_HOST", "PGHOST", "RENDER_DB_HOST", "DATABASE_HOST"):
            candidate = (os.environ.get(key) or "").strip()
            if candidate and "." in candidate:
                return candidate
    return normalized


def _validate_render_db_host(host: str, source_name: str) -> None:
    """
    Fail fast on Render when DB host is likely an unresolved short identifier.
    """
    clean = (host or "").strip()
    if clean.startswith("dpg-") and "." not in clean:
        raise ImproperlyConfigured(
            f"{source_name} contains an unresolvable Render DB host '{clean}'. "
            "Set DATABASE_URL/DB_HOST to the full PostgreSQL hostname from Render "
            "(Internal/External Database URL)."
        )


def _apply_db_ssl_options(db_config: dict) -> None:
    """
    Apply SSL mode only when explicitly configured.
    On some managed/internal Postgres networks forcing `sslmode=require`
    can fail with "SSL connection has been closed unexpectedly".
    """
    existing_sslmode = (
        (db_config.get("OPTIONS") or {}).get("sslmode", "") if isinstance(db_config.get("OPTIONS"), dict) else ""
    )

    sslmode = (
        os.environ.get("DB_SSLMODE")
        or os.environ.get("PGSSLMODE")
        or existing_sslmode
        or ""
    ).strip().lower()

    # Render-safe default: when host is a Render Postgres host and SSL mode
    # is not explicitly configured, prefer non-SSL to avoid handshake failures
    # like "SSL connection has been closed unexpectedly".
    if not sslmode:
        host = (db_config.get("HOST") or "").strip().lower()
        if host.startswith("dpg-") and "postgres.render.com" in host:
            sslmode = "disable"

    if not sslmode:
        return
    db_config.setdefault("OPTIONS", {})
    db_config["OPTIONS"]["sslmode"] = sslmode

# --- ПУТИ ---
BASE_DIR = Path(__file__).resolve().parent.parent

# --- БЕЗОПАСНОСТЬ ---
SECRET_KEY = os.environ.get('SECRET_KEY', get_random_secret_key())
IS_RENDER = os.environ.get('RENDER') == 'true' or bool(os.environ.get('RENDER_EXTERNAL_HOSTNAME'))
# На Render по умолчанию production-режим (меньше CSRF/SSL сюрпризов).
if IS_RENDER and os.environ.get('DEBUG') is None:
    DEBUG = False
else:
    DEBUG = os.environ.get('DEBUG', 'True') == 'True'

# --- ALLOWED_HOSTS ---
ALLOWED_HOSTS_ENV = os.environ.get('ALLOWED_HOSTS', '')
if ALLOWED_HOSTS_ENV:
    ALLOWED_HOSTS = [host.strip() for host in ALLOWED_HOSTS_ENV.split(',')]
else:
    ALLOWED_HOSTS = ['*']

# Демо-платформа: по умолчанию гарантируем admin/admin123456 (отключить: ENSURE_DEMO_ADMIN=false).
ENSURE_DEMO_ADMIN = os.environ.get('ENSURE_DEMO_ADMIN', 'true').lower() not in ('0', 'false', 'no')

render_host = (os.environ.get('RENDER_EXTERNAL_HOSTNAME') or '').strip()
if render_host:
    if render_host not in ALLOWED_HOSTS and '.onrender.com' not in ALLOWED_HOSTS:
        ALLOWED_HOSTS.append(render_host)
    if '.onrender.com' not in ALLOWED_HOSTS and '*.onrender.com' not in ALLOWED_HOSTS:
        ALLOWED_HOSTS.append('.onrender.com')


def _build_csrf_trusted_origins():
    """
    Django не поддерживает wildcards в CSRF_TRUSTED_ORIGINS (https://*.onrender.com не работает).
    Добавляем точные origin для Render и доменов из env.
    """
    origins = set()

    for part in os.environ.get('CSRF_TRUSTED_ORIGINS', '').split(','):
        part = part.strip()
        if not part or '*' in part:
            continue
        if '://' in part:
            origins.add(part.rstrip('/'))
        else:
            origins.add(f'https://{part.lstrip(".")}')

    if render_host:
        origins.add(f'https://{render_host}')
        origins.add(f'http://{render_host}')

    render_url = (os.environ.get('RENDER_EXTERNAL_URL') or '').strip().rstrip('/')
    if render_url.startswith('http'):
        origins.add(render_url)

    for host in ALLOWED_HOSTS:
        host = (host or '').strip()
        if not host or host == '*' or '*' in host:
            continue
        # .onrender.com в ALLOWED_HOSTS — не добавляем https://onrender.com (неверный origin).
        if host.startswith('.'):
            continue
        clean = host.lstrip('.')
        origins.add(f'https://{clean}')
        if DEBUG:
            origins.add(f'http://{clean}')

    return sorted(origins)


CSRF_TRUSTED_ORIGINS = _build_csrf_trusted_origins()

# Render всегда за HTTPS reverse proxy — нужно даже при DEBUG=True.
if IS_RENDER or not DEBUG:
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# Безопасность для production
if not DEBUG:
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_BROWSER_XSS_FILTER = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = 'DENY'
elif IS_RENDER:
    # DEBUG на Render: cookies всё равно secure (сайт только по HTTPS).
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True

# Cookies: Lax помогает формам login/POST за HTTPS на Render.
SESSION_COOKIE_SAMESITE = 'Lax'
CSRF_COOKIE_SAMESITE = 'Lax'
CSRF_COOKIE_PATH = '/'

# Логирование причин CSRF (видно в Render Logs).
CSRF_FAILURE_VIEW = 'main.views.csrf_failure_view'

# --- УСТАНОВЛЕННЫЕ ПРИЛОЖЕНИЯ ---
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'main',
]

# --- МИДЛВАР ---
MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'main.middleware.RenderCsrfMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'main.middleware.AccessAuditMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'uniquest.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'uniquest.wsgi.application'

# --- БАЗА ДАННЫХ ---
# Для локальной разработки по умолчанию используем SQLite.
# PostgreSQL включается через DATABASE_URL или явные DB_* переменные.
USE_SQLITE = os.environ.get('USE_SQLITE', 'False').lower() in ('1', 'true', 'yes')
DB_URL = (os.environ.get('RENDER_DB_URL') or os.environ.get('DATABASE_URL') or '').strip()

if USE_SQLITE:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }
elif DB_URL:
    try:
        import dj_database_url
        DATABASES = {'default': dj_database_url.config(
            default=DB_URL,
            conn_max_age=600,
            conn_health_checks=True,
        )}
        db_host = _normalize_render_db_host(DATABASES['default'].get('HOST', ''))
        DATABASES['default']['HOST'] = db_host
        _validate_render_db_host(db_host, 'RENDER_DB_URL/DATABASE_URL')
        _apply_db_ssl_options(DATABASES['default'])
    except ImportError:
        # Если dj-database-url не установлен, парсим вручную
        import urllib.parse
        if DB_URL:
            parsed = urllib.parse.urlparse(DB_URL)
            DATABASES = {'default': {
                'ENGINE': 'django.db.backends.postgresql',
                'NAME': parsed.path[1:] if parsed.path.startswith('/') else parsed.path,
                'USER': parsed.username,
                'PASSWORD': parsed.password,
                'HOST': _normalize_render_db_host(parsed.hostname),
                'PORT': parsed.port or '5432',
                'OPTIONS': {
                    'connect_timeout': 10,
                },
                'CONN_MAX_AGE': 600,
            }}
            _validate_render_db_host(DATABASES['default']['HOST'], 'RENDER_DB_URL/DATABASE_URL')
            _apply_db_ssl_options(DATABASES['default'])
        else:
            DATABASES = {
                'default': {
                    'ENGINE': 'django.db.backends.sqlite3',
                    'NAME': BASE_DIR / 'db.sqlite3',
                }
            }
else:
    db_host = _normalize_render_db_host(os.environ.get('DB_HOST', ''))
    db_password = os.environ.get('DB_PASSWORD', '')

    if db_password or _is_remote_db_host(db_host):
        DATABASES = {
            'default': {
                'ENGINE': 'django.db.backends.postgresql',
                'NAME': os.environ.get('DB_NAME', 'uniquestus'),
                'USER': os.environ.get('DB_USER', 'uniquest_user'),
                'PASSWORD': db_password,
                'HOST': db_host or 'localhost',
                'PORT': os.environ.get('DB_PORT', '5432'),
                'OPTIONS': {
                    'connect_timeout': 10,
                },
                'CONN_MAX_AGE': 600,
            }
        }
        _validate_render_db_host(db_host, 'DB_HOST')
        _apply_db_ssl_options(DATABASES['default'])
    else:
        DATABASES = {
            'default': {
                'ENGINE': 'django.db.backends.sqlite3',
                'NAME': BASE_DIR / 'db.sqlite3',
            }
        }

# --- ПАРОЛИ ---
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# --- ЛОКАЛЬНЫЕ НАСТРОЙКИ ---
LANGUAGE_CODE = 'ru-ru'
TIME_ZONE = 'Asia/Almaty'
USE_I18N = True
USE_L10N = True
USE_TZ = True

# --- СТАТИКА ---
STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'
# Для Render safer-режим: без strict manifest, чтобы исключить 500 на статику
STATICFILES_STORAGE = 'whitenoise.storage.CompressedStaticFilesStorage'

# --- МЕДИА ---
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# --- AUTH URLS ---
# Чтобы @login_required не уводил на /accounts/login/ (которого нет в проекте)
LOGIN_URL = '/login/'
LOGIN_REDIRECT_URL = '/dashboard/'
LOGOUT_REDIRECT_URL = '/'

# --- LOGGING ---
# Явно выводим traceback'и 500 в stdout/stderr, чтобы видеть их в Render Logs.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
        },
    },
    "loggers": {
        "django.request": {
            "handlers": ["console"],
            "level": "ERROR",
            "propagate": False,
        },
        "main": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": True,
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "WARNING",
    },
}
