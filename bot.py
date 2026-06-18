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

# ========== РАБОТА С ПОЛЬЗОВАТЕЛЕМ ==========

async def get_or_create_user(message: types.Message):
    db = get_db()
    user = db.query(User).filter_by(telegram_id=message.from_user.id).first()

    if not user:
        user = User(
            telegram_id=message.from_user.id,
            username=message.from_user.username
        )
        db.add(user)
        db.commit()

    # Проверяем и добавляем стандартные категории, если их нет
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
        logger.info("Default categories added to database.")

    user_id = user.id
    db.close()
    return user_id

async def get_all_categories(user_id: int, transaction_type: str = None):
    """Получает все категории для пользователя (дефолтные + пользовательские)"""
    db = get_db()

    # Получаем дефолтные категории
    default_cats = db.query(DefaultCategory)
    if transaction_type:
        default_cats = default_cats.filter_by(type=transaction_type)
    default_cats = default_cats.all()

    # Получаем пользовательские категории
    custom_cats = db.query(CustomCategory).filter_by(user_id=user_id)
    if transaction_type:
        custom_cats = custom_cats.filter_by(type=transaction_type)
    custom_cats = custom_cats.all()

    db.close()

    # Отладка
    logger.info(f"=== GET_ALL_CATEGORIES for user_id={user_id} ===")
    logger.info(f"Default categories: {len(default_cats)}")
    for cat in default_cats:
        logger.info(f"  DEFAULT: {cat.emoji} {cat.name} ({cat.type})")
    logger.info(f"Custom categories: {len(custom_cats)}")
    for cat in custom_cats:
        logger.info(f"  CUSTOM: {cat.emoji} {cat.name} ({cat.type})")

    # Форматируем результат
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

    # Получаем все категории (дефолтные + пользовательские)
    categories = await get_all_categories(user.id, transaction_type)

    if not categories:
        await message.answer(
            "❌ Нет категорий для этого типа транзакций.\n"
            "Сначала создайте категорию через раздел 'Категории'."
        )
        await state.clear()
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    for cat in categories:
        # Добавляем метку для пользовательских категорий
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

    if callback.data == "cancel":
        await callback.message.delete()
        await callback.message.answer("❌ Операция отменена")
        await state.clear()
        return

    # Разбираем callback_data: cat_{source}_{id}
    parts = callback.data.split('_')
    source = parts[1]  # 'default' или 'custom'
    category_id = int(parts[2])

    await state.update_data(category_source=source)
    await state.update_data(category_id=category_id)

    await callback.message.delete()
    await state.set_state(AddTransaction.entering_amount)
    await callback.message.answer(
        "💰 Введите сумму:\n(можно использовать точку для копеек, например: 1500.50)"
    )

@dp.message(AddTransaction.entering_amount)
async def process_amount(message: types.Message, state: FSMContext):
    try:
        amount = float(message.text.replace(',', '.'))
        if amount <= 0:
            raise ValueError("Сумма должна быть больше 0")

        await state.update_data(amount=amount)
        data = await state.get_data()
        transaction_type = data.get('transaction_type')

        if transaction_type == 'income':
            await save_transaction(message, state, description='')
        else:
            await state.set_state(AddTransaction.entering_description)
            await message.answer("📝 Введите описание (или отправьте '-' для пропуска):")
    except ValueError:
        await message.answer("❌ Неверный формат. Введите число, например: 1500 или 1500.50")

async def save_transaction(message: types.Message, state: FSMContext, description: str = ''):
    """Сохраняет транзакцию в базу данных"""
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
        category_type=data.get('category_source'),  # 'default' или 'custom'
        category_id=data.get('category_id'),
        amount=data.get('amount'),
        type=data.get('transaction_type'),
        description=description
    )
    db.add(transaction)
    db.commit()

    # Получаем название категории для отображения
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

# ========== БЫСТРЫЙ ВВОД ==========

