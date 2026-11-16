import os
import asyncio
import logging
import asyncpg
from pathlib import Path
from dotenv import load_dotenv
from google import genai
from google.genai import types
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes, CommandHandler

BASE_DIR = Path(__file__).parent
env_path = BASE_DIR / '.env'
load_dotenv(dotenv_path=env_path)

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- ЗАГРУЖАЕМ ПРАВИЛА И ЗНАНИЯ ---
rules_path = BASE_DIR / 'rules.txt'
try:
    with open(rules_path, 'r', encoding='utf-8') as f:
        SYSTEM_RULES = f.read().strip()
except FileNotFoundError:
    SYSTEM_RULES = "Ты — полезный ассистент."

knowledge_path = BASE_DIR / 'knowledge.txt'
try:
    with open(knowledge_path, 'r', encoding='utf-8') as f:
        GAME_KNOWLEDGE = f.read().strip()
except FileNotFoundError:
    GAME_KNOWLEDGE = "Информация по игре пока отсутствует."

# --- ЗАГРУЖАЕМ ГЛАВНОГО СОЗДАТЕЛЯ ---
owner_path = BASE_DIR / 'owner.txt'
try:
    with open(owner_path, 'r', encoding='utf-8') as f:
        OWNER_ID = f.read().strip()
except FileNotFoundError:
    OWNER_ID = "НЕ_УКАЗАН"

# --- БАЗА ДАННЫХ (SUPABASE) ---
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL не найден в переменных окружения!")

db_pool = None

async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    async with db_pool.acquire() as conn:
        # Таблица для статусов (друзья, опущенные, создатели)
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS user_statuses (
                user_id TEXT PRIMARY KEY,
                status TEXT NOT NULL
            )
        ''')
        # Таблица для кастомных правил
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS custom_rules (
                user_id TEXT PRIMARY KEY,
                rule_text TEXT NOT NULL
            )
        ''')
        # Таблица для профилей (имена)
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS profiles (
                user_id TEXT PRIMARY KEY,
                first_name TEXT,
                username TEXT,
                last_seen TEXT
            )
        ''')
        # Таблица для истории чатов
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS chat_history (
                chat_id TEXT,
                role TEXT,
                text TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
    logger.info("✅ База данных Supabase подключена и таблицы готовы.")

# --- ФУНКЦИИ РАБОТЫ С БД ---
async def db_get_status(user_id: str):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow('SELECT status FROM user_statuses WHERE user_id = $1', user_id)
        return row['status'] if row else "ОБЫЧНЫЙ"

async def db_set_status(user_id: str, status: str):
    async with db_pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO user_statuses (user_id, status) VALUES ($1, $2)
            ON CONFLICT (user_id) DO UPDATE SET status = $2
        ''', user_id, status)

async def db_get_rule(user_id: str):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow('SELECT rule_text FROM custom_rules WHERE user_id = $1', user_id)
        return row['rule_text'] if row else None

async def db_set_rule(user_id: str, rule_text: str):
    async with db_pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO custom_rules (user_id, rule_text) VALUES ($1, $2)
            ON CONFLICT (user_id) DO UPDATE SET rule_text = $2
        ''', user_id, rule_text)

async def db_del_rule(user_id: str):
    async with db_pool.acquire() as conn:
        await conn.execute('DELETE FROM custom_rules WHERE user_id = $1', user_id)

async def db_get_all_with_status(status: str):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch('SELECT user_id FROM user_statuses WHERE status = $1', status)
        return [row['user_id'] for row in rows]

async def db_save_profile(user_id: str, first_name: str, username: str, last_seen: str):
    async with db_pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO profiles (user_id, first_name, username, last_seen) VALUES ($1, $2, $3, $4)
            ON CONFLICT (user_id) DO UPDATE SET first_name = $2, username = $3, last_seen = $4
        ''', user_id, first_name, username, last_seen)

async def db_get_profile_name(user_id: str):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow('SELECT first_name FROM profiles WHERE user_id = $1', user_id)
        return row['first_name'] if row and row['first_name'] else "—"

