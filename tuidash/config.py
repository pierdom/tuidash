import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path.home() / ".config" / "tuidash" / ".env")
load_dotenv()  # also pick up a local .env


def get(key: str, default: str | None = None) -> str | None:
    return os.environ.get(key, default)


def require(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(f"Missing required env var: {key}")
    return val
