#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import asyncio
import hashlib
import time
import random
import string
import logging
from datetime import datetime, timedelta

# Telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, CallbackQueryHandler
)
from telegram.error import BadRequest

# База данных
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean, Text, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship

# Планировщик для очистки заданий
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ==================== КОНФИГУРАЦИЯ ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан в переменных окружения")

ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789"))
CURRENCY_NAME = "💷"
INITIAL_BALANCE = 500
MIN_REWARD = 10
MAX_REWARD = 500
REFERRAL_BONUS = 250
MAX_ACTIVE_TASKS = 5
TASK_DURATION_DAYS = 7
CODE_SECRET = "PROMO_CURRENCY_BOT_ZETA"

# База данных: Railway даст DATABASE_URL, иначе SQLite
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///promo_bot.db")

# ==================== МОДЕЛИ БАЗЫ ДАННЫХ ====================
Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, unique=True, nullable=False)
    username = Column(String)
    first_name = Column(String)
    balance = Column(Integer, default=INITIAL_BALANCE)
    join_date = Column(DateTime, default=datetime.now)
    referral_code = Column(String, unique=True, nullable=True)
    referred_by = Column(Integer, nullable=True)

    channels = relationship("Channel", back_populates="owner")
    tasks = relationship("Task", back_populates="creator")
    completions = relationship("TaskCompletion", back_populates="user")

class Channel(Base):
    __tablename__ = "channels"
    id = Column(Integer, primary_key=True)
    channel_id = Column(String, unique=True, nullable=False)
    channel_name = Column(String)
    owner_id = Column(Integer, ForeignKey("users.id"))
    invite_link = Column(String)
    category = Column(String, default="general")
    is_verified = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    added_date = Column(DateTime, default=datetime.now)

    owner = relationship("User", back_populates="channels")
    tasks = relationship("Task", back_populates="channel")

class Task(Base):
    __tablename__ = "tasks"
    id = Column(Integer, primary_key=True)
    channel_id = Column(String, ForeignKey("channels.channel_id"))
    creator_id = Column(Integer, ForeignKey("users.id"))
    reward = Column(Integer, nullable=False)
    max_completions = Column(Integer, default=50)
    current_completions = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    priority = Column(Integer, default=0)
    created_date = Column(DateTime, default=datetime.now)
    expiry_date = Column(DateTime, nullable=False)

    channel = relationship("Channel", back_populates="tasks")
    creator = relationship("User", back_populates="tasks")
    completions = relationship("TaskCompletion", back_populates="task")

class TaskCompletion(Base):
    __tablename__ = "task_completions"
    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey("tasks.id"))
    user_id = Column(Integer, ForeignKey("users.id"))
    completed_date = Column(DateTime, default=datetime.now)
    is_verified = Column(Boolean, default=False)

    task = relationship("Task", back_populates="completions")
    user = relationship("User", back_populates="completions")

class Transaction(Base):
    __tablename__ = "transactions"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    amount = Column(Integer)
    type = Column(String)   # task_creation, task_reward, referral, refund, daily
    description = Column(Text)
    date = Column(DateTime, default=datetime.now)

class DailyBonus(Base):
    __tablename__ = "daily_bonuses"
    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    last_claim = Column(DateTime, default=datetime.now)
    streak = Column(Integer, default=0)

# Создание движка и сессии
engine = create_engine(DATABASE_URL)
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)

# ==================== ОСНОВНОЙ КЛАСС БОТА ====================
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

