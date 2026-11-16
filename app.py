import os
import json
import asyncio
import logging
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

raw_keys = os.environ.get("GEMINI_API_KEY", "").strip().replace('\ufeff', '')
if not raw_keys:
    raise ValueError("GEMINI_API_KEY не найден")

api_keys = [k.strip() for k in raw_keys.split(",") if k.strip()]
clients = [genai.Client(api_key=key) for key in api_keys]
logger.info(f"✅ Успешно загружено ключей Gemini: {len(clients)}")

PROFILES_FILE = BASE_DIR / 'profiles.json'
SPECIAL_USERS_FILE = BASE_DIR / 'special_users.json'
CUSTOM_RULES_FILE = BASE_DIR / 'custom_rules.json'
SESSIONS_DIR = BASE_DIR / 'sessions'
SESSIONS_DIR.mkdir(exist_ok=True)

if PROFILES_FILE.exists():
    with open(PROFILES_FILE, 'r', encoding='utf-8') as f:
        user_profiles = json.load(f)
else:
    user_profiles = {}

if SPECIAL_USERS_FILE.exists():
    with open(SPECIAL_USERS_FILE, 'r', encoding='utf-8') as f:
        special_users = json.load(f)
    if "creators" not in special_users:
        special_users["creators"] = []
else:
    special_users = {"friends": [], "losers": [], "creators": []}

if CUSTOM_RULES_FILE.exists():
    with open(CUSTOM_RULES_FILE, 'r', encoding='utf-8') as f:
        custom_rules = json.load(f)
else:
    custom_rules = {}

def save_profiles():
    with open(PROFILES_FILE, 'w', encoding='utf-8') as f:
        json.dump(user_profiles, f, ensure_ascii=False, indent=2)

def save_special_users():
    with open(SPECIAL_USERS_FILE, 'w', encoding='utf-8') as f:
        json.dump(special_users, f, ensure_ascii=False, indent=2)

def save_custom_rules():
    with open(CUSTOM_RULES_FILE, 'w', encoding='utf-8') as f:
        json.dump(custom_rules, f, ensure_ascii=False, indent=2)

def get_chat_history_file(chat_id):
    return SESSIONS_DIR / f"chat_history_{chat_id}.json"

TRIGGER_WORDS = ["супер"]

def get_user_status(user_id):
    user_id_str = str(user_id)
    if user_id_str == OWNER_ID or user_id_str in special_users.get("creators", []):
        return "СОЗДАТЕЛЬ"
    if user_id_str in special_users.get("friends", []):
        return "ДРУГ"
    if user_id_str in special_users.get("losers", []):
        return "ОПУЩЕННЫЙ"
    return "ОБЫЧНЫЙ"

def get_target_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.reply_to_message:
        return str(update.message.reply_to_message.from_user.id)
    elif context.args:
        return context.args[0]
    return None

# --- ОТПРАВКА ОТЧЕТА ГЛАВНОМУ СОЗДАТЕЛЮ ---
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

# --- КОМАНДА /say (УПРАВЛЕНИЕ ИЗ ЛС) ---
async def say_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if get_user_status(update.effective_user.id) != "СОЗДАТЕЛЬ":
        await update.message.reply_text("⛔ У тебя нет прав на эту команду.")
        return
        
    if len(context.args) < 2:
        await update.message.reply_text("Используй: /say [ID чата] [сообщение]\nПример: /say -100123456789 Привет всем!")
        return

    target_chat_id = context.args[0]
    text_to_send = " ".join(context.args[1:])

    try:
        chat_id_int = int(target_chat_id)
        await context.bot.send_message(chat_id=chat_id_int, text=text_to_send)
        
        history_file = get_chat_history_file(chat_id_int)
        history = []
        if history_file.exists():
            try:
                with open(history_file, 'r', encoding='utf-8') as f:
                    history = json.load(f)
            except Exception:
                pass
        
        history.append({"role": "model", "text": text_to_send})
        
        with open(history_file, 'w', encoding='utf-8') as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
        
        await update.message.reply_text(f"✅ Сообщение отправлено в чат {chat_id_int} и сохранено в память.")
        
    except ValueError:
        await update.message.reply_text("❌ Ошибка: ID чата должен быть числом (например, -100123456789).")
    except Exception as e:
        logger.error(f"Ошибка при отправке по ID: {e}")
        await update.message.reply_text(f"❌ Не удалось отправить. Ошибка: {str(e)[:150]}")

