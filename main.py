import os
import re
import json
import logging
import asyncio
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple
from urllib.parse import quote_plus

# Сторонние библиотеки
import aiohttp
import requests
from bs4 import BeautifulSoup
from fake_useragent import UserAgent

# Telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler

# Ядра OSINT (должны быть в requirements.txt)
try:
    from sherlock import sherlock
except ImportError:
    sherlock = None

try:
    from maigret.maigret import search as maigret_search
except ImportError:
    maigret_search = None

try:
    from holehe.core import holehe
except ImportError:
    holehe = None

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("FakeSherlock")

# ----------------------------------------------------------------------
# КОНФИГУРАЦИЯ (все данные берутся из переменных окружения)
# ----------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("❌ Переменная окружения BOT_TOKEN не установлена! Без неё бот не запустится.")

# Опциональные API-ключи для расширенного функционала
DEHASHED_API_KEY = os.environ.get("DEHASHED_API_KEY")
LEAKCHECK_API_KEY = os.environ.get("LEAKCHECK_API_KEY")
HIBP_API_KEY = os.environ.get("HIBP_API_KEY")
PROXY_LIST = os.environ.get("PROXY_LIST")  # Прокси через запятую, например "http://1.1.1.1:8080,http://2.2.2.2:8080"

# ----------------------------------------------------------------------
# ВСПОМОГАТЕЛЬНЫЕ КЛАССЫ И ФУНКЦИИ
# ----------------------------------------------------------------------
ua = UserAgent()

class AsyncRequests:
    """Асинхронный HTTP-клиент с поддержкой прокси и повторных попыток."""

    def __init__(self):
        self.session = None
        self.proxies = []
        if PROXY_LIST:
            self.proxies = [p.strip() for p in PROXY_LIST.split(",") if p.strip()]

    async def init_session(self):
        if self.session is None:
            connector = aiohttp.TCPConnector(limit=100)
            timeout = aiohttp.ClientTimeout(total=30)
            self.session = aiohttp.ClientSession(connector=connector, timeout=timeout)
        return self.session

    async def close(self):
        if self.session:
            await self.session.close()
            self.session = None

    async def get(self, url: str, headers: dict = None, proxy: str = None) -> Optional[str]:
        await self.init_session()
        if headers is None:
            headers = {"User-Agent": ua.random}
        try:
            async with self.session.get(url, headers=headers, proxy=proxy, ssl=False) as resp:
                if resp.status == 200:
                    return await resp.text()
                else:
                    logger.warning(f"HTTP {resp.status} для {url}")
        except Exception as e:
            logger.error(f"Ошибка при получении {url}: {e}")
        return None

    async def post(self, url: str, data: dict = None, json_data: dict = None, headers: dict = None) -> Optional[dict]:
        await self.init_session()
        if headers is None:
            headers = {"User-Agent": ua.random}
        try:
            async with self.session.post(url, data=data, json=json_data, headers=headers, ssl=False) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    logger.warning(f"HTTP {resp.status} для {url}")
        except Exception as e:
            logger.error(f"Ошибка при запросе к {url}: {e}")
        return None

async_requests = AsyncRequests()

# ----------------------------------------------------------------------
# МОДУЛИ СБОРА ДАННЫХ
# ----------------------------------------------------------------------

async def search_sherlock(username: str) -> Dict[str, str]:
    """Поиск по 400+ соцсетям через Sherlock (если установлен)."""
    if sherlock is None:
        return {}
    loop = asyncio.get_running_loop()
    try:
        results = await loop.run_in_executor(None, lambda: sherlock(username, verbose=False, print_all=False))
        if results:
            return {site: url for site, url in results.items() if url}
    except Exception as e:
        logger.error(f"Ошибка Sherlock для {username}: {e}")
    return {}

async def search_maigret(username: str) -> Dict[str, Any]:
    """Расширенный поиск по 3000+ сайтам через Maigret (если установлен)."""
    if maigret_search is None:
        return {}
    loop = asyncio.get_running_loop()
    try:
        # Запускаем Maigret с базовыми настройками
        results = await loop.run_in_executor(None, lambda: maigret_search(username))
        if results and isinstance(results, dict):
            return results
    except Exception as e:
        logger.error(f"Ошибка Maigret для {username}: {e}")
    return {}

async def check_holehe(email: str) -> Dict[str, bool]:
    """Проверка наличия email на 120+ платформах через Holehe."""
    if holehe is None:
        return {}
    loop = asyncio.get_running_loop()
    try:
        # Holehe возвращает словарь {service: True/False}
        results = await loop.run_in_executor(None, lambda: holehe(email))
        if results and isinstance(results, dict):
            return results
    except Exception as e:
        logger.error(f"Ошибка Holehe для {email}: {e}")
    return {}

