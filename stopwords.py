# -*- coding: utf-8 -*-
"""Стоп-слова: список человеческих слов -> регулярка для --excluded-filter.

Единственная реализация на весь проект. Ею пользуются и Telegram-бот
(через C:\\HH_SCAM\\build_filter.py), и десктопная апка.

Матчинг по КОРНЮ слова: пользователь пишет «продажи», блокируются
«продажам», «продаж», «продажник». Стемминг — Snowball (русский).
"""
from __future__ import annotations

import io
import re

try:
    from snowballstemmer import stemmer as _sb

    _RU = _sb("russian")

    def stem(word: str) -> str:
        if len(word) < 4 or not re.search(r"[а-яё]", word, re.IGNORECASE):
            return word
        return _RU.stemWord(word.lower())
except Exception:  # noqa: BLE001
    def stem(word: str) -> str:
        return word


def parse_words(raw: str) -> list[str]:
    """Разбирает пользовательский ввод: запятые, переводы строк, мусорные +/-."""
    words = [w.strip().lstrip("+-").strip().lower()
             for w in re.split(r"[,\n\r;]+", raw or "")]
    seen, out = set(), []
    for w in words:
        if w and w not in seen:
            seen.add(w)
            out.append(w)
    return out


def read_words(path: str) -> list[str]:
    return parse_words(io.open(path, encoding="utf-8").read())


def build_pattern(words: list[str]) -> str:
    parts = []
    for phrase in words:
        tokens = [re.escape(stem(t)) + r"\w*" for t in phrase.split()]
        parts.append(r"\b" + r"\s+".join(tokens))
    return "|".join(parts)


def looks_like_regex(text: str) -> bool:
    """Отличает готовую регулярку от списка слов через запятую."""
    return bool(re.search(r"\\b|\\w|\|", text or ""))


def to_pattern(text: str) -> str:
    """Главная точка входа: принимает что угодно, отдаёт валидную регулярку."""
    text = (text or "").strip()
    if not text:
        return ""
    if looks_like_regex(text):
        try:
            re.compile(text)
            return text
        except re.error:
            pass
    return build_pattern(parse_words(text))
