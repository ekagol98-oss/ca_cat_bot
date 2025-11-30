from dotenv import load_dotenv
import os
import json
from datetime import datetime
from collections import defaultdict
from groq import Groq
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
import asyncio
import pytz
import time as time_module
import telegram

# Загружаем переменные окружения
load_dotenv()

# Инициализируем клиент Groq
client = Groq(api_key=os.getenv("GROQ_API_KEY"))

# Хранилище сообщений: {chat_id: [messages]}
chat_messages = defaultdict(list)

# Индекс последней проанализированной сводки: {chat_id: index}
last_summary_index = defaultdict(int)

# Файлы для сохранения истории
HISTORY_FILE = "chat_history.json"
SUMMARY_INDEX_FILE = "summary_index.json"

# Часовой пояс UTC+3
TIMEZONE = pytz.timezone('Europe/Moscow')

def load_history():
    """Загрузка истории из файла"""
    global chat_messages, last_summary_index
    try:
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                chat_messages = defaultdict(list, data)
                print(f"История загружена: {sum(len(v) for v in chat_messages.values())} сообщений")
        
        if os.path.exists(SUMMARY_INDEX_FILE):
            with open(SUMMARY_INDEX_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                last_summary_index = defaultdict(int, data)
                print(f"Индексы сводок загружены")
    except Exception as e:
        print(f"Ошибка загрузки истории: {e}")

def save_history():
    """Сохранение истории в файл"""
    try:
        with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
            json.dump(dict(chat_messages), f, ensure_ascii=False, indent=2)
        
        with open(SUMMARY_INDEX_FILE, 'w', encoding='utf-8') as f:
            json.dump(dict(last_summary_index), f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Ошибка сохранения истории: {e}")

async def save_history_async():
    """Асинхронная обертка для сохранения"""
    save_history()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    await update.message.reply_text(
        "🐱 Мяу! Я бот-хроникёр вашего чата!\n\n"
        "Я молча наблюдаю за всеми разговорами и выдаю сводки только по команде.\n\n"
        "Команды:\n"
        "/whatsnew - получить сводку новых сообщений (с последней сводки)\n"
        "/stats - статистика сообщений\n"
        "/clear_history - очистить историю (только для админов)"
    )

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Статистика по сообщениям"""
    chat_id = str(update.effective_chat.id)
    
    if chat_id not in chat_messages or not chat_messages[chat_id]:
        await update.message.reply_text("Пока нет сохранённых сообщений!")
        return
    
    messages = chat_messages[chat_id]
    user_counts = defaultdict(int)
    
    for msg in messages:
        user_counts[msg['username']] += 1
    
    # Сообщения с последней сводки
    last_index = last_summary_index.get(chat_id, 0)
    new_messages_count = len(messages) - last_index
    
    stats_text = f"📊 Статистика чата:\n\n"
    stats_text += f"Всего сообщений: {len(messages)}\n"
    stats_text += f"Новых с последней сводки: {new_messages_count}\n"
    stats_text += f"Участников: {len(user_counts)}\n\n"
    stats_text += "Топ болтунов:\n"
    
    sorted_users = sorted(user_counts.items(), key=lambda x: x[1], reverse=True)
    for i, (username, count) in enumerate(sorted_users[:5], 1):
        stats_text += f"{i}. {username}: {count} сообщений\n"
    
    await update.message.reply_text(stats_text)

async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Очистка истории (только для админов)"""
    chat_id = str(update.effective_chat.id)
    user = update.effective_user
    
    # Проверка прав администратора
    chat_admins = await context.bot.get_chat_administrators(update.effective_chat.id)
    is_admin = any(admin.user.id == user.id for admin in chat_admins)
    
    if not is_admin:
        await update.message.reply_text("Эта команда доступна только администраторам!")
        return
    
    chat_messages[chat_id] = []
    last_summary_index[chat_id] = 0
    save_history()
    await update.message.reply_text("История очищена! 🧹")

async def collect_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохранение сообщений из чата"""
    # Проверяем, что сообщение существует
    if not update.message:
        return
    
    # Игнорируем команды
    if update.message.text and update.message.text.startswith('/'):
        return
    
    chat_id = str(update.effective_chat.id)
    user = update.effective_user
    
    # Берём текст из сообщения или подпись к медиа
    text = update.message.text or update.message.caption
    
    # Если нет ни текста, ни подписи - пропускаем
    if not text:
        return
    
    # Ограничиваем длину текста для безопасности
    if len(text) > 4000:
        text = text[:4000] + "..."
    
    message_data = {
        'username': user.first_name or user.username or "Аноним",
        'user_id': user.id,
        'text': text,
        'timestamp': datetime.now().isoformat()
    }
    
    chat_messages[chat_id].append(message_data)
    
    # Сохраняем каждые 10 сообщений
    if len(chat_messages[chat_id]) % 10 == 0:
        save_history()

def generate_summary_prompt(messages, names_list):
    """Создание промпта для Groq"""
    if not messages:
        return ""
    
    # Берём все сообщения для анализа
    recent_messages = messages
    
    # Группируем сообщения по пользователям
    user_messages = defaultdict(list)
    for msg in recent_messages:
        user_messages[msg['username']].append(msg['text'])
    
    # Формируем текст для анализа
    summary_data = ""
    for username, msgs in user_messages.items():
        summary_data += f"\n{username}:\n"
        summary_data += "\n".join(msgs[:10])
        summary_data += "\n"
    
    prompt = f"""Ты — остроумный, но добрый наблюдатель чата с теплым чувством юмора. Твой стиль — интеллигентная ирония, как у умного друга, который с улыбкой пересказывает события.

ПРИОРИТЕТЫ (по важности):
1. ГЛАВНОЕ — упомянуть ВСЕХ участников: {names_list}
2. Имена копируй ТОЧНО как указано — без изменений и сокращений
3. Сохраняй добрый ироничный юмор над повседневными ситуациями
4. Длина текста не важна — пиши столько, сколько нужно

СООБЩЕНИЯ ДЛЯ АНАЛИЗА:
{summary_data}

СОСТАВЬ СВОДКУ:
- Объединяй сообщения по темам
- Упомяни КАЖДОГО участника из списка выше
- Используй добрую иронию: "эпичная битва за печенье", "великие дебаты о погоде"
- Никогда не шути над болезнями, потерями, горем
- Сохраняй тёплую, дружескую атмосферу

ЗАВЕРШИ ФРАЗОЙ:
"🎄 Напоминание от эльфов: у нас идет Тайный Санта! Все Санты должны быть лапочками и отправить подарки вовремя, чтобы подопечные получили их к Новому Году! 🎅✨"

Выведи только текст сводки."""
    
    return prompt

async def whatsnew(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Генерация сводки по команде"""
    chat_id = str(update.effective_chat.id)
    
    if chat_id not in chat_messages or not chat_messages[chat_id]:
        await update.message.reply_text("Пока нет сообщений для анализа! Напишите что-нибудь в чат.")
        return
    
    messages = chat_messages[chat_id]
    last_index = last_summary_index.get(chat_id, 0)
    
    # Берём только НОВЫЕ сообщения с последней сводки
    new_messages = messages[last_index:]
    
    if len(new_messages) < 3:
        await update.message.reply_text(f"Новых сообщений совсем мало ({len(new_messages)}). Давайте поболтаем ещё!")
        return
    
    # Получаем список всех участников для проверки
    usernames = list(set(msg['username'] for msg in new_messages))
    names_list = ", ".join(usernames)
    
    await update.message.reply_text(f"🤔 Анализирую {len(new_messages)} сообщений от {len(usernames)} участников...")
    
    try:
        prompt = generate_summary_prompt(new_messages, names_list)
        
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {
                    "role": "system",
                    "content": f"Ты добрый и остроумный наблюдатель чатов. Твой главный приоритет — упомянуть всех участников по именам: {names_list}. Используй интеллигентный юмор и тёплый тон."
                },
                {
                    "role": "user", 
                    "content": prompt
                }
            ],
            temperature=0.7,
            max_tokens=3000
        )
        
        summary = response.choices[0].message.content
        
        # Обновляем индекс последней сводки
        last_summary_index[chat_id] = len(messages)
        save_history()
        
        # Отправляем сводку
        header = f"📰 Сводка {len(new_messages)} новых сообщений:\n\n"
        await update.message.reply_text(header + summary)
        
    except Exception as e:
        print(f"Ошибка при генерации сводки: {e}")
        await update.message.reply_text("❌ Извините, произошла ошибка при генерации сводки. Попробуйте позже.")

