import os
import re
import logging
import asyncio
from typing import Dict, List, Any, Optional

import aiohttp
import requests
from bs4 import BeautifulSoup
from fake_useragent import UserAgent
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("FakeSherlock")

# ------------------------------------------------------------
# Конфигурация (все ключи из переменных окружения Railway)
# ------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
HIBP_API_KEY = os.environ.get("HIBP_API_KEY")           # Have I Been Pwned
DEHASHED_API_KEY = os.environ.get("DEHASHED_API_KEY")   # DeHashed
LEAKCHECK_API_KEY = os.environ.get("LEAKCHECK_API_KEY") # LeakCheck

if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN не найден!")

ua = UserAgent()

# ------------------------------------------------------------
# Асинхронный HTTP-клиент с прокси
# ------------------------------------------------------------
class HttpClient:
    def __init__(self):
        self.session = None

    async def init(self):
        if self.session is None:
            self.session = aiohttp.ClientSession()

    async def close(self):
        if self.session:
            await self.session.close()
            self.session = None

    async def get(self, url: str, headers: dict = None) -> Optional[str]:
        await self.init()
        if not headers:
            headers = {"User-Agent": ua.random}
        try:
            async with self.session.get(url, headers=headers, timeout=15) as resp:
                if resp.status == 200:
                    return await resp.text()
                logger.warning(f"HTTP {resp.status} для {url}")
        except Exception as e:
            logger.error(f"Ошибка GET {url}: {e}")
        return None

http = HttpClient()

# ------------------------------------------------------------
# Модули поиска
# ------------------------------------------------------------

async def search_sherlock(username: str) -> Dict[str, str]:
    """400+ сайтов через Sherlock (синхронный, в отдельном потоке)."""
    try:
        import sherlock
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda: sherlock.sherlock(username, verbose=False, print_all=False)
        )
        if isinstance(result, dict):
            return {k: v for k, v in result.items() if v}
    except Exception as e:
        logger.error(f"Sherlock провалился: {e}")
    return {}

async def search_socialscan(username: str) -> List[str]:
    """Дополнительные платформы через socialscan (легковесная утилита)."""
    try:
        import socialscan.util
        from socialscan.scan import scan
        # socialscan ожидает список запросов Query(username, platform)
        platforms = ["instagram", "twitter", "github", "pinterest", "reddit", "snapchat", "tumblr", "youtube"]
        queries = [socialscan.util.Query(username, platform) for platform in platforms]
        loop = asyncio.get_running_loop()
        results = await loop.run_in_executor(None, lambda: scan(queries))
        found = []
        for q, avail in results.items():
            if avail is False:  # False означает, что профиль существует (занят)
                found.append(q.platform.capitalize())
        return found
    except Exception as e:
        logger.error(f"Socialscan ошибка: {e}")
        return []

async def direct_check_services(username: str) -> Dict[str, str]:
    """Прямая проверка отдельных сервисов, если другие методы не сработали."""
    checks = {
        "GitHub": f"https://github.com/{username}",
        "Instagram": f"https://www.instagram.com/{username}/",
        "VK": f"https://vk.com/{username}",
        "Twitter": f"https://twitter.com/{username}",
        "Telegram": f"https://t.me/{username}",
        "Steam": f"https://steamcommunity.com/id/{username}",
    }
    results = {}
    for name, url in checks.items():
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers={"User-Agent": ua.random}, timeout=10) as resp:
                    if resp.status == 200:
                        results[name] = url
        except:
            pass
    return results

async def find_emails_from_username(username: str) -> List[str]:
    """Поиск email'ов через DeHashed и LeakCheck (требуются API-ключи)."""
    emails = []
    if DEHASHED_API_KEY:
        headers = {"Authorization": f"Bearer {DEHASHED_API_KEY}"}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"https://api.dehashed.com/search?query=username:{username}",
                    headers=headers, timeout=20
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for entry in data.get("entries", []):
                            if entry.get("email"):
                                emails.append(entry["email"])
        except Exception as e:
            logger.error(f"DeHashed error: {e}")
    if LEAKCHECK_API_KEY:
        headers = {"X-API-Key": LEAKCHECK_API_KEY}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"https://leakcheck.io/api/v2/query/{username}",
                    headers=headers, timeout=20
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("success") and data.get("result"):
                            for entry in data["result"]:
                                if entry.get("email"):
                                    emails.append(entry["email"])
        except Exception as e:
            logger.error(f"LeakCheck error: {e}")
    return list(set(emails))