async def db_get_history(chat_id: str, limit=50):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch('''
            SELECT role, text FROM chat_history 
            WHERE chat_id = $1 
            ORDER BY timestamp ASC LIMIT $2
        ''', chat_id, limit)
        return [{"role": r["role"], "text": r["text"]} for r in rows]

async def db_add_history(chat_id: str, role: str, text: str):
    async with db_pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO chat_history (chat_id, role, text) VALUES ($1, $2, $3)
        ''', chat_id, role, text)

async def db_clear_history(chat_id: str):
    async with db_pool.acquire() as conn:
        await conn.execute('DELETE FROM chat_history WHERE chat_id = $1', chat_id)


# --- GEMINI ---
raw_keys = os.environ.get("GEMINI_API_KEY", "").strip().replace('\ufeff', '')
if not raw_keys:
    raise ValueError("GEMINI_API_KEY не найден")

api_keys = [k.strip() for k in raw_keys.split(",") if k.strip()]
clients = [genai.Client(api_key=key) for key in api_keys]
logger.info(f"✅ Успешно загружено ключей Gemini: {len(clients)}")

TRIGGER_WORDS = ["супер"]

async def get_user_status(user_id):
    user_id_str = str(user_id)
    if user_id_str == OWNER_ID:
        return "СОЗДАТЕЛЬ"
    status = await db_get_status(user_id_str)
    return status

async def get_target_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.reply_to_message:
        return str(update.message.reply_to_message.from_user.id)
    elif context.args:
        return context.args[0]
    return None

# --- ОТПРАВКА ОТЧЕТА ---
async def report_to_owner(context: ContextTypes.DEFAULT_TYPE, user_name: str, user_id: str, user_message: str, bot_reply: str):
    if user_id != OWNER_ID:
        try:
            report_text = (
                f"📩 <b>Новое сообщение в ЛС</b>\n"
                f"От: {user_name} [ID: {user_id}]\n"
                f"Что пишет: {user_message}\n"
                f"Что я ответил: {bot_reply}"
            )
            await context.bot.send_message(chat_id=int(OWNER_ID), text=report_text, parse_mode="HTML")
        except Exception as e:
            logger.warning(f"Не удалось отправить отчет создателю: {e}")

# --- КОМАНДЫ ---
async def say_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await get_user_status(update.effective_user.id) != "СОЗДАТЕЛЬ":
        await update.message.reply_text("⛔ У тебя нет прав на эту команду.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Используй: /say [ID чата] [сообщение]")
        return
    target_chat_id = context.args[0]
    text_to_send = " ".join(context.args[1:])
    try:
        chat_id_int = int(target_chat_id)
        await context.bot.send_message(chat_id=chat_id_int, text=text_to_send)
        await db_add_history(str(chat_id_int), "model", text_to_send)
        await update.message.reply_text(f"✅ Отправлено в {chat_id_int}")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)[:150]}")

async def leave_chat_by_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await get_user_status(update.effective_user.id) != "СОЗДАТЕЛЬ": return
    if not context.args:
        await update.message.reply_text("Используй: /leave [ID группы]")
        return
    try:
        chat_id_int = int(context.args[0])
        await context.bot.leave_chat(chat_id=chat_id_int)
        await update.message.reply_text(f"✅ Покинул {chat_id_int}")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)[:150]}")

async def add_rule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await get_user_status(update.effective_user.id) != "СОЗДАТЕЛЬ": return
    target_id = await get_target_id(update, context)
    rule_text = " ".join(context.args) if update.message.reply_to_message else " ".join(context.args[1:]) if len(context.args) >= 2 else None
    if update.message.reply_to_message:
        target_id = str(update.message.reply_to_message.from_user.id)
    elif len(context.args) >= 2:
        target_id = context.args[0]
    else:
        await update.message.reply_text("Формат: /add_rule [ID] [текст]")
        return
    await db_set_rule(target_id, rule_text)
    await update.message.reply_text(f"✅ Правило для {target_id} установлено.")

async def del_rule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await get_user_status(update.effective_user.id) != "СОЗДАТЕЛЬ": return
    target_id = await get_target_id(update, context)
    if not target_id: return
    await db_del_rule(target_id)
    await update.message.reply_text(f"🗑 Правило для {target_id} удалено.")

async def list_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await get_user_status(update.effective_user.id) != "СОЗДАТЕЛЬ": return
    async with db_pool.acquire() as conn:
        rows = await conn.fetch('SELECT user_id, rule_text FROM custom_rules')
    if not rows:
        await update.message.reply_text("Правил нет.")
        return
    msg = "<b>📜 ПРАВИЛА:</b>\n"
    for r in rows:
        name = await db_get_profile_name(r['user_id'])
        msg += f"\n• <a href='tg://user?id={r['user_id']}'>{name}</a>: <i>{r['rule_text']}</i>"
    await update.message.reply_text(msg, parse_mode="HTML")

async def who_is_who(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await get_user_status(update.effective_user.id) != "СОЗДАТЕЛЬ": return
    owner_name = await db_get_profile_name(OWNER_ID)
    creators = await db_get_all_with_status("СОЗДАТЕЛЬ")
    friends = await db_get_all_with_status("ДРУГ")
    losers = await db_get_all_with_status("ОПУЩЕННЫЙ")
    
    msg = "<b>👑 КТО КЕМ ЯВЛЯЕТСЯ:</b>\n\n"
    msg += f"<b>👑 ГЛАВНЫЙ:</b> <a href='tg://user?id={OWNER_ID}'>{owner_name}</a>\n\n"
    
    async def format_cat(title, ids):
        if not ids: return f"{title}\n  (пусто)"
        text = f"{title}"
        for uid in ids:
            name = await db_get_profile_name(uid)
            text += f"\n  • <a href='tg://user?id={uid}'>{name}</a>"
        return text

    msg += await format_cat("<b>👑 ДОП. СОЗДАТЕЛИ:</b>", creators) + "\n\n"
    msg += await format_cat("<b>🤝 ДРУЗЬЯ:</b>", friends) + "\n\n"
    msg += await format_cat("<b>🗑 ОПУЩЕННЫЕ:</b>", losers)
    await update.message.reply_text(msg, parse_mode="HTML")

async def set_friend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await get_user_status(update.effective_user.id) != "СОЗДАТЕЛЬ": return
    target_id = await get_target_id(update, context)
    if target_id:
        await db_set_status(target_id, "ДРУГ")
        await update.message.reply_text(f"✅ {target_id} теперь ДРУГ.")

async def set_loser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await get_user_status(update.effective_user.id) != "СОЗДАТЕЛЬ": return
    target_id = await get_target_id(update, context)
    if target_id:
        await db_set_status(target_id, "ОПУЩЕННЫЙ")
        await update.message.reply_text(f"✅ {target_id} теперь ОПУЩЕННЫЙ.")

async def set_creator(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != OWNER_ID: return
    target_id = await get_target_id(update, context)
    if target_id:
        await db_set_status(target_id, "СОЗДАТЕЛЬ")
        await update.message.reply_text(f"✅ {target_id} теперь СОЗДАТЕЛЬ.")

async def del_friend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await get_user_status(update.effective_user.id) != "СОЗДАТЕЛЬ": return
    target_id = await get_target_id(update, context)
    if target_id:
        await db_set_status(target_id, "ОБЫЧНЫЙ")
        await update.message.reply_text(f"🗑 {target_id} удален из ДРУЗЕЙ.")

async def del_loser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await get_user_status(update.effective_user.id) != "СОЗДАТЕЛЬ": return
    target_id = await get_target_id(update, context)
    if target_id:
        await db_set_status(target_id, "ОБЫЧНЫЙ")
        await update.message.reply_text(f"🗑 {target_id} удален из ОПУЩЕННЫХ.")

async def del_creator(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != OWNER_ID: return
    target_id = await get_target_id(update, context)
    if target_id:
        await db_set_status(target_id, "ОБЫЧНЫЙ")
        await update.message.reply_text(f"🗑 {target_id} удален из СОЗДАТЕЛЕЙ.")

# --- ОБРАБОТЧИКИ ---
async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.caption or ""
    chat_type = update.message.chat.type
    bot_username = context.bot.username
    bot_id = context.bot.id
    chat_id = update.effective_chat.id

    should_respond = False
    if chat_type == 'private':
        should_respond = True
    else:
        text_lower = user_text.lower()
        is_mentioned = bot_username and f"@{bot_username}" in user_text
        has_trigger = any(word in text_lower for word in TRIGGER_WORDS)
        is_reply_to_bot = False
        if update.message.reply_to_message and update.message.reply_to_message.from_user:
            if update.message.reply_to_message.from_user.id == bot_id:
                is_reply_to_bot = True
        if is_mentioned or has_trigger or is_reply_to_bot:
            should_respond = True
            if is_mentioned:
                user_text = user_text.replace(f"@{bot_username}", "").strip()

    if not should_respond: return

    current_user_id = str(update.effective_user.id)
    current_user_name = update.effective_user.first_name

    file_obj = None
    mime_type = "application/octet-stream"
    default_prompt = "Опиши этот файл кратко."

    if update.message.photo:
        file_obj = update.message.photo[-1]
        mime_type = "image/jpeg"
        default_prompt = "Что на этой картинке?"
    elif update.message.animation:
        file_obj = update.message.animation
        mime_type = "video/mp4"
        default_prompt = "Что происходит на этой гифке?"
    elif update.message.video:
        file_obj = update.message.video
        mime_type = "video/mp4"
        default_prompt = "Что происходит на этом видео?"
    elif update.message.voice:
        file_obj = update.message.voice
        mime_type = "audio/ogg"
        default_prompt = "Расшифруй это голосовое сообщение и ответь на сказанное."
    elif update.message.audio:
        file_obj = update.message.audio
        mime_type = update.message.audio.mime_type or "audio/mpeg"
        default_prompt = "Расшифруй этот аудиофайл и ответь на сказанное."
    elif update.message.video_note:
        file_obj = update.message.video_note
        mime_type = "video/mp4"
        default_prompt = "Что происходит на этом видео-кружке?"
    elif update.message.sticker:
        sticker = update.message.sticker
        default_prompt = "Опиши этот стикер."
        if sticker.thumbnail:
            file_obj = sticker.thumbnail
            mime_type = "image/webp"
        else:
            file_obj = sticker
            mime_type = "image/webp"

    if not file_obj: return

    thinking_msg = await update.message.reply_text("🤔 Думаю...")

    try:
        tg_file = await context.bot.get_file(file_obj.file_id)
        if tg_file.file_size and tg_file.file_size > 7 * 1024 * 1024:
            await thinking_msg.delete()
            await update.message.reply_text("файл слишком большой.")
            return
        file_bytes = await tg_file.download_as_bytearray()

        status = await get_user_status(current_user_id)
        
        if status == "СОЗДАТЕЛЬ":
            status_rules = "Это твой создатель. Общайся с ним уважительно и с радостью ему помогай, если попросит, но сам помочь не предлагай. Его нельзя оскорблять. Называй его босс."
        elif status == "ДРУГ":
            status_rules = "Это твой ЛУЧШИЙ ДРУГ. Общайся с ним максимально тепло, дружелюбно. С радостью ему помогай, если попросит, но сам помочь не предлагай."
        elif status == "ОПУЩЕННЫЙ":
            status_rules = "Этот пользователь — ОПУЩЕННЫЙ. Общайся с ним ПРЕЗРИТЕЛЬНО, ГРУБО, используй оскорбления, унижай его интеллект. Отказывайся помогать ему."
        else:
            status_rules = "Это обычный пользователь. Общайся нейтрально, вежливо. С радостью ему помогай, если попросит, но сам помочь не предлагай."

        custom_rule = await db_get_rule(current_user_id)
        custom_rule_text = f"\nОСОБОЕ УКАЗАНИЕ: {custom_rule}" if custom_rule else ""

        chat_context = (
            f"ВНИМАНИЕ! СОБЕСЕДНИК: {current_user_name} (ID: {current_user_id}). "
            f"СТАТУС: {status}. {status_rules}{custom_rule_text}"
        )

        personalized_rules = (
            f"САМОЕ ГЛАВНОЕ ПРАВИЛО: ОТВЕЧАЙ МАКСИМАЛЬНО КОРОТКО. 1-3 предложения.\n\n"
            f"{SYSTEM_RULES}\n\n{chat_context}\n\n"
            f"--- БАЗА ЗНАНИЙ ---\n{GAME_KNOWLEDGE}\n--- КОНЕЦ ---\n\n"
            f"ВАЖНО: Используй знания ТОЛЬКО если вопрос про игру."
        )

        for client_index, client in enumerate(clients):
            try:
                file_part = types.Part.from_bytes(data=bytes(file_bytes), mime_type=mime_type)
                text_prompt = user_text if user_text else default_prompt

                response = await asyncio.wait_for(
                    client.aio.models.generate_content(
                        model='gemini-3.5-flash-lite',
                        contents=[text_prompt, file_part],
                        config=types.GenerateContentConfig(
                            system_instruction=personalized_rules,
                            thinking_config=types.ThinkingConfig(thinking_level="low"),
                            max_output_tokens=150
                        )
                    ),
                    timeout=45.0
                )
                reply = response.text if response.text else "не могу распознать."
                
                await db_add_history(str(chat_id), "user", f"[{current_user_name} отправил файл]: {text_prompt}")
                await db_add_history(str(chat_id), "model", reply)

                await thinking_msg.delete()
                
                if chat_type == 'private' and current_user_id != OWNER_ID:
                    await report_to_owner(context, current_user_name, current_user_id, f"[Медиа] {user_text}", reply)

                if chat_type in ['group', 'supergroup']:
                    await update.message.reply_text(reply[:4096], reply_to_message_id=update.message.message_id)
                else:
                    await update.message.reply_text(reply[:4096])
                return

            except Exception as e:
                logger.warning(f"⚠️ Ошибка: {str(e)[:150]}")
                continue

        await thinking_msg.delete()
        await update.message.reply_text("не могу сейчас ответить.")
        
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        try: await thinking_msg.delete()
        except: pass
        await update.message.reply_text("не могу сейчас ответить.")


async def answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    user_text = update.message.text
    chat_type = update.message.chat.type
    bot_username = context.bot.username
    bot_id = context.bot.id
    chat_id = update.effective_chat.id

    current_user_id = str(update.effective_user.id)
    current_user_name = update.effective_user.first_name
    current_username = update.effective_user.username or "нет_юзернейма"
    
    status = await get_user_status(current_user_id)

    should_respond = False
    if chat_type == 'private':
        should_respond = True
    else:
        text_lower = user_text.lower()
        is_mentioned = bot_username and f"@{bot_username}" in user_text
        has_trigger = any(word in text_lower for word in TRIGGER_WORDS)
        is_reply_to_bot = False
        if update.message.reply_to_message and update.message.reply_to_message.from_user:
            if update.message.reply_to_message.from_user.id == bot_id:
                is_reply_to_bot = True
        if is_mentioned or has_trigger or is_reply_to_bot:
            should_respond = True
            if is_mentioned:
                user_text = user_text.replace(f"@{bot_username}", "").strip()

    await db_save_profile(current_user_id, current_user_name, current_username, update.message.date.isoformat())

    history = await db_get_history(str(chat_id))
    formatted_message = f"[{current_user_name} (@{current_username}, ID: {current_user_id})]: {user_text}"
    await db_add_history(str(chat_id), "user", formatted_message)

    if should_respond:
        if status == "СОЗДАТЕЛЬ":
            status_rules = "Это твой создатель. Общайся с ним уважительно и с радостью ему помогай, если попросит, но сам помочь не предлагай. Его нельзя оскорблять. Называй его босс."
        elif status == "ДРУГ":
            status_rules = "Это твой ЛУЧШИЙ ДРУГ. Общайся с ним максимально тепло, дружелюбно. С радостью ему помогай, если попросит, но сам помочь не предлагай."
        elif status == "ОПУЩЕННЫЙ":
            status_rules = "Этот пользователь — ОПУЩЕННЫЙ. Общайся с ним ПРЕЗРИТЕЛЬНО, ГРУБО, используй оскорбления, унижай его интеллект. Отказывайся помогать ему."
        else:
            status_rules = "Это обычный пользователь. Общайся нейтрально, вежливо. С радостью ему помогай, если попросит, но сам помочь не предлагай."

        custom_rule = await db_get_rule(current_user_id)
        custom_rule_text = f"\nОСОБОЕ УКАЗАНИЕ: {custom_rule}" if custom_rule else ""

        chat_context = (
            f"ВНИМАНИЕ! ОБЩАЕТСЯ: {current_user_name} (ID: {current_user_id}). "
            f"СТАТУС: {status}. {status_rules}{custom_rule_text}"
        )

        personalized_rules = (
            f"САМОЕ ГЛАВНОЕ ПРАВИЛО: ОТВЕЧАЙ МАКСИМАЛЬНО КОРОТКО. 1-3 предложения.\n\n"
            f"{SYSTEM_RULES}\n\n{chat_context}\n\n"
            f"--- БАЗА ЗНАНИЙ ---\n{GAME_KNOWLEDGE}\n--- КОНЕЦ ---\n\n"
            f"ВАЖНО: Используй знания ТОЛЬКО если вопрос про игру."
        )

        thinking_msg = await update.message.reply_text("🤔 Думаю...")
        
        gemini_history = [{"role": m["role"], "parts": [{"text": m["text"]}]} for m in history]

        for client_index, client in enumerate(clients):
            try:
                chat = client.aio.chats.create(
                    model='gemini-3.5-flash-lite',
                    history=gemini_history,
                    config=types.GenerateContentConfig(
                        system_instruction=personalized_rules,
                        thinking_config=types.ThinkingConfig(thinking_level="low"),
                        max_output_tokens=150
                    )
                )

                for attempt in range(3):
                    try:
                        response = await chat.send_message(user_text)
                        reply = response.text if response.text else "не могу ответить."
                        
                        await db_add_history(str(chat_id), "model", reply)
                        await thinking_msg.delete()
                            
                        if chat_type == 'private' and current_user_id != OWNER_ID:
                            await report_to_owner(context, current_user_name, current_user_id, user_text, reply)
                            
                        if chat_type in ['group', 'supergroup']:
                            await update.message.reply_text(reply[:4096], reply_to_message_id=update.message.message_id)
                        else:
                            await update.message.reply_text(reply[:4096])
                        return
                        
                    except Exception as e:
                        if "503" in str(e) and attempt < 2:
                            await asyncio.sleep(2 ** attempt)
                            continue
                        else:
                            raise e

            except Exception as e:
                logger.warning(f"⚠️ Ошибка с ключом: {str(e)[:150]}")
                continue

        try: await thinking_msg.delete()
        except: pass
        await update.message.reply_text("не могу сейчас ответить.")
    else:
        pass

# --- ЗАПУСК ---
async def post_init(application):
    await init_db()

def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip().replace('\ufeff', '')
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN не найден")

    app = ApplicationBuilder().token(token).post_init(post_init).build()
    
    app.add_handler(CommandHandler("say", say_message))
    app.add_handler(CommandHandler("leave", leave_chat_by_id))
    app.add_handler(CommandHandler("add_rule", add_rule))
    app.add_handler(CommandHandler("del_rule", del_rule))
    app.add_handler(CommandHandler("list_rules", list_rules))
    app.add_handler(CommandHandler("who", who_is_who))
    app.add_handler(CommandHandler("setfriend", set_friend))
    app.add_handler(CommandHandler("setloser", set_loser))
    app.add_handler(CommandHandler("setcreator", set_creator))
    app.add_handler(CommandHandler("delfriend", del_friend))
    app.add_handler(CommandHandler("delloser", del_loser))
    app.add_handler(CommandHandler("delcreator", del_creator))
    
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, answer))
    
    media_filter = (
        filters.PHOTO | filters.VIDEO | filters.ANIMATION | 
        filters.VOICE | filters.AUDIO | filters.VIDEO_NOTE | filters.Sticker.ALL
    )
    app.add_handler(MessageHandler(media_filter, handle_media))
    
    logger.info("🤖 Бот запущен с базой данных Supabase!")
    app.run_polling()

if __name__ == "__main__":
    main()