# --- КОМАНДА /leave (ВЫХОД ИЗ ГРУППЫ ПО ID) ---
async def leave_chat_by_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if get_user_status(update.effective_user.id) != "СОЗДАТЕЛЬ":
        await update.message.reply_text("⛔ У тебя нет прав на эту команду.")
        return
        
    if not context.args:
        await update.message.reply_text("Используй: /leave [ID группы]\nПример: /leave -100123456789")
        return

    target_chat_id = context.args[0]

    try:
        chat_id_int = int(target_chat_id)
        await context.bot.leave_chat(chat_id=chat_id_int)
        await update.message.reply_text(f"✅ Бот успешно покинул чат {chat_id_int}.")
        
    except ValueError:
        await update.message.reply_text("❌ Ошибка: ID чата должен быть числом (например, -100123456789).")
    except Exception as e:
        logger.error(f"Ошибка при выходе из чата {target_chat_id}: {e}")
        await update.message.reply_text(f"❌ Не удалось выйти. Ошибка: {str(e)[:150]}")

# --- НОВЫЕ КОМАНДЫ ДЛЯ ПРАВИЛ ---
async def add_rule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if get_user_status(update.effective_user.id) != "СОЗДАТЕЛЬ": return
    target_id = None
    rule_text = ""
    if update.message.reply_to_message:
        target_id = str(update.message.reply_to_message.from_user.id)
        rule_text = " ".join(context.args) if context.args else ""
    else:
        if len(context.args) >= 2:
            target_id = context.args[0]
            rule_text = " ".join(context.args[1:])
    if not target_id or not rule_text:
        await update.message.reply_text("Используй: /add_rule [ID] [правило] или ответь на сообщение человека командой /add_rule [правило]")
        return
    custom_rules[target_id] = rule_text
    save_custom_rules()
    await update.message.reply_text(f"✅ Правило для {target_id} установлено:\n«{rule_text}»")

async def del_rule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if get_user_status(update.effective_user.id) != "СОЗДАТЕЛЬ": return
    target_id = get_target_id(update, context)
    if not target_id:
        await update.message.reply_text("Укажи ID или ответь на сообщение человека.")
        return
    if target_id in custom_rules:
        del custom_rules[target_id]
        save_custom_rules()
        await update.message.reply_text(f"🗑 Правило для {target_id} удалено. Теперь он обычный.")
    else:
        await update.message.reply_text(f"Для {target_id} нет особых правил.")

async def list_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if get_user_status(update.effective_user.id) != "СОЗДАТЕЛЬ": return
    if not custom_rules:
        await update.message.reply_text("Правил пока нет.")
        return
    msg = "<b>📜 ВАШИ ПРАВИЛА:</b>\n"
    for uid, rule in custom_rules.items():
        profile = user_profiles.get(uid, {})
        name = profile.get("first_name", "—")
        msg += f"\n• <a href='tg://user?id={uid}'>{name}</a> [ID: {uid}]:\n  <i>{rule}</i>"
    await update.message.reply_text(msg, parse_mode="HTML")