async def find_potential_emails(username: str) -> List[str]:
    """Пытается найти возможные email по нику через утечки и API."""
    emails = []
    # Проверка через DeHashed API
    if DEHASHED_API_KEY:
        headers = {"Authorization": f"Bearer {DEHASHED_API_KEY}"}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"https://api.dehashed.com/search?query=username:{username}", headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("entries"):
                            for entry in data["entries"]:
                                if entry.get("email"):
                                    emails.append(entry["email"])
        except Exception as e:
            logger.error(f"Ошибка DeHashed API: {e}")

    # Проверка через LeakCheck API
    if LEAKCHECK_API_KEY:
        headers = {"X-API-Key": LEAKCHECK_API_KEY}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"https://leakcheck.io/api/v2/query/{username}", headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("success") and data.get("result"):
                            for entry in data["result"]:
                                if entry.get("email"):
                                    emails.append(entry["email"])
        except Exception as e:
            logger.error(f"Ошибка LeakCheck API: {e}")

    # Простейший перебор популярных почтовых сервисов (можно расширить)
    common_domains = ["gmail.com", "yahoo.com", "outlook.com", "protonmail.com", "mail.ru", "bk.ru", "inbox.ru", "list.ru"]
    for domain in common_domains:
        emails.append(f"{username}@{domain}")

    return list(set(emails))  # Убираем дубликаты

