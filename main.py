from dotenv import load_dotenv
import os
import json
import traceback
import uuid
import socket
import platform
from datetime import datetime, time, timedelta
from collections import defaultdict
from typing import Optional, Dict, Any, Tuple

import pytz
from openai import OpenAI, APIConnectionError
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# -----------------------------------------
# ЗАГРУЗКА .env
# -----------------------------------------
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Если основная попытка упала — пробуем последние N сообщений
MAX_MESSAGES_FOR_ANALYSIS = 500

# Сколько символов берём из каждого сообщения при формировании промта
MAX_TEXT_LENGTH_PER_MESSAGE = 600

# Таймзона (UTC+3)
BOT_TZ = pytz.timezone("Europe/Moscow")

# -----------------------------------------
# DATA_DIR (можно переопределить через .env: DATA_DIR=...)
# По умолчанию:
# - если существует /data — используем его
# - иначе локальную папку ./data
# -----------------------------------------
DEFAULT_DATA_DIR = "/data" if os.path.isdir("/data") else os.path.join(os.getcwd(), "data")
DATA_DIR = os.getenv("DATA_DIR", DEFAULT_DATA_DIR)
os.makedirs(DATA_DIR, exist_ok=True)

HISTORY_FILE = os.path.join(DATA_DIR, "chat_history.json")
SUMMARY_INDEX_FILE = os.path.join(DATA_DIR, "summary_index.json")
MONTHLY_STATS_SENT_FILE = os.path.join(DATA_DIR, "monthly_stats_sent.json")
ERROR_LOG_FILE = os.path.join(DATA_DIR, "error_log.txt")

# -----------------------------------------
# OPENAI
# -----------------------------------------
client = OpenAI(api_key=OPENAI_API_KEY)

# -----------------------------------------
# ХРАНИЛИЩЕ (в памяти)
# -----------------------------------------
chat_messages = defaultdict(list)          # chat_id -> list[message_data]
last_summary_index = defaultdict(int)      # chat_id -> int
monthly_stats_last_sent = defaultdict(str) # chat_id -> "YYYY-MM"

# -----------------------------------------
# ФУТЕР
# -----------------------------------------
FOOTER_TEXT = """

🧐 Бот допускает неточности в пересказе, проверяйте важные темы)"""

# -----------------------------------------
# ВСПОМОГАТЕЛЬНОЕ: время, парсинг
# -----------------------------------------
def _now_tz() -> datetime:
    return datetime.now(BOT_TZ)

def _parse_ts(ts: str) -> datetime:
    """
    Парсит timestamp из истории. Если без TZ — считаем, что это BOT_TZ.
    """
    try:
        dt = datetime.fromisoformat(ts)
    except Exception:
        dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S")

    if dt.tzinfo is None:
        dt = BOT_TZ.localize(dt)
    else:
        dt = dt.astimezone(BOT_TZ)
    return dt

def _month_range_for(dt: datetime) -> Tuple[datetime, datetime]:
    """
    Возвращает [start, end) для месяца dt в BOT_TZ
    """
    dt = dt.astimezone(BOT_TZ)
    start = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end

