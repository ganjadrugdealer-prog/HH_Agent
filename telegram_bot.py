# -*- coding: utf-8 -*-
"""Интеграция Telegram-бота в HH Agent.

Этот модуль работает в фоне основного приложения и принимает команды 
через pyTelegramBotAPI. Он обращается к AppApi и engine.py для 
выполнения действий (отклики, поднятие резюме и т.д.).
"""
import threading
import time
import re
import telebot
from telebot import types
import logging

import engine
import stopwords

logger = logging.getLogger("hh_agent.telegram_bot")

def html_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def generate_salary_stats(tool) -> str:
    try:
        conn = tool.storage.vacancies.conn
        query = "SELECT name, salary_from, alternate_url, area_name FROM vacancies WHERE salary_from > 0 ORDER BY salary_from DESC LIMIT 15"
        rows = conn.execute(query).fetchall()
        if not rows:
            return "❌ Нет данных о вакансиях в базе."
        msg = "🏆 <b>Топ-15 вакансий по ЗП:</b>\n\n"
        for row in rows:
            title_safe = html_escape(str(row[0])[:35] + ("..." if len(str(row[0])) > 35 else ""))
            msg += f"🔹 <b>{int(row[1]):,} руб.</b> | {html_escape(row[3])}\n   <a href='{row[2]}'>{title_safe}</a>\n\n"
        return msg
    except Exception as e:
        logger.exception("generate_salary_stats")
        return f"❌ Ошибка: {e}"

def generate_fresh_vacs(tool) -> str:
    try:
        conn = tool.storage.vacancies.conn
        query = "SELECT name, salary_from, alternate_url, area_name FROM vacancies ORDER BY published_at DESC LIMIT 15"
        rows = conn.execute(query).fetchall()
        if not rows:
            return "❌ Нет данных о вакансиях в базе."
        msg = "🆕 <b>Последние 15 вакансий:</b>\n\n"
        for row in rows:
            title_safe = html_escape(str(row[0])[:35] + ("..." if len(str(row[0])) > 35 else ""))
            sal_str = f"{int(row[1]):,} руб." if row[1] and row[1] > 0 else "з/п не указ."
            msg += f"🔹 <b>{sal_str}</b> | {html_escape(row[3])}\n   <a href='{row[2]}'>{title_safe}</a>\n\n"
        return msg
    except Exception as e:
        logger.exception("generate_fresh_vacs")
        return f"❌ Ошибка: {e}"

def check_whoami(api) -> str:
    try:
        status = api.get_status()
        if not status.get("authorized"):
            return "👤 <b>Статус:</b> Не авторизован!"
        
        user = status.get("user")
        if not user:
            return "👤 <b>Статус:</b> Нет данных о профиле."
            
        res = f"👤 <b>Статус:</b>\n\n"
        res += f"<b>{html_escape(user.get('first_name', ''))} {html_escape(user.get('last_name', ''))}</b>\n"
        res += f"Email: {html_escape(user.get('email', ''))}\n"
        res += f"ID: <code>{user.get('id', '')}</code>\n"
        return res
    except Exception as e:
        logger.exception("check_whoami")
        return f"❌ Ошибка профиля: {e}"

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
    
    def check_user(message):
        return message.chat.id == allowed_chat_id

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

    @bot.message_handler(func=lambda m: check_user(m) and (m.text.startswith('+') or m.text.startswith('-')))
    def handle_stop_words(message):
        text = message.text.strip()
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

    @bot.callback_query_handler(func=lambda call: check_user(call.message))
    def handle_query(call):
        bot.answer_callback_query(call.id)
        chat_id = call.message.chat.id
        
        def run_action():
            try:
                if call.data == "run_all":
                    bot.send_message(chat_id, "⏳ Запускаю: Полный цикл (Отклики + Отказы)...")
                    api.start_apply()
                    api.clear_rejections()
                    bot.send_message(chat_id, "✅ Полный цикл завершен!")
                elif call.data == "run_apply":
                    bot.send_message(chat_id, "⏳ Запускаю: Только отклики...")
                    api.start_apply()
                    bot.send_message(chat_id, "✅ Отклики завершены!")
                elif call.data == "run_ping":
                    bot.send_message(chat_id, "⏳ Запускаю: Пинги...")
                    res = api.send_pings()
                    bot.send_message(chat_id, f"✅ Пинги завершены! {res.get('message', '')}")
                elif call.data == "run_clear":
                    bot.send_message(chat_id, "⏳ Запускаю: Очистка отказов...")
                    res = api.clear_rejections()
                    bot.send_message(chat_id, f"✅ Очистка завершена! {res.get('message', '')}")
                elif call.data == "stop_all":
                    if engine.cancel_run():
                        bot.send_message(chat_id, "🛑 <b>Процесс останавливается...</b>", parse_mode="HTML")
                    else:
                        bot.send_message(chat_id, "Нет активных процессов для остановки.")
                elif call.data == "raise_resume":
                    bot.send_message(chat_id, "⏳ Запускаю: Поднятие...")
                    res = api.bump_resumes()
                    bot.send_message(chat_id, f"✅ Поднятие: {res.get('message', '')}")
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
                bot.send_message(chat_id, f"❌ Ошибка выполнения: {e}")

        threading.Thread(target=run_action, daemon=True).start()

    def polling_worker():
        try:
            bot.set_my_commands([
                telebot.types.BotCommand("/menu", "🎛 Открыть панель управления"),
                telebot.types.BotCommand("/help", "📖 Инструкция по использованию"),
                telebot.types.BotCommand("/start", "🚀 Перезапуск бота")
            ])
            logger.info("Telegram bot started successfully.")
            bot.polling(none_stop=True, timeout=90, long_polling_timeout=90)
        except Exception as e:
            logger.error(f"Telegram bot polling error: {e}")
            
    # Запускаем поллинг в отдельном потоке
    t = threading.Thread(target=polling_worker, daemon=True, name="TelegramBotPolling")
    t.start()
