# -*- coding: utf-8 -*-
"""Интеграция Telegram-бота в HH Agent.

Этот модуль работает в фоне основного приложения и принимает команды 
через pyTelegramBotAPI. Он обращается к AppApi и engine.py для 
выполнения действий (отклики, поднятие резюме и т.д.).
"""
import logging
import threading
import time

import telebot
from telebot import types

import engine
import stopwords

logger = logging.getLogger("hh_agent.telegram_bot")

# Пауза перед повторным подключением после сетевой ошибки, секунды.
RECONNECT_DELAY = 15


def html_escape(text) -> str:
    """Экранирование для parse_mode=HTML. None и числа тоже принимаются."""
    if text is None:
        return ""
    return (str(text).replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def _money(value) -> str:
    try:
        amount = int(value)
    except (TypeError, ValueError):
        return "з/п не указана"
    if amount <= 0:
        return "з/п не указана"
    return f"{amount:,}".replace(",", " ") + " ₽"


def _title(value) -> str:
    text = str(value or "без названия")
    return html_escape(text[:35] + ("..." if len(text) > 35 else ""))


def _vacancy_lines(rows) -> str:
    out = []
    for name, salary, url, area in rows:
        head = _money(salary)
        where = html_escape(area) or "регион не указан"
        link = html_escape(url) or ""
        title = _title(name)
        out.append(f"🔹 <b>{head}</b> | {where}\n   "
                   + (f"<a href='{link}'>{title}</a>" if link else title))
    return "\n\n".join(out)


def generate_salary_stats(tool) -> str:
    try:
        conn = tool.storage.vacancies.conn
        rows = conn.execute(
            "SELECT name, salary_from, alternate_url, area_name FROM vacancies"
            " WHERE salary_from > 0 ORDER BY salary_from DESC LIMIT 15"
        ).fetchall()
        if not rows:
            return "❌ Нет данных о вакансиях в базе."
        return "🏆 <b>Топ-15 вакансий по ЗП:</b>\n\n" + _vacancy_lines(rows)
    except Exception as e:
        logger.exception("generate_salary_stats")
        return f"❌ Ошибка: {html_escape(e)}"


def generate_fresh_vacs(tool) -> str:
    try:
        conn = tool.storage.vacancies.conn
        rows = conn.execute(
            "SELECT name, salary_from, alternate_url, area_name FROM vacancies"
            " ORDER BY COALESCE(published_at, created_at) DESC LIMIT 15"
        ).fetchall()
        if not rows:
            return "❌ Нет данных о вакансиях в базе."
        return "🆕 <b>Последние 15 вакансий:</b>\n\n" + _vacancy_lines(rows)
    except Exception as e:
        logger.exception("generate_fresh_vacs")
        return f"❌ Ошибка: {html_escape(e)}"

def check_whoami(api) -> str:
    try:
        status = api.get_status()
        if not status.get("authorized"):
            return "👤 <b>Статус:</b> Не авторизован!"
        
        user = status.get("user")
        if not user:
            return "👤 <b>Статус:</b> Нет данных о профиле."
            
        res = "👤 <b>Статус:</b>\n\n"
        res += f"<b>{html_escape(user.get('first_name') or '')} {html_escape(user.get('last_name') or '')}</b>\n"
        res += f"Email: {html_escape(user.get('email') or '')}\n"
        res += f"ID: <code>{user.get('id') or ''}</code>\n"
        return res
    except Exception as e:
        logger.exception("check_whoami")
        return f"❌ Ошибка профиля: {html_escape(e)}"

def show_control_panel(api, bot, chat_id, text=None):
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton(text="🚀 Все и сразу", callback_data="run_all"),
        types.InlineKeyboardButton(text="🛑 ОСТАНОВИТЬ", callback_data="stop_all")
    )
    markup.add(
        types.InlineKeyboardButton(text="📩 Отклики", callback_data="run_apply"),
        types.InlineKeyboardButton(text="💬 Пинги", callback_data="run_ping"),
        types.InlineKeyboardButton(text="🧹 Отказы HH", callback_data="run_clear")
    )
    markup.add(
        types.InlineKeyboardButton(text="🔝 Поднять резюме", callback_data="raise_resume"),
        types.InlineKeyboardButton(text="👤 Профиль", callback_data="whoami")
    )
    markup.add(
        types.InlineKeyboardButton(text="📊 ЗП", callback_data="stats"),
        types.InlineKeyboardButton(text="🆕 Вакансии", callback_data="new_vacs")
    )
    
    if not text:
        s = api.search_settings()
        words = s.get("stop_words", "")
        text = (f"🎛 <b>Панель управления HH</b>\n\n"
                f"📋 <b>Стоп-слова:</b> <code>{html_escape(words) or 'нет'}</code>\n\n"
                f"💡 <i>Добавить: <code>+ слово</code> | Удалить: <code>- слово</code></i>")
                
    bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)


