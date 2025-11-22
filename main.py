import os
import time
from openai import OpenAI
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# структура сообщения:
# {
#   "text": "...",
#   "user": "Имя",
#   "time": timestamp
# }
messages_cache = []


# --- сохраняем входящие сообщения ---
async def save_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    user_name = (
        update.message.from_user.first_name
        or update.message.from_user.username
        or "Неизвестный пользователь"
    )

    messages_cache.append({
        "text": update.message.text,
        "user": user_name,
        "time": time.time()
    })

    # удаляем старше суток
    cutoff = time.time() - 24 * 3600
    while messages_cache and messages_cache[0]["time"] < cutoff:
        messages_cache.pop(0)


# --- команда /summary ---
async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cutoff = time.time() - 24 * 3600
    last_msgs = [m for m in messages_cache if m["time"] >= cutoff]

    if not last_msgs:
        await update.message.reply_text("За последние сутки сообщений нет 😺")
        return

    # готовим формат для ChatGPT
    formatted = []
    for m in last_msgs:
        formatted.append(f"{m['user']}: {m['text']}")

    formatted_text = "\n".join(formatted)

    prompt = f"""
Ты — дружелюбный, остроумный летописец чата. 
Пиши живо, с лёгким юмором, будто рассказываешь забавные эпизоды из жизни людей.
Без токсичности, без злых шуток — тепло, иронично, наблюдательно.

Вот список сообщений за последние сутки  
(каждая строка: "<пользователь>: <сообщение>"):

{formatted_text}

Составь смешную персональную сводку.

Требования:
1. Сгруппируй сообщения по пользователям.
2. Для каждого человека создай маленькую историю: чем он "жил" в чате.
3. Можно слегка приукрасить или драматизировать для юмора.
4. Пиши без ограничений по длине — столько предложений, сколько нужно.
5. Каждый пользователь — отдельный абзац.
6. Избегай фраз типа "пользователь написал". Пиши как мини-хронику.
7. Тон: тёплый юмор, дружелюбное подшучивание, наблюдательность.

Выведи только текст сводки.
"""

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=500
    )

    summary_text = response.choices[0].message["content"]
    await update.message.reply_text(summary_text)


# --- запуск бота ---
def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        print("Ошибка: переменная TELEGRAM_BOT_TOKEN не найдена")
        return

    app = ApplicationBuilder().token(token).build()

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, save_message))
    app.add_handler(CommandHandler("summary", summary))

    print("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