@dp.message(F.text.regexp(r'^[+-]\d+'))
async def quick_add_transaction(message: types.Message):
    """Обработка быстрого ввода: -500 Еда или +1000 Зарплата"""
    text = message.text.strip()

    if text.startswith('-'):
        transaction_type = 'expense'
        amount_part = text[1:].split()[0]
        description = ' '.join(text[1:].split()[1:]) if len(text[1:].split()) > 1 else ''
    elif text.startswith('+'):
        transaction_type = 'income'
        amount_part = text[1:].split()[0]
        description = ' '.join(text[1:].split()[1:]) if len(text[1:].split()) > 1 else ''
    else:
        return

    try:
        amount = float(amount_part.replace(',', '.'))

        db = get_db()
        user = db.query(User).filter_by(telegram_id=message.from_user.id).first()

        if not user:
            await message.answer("❌ Пожалуйста, сначала выполните /start")
            db.close()
            return

        # Ищем категорию сначала среди пользовательских, потом среди дефолтных
        category = None
        category_source = None

        if description:
            # Ищем в пользовательских категориях
            custom_cat = db.query(CustomCategory).filter_by(
                user_id=user.id,
                name=description,
                type=transaction_type
            ).first()

            if custom_cat:
                category = custom_cat
                category_source = 'custom'
            else:
                # Ищем в дефолтных категориях
                default_cat = db.query(DefaultCategory).filter_by(
                    name=description,
                    type=transaction_type
                ).first()
                if default_cat:
                    category = default_cat
                    category_source = 'default'
                else:
                    # Пробуем найти по частичному совпадению в пользовательских
                    custom_cats = db.query(CustomCategory).filter_by(
                        user_id=user.id,
                        type=transaction_type
                    ).all()
                    for cat in custom_cats:
                        if description.lower() in cat.name.lower():
                            category = cat
                            category_source = 'custom'
                            break

                    if not category:
                        # Пробуем в дефолтных
                        default_cats = db.query(DefaultCategory).filter_by(
                            type=transaction_type
                        ).all()
                        for cat in default_cats:
                            if description.lower() in cat.name.lower():
                                category = cat
                                category_source = 'default'
                                break
        else:
            # Если нет описания, используем категорию "Другое"
            custom_cat = db.query(CustomCategory).filter_by(
                user_id=user.id,
                name='Другое',
                type=transaction_type
            ).first()
            if custom_cat:
                category = custom_cat
                category_source = 'custom'
            else:
                default_cat = db.query(DefaultCategory).filter_by(
                    name='Другое',
                    type=transaction_type
                ).first()
                if default_cat:
                    category = default_cat
                    category_source = 'default'

        if not category or not category_source:
            await message.answer(
                f"❌ Не найдена категория '{description}' для {'расходов' if transaction_type == 'expense' else 'доходов'}.\n"
                f"Используйте кнопки меню для добавления."
            )
            db.close()
            return

        # Создаём транзакцию
        transaction = Transaction(
            user_id=user.id,
            category_type=category_source,
            category_id=category.id,
            amount=amount,
            type=transaction_type,
            description=description or 'Быстрый ввод'
        )
        db.add(transaction)
        db.commit()
        db.close()

        emoji = "📉" if transaction_type == 'expense' else "📈"
        await message.answer(
            f"✅ {'Расход' if transaction_type == 'expense' else 'Доход'} добавлен!\n\n"
            f"{emoji} {category.emoji} {category.name}\n"
            f"💰 {amount:.2f} руб."
        )

    except ValueError:
        await message.answer("❌ Неверный формат суммы. Пример: -500 Еда")
    except Exception as e:
        logger.error(f"Error in quick_add_transaction: {e}")
        await message.answer("❌ Произошла ошибка при добавлении транзакции")

# ========== БАЛАНС ==========