def start_bot_thread(api, tool):
    """Запускает бота в отдельном потоке (если настроен)."""
    cfg = tool.config
    tg = cfg.get("telegram") or {}
    
    bot_token = tg.get("bot_token", "").strip()
    chat_id_str = tg.get("chat_id", "").strip()
    enabled = tg.get("enabled", False)
    
    if not enabled or not bot_token or not chat_id_str:
        logger.info("Telegram бот отключен или не настроен.")
        return
        
    try:
        allowed_chat_id = int(chat_id_str)
    except ValueError:
        logger.error("Неверный формат chat_id в настройках.")
        return

    bot = telebot.TeleBot(bot_token)

    def check_user(message) -> bool:
        """Пускаем только владельца. Сообщение может отсутствовать (старый колбэк)."""
        try:
            return message is not None and message.chat.id == allowed_chat_id
        except AttributeError:
            return False

    def is_stopword_command(m) -> bool:
        # telebot и так пускает сюда только content_type == "text",
        # но читаем поле безопасно: фильтр вызывается без try/except.
        text = getattr(m, "text", None)
        return bool(check_user(m) and text and text[:1] in "+-")

    @bot.message_handler(commands=['help'])
    def send_help(message):
        if not check_user(message): return
        HELP_TEXT = (
            "📖 <b>Руководство пользователя HH Агента</b>\n\n"
            "Этот бот — твой автоматический комбайн для поиска работы.\n\n"
            "<b>ГЛАВНЫЕ КНОПКИ:</b>\n"
            "🚀 <b>Все и сразу</b> — рассылает отклики.\n"
            "🛑 <b>ОСТАНОВИТЬ</b> — экстренное торможение всех процессов.\n\n"
            "<b>ТОЧЕЧНЫЕ ЗАДАЧИ:</b>\n"
            "📩 <b>Отклики</b> — рассылка откликов по свежим вакансиям.\n"
            "💬 <b>Пинги</b> — Отправка напоминаний.\n"
            "🧹 <b>Отказы HH</b> — переносит отказы в архив.\n"
            "🔝 <b>Поднять резюме</b> — обновляет время публикации на HH.\n\n"
            "<b>НАСТРОЙКИ:</b>\n"
            "⛔ <b>Стоп-слова</b> — бот пропустит вакансию, если в её названии есть эти слова.\n"
            "<i>Добавить: <code>+ слово</code> | Удалить: <code>- слово</code></i>\n"
        )
        bot.send_message(message.chat.id, HELP_TEXT, parse_mode="HTML")

    @bot.message_handler(commands=['start', 'menu'])
    def cmd_menu(message):
        if not check_user(message): return
        show_control_panel(api, bot, message.chat.id)

    @bot.message_handler(func=is_stopword_command)
    def handle_stop_words(message):
        text = (message.text or "").strip()
        action, word = text[0], text[1:].strip().lower()
        if not word: return
        
        s = api.search_settings()
        words_str = s.get("stop_words", "")
        words = stopwords.parse_words(words_str) if words_str else []
        
        if action == '+' and word not in words:
            words.append(word)
            msg = f"✅ Добавлено: <code>{html_escape(word)}</code>"
        elif action == '-' and word in words:
            words.remove(word)
            msg = f"🗑 Удалено: <code>{html_escape(word)}</code>"
        else:
            return
            
        s["stop_words"] = ", ".join(words)
        api.save_search(s)
        show_control_panel(api, bot, message.chat.id, text=msg)

    def _report(res) -> str:
        """Результат вызова AppApi -> строка для чата."""
        if not isinstance(res, dict):
            return ""
        message = str(res.get("message") or "")
        if res.get("status") == "error":
            return "❌ " + (message or "не получилось")
        return message

    def _apply_report(res) -> str:
        """Итог рассылки: статус плюс счётчики из ленты прогона."""
        text = _report(res)
        if isinstance(res, dict) and res.get("status") == "error":
            return text
        try:
            counts = (api.run_state() or {}).get("counts") or {}
        except Exception:
            counts = {}
        parts = []
        if counts.get("applied"):
            parts.append(f"✅ отправлено: {counts['applied']}")
        if counts.get("filtered"):
            parts.append(f"🛑 отсеяно: {counts['filtered']}")
        if counts.get("error"):
            parts.append(f"❌ ошибок: {counts['error']}")
        if isinstance(res, dict) and res.get("status") == "cancelled":
            parts.insert(0, "остановлено вручную")
        return ", ".join(parts) or text or "нечего отправлять"

    @bot.callback_query_handler(func=lambda call: check_user(getattr(call, "message", None)))
    def handle_query(call):
        try:
            bot.answer_callback_query(call.id)
        except Exception:
            logger.debug("answer_callback_query", exc_info=True)
        chat_id = call.message.chat.id

        def run_action():
            try:
                if call.data == "run_all":
                    bot.send_message(chat_id, "⏳ Запускаю: Полный цикл (Отклики + Отказы)...")
                    applied = _apply_report(api.start_apply())
                    cleared = _report(api.clear_rejections())
                    bot.send_message(
                        chat_id,
                        "🏁 Полный цикл завершён.\n"
                        + "\n".join(x for x in (applied, cleared) if x),
                    )
                elif call.data == "run_apply":
                    bot.send_message(chat_id, "⏳ Запускаю: Только отклики...")
                    bot.send_message(
                        chat_id,
                        "🏁 Отклики завершены. " + _apply_report(api.start_apply()),
                    )
                elif call.data == "run_ping":
                    bot.send_message(chat_id, "⏳ Запускаю: Пинги...")
                    bot.send_message(chat_id, "🏁 Пинги завершены. " + _report(api.send_pings()))
                elif call.data == "run_clear":
                    bot.send_message(chat_id, "⏳ Запускаю: Очистка отказов...")
                    bot.send_message(chat_id, "🏁 Очистка завершена. " + _report(api.clear_rejections()))
                elif call.data == "stop_all":
                    stopped = False
                    if engine.cancel_run():
                        stopped = True
                    if getattr(api, "_cancel_event", None) is not None:
                        api.cancel_apply()
                        stopped = True
                        
                    if stopped:
                        bot.send_message(chat_id, "🛑 <b>Процесс останавливается...</b>", parse_mode="HTML")
                    else:
                        bot.send_message(chat_id, "Нет активных процессов для остановки.")
                elif call.data == "raise_resume":
                    bot.send_message(chat_id, "⏳ Запускаю: Поднятие...")
                    bot.send_message(chat_id, "🏁 Поднятие: " + _report(api.bump_resumes()))
                elif call.data == "whoami":
                    bot.send_message(chat_id, check_whoami(api), parse_mode="HTML")
                elif call.data == "stats":
                    bot.send_message(chat_id, generate_salary_stats(tool), parse_mode="HTML", disable_web_page_preview=True)
                    show_control_panel(api, bot, chat_id)
                elif call.data == "new_vacs":
                    bot.send_message(chat_id, generate_fresh_vacs(tool), parse_mode="HTML", disable_web_page_preview=True)
                    show_control_panel(api, bot, chat_id)
            except Exception as e:
                logger.exception("Telegram bot action failed")
                try:
                    bot.send_message(chat_id, f"❌ Ошибка выполнения: {html_escape(e)}")
                except Exception:
                    logger.debug("не смог сообщить об ошибке в чат", exc_info=True)

        threading.Thread(target=run_action, daemon=True).start()

    def polling_worker():
        # Имена команд пишем без слэша — так их описывает Bot API.
        # (Ведущий слэш сервер срезает сам, это не ошибка, просто мусор.)
        try:
            bot.set_my_commands([
                types.BotCommand("menu", "Открыть панель управления"),
                types.BotCommand("help", "Инструкция по использованию"),
                types.BotCommand("start", "Перезапуск бота"),
            ])
        except Exception:
            # Меню команд — украшение. Без него бот обязан работать.
            logger.warning("Не удалось задать меню команд", exc_info=True)

        logger.info("Telegram bot started successfully.")
        while True:
            try:
                bot.polling(none_stop=True, timeout=90, long_polling_timeout=90)
            except Exception as e:
                # Сеть отвалилась / VPN моргнул — ждём и подключаемся снова,
                # иначе бот молча умирает до перезапуска приложения.
                logger.warning("Telegram polling error: %s", e)
                time.sleep(RECONNECT_DELAY)
            else:
                logger.info("Telegram polling остановлен")
                return

    # Запускаем поллинг в отдельном потоке
    t = threading.Thread(target=polling_worker, daemon=True, name="TelegramBotPolling")
    t.start()
    return bot
