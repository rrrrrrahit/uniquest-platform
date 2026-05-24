import os
from pathlib import Path
from django.core.management.utils import get_random_secret_key
from dotenv import load_dotenv

# Загружаем .env
load_dotenv()


def _is_remote_db_host(host: str) -> bool:
    """True for non-local DB hosts where SSL is usually required."""
    return bool(host and host not in ("localhost", "127.0.0.1"))

# --- ПУТИ ---
BASE_DIR = Path(__file__).resolve().parent.parent

# --- БЕЗОПАСНОСТЬ ---
SECRET_KEY = os.environ.get('SECRET_KEY', get_random_secret_key())
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

    render_url = (os.environ.get('RENDER_EXTERNAL_URL') or '').strip().rstrip('/')
    if render_url.startswith('http'):
        origins.add(render_url)

    for host in ALLOWED_HOSTS:
        host = (host or '').strip()
        if not host or host == '*' or '*' in host:
            continue
        clean = host.lstrip('.')
        origins.add(f'https://{clean}')
        if DEBUG:
            origins.add(f'http://{clean}')

    return sorted(origins)


CSRF_TRUSTED_ORIGINS = _build_csrf_trusted_origins()

# Безопасность для production
if not DEBUG:
    # Корректно определяем HTTPS за reverse proxy (Render/Nginx и т.д.)
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_BROWSER_XSS_FILTER = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = 'DENY'

# Cookies: Lax помогает формам login/POST за HTTPS на Render.
SESSION_COOKIE_SAMESITE = 'Lax'
CSRF_COOKIE_SAMESITE = 'Lax'

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
    'django.middleware.csrf.CsrfViewMiddleware',
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

if USE_SQLITE:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }
elif 'DATABASE_URL' in os.environ:
    try:
        import dj_database_url
        DATABASES = {'default': dj_database_url.config(
            default=os.environ.get('DATABASE_URL'),
            conn_max_age=600,
            conn_health_checks=True,
        )}
        db_host = DATABASES['default'].get('HOST', '')
        if _is_remote_db_host(db_host):
            DATABASES['default'].setdefault('OPTIONS', {})
            DATABASES['default']['OPTIONS'].setdefault('sslmode', 'require')
    except ImportError:
        # Если dj-database-url не установлен, парсим вручную
        import urllib.parse
        db_url = os.environ.get('DATABASE_URL')
        if db_url:
            parsed = urllib.parse.urlparse(db_url)
            DATABASES = {'default': {
                'ENGINE': 'django.db.backends.postgresql',
                'NAME': parsed.path[1:] if parsed.path.startswith('/') else parsed.path,
                'USER': parsed.username,
                'PASSWORD': parsed.password,
                'HOST': parsed.hostname,
                'PORT': parsed.port or '5432',
                'OPTIONS': {
                    'connect_timeout': 10,
                },
                'CONN_MAX_AGE': 600,
            }}
        else:
            DATABASES = {
                'default': {
                    'ENGINE': 'django.db.backends.sqlite3',
                    'NAME': BASE_DIR / 'db.sqlite3',
                }
            }
else:
    db_host = os.environ.get('DB_HOST', '')
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
        if _is_remote_db_host(db_host):
            DATABASES['default']['OPTIONS']['sslmode'] = 'require'
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
