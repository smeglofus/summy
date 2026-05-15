"""
Django settings for core project.
"""
from pathlib import Path

from environs import Env

env = Env()
env.read_env()

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = env.str(
    "DJANGO_SECRET_KEY",
    "django-insecure-mv0kt+(+=$5xn*pu1p#lk16zrl^pz5zdj8@(ah%glr=gz=nm9m",
)
DEBUG = env.bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", ["*"])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "integrator",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "core.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "core.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env.str("POSTGRES_DB", "symmy_task"),
        "USER": env.str("POSTGRES_USER", "postgres"),
        "PASSWORD": env.str("POSTGRES_PASSWORD", "postgres"),
        "HOST": env.str("POSTGRES_HOST", "db"),
        "PORT": env.str("POSTGRES_PORT", "5432"),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Cache ----------------------------------------------------------------
# Cache remains available for general use and as a best-effort lock fallback in
# non-Postgres environments. The production singleton sync lock uses Postgres
# advisory locking; see integrator.sync_lock.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": env.str("REDIS_CACHE_URL", "redis://redis:6379/2"),
    }
}

# --- Celery ---------------------------------------------------------------
CELERY_BROKER_URL = env.str("CELERY_BROKER_URL", "redis://redis:6379/0")
CELERY_RESULT_BACKEND = env.str("CELERY_RESULT_BACKEND", "redis://redis:6379/1")
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_ACKS_LATE = True
ERP_SYNC_INTERVAL_SECONDS = env.float("ERP_SYNC_INTERVAL_SECONDS", 300.0)
CELERY_BEAT_SCHEDULE = {
    "erp-to-eshop-sync": {
        "task": "integrator.tasks.sync_erp_to_eshop",
        "schedule": ERP_SYNC_INTERVAL_SECONDS,
    },
}

# --- Integrator -----------------------------------------------------------
ERP_DATA_PATH = BASE_DIR / env.str("ERP_DATA_FILENAME", "erp_data.json")
VAT_RATE = env.str("VAT_RATE", "0.21")

ESHOP_API_BASE_URL = env.str("ESHOP_API_BASE_URL", "https://api.fake-eshop.cz/v1")
ESHOP_API_KEY = env.str("ESHOP_API_KEY", "symma-secret-token")
ESHOP_RATE_LIMIT_PER_SEC = env.int("ESHOP_RATE_LIMIT_PER_SEC", 5)
ESHOP_MAX_RETRIES = env.int("ESHOP_MAX_RETRIES", 5)
ESHOP_REQUEST_TIMEOUT = env.float("ESHOP_REQUEST_TIMEOUT", 10.0)

# --- Logging --------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {"format": "%(asctime)s %(levelname)s %(name)s | %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "simple"},
    },
    "loggers": {
        "integrator": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}