@dp.message(F.text == "📊 Баланс")
async def show_balance(message: types.Message, state: FSMContext):
    await state.clear()
    user = await get_or_create_user(message)

    db = get_db()

    total_income = db.query(Transaction).filter_by(
        user_id=user.id, type='income'
    ).with_entities(Transaction.amount).all()
    total_income = sum(t.amount for t in total_income)

    total_expense = db.query(Transaction).filter_by(
        user_id=user.id, type='expense'
    ).with_entities(Transaction.amount).all()
    total_expense = sum(t.amount for t in total_expense)

    balance = total_income - total_expense

    start_month = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    next_month = (start_month + timedelta(days=32)).replace(day=1)

    month_incomes = db.query(Transaction).filter(
        Transaction.user_id == user.id,
        Transaction.type == 'income',
        Transaction.date >= start_month,
        Transaction.date < next_month
    ).with_entities(Transaction.amount).all()
    month_incomes = sum(i.amount for i in month_incomes)

    month_expenses = db.query(Transaction).filter(
        Transaction.user_id == user.id,
        Transaction.type == 'expense',
        Transaction.date >= start_month,
        Transaction.date < next_month
    ).with_entities(Transaction.amount).all()
    month_expenses = sum(e.amount for e in month_expenses)

    month_balance = month_incomes - month_expenses

    # Получаем расходы по категориям за месяц
    expenses_by_category = []
    transactions = db.query(Transaction).filter(
        Transaction.user_id == user.id,
        Transaction.type == 'expense',
        Transaction.date >= start_month,
        Transaction.date < next_month
    ).all()

    db.close()

    # Группируем по категориям
    categories_dict = {}
    for trans in transactions:
        # Получаем название категории
        cat_name = "Без категории"
        cat_emoji = "📌"

        if trans.category_type == 'default':
            db2 = get_db()
            cat = db2.query(DefaultCategory).filter_by(id=trans.category_id).first()
            db2.close()
            if cat:
                cat_name = cat.name
                cat_emoji = cat.emoji
        else:
            db2 = get_db()
            cat = db2.query(CustomCategory).filter_by(id=trans.category_id).first()
            db2.close()
            if cat:
                cat_name = cat.name
                cat_emoji = cat.emoji

        key = f"{cat_emoji} {cat_name}"
        categories_dict[key] = categories_dict.get(key, 0) + trans.amount

    report = "📊 **Мой баланс**\n\n"
    report += "━━━ 📅 **За всё время** ━━━\n"
    report += f"💰 Баланс: **{balance:.2f} руб.**\n"
    report += f"📈 Доходы: {total_income:.2f} руб.\n"
    report += f"📉 Расходы: {total_expense:.2f} руб.\n\n"
    report += f"━━━ 📆 **За {datetime.now().strftime('%B %Y')}** ━━━\n"
    report += f"💰 Остаток: **{month_balance:.2f} руб.**\n"
    report += f"📈 Доходы: {month_incomes:.2f} руб.\n"
    report += f"📉 Расходы: {month_expenses:.2f} руб.\n"

    if month_expenses > 0 and categories_dict:
        report += "\n📌 **Расходы по категориям:**\n"
        sorted_categories = sorted(categories_dict.items(), key=lambda x: x[1], reverse=True)
        for category, amount in sorted_categories:
            percentage = (amount / month_expenses * 100)
            report += f"  {category}: {amount:.2f} руб. ({percentage:.1f}%)\n"
    else:
        report += "\n✅ За месяц расходов нет"

    await message.answer(report, parse_mode="Markdown")

# ========== УПРАВЛЕНИЕ КАТЕГОРИЯМИ ==========

# ========== УПРАВЛЕНИЕ КАТЕГОРИЯМИ ==========

@dp.message(F.text == "🏷️ Категории")
async def manage_categories(message: types.Message, state: FSMContext):
    await state.clear()
    await get_or_create_user(message)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить категорию", callback_data="cat_add")],
        [InlineKeyboardButton(text="🗑️ Удалить категорию", callback_data="cat_delete")],
        [InlineKeyboardButton(text="📋 Список категорий", callback_data="cat_list")],
        [InlineKeyboardButton(text="❌ Закрыть", callback_data="cancel")]
    ])

    await state.set_state(ManageCategory.choosing_action)
    await message.answer("🏷️ **Управление категориями**\n\nВыберите действие:",
                         reply_markup=keyboard, parse_mode="Markdown")