async def check_email_breaches(email: str) -> List[str]:
    """Проверка email через Have I Been Pwned API (если есть ключ)."""
    breaches = []
    if not HIBP_API_KEY:
        return breaches
    headers = {"hibp-api-key": HIBP_API_KEY, "user-agent": "FakeSherlockBot"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://haveibeenpwned.com/api/v3/breachedaccount/{email}", headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if isinstance(data, list):
                        breaches = [b["Name"] for b in data]
    except Exception as e:
        logger.error(f"Ошибка HIBP API: {e}")
    return breaches

async def search_telegram_channels(username: str) -> List[str]:
    """Поиск упоминаний username в публичных Telegram-каналах через различные API."""
    channels = []
    # Используем Telesco.pe API (публичный)
    try:
        url = f"https://telesco.pe/search?q={quote_plus(username)}"
        html = await async_requests.get(url)
        if html:
            soup = BeautifulSoup(html, 'html.parser')
            for link in soup.select("a[href*='t.me']"):
                href = link.get('href')
                if href and 't.me' in href:
                    channels.append(href)
    except Exception as e:
        logger.error(f"Ошибка поиска каналов: {e}")

    # Альтернативный поиск через Tgstat
    try:
        url = f"https://tgstat.ru/search?q={quote_plus(username)}"
        html = await async_requests.get(url)
        if html:
            soup = BeautifulSoup(html, 'html.parser')
            for link in soup.select("a[href*='t.me']"):
                href = link.get('href')
                if href and 't.me' in href:
                    channels.append(href)
    except Exception as e:
        logger.error(f"Ошибка поиска каналов Tgstat: {e}")

    return list(set(channels))[:20]  # Ограничиваем до 20 результатов

# ----------------------------------------------------------------------
# ФОРМАТИРОВАНИЕ ОТЧЁТОВ
# ----------------------------------------------------------------------

def format_sherlock_report(results: Dict[str, str]) -> str:
    """Форматирует результаты Sherlock в читаемый текст."""
    if not results:
        return ""

    lines = ["**🌐 Найденные профили (Sherlock):**"]
    # Группируем по категориям
    categories = {
        "📱 Соцсети": ["instagram", "twitter", "facebook", "vk", "tiktok", "snapchat", "youtube", "reddit", "pinterest", "tumblr"],
        "💻 Разработка": ["github", "gitlab", "bitbucket", "stackoverflow", "codepen"],
        "🎮 Игры": ["steam", "xbox", "playstation", "twitch", "discord"],
        "💰 Финансы": ["paypal", "cashapp", "venmo"],
        "🔞 Adult": ["onlyfans", "fansly", "adultfriendfinder"],
    }

    categorized = {cat: [] for cat in categories}
    other_sites = []

    for site, url in results.items():
        placed = False
        for cat, keywords in categories.items():
            if any(kw in site.lower() for kw in keywords):
                categorized[cat].append(f"[{site}]({url})")
                placed = True
                break
        if not placed:
            other_sites.append(f"[{site}]({url})")

    for cat, items in categorized.items():
        if items:
            lines.append(f"\n{cat}:")
            lines.extend([f"  • {item}" for item in items])

    if other_sites:
        lines.append(f"\n**📎 Прочее:**")
        lines.extend([f"  • {item}" for item in other_sites])

    return "\n".join(lines)

def format_holehe_report(holehe_results: Dict[str, bool]) -> str:
    """Форматирует результаты Holehe."""
    if not holehe_results:
        return ""

    found = [service for service, exists in holehe_results.items() if exists]
    not_found_count = sum(1 for exists in holehe_results.values() if not exists)

    if not found:
        return "\n**📧 Почтовые сервисы:** Аккаунтов не найдено."

    lines = [f"\n**📧 Почтовые сервисы (найдено {len(found)}, не найдено {not_found_count}):**"]
    # Показываем только найденные, для экономии места
    for service in found[:30]:  # Ограничиваем 30 для читаемости
        lines.append(f"  ✅ {service}")
    if len(found) > 30:
        lines.append(f"  ... и ещё {len(found) - 30}")

    return "\n".join(lines)

def build_telegram_message(username: str, report_data: dict) -> Tuple[str, Optional[InlineKeyboardMarkup]]:
    """Собирает итоговое сообщение для отправки в Telegram."""
    sections = []

    # 1. Заголовок
    sections.append(f"⚡️ **ДОСЬЕ на @{username}** ⚡️\n")

    # 2. Социальные сети (Sherlock)
    if report_data.get("sherlock"):
        sherlock_text = format_sherlock_report(report_data["sherlock"])
        if sherlock_text:
            sections.append(sherlock_text)

    # 3. Результаты Maigret (если есть)
    if report_data.get("maigret"):
        # Maigret может содержать очень много данных, выводим краткую сводку
        maigret_data = report_data["maigret"]
        if isinstance(maigret_data, dict):
            sites_found = maigret_data.get("sites", [])
            if sites_found:
                sections.append(f"\n**🔎 Maigret (расширенный поиск):** Найдено {len(sites_found)} сайтов.")

    # 4. Emails и их проверка
    emails = report_data.get("emails", [])
    if emails:
        sections.append(f"\n**✉️ Найденные email:** {', '.join(emails[:5])}")
        if len(emails) > 5:
            sections.append(f"_... и ещё {len(emails) - 5}_")

    # 5. Утечки (HIBP)
    if report_data.get("breaches"):
        breaches = report_data["breaches"]
        sections.append(f"\n**💀 Утечки (Have I Been Pwned):**")
        sections.extend([f"  • {b}" for b in breaches[:10]])
        if len(breaches) > 10:
            sections.append(f"  ... и ещё {len(breaches) - 10}")

    # 6. Упоминания в Telegram-каналах
    channels = report_data.get("channels", [])
    if channels:
        sections.append(f"\n**📢 Найден в Telegram-каналах:**")
        sections.extend([f"  • {ch}" for ch in channels[:10]])

    # 7. Данные из DeHashed/LeakCheck (если были получены)
    if report_data.get("dehashed_count"):
        sections.append(f"\n**🔓 DeHashed:** Найдено {report_data['dehashed_count']} записей.")

    # 8. Футер
    sections.append(f"\n🔍 _Данные собраны {datetime.now().strftime('%d.%m.%Y %H:%M')}_")

    text = "\n".join(sections)

    # Создаём кнопки для детальной информации, если она слишком большая
    buttons = []
    if report_data.get("sherlock") and len(str(report_data["sherlock"])) > 400:
        buttons.append([InlineKeyboardButton("🌐 Все соцсети (подробно)", callback_data="det_sherlock")])
    if report_data.get("emails") and len(report_data["emails"]) > 5:
        buttons.append([InlineKeyboardButton("✉️ Все email", callback_data="det_emails")])
    if report_data.get("breaches") and len(report_data["breaches"]) > 10:
        buttons.append([InlineKeyboardButton("💀 Все утечки", callback_data="det_breaches")])

    keyboard = InlineKeyboardMarkup(buttons) if buttons else None

    # Telegram имеет лимит в 4096 символов на сообщение
    if len(text) > 4000:
        text = text[:4000] + "\n\n... (сообщение обрезано из-за лимита Telegram)"

    return text, keyboard

# ----------------------------------------------------------------------
# ОБРАБОТЧИКИ КОМАНД
# ----------------------------------------------------------------------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start."""
    await update.message.reply_text(
        "👋 Я **Fake Sherlock** — твой персональный OSINT-детектив.\n\n"
        "Отправь мне команду:\n"
        "`/sherlock @username` — для поиска информации по нику\n"
        "`/email user@example.com` — для проверки email\n\n"
        "Я пробью профили в соцсетях, найду утечки и упоминания. Всё, что доступно в открытых источниках.",
        parse_mode='Markdown'
    )

async def sherlock_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Основная команда для поиска по username."""
    message = update.message
    if not message or not message.text:
        return

    parts = message.text.strip().split()
    if len(parts) < 2:
        await message.reply_text("❌ Укажите юзернейм: `/sherlock @username`", parse_mode='Markdown')
        return

    raw_username = parts[1].replace('@', '').strip()
    # Очистка от лишних символов
    raw_username = re.sub(r'[^\w\d._-]', '', raw_username)

    if not raw_username:
        await message.reply_text("❌ Некорректный юзернейм.")
        return

    status_msg = await message.reply_text(f"🔎 Ищу информацию по **{raw_username}**... Это может занять до минуты.")

    report = {}

    # Параллельный запуск всех сборщиков
    tasks = []
    tasks.append(asyncio.create_task(search_sherlock(raw_username)))
    tasks.append(asyncio.create_task(search_maigret(raw_username)))
    tasks.append(asyncio.create_task(find_potential_emails(raw_username)))
    tasks.append(asyncio.create_task(search_telegram_channels(raw_username)))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    sherlock_results, maigret_results, emails, channels = results

    if isinstance(sherlock_results, dict) and sherlock_results:
        report["sherlock"] = sherlock_results
    if isinstance(maigret_results, dict) and maigret_results:
        report["maigret"] = maigret_results
    if isinstance(emails, list) and emails:
        report["emails"] = emails
        # Для первого найденного email проверяем утечки
        if emails and HIBP_API_KEY:
            breaches = await check_email_breaches(emails[0])
            if breaches:
                report["breaches"] = breaches
    if isinstance(channels, list) and channels:
        report["channels"] = channels

    # Удаляем статусное сообщение
    await status_msg.delete()

    if not any([report.get("sherlock"), report.get("maigret"), report.get("emails"), report.get("channels")]):
        await message.reply_text(f"🤷‍♂️ По нику **{raw_username}** ничего не найдено. Либо пользователь очень скрытный, либо ник не используется.")
        return

    text, keyboard = build_telegram_message(raw_username, report)
    await message.reply_text(text, parse_mode='Markdown', disable_web_page_preview=True, reply_markup=keyboard)

async def email_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Поиск по email."""
    message = update.message
    if not message or not message.text:
        return

    parts = message.text.strip().split()
    if len(parts) < 2:
        await message.reply_text("❌ Укажите email: `/email user@example.com`", parse_mode='Markdown')
        return

    email = parts[1].strip()

    if not re.match(r"[^@]+@[^@]+\.[^@]+", email):
        await message.reply_text("❌ Некорректный email.")
        return

    status_msg = await message.reply_text(f"📧 Проверяю **{email}**...")

    tasks = []
    tasks.append(asyncio.create_task(check_holehe(email)))
    tasks.append(asyncio.create_task(check_email_breaches(email)))

    holehe_results, breaches = await asyncio.gather(*tasks, return_exceptions=True)

    await status_msg.delete()

    text = f"⚡️ **ДОСЬЕ на email {email}** ⚡️\n"

    if isinstance(holehe_results, dict) and holehe_results:
        text += format_holehe_report(holehe_results)

    if isinstance(breaches, list) and breaches:
        text += f"\n\n**💀 Утечки:**\n"
        text += "\n".join([f"  • {b}" for b in breaches])

    if not any([isinstance(holehe_results, dict) and holehe_results, isinstance(breaches, list) and breaches]):
        text += "\n🤷‍♂️ Никакой информации не найдено."

    await message.reply_text(text, parse_mode='Markdown', disable_web_page_preview=True)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик нажатий на инлайн-кнопки."""
    query = update.callback_query
    await query.answer()

    # Здесь можно реализовать выдачу детальной информации по запросу
    if query.data == "det_sherlock":
        await query.message.reply_text("🔍 Детальная информация по соцсетям будет доступна в следующем обновлении.")
    elif query.data == "det_emails":
        await query.message.reply_text("✉️ Полный список email пока в разработке.")
    elif query.data == "det_breaches":
        await query.message.reply_text("💀 Полный список утечек пока в разработке.")

# ----------------------------------------------------------------------
# ТОЧКА ВХОДА
# ----------------------------------------------------------------------

def main():
    """Запуск бота."""
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN не задан. Бот не может быть запущен.")
        return

    app = Application.builder().token(BOT_TOKEN).build()

    # Регистрация обработчиков
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("sherlock", sherlock_command))
    app.add_handler(CommandHandler("email", email_command))
    app.add_handler(CallbackQueryHandler(button_handler))

    logger.info("Fake Sherlock запущен и готов к работе!")
    app.run_polling()

if __name__ == "__main__":
    main()