async def check_holehe(email: str) -> Dict[str, bool]:
    """Проверка email через Holehe (на 120+ сервисах)."""
    try:
        import holehe
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, lambda: holehe.holehe(email))
        if isinstance(result, dict):
            return result
    except Exception as e:
        logger.error(f"Holehe ошибка: {e}")
    return {}

async def check_hibp(email: str) -> List[str]:
    """Утечки через Have I Been Pwned API."""
    if not HIBP_API_KEY:
        return []
    headers = {"hibp-api-key": HIBP_API_KEY, "user-agent": "FakeSherlockBot"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://haveibeenpwned.com/api/v3/breachedaccount/{email}",
                headers=headers, timeout=15
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return [b["Name"] for b in data] if isinstance(data, list) else []
    except Exception as e:
        logger.error(f"HIBP error: {e}")
    return []

async def search_telegram_mentions(username: str) -> List[str]:
    """Поиск упоминаний username в публичных каналах через Telesco.pe и Tgstat."""
    mentions = []
    try:
        html = await http.get(f"https://telesco.pe/search?q={username}")
        if html:
            soup = BeautifulSoup(html, 'html.parser')
            for a in soup.select("a[href*='t.me']"):
                href = a['href']
                if href and 't.me' in href:
                    mentions.append(href)
    except Exception as e:
        logger.error(f"Telesco.pe error: {e}")

    try:
        html = await http.get(f"https://tgstat.ru/search?q={username}")
        if html:
            soup = BeautifulSoup(html, 'html.parser')
            for a in soup.select("a[href*='t.me']"):
                href = a['href']
                if href and 't.me' in href:
                    mentions.append(href)
    except Exception as e:
        logger.error(f"Tgstat error: {e}")

    return list(set(mentions))[:15]

# ------------------------------------------------------------
# Форматирование результатов
# ------------------------------------------------------------

def build_response(username: str, results: dict) -> str:
    text = f"⚡️ **ДОСЬЕ на @{username}** ⚡️\n\n"

    # 1. Sherlock
    if "sherlock" in results and results["sherlock"]:
        text += "**🌐 Sherlock (соцсети):**\n"
        for site, url in list(results["sherlock"].items())[:20]:
            text += f"  • [{site}]({url})\n"
        if len(results["sherlock"]) > 20:
            text += f"  ... и ещё {len(results['sherlock']) - 20}\n"
        text += "\n"

    # 2. Socialscan
    if "socialscan" in results and results["socialscan"]:
        text += "**🔎 Socialscan (дополнительно):**\n"
        text += ", ".join(results["socialscan"]) + "\n\n"

    # 3. Прямые проверки
    if "direct" in results and results["direct"]:
        text += "**🔍 Прямые проверки:**\n"
        for name, url in results["direct"].items():
            text += f"  • [{name}]({url})\n"
        text += "\n"

    # 4. Email'ы из утечек
    if "emails" in results and results["emails"]:
        emails = results["emails"]
        text += f"**✉️ Найдены email'ы из утечек ({len(emails)}):**\n"
        for e in emails[:10]:
            text += f"  • `{e}`\n"
        if len(emails) > 10:
            text += f"  ... и ещё {len(emails) - 10}\n"
        text += "\n"

    # 5. Утечки HIBP (если нашлись, то добавляются при проверке первого email'а)
    if "hibp" in results and results["hibp"]:
        text += "**💀 Утечки Have I Been Pwned:**\n"
        for b in results["hibp"][:10]:
            text += f"  • {b}\n"
        if len(results["hibp"]) > 10:
            text += f"  ... и ещё {len(results['hibp']) - 10}\n"
        text += "\n"

    # 6. Упоминания в Telegram
    if "telegram_mentions" in results and results["telegram_mentions"]:
        text += "**📢 Найден в Telegram-каналах:**\n"
        for link in results["telegram_mentions"][:10]:
            text += f"  • {link}\n"
        if len(results["telegram_mentions"]) > 10:
            text += f"  ... и ещё {len(results['telegram_mentions']) - 10}\n"
        text += "\n"

    # 7. Email analysis (Holehe) - отдельно не добавляем, будет в команде /email
    if "holehe" in results and results["holehe"]:
        found_services = sum(1 for v in results["holehe"].values() if v)
        text += f"**📬 Holehe (для email):** найдено {found_services} сервисов.\n\n"

    if text.strip().endswith("⚡️ **ДОСЬЕ на @"):
        text += "🤷‍♂️ Абсолютно ничего не найдено."
    else:
        text += "🔍 _Поиск завершён. Никакой выдумки, только реальные данные._"
    return text

# ------------------------------------------------------------
# Команды бота
# ------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Я **Fake Sherlock** — OSINT-монстр.\n\n"
        "Использую:\n"
        "• Sherlock (400+ сайтов)\n"
        "• Socialscan (Instagram, GitHub, Snapchat и др.)\n"
        "• Прямые проверки VK, Steam, Twitter\n"
        "• DeHashed / LeakCheck (email из утечек)\n"
        "• Have I Been Pwned\n"
        "• Поиск упоминаний в Telegram\n\n"
        "Команды:\n"
        "/sherlock @username\n"
        "/email user@example.com"
    )

