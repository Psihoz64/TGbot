from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from datetime import datetime

Base = declarative_base()

class User(Base):
    __tablename__ = 'users'

    id = Column(Integer, primary_key=True)
    telegram_id = Column(Integer, unique=True, nullable=False)
    username = Column(String)
    currency = Column(String, default='RUB')
    created_at = Column(DateTime, default=datetime.now)

    custom_categories = relationship('CustomCategory', back_populates='user')
    transactions = relationship('Transaction', back_populates='user')

class DefaultCategory(Base):
    __tablename__ = 'default_categories'

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    type = Column(String, nullable=False)  # 'income' или 'expense'
    emoji = Column(String, default='📌')
    created_at = Column(DateTime, default=datetime.now)

class CustomCategory(Base):
    __tablename__ = 'custom_categories'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    name = Column(String, nullable=False)
    type = Column(String, nullable=False)  # 'income' или 'expense'
    emoji = Column(String, default='📌')
    created_at = Column(DateTime, default=datetime.now)

    user = relationship('User', back_populates='custom_categories')
    # Убираем связь с транзакциями, чтобы избежать ошибки

class Transaction(Base):
    __tablename__ = 'transactions'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    category_type = Column(String, nullable=False)  # 'default' или 'custom'
    category_id = Column(Integer, nullable=False)  # ID из соответствующей таблицы
    amount = Column(Float, nullable=False)
    type = Column(String, nullable=False)  # 'income' или 'expense'
    description = Column(String, default='')
    date = Column(DateTime, default=datetime.now)
    created_at = Column(DateTime, default=datetime.now)

    user = relationship('User', back_populates='transactions')

# Создаём подключение к БД
engine = create_engine('sqlite:///budget.db', echo=True)
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)

# Функция для получения сессии
def get_db():
    return Session()