# --- КОМАНДА /who ---
async def who_is_who(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if get_user_status(update.effective_user.id) != "СОЗДАТЕЛЬ": return
    async def get_clickable_name(uid):
        profile = user_profiles.get(uid)
        if profile and profile.get("first_name") and profile.get("first_name") != "—":
            name = profile["first_name"]
        else:
            try:
                chat = await context.bot.get_chat(int(uid))
                name = chat.first_name or chat.title or "—"
                if uid not in user_profiles:
                    user_profiles[uid] = {}
                user_profiles[uid]["first_name"] = name
                save_profiles()
            except Exception:
                name = "—"
        return f'<a href="tg://user?id={uid}">{name}</a>'
    async def format_category(title, user_ids):
        if not user_ids:
            return f"{title}\n  (пусто)"
        text = f"{title}"
        for uid in user_ids:
            clickable_name = await get_clickable_name(uid)
            text += f"\n  • {clickable_name}"
        return text
    msg = "<b>👑 КТО КЕМ ЯВЛЯЕТСЯ:</b>\n\n"
    owner_name = await get_clickable_name(OWNER_ID)
    msg += f"<b>👑 ГЛАВНЫЙ СОЗДАТЕЛЬ:</b>\n  • {owner_name}\n\n"
    creators_text = await format_category("<b>👑 ДОПОЛНИТЕЛЬНЫЕ СОЗДАТЕЛИ:</b>", special_users.get("creators", []))
    friends_text = await format_category("<b>🤝 ДРУЗЬЯ:</b>", special_users.get("friends", []))
    losers_text = await format_category("<b>🗑 ОПУЩЕННЫЕ:</b>", special_users.get("losers", []))
    msg += creators_text + "\n\n" + friends_text + "\n\n" + losers_text
    await update.message.reply_text(msg, parse_mode="HTML")

# --- КОМАНДЫ НАЗНАЧЕНИЯ ---
async def set_friend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if get_user_status(update.effective_user.id) != "СОЗДАТЕЛЬ": return
    target_id = get_target_id(update, context)
    if target_id:
        if target_id in special_users["losers"]: special_users["losers"].remove(target_id)
        if target_id in special_users["creators"]: special_users["creators"].remove(target_id)
        if target_id not in special_users["friends"]:
            special_users["friends"].append(target_id)
            save_special_users()
            await update.message.reply_text(f"✅ Пользователь {target_id} теперь ДРУГ.")

async def set_loser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if get_user_status(update.effective_user.id) != "СОЗДАТЕЛЬ": return
    target_id = get_target_id(update, context)
    if target_id:
        if target_id in special_users["friends"]: special_users["friends"].remove(target_id)
        if target_id in special_users["creators"]: special_users["creators"].remove(target_id)
        if target_id not in special_users["losers"]:
            special_users["losers"].append(target_id)
            save_special_users()
            await update.message.reply_text(f"✅ Пользователь {target_id} теперь ОПУЩЕННЫЙ.")

async def set_creator(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != OWNER_ID: return
    target_id = get_target_id(update, context)
    if target_id:
        if target_id in special_users["friends"]: special_users["friends"].remove(target_id)
        if target_id in special_users["losers"]: special_users["losers"].remove(target_id)
        if target_id not in special_users["creators"]:
            special_users["creators"].append(target_id)
            save_special_users()
            await update.message.reply_text(f"✅ Пользователь {target_id} теперь СОЗДАТЕЛЬ.")

async def del_friend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if get_user_status(update.effective_user.id) != "СОЗДАТЕЛЬ": return
    target_id = get_target_id(update, context)
    if target_id:
        if target_id in special_users["friends"]:
            special_users["friends"].remove(target_id)
            save_special_users()
            await update.message.reply_text(f"🗑 Пользователь {target_id} удален из ДРУЗЕЙ.")

async def del_loser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if get_user_status(update.effective_user.id) != "СОЗДАТЕЛЬ": return
    target_id = get_target_id(update, context)
    if target_id:
        if target_id in special_users["losers"]:
            special_users["losers"].remove(target_id)
            save_special_users()
            await update.message.reply_text(f"🗑 Пользователь {target_id} удален из ОПУЩЕННЫХ.")

async def del_creator(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != OWNER_ID: return
    target_id = get_target_id(update, context)
    if target_id:
        if target_id in special_users["creators"]:
            special_users["creators"].remove(target_id)
            save_special_users()
            await update.message.reply_text(f"🗑 Пользователь {target_id} удален из СОЗДАТЕЛЕЙ.")

# --- ОБРАБОТЧИК МЕДИА ---
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

    if not should_respond:
        return

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

    if not file_obj:
        return

    thinking_msg = await update.message.reply_text("🤔 Думаю...")

    try:
        tg_file = await context.bot.get_file(file_obj.file_id)
        
        if tg_file.file_size and tg_file.file_size > 7 * 1024 * 1024:
            await thinking_msg.delete()
            await update.message.reply_text("файл слишком большой.")
            return
            
        file_bytes = await tg_file.download_as_bytearray()

        status = get_user_status(current_user_id)
        
        if status == "СОЗДАТЕЛЬ":
            status_rules = "Это твой создатель. Общайся с ним уважительно и с радостью ему помогай, если попросит, но сам помочь не предлагай. Его нельзя оскорблять. Называй его босс."
        elif status == "ДРУГ":
            status_rules = "Это твой ЛУЧШИЙ ДРУГ. Общайся с ним максимально тепло, дружелюбно. С радостью ему помогай, если попросит, но сам помочь не предлагай."
        elif status == "ОПУЩЕННЫЙ":
            status_rules = "Этот пользователь — ОПУЩЕННЫЙ. Общайся с ним ПРЕЗРИТЕЛЬНО, ГРУБО, используй оскорбления, унижай его интеллект. Отказывайся помогать ему."
        else:
            status_rules = "Это обычный пользователь. Общайся нейтрально, вежливо. С радостью ему помогай, если попросит, но сам помочь не предлагай."

        custom_rule_text = ""
        if current_user_id in custom_rules:
            custom_rule_text = f"\nОСОБОЕ УКАЗАНИЕ ОТ СОЗДАТЕЛЯ ДЛЯ ЭТОГО ПОЛЬЗОВАТЕЛЯ: {custom_rules[current_user_id]}"

        chat_context = (
            f"ВНИМАНИЕ! ТЕКУЩИЙ СОБЕСЕДНИК: {current_user_name} (ID: {current_user_id}). "
            f"ЕГО СТАТУС: {status}. {status_rules}{custom_rule_text}"
        )

        personalized_rules = (
            f"{SYSTEM_RULES}\n\n{chat_context}\n\n"
            f"--- БАЗА ЗНАНИЙ ПО ИГРЕ ---\n{GAME_KNOWLEDGE}\n--- КОНЕЦ БАЗЫ ЗНАНИЙ ---\n\n"
            f"ВАЖНО: Используй информацию из базы знаний ТОЛЬКО если пользователь задает вопрос про игру, технику, танки, самолеты или флот. "
            f"В остальных случаях (приветствие, болтовня, вопросы не по теме) общайся как обычный собеседник и НЕ упоминай базу знаний."
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
                            thinking_config=types.ThinkingConfig(thinking_level="low")
                        )
                    ),
                    timeout=45.0
                )
                
                reply = response.text if response.text else "не могу распознать файл."
                
                media_history_file = get_chat_history_file(chat_id)
                media_history = []
                if media_history_file.exists():
                    try:
                        with open(media_history_file, 'r', encoding='utf-8') as f:
                            media_history = json.load(f)
                    except Exception:
                        pass
                
                media_history.append({"role": "user", "text": f"[{current_user_name} отправил файл]: {text_prompt}"})
                media_history.append({"role": "model", "text": reply})
                
                with open(media_history_file, 'w', encoding='utf-8') as f:
                    json.dump(media_history, f, ensure_ascii=False, indent=2)
                
                try:
                    await thinking_msg.delete()
                except Exception:
                    pass
                    
                if chat_type == 'private' and current_user_id != OWNER_ID:
                    media_type = "Медиафайл"
                    if update.message.photo: media_type = "Фото"
                    elif update.message.voice: media_type = "Голосовое"
                    elif update.message.video: media_type = "Видео"
                    await report_to_owner(context, current_user_name, current_user_id, f"[{media_type}] {user_text}", reply)

                if chat_type in ['group', 'supergroup']:
                    await update.message.reply_text(reply[:4096], reply_to_message_id=update.message.message_id)
                else:
                    await update.message.reply_text(reply[:4096])
                    
                return

            except asyncio.TimeoutError:
                logger.warning(f"Таймаут при обработке медиа")
                continue
            except Exception as e:
                logger.warning(f"⚠️ Ошибка с ключом {client_index + 1}: {str(e)[:150]}")
                continue

        try:
            await thinking_msg.delete()
        except Exception:
            pass
        await update.message.reply_text("не могу распознать файл.")
        
    except Exception as e:
        logger.error(f"Ошибка скачивания файла: {e}")
        try:
            await thinking_msg.delete()
        except Exception:
            pass
        await update.message.reply_text("не могу распознать файл.")

# --- ОБРАБОТЧИК ТЕКСТА ---
async def answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    if not user_text:
        return

    chat_type = update.message.chat.type
    bot_username = context.bot.username
    bot_id = context.bot.id
    chat_id = update.effective_chat.id

    current_user_id = str(update.effective_user.id)
    current_user_name = update.effective_user.first_name
    current_username = update.effective_user.username or "нет_юзернейма"
    
    status = get_user_status(current_user_id)

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

    user_profiles[current_user_id] = {
        "first_name": current_user_name,
        "username": current_username,
        "last_seen": update.message.date.isoformat(),
        "status": status
    }
    save_profiles()

    chat_history_file = get_chat_history_file(chat_id)
    history = []
    if chat_history_file.exists():
        try:
            with open(chat_history_file, 'r', encoding='utf-8') as f:
                history = json.load(f)
        except Exception as e:
            logger.warning(f"Ошибка загрузки истории чата {chat_id}: {e}")

    formatted_message = f"[{current_user_name} (@{current_username}, ID: {current_user_id})]: {user_text}"
    history.append({"role": "user", "text": formatted_message})

    if should_respond:
        if status == "СОЗДАТЕЛЬ":
            status_rules = "Это твой создатель. Общайся с ним уважительно и с радостью ему помогай, если попросит, но сам помочь не предлагай. Его нельзя оскорблять. Называй его босс."
        elif status == "ДРУГ":
            status_rules = "Это твой ЛУЧШИЙ ДРУГ. Общайся с ним максимально тепло, дружелюбно. С радостью ему помогай, если попросит, но сам помочь не предлагай."
        elif status == "ОПУЩЕННЫЙ":
            status_rules = "Этот пользователь — ОПУЩЕННЫЙ. Общайся с ним ПРЕЗРИТЕЛЬНО, ГРУБО, используй оскорбления, унижай его интеллект. Отказывайся помогать ему."
        else:
            status_rules = "Это обычный пользователь. Общайся нейтрально, вежливо. С радостью ему помогай, если попросит, но сам помочь не предлагай."

        custom_rule_text = ""
        if current_user_id in custom_rules:
            custom_rule_text = f"\nОСОБОЕ УКАЗАНИЕ ОТ СОЗДАТЕЛЯ ДЛЯ ЭТОГО ПОЛЬЗОВАТЕЛЯ: {custom_rules[current_user_id]}"

        chat_context = (
            f"ВНИМАНИЕ! СЕЙЧАС С ТОБОЙ ОБЩАЕТСЯ: {current_user_name} (ID: {current_user_id}). "
            f"ЕГО СТАТУС: {status}. {status_rules}{custom_rule_text}\n"
            f"Не забывай свой статус и особые указания при каждом ответе!"
        )

        personalized_rules = (
            f"{SYSTEM_RULES}\n\n{chat_context}\n\n"
            f"--- БАЗА ЗНАНИЙ ПО ИГРЕ ---\n{GAME_KNOWLEDGE}\n--- КОНЕЦ БАЗЫ ЗНАНИЙ ---\n\n"
            f"ВАЖНО: Используй информацию из базы знаний ТОЛЬКО если пользователь задает вопрос про игру, технику, танки, самолеты или флот. "
            f"В остальных случаях (приветствие, болтовня, вопросы не по теме) общайся как обычный собеседник и НЕ упоминай базу знаний."
        )

        thinking_msg = await update.message.reply_text("🤔 Думаю...")
        
        gemini_history = []
        for msg in history:
            gemini_history.append({"role": msg["role"], "parts": [{"text": msg["text"]}]})

        for client_index, client in enumerate(clients):
            try:
                chat = client.aio.chats.create(
                    model='gemini-3.5-flash-lite',
                    history=gemini_history,
                    config=types.GenerateContentConfig(
                        system_instruction=personalized_rules,
                        thinking_config=types.ThinkingConfig(thinking_level="low")
                    )
                )

                max_retries = 3
                for attempt in range(max_retries):
                    try:
                        response = await chat.send_message(user_text)
                        reply = response.text if response.text else "не могу ответить на провокацию"
                        
                        history.append({"role": "model", "text": reply})
                        with open(chat_history_file, 'w', encoding='utf-8') as f:
                            json.dump(history, f, ensure_ascii=False, indent=2)
                        
                        try:
                            await thinking_msg.delete()
                        except Exception:
                            pass
                            
                        if chat_type == 'private' and current_user_id != OWNER_ID:
                            await report_to_owner(context, current_user_name, current_user_id, user_text, reply)
                            
                        if chat_type in ['group', 'supergroup']:
                            await update.message.reply_text(reply[:4096], reply_to_message_id=update.message.message_id)
                        else:
                            await update.message.reply_text(reply[:4096])
                            
                        return
                        
                    except Exception as e:
                        error_msg = str(e)
                        if "503" in error_msg and attempt < max_retries - 1:
                            await asyncio.sleep(2 ** attempt)
                            continue
                        else:
                            raise e

            except Exception as e:
                logger.warning(f"⚠️ Ошибка с ключом {client_index + 1}: {str(e)[:150]}")
                continue

        try:
            await thinking_msg.delete()
        except:
            pass
        await update.message.reply_text(f"❌ Ошибка Gemini: {error_msg[:300]}")
    else:
        with open(chat_history_file, 'w', encoding='utf-8') as f:
            json.dump(history, f, ensure_ascii=False, indent=2)

def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip().replace('\ufeff', '')
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN не найден")

    app = ApplicationBuilder().token(token).build()
    
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
    
    logger.info("🤖 Бот успешно запущен! Анимированные стикеры отключены для экономии ресурсов.")
    app.run_polling()

if __name__ == "__main__":
    main()