async def sherlock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return
    parts = msg.text.strip().split()
    if len(parts) < 2:
        await msg.reply_text("❌ Укажи юзернейм: /sherlock @username")
        return
    username = parts[1].lstrip('@')
    if not re.match(r'^[\w\.\_\-]{1,30}$', username):
        await msg.reply_text("❌ Некорректный username.")
        return

    status = await msg.reply_text(f"🔎 Ищу **{username}** во всех возможных местах...")

    # Параллельный запуск всех поисков
    tasks = {
        "sherlock": asyncio.create_task(search_sherlock(username)),
        "socialscan": asyncio.create_task(search_socialscan(username)),
        "direct": asyncio.create_task(direct_check_services(username)),
        "emails": asyncio.create_task(find_emails_from_username(username)),
        "telegram_mentions": asyncio.create_task(search_telegram_mentions(username)),
    }
    results = {}
    for key, task in tasks.items():
        try:
            results[key] = await task
        except Exception as e:
            logger.error(f"Ошибка в {key}: {e}")

    # Если найдены email'ы, проверяем первый через HIBP и Holehe
    if results.get("emails"):
        first_email = results["emails"][0]
        hibp_task = asyncio.create_task(check_hibp(first_email))
        holehe_task = asyncio.create_task(check_holehe(first_email))
        try:
            results["hibp"] = await hibp_task
        except:
            pass
        try:
            results["holehe"] = await holehe_task
        except:
            pass

    await status.delete()
    response = build_response(username, results)
    await msg.reply_text(response, parse_mode='Markdown', disable_web_page_preview=True)

async def email_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return
    parts = msg.text.strip().split()
    if len(parts) < 2:
        await msg.reply_text("❌ Укажи email: /email user@example.com")
        return
    email = parts[1]
    if "@" not in email:
        await msg.reply_text("❌ Некорректный email.")
        return

    status = await msg.reply_text(f"📧 Проверяю **{email}**...")

    holehe_task = asyncio.create_task(check_holehe(email))
    hibp_task = asyncio.create_task(check_hibp(email))

    holehe_result = await holehe_task
    hibp_result = await hibp_task

    await status.delete()

    text = f"⚡️ **ДОСЬЕ на email {email}** ⚡️\n\n"

    if holehe_result:
        found = [s for s, v in holehe_result.items() if v]
        not_found = len(holehe_result) - len(found)
        text += f"**📬 Holehe (проверка на сервисах):**\n"
        if found:
            text += "Найден на:\n" + "\n".join([f"  ✅ {s}" for s in found]) + "\n"
        else:
            text += "  ❌ Не найден ни на одном сервисе.\n"
        text += f"Всего проверено {len(holehe_result)} сервисов.\n\n"
    else:
        text += "**📬 Holehe:** не удалось выполнить проверку.\n\n"

    if hibp_result:
        text += f"**💀 Утечки (HIBP):**\n" + "\n".join([f"  • {b}" for b in hibp_result]) + "\n\n"
    elif HIBP_API_KEY:
        text += "**💀 Утечек не найдено.**\n\n"
    else:
        text += "**💀 HIBP:** укажи API-ключ в переменных окружения для проверки утечек.\n\n"

    await msg.reply_text(text, parse_mode='Markdown', disable_web_page_preview=True)

# ------------------------------------------------------------
# Точка входа
# ------------------------------------------------------------
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("sherlock", sherlock_cmd))
    app.add_handler(CommandHandler("email", email_cmd))
    logger.info("Fake Sherlock запущен и готов к работе!")
    app.run_polling()

if __name__ == "__main__":
    main()