async def show_categories(message: types.Message, user_id: int = None):
    """Показывает список категорий"""
    if user_id is None:
        user = await get_or_create_user(message)
        user_id = user.id
    else:
        db = get_db()
        user = db.query(User).filter_by(id=user_id).first()
        db.close()
        if not user:
            await message.answer("❌ Пользователь не найден")
            return

    categories = await get_all_categories(user_id)

    if not categories:
        await message.answer("📭 У вас пока нет категорий")
        return

    expense_cats = [c for c in categories if c['type'] == 'expense']
    income_cats = [c for c in categories if c['type'] == 'income']

    text = "📋 **Ваши категории:**\n\n"

    if expense_cats:
        text += "📉 **Расходы:**\n"
        for cat in expense_cats:
            mark = " ⭐" if cat['source'] == 'default' else " ✏️"
            text += f"  {cat['emoji']} {cat['name']}{mark}\n"
    else:
        text += "📉 Расходов: нет\n"

    if income_cats:
        text += "\n📈 **Доходы:**\n"
        for cat in income_cats:
            mark = " ⭐" if cat['source'] == 'default' else " ✏️"
            text += f"  {cat['emoji']} {cat['name']}{mark}\n"
    else:
        text += "\n📈 Доходов: нет\n"

    text += "\n\n⭐ - стандартная категория (защищена)\n✏️ - ваша категория"
    await message.answer(text, parse_mode="Markdown")

async def show_categories_for_deletion(message: types.Message, state: FSMContext, user_id: int):
    """Показывает только пользовательские категории для удаления"""
    db = get_db()
    categories = db.query(CustomCategory).filter_by(user_id=user_id).all()
    db.close()

    if not categories:
        await message.answer(
            "📭 Нет пользовательских категорий для удаления.\n"
            "Стандартные категории защищены от удаления."
        )
        await state.clear()
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    for cat in categories:
        keyboard.inline_keyboard.append([
            InlineKeyboardButton(
                text=f"🗑️ {cat.emoji} {cat.name}",
                callback_data=f"delcat_{cat.id}"
            )
        ])
    keyboard.inline_keyboard.append([
        InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")
    ])

    await state.set_state(ManageCategory.deleting_category)

    try:
        await message.edit_text(
            "🗑️ **Выберите категорию для удаления:**\n"
            "(показываются только созданные вами категории)",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
    except Exception:
        await message.answer(
            "🗑️ **Выберите категорию для удаления:**\n"
            "(показываются только созданные вами категории)",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )

@dp.callback_query(ManageCategory.choosing_action)
async def process_category_action(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    action = callback.data

    if action == "cancel":
        await callback.message.delete()
        await callback.message.answer("❌ Закрыто")
        await state.clear()
        return

    # Получаем пользователя из callback (нажавшего кнопку)
    db = get_db()
    user = db.query(User).filter_by(telegram_id=callback.from_user.id).first()
    db.close()

    if not user:
        await callback.message.answer("❌ Пользователь не найден. Отправьте /start")
        await state.clear()
        return

    if action == "cat_list":
        await show_categories(callback.message, user.id)
        await state.clear()
        return

    if action == "cat_add":
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📉 Расход", callback_data="type_expense")],
            [InlineKeyboardButton(text="📈 Доход", callback_data="type_income")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]
        ])
        await state.set_state(ManageCategory.choosing_type_for_category)
        await callback.message.edit_text(
            "📂 Выберите тип категории:",
            reply_markup=keyboard
        )
        return

    if action == "cat_delete":
        await show_categories_for_deletion(callback.message, state, user.id)
        return

