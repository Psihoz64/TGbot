import asyncio
import logging
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from datetime import datetime, timedelta
import os
from dotenv import load_dotenv
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError

from database import get_db, User, DefaultCategory, CustomCategory, Transaction
from default_categories import DEFAULT_CATEGORIES

load_dotenv()

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Класс с повторными попытками
class RetryAiohttpSession(AiohttpSession):
    async def make_request(self, bot, method, timeout=None, **kwargs):
        for attempt in range(3):
            try:
                return await super().make_request(bot, method, timeout, **kwargs)
            except TelegramNetworkError as e:
                if attempt == 2:
                    raise
                logger.warning(f"Attempt {attempt+1} failed, retrying... ({e})")
                await asyncio.sleep(2 ** attempt)

# Инициализация бота
session = RetryAiohttpSession()
bot = Bot(token=os.getenv('BOT_TOKEN'), session=session)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Состояния для FSM
class AddTransaction(StatesGroup):
    choosing_category = State()
    entering_amount = State()
    entering_description = State()

class ManageCategory(StatesGroup):
    choosing_action = State()
    entering_name = State()
    choosing_type_for_category = State()
    deleting_category = State()

# ========== КЛАВИАТУРЫ ==========

def get_main_keyboard():
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Добавить расход"), KeyboardButton(text="💳 Добавить доход")],
            [KeyboardButton(text="📊 Баланс"), KeyboardButton(text="🏷️ Категории")],
        ],
        resize_keyboard=True
    )
    return keyboard