def main():
    """Основная функция запуска бота"""
    print("=== ЗАПУСК БОТА ===")
    print(f"Текущая рабочая директория: {os.getcwd()}")
    
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    groq_key = os.getenv('GROQ_API_KEY')
    
    if not token:
        print("❌ ОШИБКА: TELEGRAM_BOT_TOKEN не найден!")
        return 1
    
    if not groq_key:
        print("❌ ОШИБКА: GROQ_API_KEY не найден!")
        return 1
    
    print("✅ Все переменные окружения загружены")
    
    # Загружаем историю при старте
    load_history()
    
    # Создаём приложение с настройками сети
    app = ApplicationBuilder().token(token).build()
    
    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("clear_history", clear_history))
    
    # Обработчик для /whatsnew
    app.add_handler(MessageHandler(
        filters.Regex(r'^/whatsnew(@\w+)?$'), 
        whatsnew
    ))
    
    # Сбор всех сообщений
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.PHOTO | filters.VIDEO | filters.Document.ALL) & ~filters.COMMAND,
        collect_message
    ))
    
    print("Бот запущен!")
    print("Режим: только ручные сводки по команде /whatsnew")
    print(f"Загружено сообщений: {sum(len(v) for v in chat_messages.values())}")
    
    # Запускаем бота с обработкой сетевых ошибок
    print("Начинаю polling...")
    
    max_retries = 5
    retry_delay = 30  # секунд
    
    for attempt in range(max_retries):
        try:
            app.run_polling(
                drop_pending_updates=True,
                close_loop=False
            )
            break  # Успешный запуск, выходим из цикла
            
        except telegram.error.NetworkError as e:
            print(f"⚠️ Сетевая ошибка (попытка {attempt + 1}/{max_retries}): {e}")
            
            if attempt < max_retries - 1:
                print(f"🔄 Повторная попытка через {retry_delay} секунд...")
                time_module.sleep(retry_delay)
                retry_delay *= 2  # Увеличиваем задержку
            else:
                print("❌ Превышено количество попыток подключения")
                return 1
                
        except telegram.error.TimedOut as e:
            print(f"⚠️ Таймаут подключения (попытка {attempt + 1}/{max_retries}): {e}")
            
            if attempt < max_retries - 1:
                print(f"🔄 Повторная попытка через {retry_delay} секунд...")
                time_module.sleep(retry_delay)
                retry_delay *= 2
            else:
                print("❌ Превышено количество попыток подключения")
                return 1
                
        except KeyboardInterrupt:
            print("Бот остановлен пользователем")
            break
        except Exception as e:
            print(f"❌ Неожиданная ошибка: {e}")
            return 1
    
    # Сохраняем историю при завершении
    save_history()
    return 0

if __name__ == '__main__':
    try:
        exit_code = main()
        if exit_code != 0:
            print(f"Бот завершил работу с кодом ошибки: {exit_code}")
            input("Нажмите Enter для выхода...")
    except Exception as e:
        print(f"ФАТАЛЬНАЯ ОШИБКА: {e}")
        import traceback
        traceback.print_exc()
        with open("crash_log.txt", "w", encoding="utf-8") as f:
            f.write(f"Ошибка: {e}\n")
            traceback.print_exc(file=f)
        input("Нажмите Enter для выхода...")