# -----------------------------------------
# ЛОГИРОВАНИЕ ОШИБОК
# -----------------------------------------
def log_error(error_id: str, where: str, exc: Exception, extra: Optional[Dict[str, Any]] = None) -> None:
    """
    Пишет расширенный репорт в stdout и в файл ERROR_LOG_FILE.
    """
    ts = _now_tz().isoformat()
    tb = traceback.format_exc()

    lines = []
    lines.append("\n" + "=" * 90)
    lines.append(f"{ts} | error_id={error_id} | where={where}")
    lines.append(f"Exception: {repr(exc)}")
    if extra:
        try:
            lines.append("Extra: " + json.dumps(extra, ensure_ascii=False))
        except Exception:
            lines.append("Extra (raw): " + str(extra))
    lines.append("Traceback:\n" + tb)
    lines.append("=" * 90 + "\n")

    msg = "\n".join(lines)
    print(msg)

    try:
        with open(ERROR_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(msg)
    except Exception as file_exc:
        print("Не удалось записать в error_log.txt:", repr(file_exc))

# -----------------------------------------
# ИСТОРИЯ: загрузка/сохранение
# -----------------------------------------
def load_monthly_stats_sent() -> None:
    try:
        if os.path.exists(MONTHLY_STATS_SENT_FILE):
            with open(MONTHLY_STATS_SENT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for chat_id, month_key in data.items():
                monthly_stats_last_sent[chat_id] = str(month_key)
    except Exception as e:
        print("Ошибка загрузки monthly_stats_sent:", repr(e))

def save_monthly_stats_sent() -> None:
    try:
        with open(MONTHLY_STATS_SENT_FILE, "w", encoding="utf-8") as f:
            json.dump(dict(monthly_stats_last_sent), f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("Ошибка сохранения monthly_stats_sent:", repr(e))

def load_history() -> None:
    global chat_messages, last_summary_index
    try:
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)

            for chat_id, messages in data.items():
                if chat_id in chat_messages and chat_messages[chat_id]:
                    existing_ts = {m.get("timestamp") for m in chat_messages[chat_id]}
                    for m in messages:
                        if m.get("timestamp") not in existing_ts:
                            chat_messages[chat_id].append(m)
                else:
                    chat_messages[chat_id] = messages.copy()

        if os.path.exists(SUMMARY_INDEX_FILE):
            with open(SUMMARY_INDEX_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for chat_id, idx in data.items():
                idx_int = int(idx)
                if chat_id in last_summary_index:
                    last_summary_index[chat_id] = max(last_summary_index[chat_id], idx_int)
                else:
                    last_summary_index[chat_id] = idx_int

        load_monthly_stats_sent()

    except Exception as e:
        print("Ошибка загрузки истории:", repr(e))

def save_history() -> None:
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(dict(chat_messages), f, ensure_ascii=False, indent=2)

        with open(SUMMARY_INDEX_FILE, "w", encoding="utf-8") as f:
            json.dump(dict(last_summary_index), f, ensure_ascii=False, indent=2)

        save_monthly_stats_sent()
    except Exception as e:
        print("Ошибка сохранения истории:", repr(e))

def save_message_immediately(chat_id: str) -> None:
    try:
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                existing_data = json.load(f)
        else:
            existing_data = {}

        existing_data[chat_id] = chat_messages.get(chat_id, [])

        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(existing_data, f, ensure_ascii=False, indent=2)

        with open(SUMMARY_INDEX_FILE, "w", encoding="utf-8") as f:
            json.dump(dict(last_summary_index), f, ensure_ascii=False, indent=2)

        save_monthly_stats_sent()

    except Exception as e:
        print(f"Ошибка немедленного сохранения для чата {chat_id}:", repr(e))

# -----------------------------------------
# START
# -----------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🐱 Я хроникёр вашего чата!\n"
        "Команды:\n"
        "/whatsnew — сводка\n"
        "/stats — статистика\n"
        "/netcheck — диагностика сети\n"
        "/clear_history — очистка истории"
    )

# -----------------------------------------
# СТАТИСТИКА: текущий календарный месяц
# -----------------------------------------
def _format_stats_for_period(
    messages: list,
    last_i: int,
    period_start: datetime,
    period_end: datetime
) -> str:
    period_msgs = []
    for m in messages:
        ts = m.get("timestamp")
        if not ts:
            continue
        try:
            dt = _parse_ts(ts)
        except Exception:
            continue
        if period_start <= dt < period_end:
            period_msgs.append(m)

    if not period_msgs:
        return "Нет данных за выбранный период."

    user_msg_count = defaultdict(int)
    user_media_count = defaultdict(int)
    total_media = {"photo": 0, "video": 0, "voice": 0, "document": 0}

    for msg in period_msgs:
        t = msg.get("type", "text")
        u = msg.get("username", "Аноним")
        if t == "text":
            user_msg_count[u] += 1
        else:
            user_media_count[u] += 1
            if t in total_media:
                total_media[t] += 1

    new_msgs = []
    if last_i is None or last_i < 0:
        last_i = 0

    for m in messages[last_i:]:
        ts = m.get("timestamp")
        if not ts:
            continue
        try:
            dt = _parse_ts(ts)
        except Exception:
            continue
        if period_start <= dt < period_end:
            new_msgs.append(m)

    new_media = sum(1 for m in new_msgs if m.get("type", "text") != "text")

    title = (
        "📊 Статистика чата за период: "
        + period_start.strftime("%d.%m.%Y")
        + "–"
        + (period_end - timedelta(seconds=1)).strftime("%d.%m.%Y")
        + "\n\n"
    )

    text = (
        title
        + f"Всего сообщений: {len(period_msgs)}\n"
        + f"Новых сообщений с последней сводки: {len(new_msgs)}\n"
        + f"Нового медиа: {new_media}\n\n"
        + "🏆 Топ по сообщениям:\n"
    )

    for i, (u, c) in enumerate(sorted(user_msg_count.items(), key=lambda x: x[1], reverse=True)[:15], 1):
        text += f"{i}. {u}: {c}\n"

    text += "\n🎞 Топ по медиа:\n"
    for i, (u, c) in enumerate(sorted(user_media_count.items(), key=lambda x: x[1], reverse=True)[:15], 1):
        text += f"{i}. {u}: {c}\n"

    text += "\n🔎 Медиаконтент всего:\n"
    for k, v in total_media.items():
        text += f"- {k}: {v}\n"

    return text

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    load_history()

    chat_id = str(update.effective_chat.id)
    if not chat_messages.get(chat_id):
        await update.message.reply_text("Нет данных.")
        return

    messages = chat_messages[chat_id]
    now = _now_tz()
    start_m, end_m = _month_range_for(now)
    last_i = last_summary_index.get(chat_id, 0)

    text = _format_stats_for_period(messages, last_i, start_m, end_m)
    await update.message.reply_text(text)

# -----------------------------------------
# ОЧИСТКА ИСТОРИИ
# -----------------------------------------
async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    user = update.effective_user

    admins = await context.bot.get_chat_administrators(update.effective_chat.id)
    if not any(a.user.id == user.id for a in admins):
        await update.message.reply_text("Команда только для администраторов.")
        return

    chat_messages[chat_id] = []
    last_summary_index[chat_id] = 0
    monthly_stats_last_sent[chat_id] = ""

    save_message_immediately(chat_id)
    await update.message.reply_text("История очищена 🧹")

# -----------------------------------------
# СБОР СООБЩЕНИЙ
# -----------------------------------------
async def collect_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    if update.message.text and update.message.text.startswith("/"):
        return

    chat_id = str(update.effective_chat.id)
    user = update.effective_user
    username = user.first_name or "Аноним"

    if update.message.photo:
        msg_type = "photo"
        text = update.message.caption or ""
    elif update.message.video:
        msg_type = "video"
        text = update.message.caption or ""
    elif update.message.voice:
        msg_type = "voice"
        text = ""
    elif update.message.document:
        msg_type = "document"
        text = update.message.caption or update.message.document.file_name or ""
    else:
        msg_type = "text"
        text = update.message.text or ""

    if msg_type == "text" and not text:
        return

    if len(text) > 4000:
        text = text[:4000] + "..."

    message_data = {
        "username": username,
        "user_id": user.id,
        "text": text,
        "timestamp": _now_tz().isoformat(),
        "type": msg_type,
    }

    chat_messages[chat_id].append(message_data)
    save_message_immediately(chat_id)

    if len(chat_messages[chat_id]) % 10 == 0:
        save_history()

# -----------------------------------------
# ПРОМПТ СВОДКИ (стендап-режим)
# -----------------------------------------
def generate_summary_prompt(messages: list) -> str:
    raw = ""
    for m in messages:
        text = m.get("text", "")
        if len(text) > MAX_TEXT_LENGTH_PER_MESSAGE:
            text = text[:MAX_TEXT_LENGTH_PER_MESSAGE] + "..."
        raw += f"{m.get('username', 'Аноним')}: {text}\n"

    return f"""Ты — стендап-комик и хроникёр чата одновременно: добрый, остроумный, ироничный.
Твоя задача — сделать сводку, которую реально смешно и приятно читать.

КЛЮЧЕВОЕ:
- Юмор — это наблюдение, а не насмешка.
- Никакой токсичности, унижений, хамства и "приколов" над людьми.

ФОРМАТ (обязателен):
1) мини-сцены на каждую тему, которая обсуждалась (каждая 1–4 предложения).
   - Каждая сцена начинается с эмодзи и короткой подводки (1 строка),
     затем текст сцены.
   - Можно иногда вставлять короткие ремарки в скобках: (да-да), (удивительно), (логично).
2) Упомяни как можно больше тем, которые обсуждались в чате.

ОБЯЗАТЕЛЬНЫЕ ПРАВИЛА ПРО ИМЕНА:
1) Имена пользователей — копируй строго как в сообщениях, символ в символ.
   НЕЛЬЗЯ: сокращать, изменять, склонять, "улучшать", добавлять смайлики к имени,
   менять регистр, транслитерировать, переводить на другие языки.
2) В каждой сцене упоминай только тех, кто реально в ней участвовал (обычно 1–3 человека).

ПРО СОДЕРЖАНИЕ:
- Пиши по темам, а не перечислением сообщений.
- Подмечай: внезапные повороты, драму по мелочам, прокрастинацию, "гениальные планы",
  неожиданные признания, хаос, бытовые ритуалы.
- Если пост слишком короткий для нормальной сцены — НЕ ВЫДУМЫВАЙ.
  Лучше процитируй 1–3 строки дословно и добавь короткий комментарий.

ВАЖНО:
Если тема грустная, тяжёлая или чувствительная
(болезни, утраты, тревога, конфликты, вина, эмоциональные срывы):
→ резко сбавляй тон
→ пересказывай спокойно, нейтрально, без шуток и иронии
→ без панчей, без ремарок в скобках, без "стендап-подачи"

Вот сообщения чата:
{raw}

Сделай сводку: смешно, живо, бережно, без выдумывания фактов.
"""

# -----------------------------------------
# МЕДИА СТРОКА
# -----------------------------------------
def _media_summary_line(media_counts: Dict[str, int]) -> str:
    parts = []
    if media_counts.get("photo"):
        parts.append(f"{media_counts['photo']} фотографий")
    if media_counts.get("video"):
        parts.append(f"{media_counts['video']} видео")
    if media_counts.get("voice"):
        parts.append(f"{media_counts['voice']} голосовых")
    if media_counts.get("document"):
        parts.append(f"{media_counts['document']} файлов")

    if not parts:
        return ""
    return "\n\n🔎 Также было прислано: " + ", ".join(parts)

# -----------------------------------------
# СВОДКА: генерация через OpenAI + расширенные репорты
# -----------------------------------------
async def _build_summary_from_new_messages(all_new_messages: list) -> Tuple[Optional[str], Dict[str, int], Optional[str]]:
    """
    Возвращает (summary, media_counts, error_id)

    error_id может иметь префикс:
    - "NETWORK:xxxx" — если это сетевой доступ до OpenAI
    - "xxxx" — прочие ошибки
    """
    media_counts = {"photo": 0, "video": 0, "voice": 0, "document": 0}
    for m in all_new_messages:
        t = m.get("type", "text")
        if t in media_counts:
            media_counts[t] += 1

    new = all_new_messages
    prompt = generate_summary_prompt(new)

    system_msg = (
        "Ты — стендап-хроникёр чата: добрый, ироничный, наблюдательный. "
        "КРИТИЧНО важно: имена пользователей копируй строго как они написаны, "
        "символ в символ, без любых изменений и без перевода/транслита/склонения/смены регистра. "
        "Если тема тяжёлая/чувствительная — мгновенно переходи на нейтральный тон без шуток."
    )

    # --- Primary
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": prompt},
            ],
            temperature=1.05,
            presence_penalty=0.45,
            frequency_penalty=0.2,
            max_tokens=3000,
        )
        summary = response.choices[0].message.content
        return summary, media_counts, None

    except APIConnectionError as e:
        # СЕТЬ/МАРШРУТ/ДОСТУП ДО OPENAI
        error_id = uuid.uuid4().hex[:8]
        log_error(
            error_id=error_id,
            where="openai.chat.completions.create (connection)",
            exc=e,
            extra={
                "type": "connection",
                "model": "gpt-4o-mini",
                "messages_total": len(all_new_messages),
                "used_messages": len(new),
                "data_dir": DATA_DIR,
            },
        )
        return None, media_counts, f"NETWORK:{error_id}"

    except Exception as e:
        error_id = uuid.uuid4().hex[:8]
        log_error(
            error_id=error_id,
            where="openai.chat.completions.create (primary)",
            exc=e,
            extra={
                "type": "other",
                "model": "gpt-4o-mini",
                "messages_total": len(all_new_messages),
                "used_messages": len(new),
                "data_dir": DATA_DIR,
            },
        )

        # fallback: последние MAX_MESSAGES_FOR_ANALYSIS сообщений
        if len(all_new_messages) > MAX_MESSAGES_FOR_ANALYSIS:
            new = all_new_messages[-MAX_MESSAGES_FOR_ANALYSIS:]
            prompt = generate_summary_prompt(new)
            try:
                response = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {"role": "system", "content": system_msg},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=1.0,
                    max_tokens=3000,
                )
                summary = response.choices[0].message.content
                return summary, media_counts, None

            except APIConnectionError as e2:
                error_id2 = uuid.uuid4().hex[:8]
                log_error(
                    error_id=error_id2,
                    where="openai.chat.completions.create (fallback connection)",
                    exc=e2,
                    extra={
                        "type": "connection",
                        "model": "gpt-4o-mini",
                        "messages_total": len(all_new_messages),
                        "used_messages": len(new),
                        "data_dir": DATA_DIR,
                    },
                )
                return None, media_counts, f"NETWORK:{error_id2}"

            except Exception as e2:
                error_id2 = uuid.uuid4().hex[:8]
                log_error(
                    error_id=error_id2,
                    where="openai.chat.completions.create (fallback)",
                    exc=e2,
                    extra={
                        "type": "other",
                        "model": "gpt-4o-mini",
                        "messages_total": len(all_new_messages),
                        "used_messages": len(new),
                        "data_dir": DATA_DIR,
                    },
                )
                return None, media_counts, error_id2

        return None, media_counts, error_id