def get_number_inline_keyboard(current_input: str = ""):
    """Инлайн-клавиатура с цифрами для ввода суммы"""
    buttons = [
        [InlineKeyboardButton(text="1", callback_data="num_1"),
         InlineKeyboardButton(text="2", callback_data="num_2"),
         InlineKeyboardButton(text="3", callback_data="num_3")],
        [InlineKeyboardButton(text="4", callback_data="num_4"),
         InlineKeyboardButton(text="5", callback_data="num_5"),
         InlineKeyboardButton(text="6", callback_data="num_6")],
        [InlineKeyboardButton(text="7", callback_data="num_7"),
         InlineKeyboardButton(text="8", callback_data="num_8"),
         InlineKeyboardButton(text="9", callback_data="num_9")],
        [InlineKeyboardButton(text="0", callback_data="num_0"),
         InlineKeyboardButton(text=".", callback_data="num_dot"),
         InlineKeyboardButton(text="⌫", callback_data="num_backspace")],
        [InlineKeyboardButton(text="✅ Готово", callback_data="num_done"),
         InlineKeyboardButton(text="❌ Отмена", callback_data="num_cancel")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ========== РАБОТА С ПОЛЬЗОВАТЕЛЕМ ==========

async def get_or_create_user(message: types.Message):
    """Получает ID пользователя из БД или создаёт нового"""
    db = get_db()
    user = db.query(User).filter_by(telegram_id=message.from_user.id).first()

    if not user:
        user = User(
            telegram_id=message.from_user.id,
            username=message.from_user.username
        )
        db.add(user)
        db.commit()

        default_count = db.query(DefaultCategory).count()
        if default_count == 0:
            for category_type, categories in DEFAULT_CATEGORIES.items():
                for cat in categories:
                    db_category = DefaultCategory(
                        name=cat['name'],
                        type=category_type,
                        emoji=cat['emoji']
                    )
                    db.add(db_category)
            db.commit()

    user_id = user.id
    db.close()
    return user_id

async def get_all_categories(user_id: int, transaction_type: str = None):
    """Получает все категории для пользователя (дефолтные + пользовательские)"""
    db = get_db()

    default_cats = db.query(DefaultCategory)
    if transaction_type:
        default_cats = default_cats.filter_by(type=transaction_type)
    default_cats = default_cats.all()

    custom_cats = db.query(CustomCategory).filter_by(user_id=user_id)
    if transaction_type:
        custom_cats = custom_cats.filter_by(type=transaction_type)
    custom_cats = custom_cats.all()

    db.close()

    result = []
    for cat in default_cats:
        result.append({
            'id': cat.id,
            'name': cat.name,
            'emoji': cat.emoji,
            'type': cat.type,
            'source': 'default'
        })
    for cat in custom_cats:
        result.append({
            'id': cat.id,
            'name': cat.name,
            'emoji': cat.emoji,
            'type': cat.type,
            'source': 'custom'
        })

    return result

# ========== ОСНОВНЫЕ КОМАНДЫ ==========

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await get_or_create_user(message)
    await message.answer(
        f"С возвращением, {message.from_user.first_name}! 👋\n\n"
        f"Чем займёмся?",
        reply_markup=get_main_keyboard()
    )

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    help_text = (
        "🤖 **Бюджетный менеджер - помощь**\n\n"
        "**Основные команды:**\n"
        "/start - Начать работу\n"
        "/help - Помощь\n"
        "/cancel - Отменить операцию\n\n"
        "**Быстрый ввод:**\n"
        "`-500 Еда Обед` - расход\n"
        "`+1000 Зарплата` - доход\n\n"
        "**Кнопки меню** для удобной навигации!"
    )
    await message.answer(help_text, parse_mode="Markdown")

@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("❌ Нет активных операций для отмены.")
        return
    await state.clear()
    await message.answer("❌ Операция отменена.", reply_markup=get_main_keyboard())

# ========== ДОБАВЛЕНИЕ ТРАНЗАКЦИЙ ==========

@dp.message(F.text == "➕ Добавить расход")
async def add_expense(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = await get_or_create_user(message)
    await state.update_data(transaction_type='expense')
    await show_category_selection(message, state, 'expense', user_id)

@dp.message(F.text == "💳 Добавить доход")
async def add_income(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = await get_or_create_user(message)
    await state.update_data(transaction_type='income')
    await show_category_selection(message, state, 'income', user_id)

async def show_category_selection(message: types.Message, state: FSMContext, transaction_type: str, user_id: int):
    categories = await get_all_categories(user_id, transaction_type)

    if not categories:
        await message.answer(
            "❌ Нет категорий для этого типа транзакций.\n"
            "Сначала создайте категорию через раздел 'Категории'.",
            reply_markup=get_main_keyboard()
        )
        await state.clear()
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    for cat in categories:
        label = f"{cat['emoji']} {cat['name']}"
        if cat['source'] == 'custom':
            label += " ✏️"
        keyboard.inline_keyboard.append([
            InlineKeyboardButton(
                text=label,
                callback_data=f"cat_{cat['source']}_{cat['id']}"
            )
        ])
    keyboard.inline_keyboard.append([
        InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")
    ])

    await state.set_state(AddTransaction.choosing_category)
    await message.answer(
        f"📂 Выберите категорию для {'расхода' if transaction_type == 'expense' else 'дохода'}:",
        reply_markup=keyboard
    )

@dp.callback_query(AddTransaction.choosing_category)
async def process_category_selection(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    chat_id = callback.from_user.id

    if callback.data == "cancel":
        await callback.message.delete()
        await callback.bot.send_message(chat_id, "❌ Операция отменена", reply_markup=get_main_keyboard())
        await state.clear()
        return

    parts = callback.data.split('_')
    source = parts[1]
    category_id = int(parts[2])

    await state.update_data(category_source=source)
    await state.update_data(category_id=category_id)

    await callback.message.delete()
    await state.set_state(AddTransaction.entering_amount)
    await callback.bot.send_message(
        chat_id,
        "💰 Введите сумму (используйте кнопки ниже):",
        reply_markup=get_number_inline_keyboard()
    )

@dp.callback_query(F.data.startswith("num_"))
async def process_number_input(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    chat_id = callback.from_user.id
    action = callback.data.split('_')[1]

    data = await state.get_data()
    current_input = data.get('current_amount_input', '')

    if action == "cancel":
        await callback.message.delete()
        await callback.bot.send_message(chat_id, "❌ Операция отменена", reply_markup=get_main_keyboard())
        await state.clear()
        return

    if action == "done":
        if not current_input:
            await callback.message.edit_text(
                "❌ Введите сумму перед подтверждением.",
                reply_markup=get_number_inline_keyboard()
            )
            return

        try:
            amount = float(current_input.replace(',', '.'))
            if amount <= 0:
                raise ValueError("Сумма должна быть больше 0")

            await state.update_data(amount=amount)
            await state.update_data(current_amount_input='')
            await callback.message.delete()

            transaction_type = data.get('transaction_type')
            if transaction_type == 'income':
                await save_transaction(callback.message, state, description='')
            else:
                await state.set_state(AddTransaction.entering_description)
                await callback.bot.send_message(
                    chat_id,
                    "📝 Введите описание (или отправьте '-' для пропуска):",
                    reply_markup=get_main_keyboard()
                )
        except ValueError:
            await callback.message.edit_text(
                "❌ Неверный формат. Введите число, например: 1500 или 1500.50",
                reply_markup=get_number_inline_keyboard()
            )
        return

    if action == "backspace":
        current_input = current_input[:-1]
    else:
        if len(current_input) < 10:
            if action == "dot":
                if '.' not in current_input:
                    current_input += '.'
            else:
                current_input += action
        else:
            await callback.answer("⚠️ Слишком длинное число. Максимум 10 символов.", show_alert=True)
            return

    await state.update_data(current_amount_input=current_input)
    display_text = current_input if current_input else "0"
    await callback.message.edit_text(
        f"💰 Текущая сумма: {display_text}",
        reply_markup=get_number_inline_keyboard()
    )

@dp.message(AddTransaction.entering_amount)
async def process_amount(message: types.Message, state: FSMContext):
    # Этот обработчик теперь не будет вызываться для кнопок, но оставлен для ручного ввода
    try:
        amount = float(message.text.replace(',', '.'))
        if amount <= 0:
            raise ValueError("Сумма должна быть больше 0")

        await state.update_data(amount=amount)
        await state.update_data(current_amount_input='')
        data = await state.get_data()
        transaction_type = data.get('transaction_type')

        if transaction_type == 'income':
            await save_transaction(message, state, description='')
        else:
            await state.set_state(AddTransaction.entering_description)
            await message.answer(
                "📝 Введите описание (или отправьте '-' для пропуска):",
                reply_markup=get_main_keyboard()
            )
    except ValueError:
        await message.answer(
            "❌ Неверный формат. Введите число, например: 1500 или 1500.50",
            reply_markup=get_number_inline_keyboard()
        )

async def save_transaction(message: types.Message, state: FSMContext, description: str = ''):
    data = await state.get_data()
    db = get_db()

    user = db.query(User).filter_by(telegram_id=message.from_user.id).first()

    if not user:
        await state.clear()
        await message.answer("❌ Ошибка: пользователь не найден. Выполните /start")
        db.close()
        return

    transaction = Transaction(
        user_id=user.id,
        category_type=data.get('category_source'),
        category_id=data.get('category_id'),
        amount=data.get('amount'),
        type=data.get('transaction_type'),
        description=description
    )
    db.add(transaction)
    db.commit()

    category_name = "Без категории"
    category_emoji = "📌"

    if data.get('category_source') == 'default':
        category = db.query(DefaultCategory).filter_by(id=data.get('category_id')).first()
        if category:
            category_name = category.name
            category_emoji = category.emoji
    else:
        category = db.query(CustomCategory).filter_by(id=data.get('category_id')).first()
        if category:
            category_name = category.name
            category_emoji = category.emoji

    db.close()
    await state.clear()

    emoji = "📉" if data.get('transaction_type') == 'expense' else "📈"
    await message.answer(
        f"✅ {'Расход' if data.get('transaction_type') == 'expense' else 'Доход'} добавлен!\n\n"
        f"{emoji} {category_emoji} {category_name}\n"
        f"💰 {data.get('amount', 0):.2f} руб.\n"
        f"📝 {description if description else 'Без описания'}"
    )

    await message.answer("Что делаем дальше?", reply_markup=get_main_keyboard())

@dp.message(AddTransaction.entering_description)
async def process_description(message: types.Message, state: FSMContext):
    if message.text in ['➕ Добавить расход', '💳 Добавить доход', '📊 Баланс', '🏷️ Категории']:
        await state.clear()
        if message.text == '➕ Добавить расход':
            await add_expense(message, state)
        elif message.text == '💳 Добавить доход':
            await add_income(message, state)
        elif message.text == '📊 Баланс':
            await show_balance(message, state)
        elif message.text == '🏷️ Категории':
            await manage_categories(message, state)
        return

    if message.text.startswith('/'):
        await state.clear()
        return

    description = message.text if message.text != '-' else ''
    await save_transaction(message, state, description)

# ========== ОСТАЛЬНЫЕ ФУНКЦИИ (БАЛАНС, КАТЕГОРИИ) ==========
# ... (остальной код без изменений: quick_add_transaction, show_balance, manage_categories, show_categories, show_categories_for_deletion, process_category_action, confirm_delete_category, process_category_type, save_new_category, dbcheck, myid) ...

# ========== ЗАПУСК ==========

async def main():
    logger.info("Starting bot...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