@dp.callback_query(ManageCategory.deleting_category)
async def confirm_delete_category(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()

    if callback.data == "cancel":
        await callback.message.delete()
        await callback.message.answer("❌ Отменено")
        await state.clear()
        return

    category_id = int(callback.data.split('_')[1])
    db = get_db()

    category = db.query(CustomCategory).filter_by(id=category_id).first()
    if category:
        db.delete(category)
        db.commit()
        await callback.message.edit_text(f"✅ Категория '{category.emoji} {category.name}' удалена")
    else:
        await callback.message.edit_text("❌ Категория не найдена")

    db.close()
    await state.clear()

@dp.callback_query(ManageCategory.choosing_type_for_category)
async def process_category_type(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()

    if callback.data == "cancel":
        await callback.message.delete()
        await callback.message.answer("❌ Отменено")
        await state.clear()
        return

    transaction_type = "expense" if callback.data == "type_expense" else "income"
    await state.update_data(transaction_type=transaction_type)

    await callback.message.delete()
    await callback.message.answer(
        f"📝 Введите название новой категории для {'расходов' if transaction_type == 'expense' else 'доходов'}:"
    )
    await state.set_state(ManageCategory.entering_name)

@dp.message(ManageCategory.entering_name)
async def save_new_category(message: types.Message, state: FSMContext):
    name = message.text.strip()

    if not name:
        await message.answer("❌ Название не может быть пустым")
        return

    data = await state.get_data()
    transaction_type = data.get('transaction_type')

    if not transaction_type:
        await message.answer("❌ Ошибка: не выбран тип категории. Попробуйте снова.")
        await state.clear()
        return

    user = await get_or_create_user(message)

    db = get_db()

    existing = db.query(CustomCategory).filter_by(
        user_id=user.id,
        name=name,
        type=transaction_type
    ).first()

    if existing:
        await message.answer(f"❌ Категория '{name}' уже существует у вас")
        db.close()
        await state.clear()
        return

    category = CustomCategory(
        user_id=user.id,
        name=name,
        type=transaction_type,
        emoji="📌"
    )
    db.add(category)
    db.commit()
    db.close()

    await state.clear()

    await message.answer(
        f"✅ Категория '{name}' добавлена!",
        reply_markup=get_main_keyboard()
    )

# ========== ОТЛАДОЧНЫЕ КОМАНДЫ ==========

@dp.message(Command("dbcheck"))
async def cmd_dbcheck(message: types.Message):
    """Проверка содержимого базы данных"""
    user = await get_or_create_user(message)

    categories = await get_all_categories(user.id)

    text = f"📊 **Категории пользователя {user.id}**\n\n"

    if not categories:
        text += "Нет категорий"
    else:
        for cat in categories:
            source = "⭐" if cat['source'] == 'default' else "✏️"
            text += f"{source} {cat['emoji']} {cat['name']} | {cat['type']}\n"

    await message.answer(text, parse_mode="Markdown")

@dp.message(Command("myid"))
async def cmd_myid(message: types.Message):
    """Показывает ID пользователя в базе данных"""
    user = await get_or_create_user(message)
    await message.answer(f"Ваш ID в базе данных: {user.id}\nВаш Telegram ID: {message.from_user.id}")

@dp.message(Command("debug"))
async def cmd_debug(message: types.Message):
    """Диагностика категорий"""
    user = await get_or_create_user(message)

    db = get_db()
    default_cats = db.query(DefaultCategory).all()
    custom_cats = db.query(CustomCategory).filter_by(user_id=user.id).all()
    db.close()

    text = f"📊 **Диагностика для user_id={user.id}**\n\n"

    text += f"📋 Стандартных категорий: {len(default_cats)}\n"
    for cat in default_cats:
        text += f"  ⭐ {cat.emoji} {cat.name} ({cat.type})\n"

    text += f"\n📋 Пользовательских категорий: {len(custom_cats)}\n"
    for cat in custom_cats:
        text += f"  ✏️ {cat.emoji} {cat.name} ({cat.type})\n"

    # Проверяем через get_all_categories
    all_cats = await get_all_categories(user.id)
    text += f"\n📋 Всего через get_all_categories: {len(all_cats)}"

    await message.answer(text, parse_mode="Markdown")
# ========== ЗАПУСК ==========

async def main():
    logger.info("Starting bot...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