class PromoBot:
    def __init__(self):
        self.current_code = None
        self.code_expiry = None
        self.authorized_users = set()
        self.scheduler = None   # для очистки заданий

    def generate_code(self):
        time_slot = int(time.time()) // 600   # 10 минут
        secret = f"{CODE_SECRET}_{time_slot}"
        self.current_code = hashlib.sha256(secret.encode()).hexdigest()[:8].upper()
        self.code_expiry = datetime.now() + timedelta(minutes=10)
        return self.current_code

    def validate_code(self, inp):
        return (self.current_code and
                datetime.now() < self.code_expiry and
                inp.upper() == self.current_code)

    # ========== РАБОТА С БД ==========
    def register_user(self, tg_user, ref_code=None):
        session = Session()
        user = session.query(User).filter_by(user_id=tg_user.id).first()
        if not user:
            referrer = None
            if ref_code:
                referrer = session.query(User).filter_by(referral_code=ref_code).first()
            new_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
            new_user = User(
                user_id=tg_user.id,
                username=tg_user.username,
                first_name=tg_user.first_name,
                referral_code=new_code,
                referred_by=referrer.user_id if referrer else None
            )
            session.add(new_user)
            session.flush()
            if referrer:
                referrer.balance += REFERRAL_BONUS
                trans = Transaction(
                    user_id=referrer.user_id,
                    amount=REFERRAL_BONUS,
                    type="referral",
                    description=f"Реферал {tg_user.id}"
                )
                session.add(trans)
            session.commit()
        session.close()

    def get_balance(self, user_id):
        session = Session()
        user = session.query(User).filter_by(user_id=user_id).first()
        bal = user.balance if user else 0
        session.close()
        return bal

    def is_authorized(self, user_id):
        return user_id in self.authorized_users or user_id == ADMIN_ID

    # ========== КЛАВИАТУРЫ ==========
    def get_main_keyboard(self):
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📺 Добавить канал", callback_data="add_channel_help")],
            [InlineKeyboardButton("📝 Создать задание", callback_data="create_task")],
            [InlineKeyboardButton("📋 Задания", callback_data="available_tasks")],
            [InlineKeyboardButton("📊 Мои каналы/задания", callback_data="my_channels")],
            [InlineKeyboardButton("💷 Баланс", callback_data="balance")],
            [InlineKeyboardButton("🎁 Ежедневный бонус", callback_data="daily")]
        ])

    def get_admin_keyboard(self):
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Статистика", callback_data="admin_stats")],
            [InlineKeyboardButton("🔄 Новый код", callback_data="refresh_code")],
            [InlineKeyboardButton("📢 Рассылка", callback_data="broadcast")]
        ])

    # ========== ХЭНДЛЕРЫ ==========
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        args = context.args
        ref = args[0] if args and args[0].startswith("ref_") else None
        self.register_user(update.effective_user, ref)

        if uid == ADMIN_ID:
            self.generate_code()
            await update.message.reply_text(
                f"👑 *Alpha, добро пожаловать!*\n\n"
                f"🔑 Код доступа: `{self.current_code}` (10 мин)\n"
                f"💎 Используйте /code <код> для входа другим пользователям.",
                parse_mode="Markdown",
                reply_markup=self.get_admin_keyboard()
            )
        else:
            await update.message.reply_text(
                f"🤝 *Бот взаимного пиара*\n\n"
                f"💷 Ваш баланс: *{self.get_balance(uid)} {CURRENCY_NAME}*\n\n"
                f"🔐 Доступ по коду: /code <код>",
                parse_mode="Markdown"
            )

    async def code_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        if not context.args:
            await update.message.reply_text("❌ Использование: /code <код>")
            return
        if self.validate_code(context.args[0]):
            self.authorized_users.add(uid)
            await update.message.reply_text(
                f"✅ Доступ открыт!\n"
                f"💷 Баланс: {self.get_balance(uid)} {CURRENCY_NAME}",
                reply_markup=self.get_main_keyboard()
            )
        else:
            await update.message.reply_text("❌ Неверный или просроченный код")

    async def add_channel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        if not self.is_authorized(uid):
            await update.message.reply_text("🔐 Требуется авторизация через /code")
            return

        if not update.message.forward_from_chat:
            await update.message.reply_text(
                "📺 *Добавление канала*\n\n"
                "1. Добавьте бота в администраторы канала\n"
                "2. Перешлите любое сообщение из канала сюда",
                parse_mode="Markdown"
            )
            return

        chat = update.message.forward_from_chat
        try:
            member = await context.bot.get_chat_member(chat.id, context.bot.id)
            if member.status not in ("administrator", "creator"):
                await update.message.reply_text("❌ Бот не является администратором канала!")
                return
        except BadRequest:
            await update.message.reply_text("❌ Не удалось проверить права бота. Добавьте бота в админы.")
            return

        session = Session()
        existing = session.query(Channel).filter_by(channel_id=str(chat.id)).first()
        if existing:
            await update.message.reply_text("❌ Этот канал уже добавлен в систему")
            session.close()
            return

        try:
            link = await context.bot.create_chat_invite_link(chat.id, creates_join_request=True)
            invite_url = link.invite_link
        except:
            invite_url = f"https://t.me/{chat.username}" if chat.username else "ссылка недоступна"

        channel = Channel(
            channel_id=str(chat.id),
            channel_name=chat.title,
            owner_id=uid,
            invite_link=invite_url,
            category="general"
        )
        session.add(channel)
        session.commit()
        session.close()

        await update.message.reply_text(
            f"✅ Канал *{chat.title}* добавлен!\n"
            f"🔗 Ссылка: {invite_url}\n\n"
            f"Теперь вы можете создавать задания на подписку.",
            parse_mode="Markdown"
        )

    async def daily_bonus_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        uid = query.from_user.id
        if not self.is_authorized(uid):
            await query.answer("🔐 Требуется авторизация")
            return
        await query.answer()

        session = Session()
        now = datetime.now()
        bonus = session.query(DailyBonus).filter_by(user_id=uid).first()

        if not bonus or (now - bonus.last_claim).days >= 1:
            # расчёт награды
            if not bonus:
                streak = 1
                reward = 100
                bonus = DailyBonus(user_id=uid, streak=1, last_claim=now)
                session.add(bonus)
            else:
                if (now - bonus.last_claim).days == 1:
                    streak = bonus.streak + 1
                    reward = min(100 + streak * 20, 500)
                    bonus.streak = streak
                else:
                    streak = 1
                    reward = 100
                    bonus.streak = 1
                bonus.last_claim = now
            user = session.query(User).filter_by(user_id=uid).first()
            user.balance += reward
            trans = Transaction(user_id=uid, amount=reward, type="daily", description="Ежедневный бонус")
            session.add(trans)
            session.commit()
            await query.edit_message_text(f"🎁 Получено +{reward} {CURRENCY_NAME}\n🔥 Серия: {bonus.streak} дней")
        else:
            hours_left = 24 - (now - bonus.last_claim).seconds // 3600
            await query.edit_message_text(f"⏳ Следующий бонус через {hours_left} ч.")
        session.close()

    async def my_channels(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        uid = query.from_user.id
        if not self.is_authorized(uid):
            await query.answer("🔐 Нет доступа")
            return
        await query.answer()

        session = Session()
        channels = session.query(Channel).filter_by(owner_id=uid).all()
        if not channels:
            await query.edit_message_text("📭 У вас нет добавленных каналов.\nИспользуйте 'Добавить канал'.")
            session.close()
            return

        text = "📺 *Ваши каналы:*\n\n"
        for ch in channels:
            tasks = session.query(Task).filter_by(channel_id=ch.channel_id, is_active=True).count()
            text += f"• {ch.channel_name}\n   👥 Активных заданий: {tasks}\n"
        await query.edit_message_text(text, parse_mode="Markdown")
        session.close()

    async def balance_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        uid = query.from_user.id
        if not self.is_authorized(uid):
            await query.answer("🔐 Нет доступа")
            return
        bal = self.get_balance(uid)
        await query.edit_message_text(f"💷 Ваш баланс: *{bal} {CURRENCY_NAME}*", parse_mode="Markdown")

    async def create_task_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        uid = query.from_user.id
        if not self.is_authorized(uid):
            await query.answer("🔐 Нет доступа")
            return
        await query.answer()

        session = Session()
        channels = session.query(Channel).filter_by(owner_id=uid).all()
        if not channels:
            await query.edit_message_text("❌ Сначала добавьте канал через 'Добавить канал'.")
            session.close()
            return

        active_tasks = session.query(Task).filter_by(creator_id=uid, is_active=True).count()
        if active_tasks >= MAX_ACTIVE_TASKS:
            await query.edit_message_text(f"⚠️ Лимит активных заданий: {MAX_ACTIVE_TASKS}. Завершите или удалите текущие.")
            session.close()
            return

        keyboard = []
        for ch in channels:
            keyboard.append([InlineKeyboardButton(f"📺 {ch.channel_name}", callback_data=f"selch_{ch.channel_id}")])
        keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="main_menu")])
        await query.edit_message_text(
            "Выберите канал для создания задания:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        session.close()

    async def select_channel_for_task(self, update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: str):
        query = update.callback_query
        context.user_data["temp_channel_id"] = channel_id
        await query.edit_message_text(
            f"💰 Введите награду за подписку (от {MIN_REWARD} до {MAX_REWARD} {CURRENCY_NAME}):"
        )
        context.user_data["waiting_reward"] = True

    async def handle_reward_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        if not self.is_authorized(uid):
            return
        if not context.user_data.get("waiting_reward"):
            return

        try:
            reward = int(update.message.text.strip())
        except ValueError:
            await update.message.reply_text("❌ Введите целое число.")
            return

        if reward < MIN_REWARD or reward > MAX_REWARD:
            await update.message.reply_text(f"❌ Награда должна быть от {MIN_REWARD} до {MAX_REWARD}.")
            return
        if reward > self.get_balance(uid):
            await update.message.reply_text(f"❌ Недостаточно средств. Ваш баланс: {self.get_balance(uid)} {CURRENCY_NAME}")
            return

        ch_id = context.user_data.get("temp_channel_id")
        if not ch_id:
            await update.message.reply_text("❌ Ошибка: канал не выбран. Начните заново /create_task")
            return

        session = Session()
        channel = session.query(Channel).filter_by(channel_id=ch_id).first()
        if not channel:
            await update.message.reply_text("❌ Канал не найден")
            session.close()
            return

        # Создание задания
        task = Task(
            channel_id=ch_id,
            creator_id=uid,
            reward=reward,
            expiry_date=datetime.now() + timedelta(days=TASK_DURATION_DAYS),
            max_completions=50
        )
        session.add(task)
        # Списываем средства
        user = session.query(User).filter_by(user_id=uid).first()
        user.balance -= reward
        trans = Transaction(
            user_id=uid,
            amount=-reward,
            type="task_creation",
            description=f"Создание задания для {channel.channel_name}"
        )
        session.add(trans)
        session.commit()
        session.close()

        await update.message.reply_text(
            f"✅ Задание создано!\n"
            f"📺 Канал: {channel.channel_name}\n"
            f"💰 Награда: {reward} {CURRENCY_NAME}\n"
            f"⏳ Действует {TASK_DURATION_DAYS} дней\n\n"
            f"Теперь другие пользователи увидят его в списке заданий."
        )
        context.user_data["waiting_reward"] = False
        context.user_data.pop("temp_channel_id", None)

    async def available_tasks_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        uid = query.from_user.id
        if not self.is_authorized(uid):
            await query.answer("🔐 Нет доступа")
            return
        await query.answer()

        session = Session()
        # Исключаем свои задания и уже выполненные
        subquery = session.query(TaskCompletion.task_id).filter(TaskCompletion.user_id == uid)
        tasks = session.query(Task).filter(
            Task.is_active == True,
            Task.creator_id != uid,          # свои не показываем
            ~Task.id.in_(subquery),          # уже выполненные не показываем
            Task.expiry_date > datetime.now(),
            Task.current_completions < Task.max_completions
        ).order_by(Task.priority.desc(), Task.created_date).limit(30).all()

        if not tasks:
            await query.edit_message_text("📭 Нет доступных заданий. Зайдите позже.")
            session.close()
            return

        text = "📋 *Доступные задания:*\n\n"
        keyboard = []
        for t in tasks:
            text += f"📺 {t.channel.channel_name}\n"
            text += f"💰 {t.reward} {CURRENCY_NAME}\n"
            text += f"👥 Осталось мест: {t.max_completions - t.current_completions}\n\n"
            keyboard.append([InlineKeyboardButton(f"Выполнить {t.channel.channel_name}", callback_data=f"do_{t.id}")])

        keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="main_menu")])
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        session.close()

    async def start_task_execution(self, update: Update, context: ContextTypes.DEFAULT_TYPE, task_id: int):
        query = update.callback_query
        uid = query.from_user.id
        if not self.is_authorized(uid):
            await query.answer("🔐 Нет доступа")
            return
        await query.answer()

        session = Session()
        task = session.query(Task).filter_by(id=task_id).first()
        if not task or not task.is_active or task.expiry_date < datetime.now():
            await query.edit_message_text("❌ Задание уже недоступно.")
            session.close()
            return

        # Проверка, не выполнял ли уже
        done = session.query(TaskCompletion).filter_by(task_id=task_id, user_id=uid).first()
        if done:
            await query.edit_message_text("❌ Вы уже выполняли это задание.")
            session.close()
            return

        # Запоминаем задание для последующей проверки
        context.user_data[f"pending_task_{uid}"] = task_id

        await query.edit_message_text(
            f"📌 *Чтобы получить награду:*\n"
            f"1. Подпишитесь на канал: {task.channel.invite_link}\n"
            f"2. После подписки нажмите кнопку '✅ Проверить подписку'.\n\n"
            f"⚠️ Бот автоматически проверит вашу подписку и начислит {task.reward} {CURRENCY_NAME}.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Проверить подписку", callback_data=f"verify_{task_id}")],
                [InlineKeyboardButton("🔙 Назад", callback_data="available_tasks")]
            ])
        )
        session.close()

    async def verify_subscription(self, update: Update, context: ContextTypes.DEFAULT_TYPE, task_id: int):
        query = update.callback_query
        uid = query.from_user.id
        if not self.is_authorized(uid):
            await query.answer("🔐 Нет доступа")
            return
        await query.answer()

        session = Session()
        task = session.query(Task).filter_by(id=task_id).first()
        if not task or not task.is_active or task.expiry_date < datetime.now():
            await query.edit_message_text("❌ Задание устарело.")
            session.close()
            return

        # Повторная проверка, не выполнено ли уже
        done = session.query(TaskCompletion).filter_by(task_id=task_id, user_id=uid).first()
        if done:
            await query.edit_message_text("✅ Вы уже получили награду за это задание.")
            session.close()
            return

        # Проверяем подписку через Telegram API
        try:
            member = await context.bot.get_chat_member(chat_id=int(task.channel_id), user_id=uid)
            is_subscribed = member.status in ("member", "administrator", "creator")
        except Exception as e:
            logger.error(f"Ошибка проверки подписки: {e}")
            is_subscribed = False

        if is_subscribed:
            user = session.query(User).filter_by(user_id=uid).first()
            user.balance += task.reward
            task.current_completions += 1
            completion = TaskCompletion(task_id=task_id, user_id=uid, is_verified=True)
            session.add(completion)
            trans = Transaction(
                user_id=uid,
                amount=task.reward,
                type="task_reward",
                description=f"Выполнение задания {task.channel.channel_name}"
            )
            session.add(trans)
            session.commit()
            await query.edit_message_text(
                f"✅ Подписка подтверждена!\n"
                f"💰 Вы получили {task.reward} {CURRENCY_NAME}\n"
                f"💷 Новый баланс: {user.balance} {CURRENCY_NAME}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📋 Другие задания", callback_data="available_tasks")],
                    [InlineKeyboardButton("💷 Баланс", callback_data="balance")]
                ])
            )
        else:
            await query.edit_message_text(
                "❌ Вы не подписаны на канал.\n"
                "Пожалуйста, подпишитесь и нажмите 'Проверить подписку' снова.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔗 Перейти в канал", url=task.channel.invite_link)],
                    [InlineKeyboardButton("✅ Проверить снова", callback_data=f"verify_{task_id}")],
                    [InlineKeyboardButton("🔙 Назад", callback_data="available_tasks")]
                ])
            )
        session.close()

    async def my_tasks(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        uid = query.from_user.id
        if not self.is_authorized(uid):
            await query.answer("🔐 Нет доступа")
            return
        await query.answer()

        session = Session()
        tasks = session.query(Task).filter_by(creator_id=uid).order_by(Task.created_date.desc()).limit(20).all()
        if not tasks:
            await query.edit_message_text("📭 У вас нет созданных заданий.")
            session.close()
            return

        text = "📊 *Ваши задания:*\n\n"
        keyboard = []
        for t in tasks:
            status = "🟢 Активно" if (t.is_active and t.expiry_date > datetime.now()) else "🔴 Завершено"
            text += f"• {t.channel.channel_name}\n"
            text += f"  💰 {t.reward} {CURRENCY_NAME} | Выполнено: {t.current_completions}/{t.max_completions}\n"
            text += f"  {status}\n\n"
            if t.is_active and t.expiry_date > datetime.now():
                keyboard.append([InlineKeyboardButton(f"❌ Отменить {t.channel.channel_name}", callback_data=f"cancel_{t.id}")])

        keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="main_menu")])
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        session.close()

    async def cancel_task(self, update: Update, context: ContextTypes.DEFAULT_TYPE, task_id: int):
        query = update.callback_query
        uid = query.from_user.id
        if not self.is_authorized(uid):
            await query.answer("🔐 Нет доступа")
            return
        await query.answer()

        session = Session()
        task = session.query(Task).filter_by(id=task_id, creator_id=uid).first()
        if not task:
            await query.edit_message_text("❌ Задание не найдено.")
            session.close()
            return

        if not task.is_active:
            await query.edit_message_text("❌ Задание уже завершено.")
            session.close()
            return

        # Возврат неизрасходованных средств
        remaining = task.max_completions - task.current_completions
        refund = int((task.reward / task.max_completions) * remaining)
        if refund > 0:
            user = session.query(User).filter_by(user_id=uid).first()
            user.balance += refund
            trans = Transaction(
                user_id=uid,
                amount=refund,
                type="refund",
                description=f"Отмена задания {task.channel.channel_name}"
            )
            session.add(trans)
        task.is_active = False
        session.commit()

        await query.edit_message_text(
            f"✅ Задание для канала {task.channel.channel_name} отменено.\n"
            f"💰 Возвращено {refund} {CURRENCY_NAME}"
        )
        session.close()

    # ========== АДМИНСКИЕ ФУНКЦИИ ==========
    async def admin_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        uid = query.from_user.id
        if uid != ADMIN_ID:
            await query.answer("Нет прав")
            return
        await query.answer()
        session = Session()
        users = session.query(User).count()
        channels = session.query(Channel).count()
        active_tasks = session.query(Task).filter_by(is_active=True).count()
        total_currency = session.query(User.balance).scalar() or 0
        await query.edit_message_text(
            f"📊 *Статистика Zeta Bot*\n"
            f"👥 Пользователей: {users}\n"
            f"📺 Каналов: {channels}\n"
            f"📝 Активных заданий: {active_tasks}\n"
            f"💷 Всего валюты: {total_currency} {CURRENCY_NAME}",
            parse_mode="Markdown"
        )
        session.close()

    async def refresh_admin_code(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if query.from_user.id != ADMIN_ID:
            await query.answer("Нет прав")
            return
        await query.answer()
        new_code = self.generate_code()
        await query.edit_message_text(f"🔑 Новый код доступа: `{new_code}`", parse_mode="Markdown")
        # Также отправим в личку админу
        await context.bot.send_message(ADMIN_ID, f"🔑 Код доступа обновлён: `{new_code}`", parse_mode="Markdown")

    # ========== ГЛАВНЫЙ КОЛБЭК ==========
    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        data = query.data
        uid = query.from_user.id

        if not self.is_authorized(uid):
            await query.answer("🔐 Сначала получите доступ через /code")
            return

        if data == "main_menu":
            await query.edit_message_text(
                f"🤝 *Главное меню*\n💷 Баланс: {self.get_balance(uid)} {CURRENCY_NAME}",
                parse_mode="Markdown",
                reply_markup=self.get_main_keyboard()
            )
        elif data == "add_channel_help":
            await query.edit_message_text(
                "📺 *Как добавить канал:*\n"
                "1. Добавьте @bot_username в администраторы канала\n"
                "2. Перешлите любое сообщение из канала этому боту\n\n"
                "После этого бот сможет проверять подписки.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="main_menu")]])
            )
        elif data == "create_task":
            await self.create_task_menu(update, context)
        elif data == "available_tasks":
            await self.available_tasks_callback(update, context)
        elif data == "my_channels":
            await self.my_channels(update, context)
        elif data == "balance":
            await self.balance_callback(update, context)
        elif data == "daily":
            await self.daily_bonus_callback(update, context)
        elif data == "my_tasks":        # если понадобится
            await self.my_tasks(update, context)
        elif data.startswith("selch_"):
            ch_id = data[6:]
            await self.select_channel_for_task(update, context, ch_id)
        elif data.startswith("do_"):
            task_id = int(data[3:])
            await self.start_task_execution(update, context, task_id)
        elif data.startswith("verify_"):
            task_id = int(data[7:])
            await self.verify_subscription(update, context, task_id)
        elif data.startswith("cancel_"):
            task_id = int(data[7:])
            await self.cancel_task(update, context, task_id)
        elif data == "admin_stats":
            await self.admin_stats(update, context)
        elif data == "refresh_code":
            await self.refresh_admin_code(update, context)
        else:
            await query.answer("Неизвестная команда")

    # ========== ФОНОВЫЕ ЗАДАЧИ ==========
    async def clean_expired_tasks(self):
        """Раз в час удаляет просроченные задания, возвращает средства"""
        session = Session()
        now = datetime.now()
        expired = session.query(Task).filter(Task.expiry_date < now, Task.is_active == True).all()
        for task in expired:
            remaining = task.max_completions - task.current_completions
            refund = int((task.reward / task.max_completions) * remaining) if task.max_completions > 0 else 0
            if refund > 0:
                user = session.query(User).filter_by(user_id=task.creator_id).first()
                if user:
                    user.balance += refund
                    trans = Transaction(
                        user_id=task.creator_id,
                        amount=refund,
                        type="refund",
                        description=f"Возврат за просроченное задание {task.channel.channel_name}"
                    )
                    session.add(trans)
            task.is_active = False
        session.commit()
        session.close()
        logger.info("Очистка просроченных заданий выполнена")

    async def code_updater(self, application):
        """Обновление кода каждые 10 минут и отправка админу"""
        while True:
            await asyncio.sleep(600)
            new_code = self.generate_code()
            try:
                await application.bot.send_message(
                    ADMIN_ID,
                    f"🔄 Код доступа обновлён: `{new_code}`\nДействует до {self.code_expiry.strftime('%H:%M:%S')}",
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"Не удалось отправить код админу: {e}")
            logger.info(f"Код обновлён: {new_code}")

    # ========== ЗАПУСК ==========
    def run(self):
        self.generate_code()
        logger.info(f"Стартовый код доступа: {self.current_code}")

        # Создаём приложение
        app = Application.builder().token(BOT_TOKEN).build()

        # Регистрируем хэндлеры
        app.add_handler(CommandHandler("start", self.start))
        app.add_handler(CommandHandler("code", self.code_command))
        app.add_handler(MessageHandler(filters.FORWARDED, self.add_channel))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_reward_input))
        app.add_handler(CallbackQueryHandler(self.handle_callback))

        # Планировщик для очистки заданий
        scheduler = AsyncIOScheduler()
        scheduler.add_job(self.clean_expired_tasks, "interval", hours=1)
        scheduler.start()

        # Запускаем фоновую задачу обновления кода
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.create_task(self.code_updater(app))

        logger.info("🚀 Бот запущен и готов к работе")
        app.run_polling()

if __name__ == "__main__":
    bot = PromoBot()
    bot.run()
