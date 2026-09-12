import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.update(
    {
        "DEBUG": "True",
        "DATA_DIR": str(ROOT / ".local/data/tests"),
        "ENV_PATH": str(ROOT / ".local/test.env"),
        "ALLOWED_HOSTS": "localhost,127.0.0.1,testserver",
        "SECRET_KEY": "isolated-test-key-not-for-production",
        "DATABASE_URL": os.environ.get("TEST_DATABASE_URL", "sqlite://:memory:"),
        "EMAIL_URL": "memorymail://",
        "CACHE_URL": "locmemcache://",
        "DEFAULT_FROM_EMAIL": "noreply@example.invalid",
        "SERVER_EMAIL": "server@example.invalid",
        "ADMINS": "Test Admin <admin@example.invalid>",
        "SITE_URL": "http://testserver",
    }
)

from ephios.settings import *

LANGUAGE_CODE = "en"
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
LOGGING = {"version": 1, "disable_existing_loggers": False}
COMPRESS_ENABLED = False
COMPRESS_PRECOMPILERS = []
