import os


os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://nano_app:test@127.0.0.1:5432/nanotateczki_test",
)
os.environ.setdefault("APP_ORIGIN", "https://app.example.test")
os.environ.setdefault("JWT_SECRET", "a" * 64)
os.environ.setdefault("ALLOW_REGISTRATION", "false")
os.environ.setdefault("DATA_PATH", "/tmp/nanotateczki-api-test")
