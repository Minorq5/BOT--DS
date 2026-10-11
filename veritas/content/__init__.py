"""Тексты для участников. Длинные тексты — в .md рядом, короткие — в texts.py."""

from pathlib import Path

CONTENT_DIR = Path(__file__).parent


def load(name: str) -> str:
    return (CONTENT_DIR / name).read_text(encoding="utf-8").strip()