# -----------------------------------------
# СВОДКА: отправка в чат
# -----------------------------------------
async def _send_summary_to_chat(chat_id: str, context: ContextTypes.DEFAULT_TYPE) -> bool:
    save_history()
    load_history()

    if not chat_messages.get(chat_id):
        return False

    messages = chat_messages[chat_id]
    last_i = last_summary_index.get(chat_id, 0)
    all_new_messages = messages[last_i:]

    if len(all_new_messages) < 3:
        return False

    summary, media_counts, _error_id = await _build_summary_from_new_messages(all_new_messages)
    if not summary:
        return False

    last_summary_index[chat_id] = len(messages)
    save_message_immediately(chat_id)

    final_text = "📰 Сводка:\n\n" + summary + _media_summary_line(media_counts) + FOOTER_TEXT
    await context.bot.send_message(chat_id=chat_id, text=final_text)
    return True

# -----------------------------------------
# РУЧНАЯ СВОДКА
# -----------------------------------------
async def whatsnew(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)

    save_history()
    load_history()

    if not chat_messages.get(chat_id):
        await update.message.reply_text("Нет сообщений.")
        return

    messages = chat_messages[chat_id]
    last_i = last_summary_index.get(chat_id, 0)
    all_new_messages = messages[last_i:]

    if len(all_new_messages) < 3:
        await update.message.reply_text(f"Новых сообщений мало ({len(all_new_messages)}).")
        return

    await update.message.reply_text(f"🤔 Анализирую {len(all_new_messages)} сообщений...")

    summary, media_counts, error_id = await _build_summary_from_new_messages(all_new_messages)
    if not summary:
        if error_id and isinstance(error_id, str) and error_id.startswith("NETWORK:"):
            clean_id = error_id.split(":", 1)[1]
            msg = (
                "🌐 Сейчас нет доступа к OpenAI из этого окружения.\n"
                "Похоже на сетевую проблему или маршрут/доступ с хостинга.\n\n"
                f"Код ошибки: {clean_id}"
            )
        else:
            msg = "❌ Ошибка при создании сводки."
            if error_id:
                msg += f"\nКод ошибки: {error_id}\nЛог: {ERROR_LOG_FILE}"
        await update.message.reply_text(msg)
        return

    last_summary_index[chat_id] = len(messages)
    save_message_immediately(chat_id)

    final_text = "📰 Сводка:\n\n" + summary + _media_summary_line(media_counts) + FOOTER_TEXT
    await update.message.reply_text(final_text)

# -----------------------------------------
# АВТОСВОДКА: 05:00 и 18:00 (UTC+3)
# -----------------------------------------
async def autosummary_job(context: ContextTypes.DEFAULT_TYPE):
    load_history()
    if not chat_messages:
        return

    for chat_id in list(chat_messages.keys()):
        try:
            await _send_summary_to_chat(chat_id, context)
        except Exception as e:
            error_id = uuid.uuid4().hex[:8]
            log_error(error_id, "autosummary_job loop", e, {"chat_id": chat_id})

# -----------------------------------------
# АВТОСТАТИСТИКА: 1-го числа 05:05 (UTC+3) + дедуп
# -----------------------------------------
async def monthly_stats_job(context: ContextTypes.DEFAULT_TYPE):
    load_history()

    now = _now_tz()
    if now.day != 1:
        return

    this_month_start, _ = _month_range_for(now)
    prev_month_end = this_month_start
    prev_month_start, _ = _month_range_for(prev_month_end - timedelta(seconds=1))
    prev_month_key = prev_month_start.strftime("%Y-%m")

    for chat_id, messages in list(chat_messages.items()):
        try:
            if monthly_stats_last_sent.get(chat_id) == prev_month_key:
                continue

            last_i = last_summary_index.get(chat_id, 0)
            text = _format_stats_for_period(messages, last_i, prev_month_start, prev_month_end)
            text = "🗓 Ежемесячная статистика\n\n" + text

            await context.bot.send_message(chat_id=chat_id, text=text)

            monthly_stats_last_sent[chat_id] = prev_month_key
            save_monthly_stats_sent()

        except Exception as e:
            error_id = uuid.uuid4().hex[:8]
            log_error(error_id, "monthly_stats_job loop", e, {"chat_id": chat_id, "prev_month_key": prev_month_key})

# -----------------------------------------
# /NETCHECK: диагностика сети из Telegram
# -----------------------------------------
def _tcp_probe(host: str, port: int, family: int, timeout: float = 5.0) -> str:
    try:
        infos = socket.getaddrinfo(host, port, family, socket.SOCK_STREAM)
    except Exception as e:
        return f"DNS/addrinfo ошибка: {repr(e)}"

    last_err = None
    for info in infos[:5]:
        _, _, _, _, sockaddr = info
        try:
            with socket.create_connection(sockaddr, timeout=timeout):
                return f"OK (через {sockaddr[0]})"
        except Exception as e:
            last_err = e

    return f"FAIL: {repr(last_err)}"

def _tls_probe(host: str, port: int = 443, timeout: float = 7.0) -> str:
    import ssl
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host):
                return "OK (TLS handshake прошёл)"
    except Exception as e:
        return f"FAIL: {repr(e)}"

async def netcheck(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = []
    lines.append("🧪 Netcheck (диагностика сети)")
    lines.append(f"🕒 Время: {_now_tz().strftime('%Y-%m-%d %H:%M:%S %Z')}")
    lines.append(f"🧩 Python: {platform.python_version()} | OS: {platform.system()} {platform.release()}")
    lines.append(f"📁 DATA_DIR: {DATA_DIR}")
    lines.append("")

    host = "api.openai.com"
    try:
        infos = socket.getaddrinfo(host, 443, 0, socket.SOCK_STREAM)
        v4 = sorted({sockaddr[0] for fam, _, _, _, sockaddr in infos if fam == socket.AF_INET})
        v6 = sorted({sockaddr[0] for fam, _, _, _, sockaddr in infos if fam == socket.AF_INET6})
        lines.append("🔎 DNS api.openai.com:")
        lines.append(f"  IPv4: {', '.join(v4[:5]) if v4 else 'нет'}")
        lines.append(f"  IPv6: {', '.join(v6[:5]) if v6 else 'нет'}")
    except Exception as e:
        lines.append(f"🔎 DNS api.openai.com: FAIL ({repr(e)})")

    lines.append("")
    lines.append("🔌 TCP connect probes:")
    lines.append(f"  example.com:443 (IPv4) → {_tcp_probe('example.com', 443, socket.AF_INET)}")
    lines.append(f"  example.com:443 (IPv6) → {_tcp_probe('example.com', 443, socket.AF_INET6)}")
    lines.append(f"  api.openai.com:443 (IPv4) → {_tcp_probe('api.openai.com', 443, socket.AF_INET)}")
    lines.append(f"  api.openai.com:443 (IPv6) → {_tcp_probe('api.openai.com', 443, socket.AF_INET6)}")

    lines.append("")
    lines.append("🔒 TLS probes:")
    lines.append(f"  api.openai.com:443 → {_tls_probe('api.openai.com', 443)}")

    http_proxy = os.getenv("HTTP_PROXY") or os.getenv("http_proxy")
    https_proxy = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
    if http_proxy or https_proxy:
        lines.append("")
        lines.append("🛰 Proxy env обнаружен:")
        if http_proxy:
            lines.append("  HTTP_PROXY: set")
        if https_proxy:
            lines.append("  HTTPS_PROXY: set")

    text = "\n".join(lines)
    if len(text) > 3500:
        text = text[:3500] + "\n…(обрезано)"

    await update.message.reply_text(text)

# -----------------------------------------
# MAIN
# -----------------------------------------
def main():
    print("=== ЗАПУСК БОТА ===")

    if not TELEGRAM_TOKEN:
        print("❌ Нет TELEGRAM_BOT_TOKEN")
        return
    if not OPENAI_API_KEY:
        print("❌ Нет OPENAI_API_KEY")
        return

    print(f"DATA_DIR: {DATA_DIR}")
    print(f"HISTORY_FILE: {HISTORY_FILE}")
    print(f"ERROR_LOG_FILE: {ERROR_LOG_FILE}")
    print(f"MAX_MESSAGES_FOR_ANALYSIS: {MAX_MESSAGES_FOR_ANALYSIS}")
    print(f"MAX_TEXT_LENGTH_PER_MESSAGE: {MAX_TEXT_LENGTH_PER_MESSAGE}")

    load_history()

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("netcheck", netcheck))
    app.add_handler(CommandHandler("clear_history", clear_history))
    app.add_handler(CommandHandler("whatsnew", whatsnew))

    app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.PHOTO | filters.VIDEO | filters.Document.ALL | filters.VOICE)
            & ~filters.COMMAND,
            collect_message,
        )
    )

    # Автосводка: 05:00 UTC+3
    app.job_queue.run_daily(
        autosummary_job,
        time=time(hour=5, minute=0, tzinfo=BOT_TZ),
        name="autosummary_0500",
    )

    # Автосводка: 18:00 UTC+3
    app.job_queue.run_daily(
        autosummary_job,
        time=time(hour=18, minute=0, tzinfo=BOT_TZ),
        name="autosummary_1800",
    )

    # Автостатистика: 05:05 UTC+3 (проверяем, что сегодня 1-е число)
    app.job_queue.run_daily(
        monthly_stats_job,
        time=time(hour=5, minute=5, tzinfo=BOT_TZ),
        name="monthly_stats_0505",
    )

    print("Бот запущен! Используй /whatsnew для ручной сводки.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
