import os
import re
import random
import sqlite3
import asyncio
from datetime import datetime, timedelta

from dotenv import load_dotenv
from telethon import TelegramClient

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery
)

# =========================================================
# LOAD ENV
# =========================================================
load_dotenv()

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

DB_NAME = "notes.db"
sync_lock = asyncio.Lock()

# =========================================================
# DATABASE DEVELOPMENTS
# =========================================================
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS quiz_notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        topic TEXT,
        question_text TEXT,
        question_msg_id INTEGER,
        answer_text TEXT,
        answer_msg_id INTEGER,
        answer_type TEXT
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS user_progress (
        user_id INTEGER,
        question_id INTEGER,
        next_available_time TEXT,
        PRIMARY KEY (user_id, question_id)
    )
    """)

    conn.commit()
    conn.close()

def save_note(topic, question_text, question_msg_id, answer_text, answer_msg_id, answer_type):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
    INSERT INTO quiz_notes (topic, question_text, question_msg_id, answer_text, answer_msg_id, answer_type)
    VALUES (?, ?, ?, ?, ?, ?)
    """, (topic, question_text, question_msg_id, answer_text, answer_msg_id, answer_type))
    conn.commit()
    conn.close()

def question_exists(question_msg_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM quiz_notes WHERE question_msg_id = ?", (question_msg_id,))
    result = cursor.fetchone()
    conn.close()
    return result is not None

def get_random_question(user_id, topic=None):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    current_time_str = datetime.now().isoformat()

    if topic:
        cursor.execute("""
        SELECT q.* FROM quiz_notes q
        LEFT JOIN user_progress p ON q.id = p.question_id AND p.user_id = ?
        WHERE q.topic = ? AND (p.next_available_time IS NULL OR p.next_available_time <= ?)
        ORDER BY RANDOM()
        LIMIT 1
        """, (user_id, topic, current_time_str))
    else:
        cursor.execute("""
        SELECT q.* FROM quiz_notes q
        LEFT JOIN user_progress p ON q.id = p.question_id AND p.user_id = ?
        WHERE p.next_available_time IS NULL OR p.next_available_time <= ?
        ORDER BY RANDOM()
        LIMIT 1
        """, (user_id, current_time_str))

    note = cursor.fetchone()
    conn.close()
    return note

def get_topics():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT topic FROM quiz_notes WHERE topic IS NOT NULL")
    topics = cursor.fetchall()
    conn.close()
    return [t[0] for t in topics if t[0]]

def update_user_progress(user_id, question_id, difficulty):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    now = datetime.now()
    
    if difficulty == "easy":
        next_time = (now + timedelta(days=7)).isoformat()
    elif difficulty == "good":
        next_time = (now + timedelta(days=3)).isoformat()
    elif difficulty == "hard":
        next_time = (now + timedelta(days=1)).isoformat()
    elif difficulty == "again":
        next_time = (now + timedelta(minutes=10)).isoformat()
    else:
        next_time = now.isoformat()

    cursor.execute("""
    INSERT INTO user_progress (user_id, question_id, next_available_time)
    VALUES (?, ?, ?)
    ON CONFLICT(user_id, question_id) 
    DO UPDATE SET next_available_time = excluded.next_available_time
    """, (user_id, question_id, next_time))
    
    conn.commit()
    conn.close()

def get_stats(user_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    cursor.execute("SELECT COUNT(*) FROM quiz_notes")
    total_questions = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM user_progress WHERE user_id = ?", (user_id,))
    reviewed_questions = cursor.fetchone()[0]
    
    current_time = datetime.now().isoformat()
    cursor.execute("SELECT COUNT(*) FROM user_progress WHERE user_id = ? AND next_available_time <= ?", (user_id, current_time))
    pending_review = cursor.fetchone()[0]
    
    cursor.execute("""
    SELECT q.topic, COUNT(p.question_id) as c 
    FROM quiz_notes q 
    JOIN user_progress p ON q.id = p.question_id 
    WHERE p.user_id = ? 
    GROUP BY q.topic ORDER BY c DESC LIMIT 1
    """, (user_id,))
    top_topic_row = cursor.fetchone()
    top_topic = top_topic_row[0] if top_topic_row else "لا يوجد"
    
    conn.close()
    return total_questions, reviewed_questions, pending_review, top_topic

def search_questions(keyword):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    like_keyword = f"%{keyword}%"
    cursor.execute("""
    SELECT id, topic, question_text 
    FROM quiz_notes 
    WHERE question_text LIKE ? OR answer_text LIKE ? 
    LIMIT 5
    """, (like_keyword, like_keyword))
    results = cursor.fetchall()
    conn.close()
    return results

# =========================================================
# TELETHON FETCHER
# =========================================================
async def fetch_notes():
    client = TelegramClient("secondary_account_session", API_ID, API_HASH)
    await client.start()
    print("Fetching notes from channel...")

    messages = []
    async for message in client.iter_messages(CHANNEL_USERNAME, limit=500):
        if message.text or message.media:
            messages.append(message)

    messages.reverse()
    current_running_topic = None

    i = 0
    while i < len(messages):
        question = messages[i]
        question_text = (question.text or "").strip()
        
        if question_text.startswith("#") and len(question_text.split()) == 1:
            current_running_topic = question_text.replace("#", "").lower()
            print(f"📌 تم رصد بداية قسم جديد: #{current_running_topic}")
            i += 1
            continue

        if not question_text:
            i += 1
            continue

        if question_exists(question.id):
            i += 1
            continue

        topic = current_running_topic
        hashtags = re.findall(r"#(\w+)", question_text)
        if hashtags:
            topic = hashtags[0].lower()

        is_merged = False
        split_char = None
        if "?" in question_text:
            split_char = "?"
        elif "؟" in question_text:
            split_char = "؟"

        if split_char:
            parts = question_text.split(split_char, 1)
            final_question = parts[0].strip() + f" {split_char}"
            final_answer = parts[1].strip()
            
            if final_answer and any(c.isalnum() for c in final_answer):
                save_note(
                    topic=topic,
                    question_text=final_question,
                    question_msg_id=question.id,
                    answer_text=final_answer,
                    answer_msg_id=question.id,
                    answer_type="text"
                )
                is_merged = True
                i += 1

        if is_merged:
            continue

        if i + 1 < len(messages):
            answer = messages[i + 1]
            answer_text = (answer.text or "").strip()
            
            if answer_text.startswith("#") and len(answer_text.split()) == 1:
                i += 1
                continue

            answer_type = "text"
            if answer.photo:
                answer_type = "image"
            elif answer.voice or answer.audio:
                answer_type = "audio"

            save_note(
                topic=topic,
                question_text=question_text,
                question_msg_id=question.id,
                answer_text=answer_text,
                answer_msg_id=answer.id,
                answer_type=answer_type
            )
            i += 2
        else:
            i += 1

    print("Notes synced successfully.")
    await client.disconnect()

# =========================================================
# BOT CONFIGURATION
# =========================================================
bot = Bot(BOT_TOKEN)
dp = Dispatcher()

active_questions = {}
user_current_topic = {} 

@dp.message(Command("start"))
async def start(message: Message):
    text = """
مرحباً بك في بوت الأسئلة الذكي 🧠

الأوامر المتاحة:
/topics -> اختيار موضوع من الأزرار المتاحة 📂
/quiz -> لعرض سؤال ❓
/cleartopic -> إلغاء تثبيت الموضوع 🔓
/stats -> إحصائيات المذاكرة 📊
/search -> للبحث داخل الأسئلة 🔍
/sync -> سحب الأسئلة الجديدة (للمشرف فقط) 🔄
"""
    await message.answer(text)

# =========================================================
# SEARCH & STATS
# =========================================================
@dp.message(Command("stats"))
async def stats_cmd(message: Message):
    user_id = message.from_user.id
    total, reviewed, pending, top_topic = get_stats(user_id)
    
    stats_text = (
        "📊 **إحصائيات المذاكرة:**\n\n"
        f"• إجمالي الأسئلة المتاحة: `{total}`\n"
        f"• الأسئلة التي تمت مراجعتها: `{reviewed}`\n"
        f"• الأسئلة الجاهزة للمراجعة الآن: `{pending}`\n"
        f"• أكثر قسم تتم مذاكرته: `#{top_topic}`\n"
    )
    await message.answer(stats_text, parse_mode="Markdown")

@dp.message(Command("search"))
async def search_cmd(message: Message, command: CommandObject):
    if not command.args:
        await message.answer("⚠️ يرجى كتابة كلمة البحث بعد الأمر، مثال:\n`/search anemia`", parse_mode="Markdown")
        return
    
    results = search_questions(command.args)
    if not results:
        await message.answer("❌ لم يتم العثور على نتائج.")
        return
    
    response = f"🔍 **نتائج البحث عن:** `{command.args}`\n\n"
    for r in results:
        response += f"• #{r[1]} | سؤال #{r[0]}\n{r[2][:70]}...\n\n"
    
    await message.answer(response, parse_mode="Markdown")

# =========================================================
# TOPICS SELECTION
# =========================================================
@dp.message(Command("topics"))
async def show_topics_menu(message: Message):
    all_topics = get_topics()
    if not all_topics:
        await message.answer("❌ لم يتم العثور على أي هاشتاجات.")
        return
    
    keyboard_buttons = []
    for topic in all_topics:
        button = InlineKeyboardButton(text=f"# {topic}", callback_data=f"select_topic_{topic}")
        keyboard_buttons.append([button])
        
    topics_keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    await message.answer("📂 **اختر القسم:**", reply_markup=topics_keyboard, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("select_topic_"))
async def handle_topic_selection(callback: CallbackQuery):
    user_id = callback.from_user.id
    try:
        await callback.answer()
    except Exception:
        pass
        
    selected_topic = callback.data.replace("select_topic_", "")
    user_current_topic[user_id] = selected_topic
    
    await callback.message.edit_text(
        text=f"🎯 **تم التثبيت على قسم:** `#{selected_topic}`",
        reply_markup=None,
        parse_mode="Markdown"
    )

@dp.message(Command("cleartopic"))
async def clear_user_topic(message: Message):
    user_id = message.from_user.id
    if user_id in user_current_topic:
        del user_current_topic[user_id]
        await message.answer("✅ تم إلغاء التثبيت.")
    else:
        await message.answer("💡 أنت لا تثبت أي قسم حالياً.")

# =========================================================
# QUIZ CORE
# =========================================================
async def send_quiz(user_id: int, chat_id: int, current_topic: str = None):
    note = get_random_question(user_id, current_topic)

    if not note:
        if current_topic:
            await bot.send_message(chat_id, f"⚠️ انتهت الأسئلة المتاحة حالياً في قسم `#{current_topic}`.")
        else:
            await bot.send_message(chat_id, "⚠️ لا توجد أسئلة جديدة متاحة حالياً.")
        return

    active_questions[user_id] = note

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="👁️ إظهار الإجابة", callback_data="show_answer")]]
    )

    topic_label = f"📂 **القسم الحالي: #{current_topic}**\n" if current_topic else ""
    await bot.send_message(
        chat_id,
        f"{topic_label}❓ **سؤال رقم #{note[0]}**\n\n{note[2]}",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

@dp.message(Command("quiz"))
async def random_quiz(message: Message):
    user_id = message.from_user.id
    current_topic = user_current_topic.get(user_id)
    await send_quiz(user_id, message.chat.id, current_topic)

@dp.callback_query(F.data == "next_quiz")
async def handle_next_quiz(callback: CallbackQuery):
    user_id = callback.from_user.id
    try:
        await callback.answer()
    except Exception:
        pass
    await send_quiz(user_id, callback.message.chat.id, user_current_topic.get(user_id))

# =========================================================
# SHOW ANSWER & EVALUATION BUTTONS
# =========================================================
@dp.callback_query(F.data == "show_answer")
async def show_answer(callback: CallbackQuery):
    user_id = callback.from_user.id
    try:
        await callback.answer()
    except Exception:
        pass

    if user_id not in active_questions:
        await callback.message.answer("لا يوجد سؤال نشط حالياً.")
        return

    note = active_questions[user_id]
    answer_text = note[4]
    answer_msg_id = note[5]
    answer_type = note[6]

    if answer_type == "text":
        await callback.message.answer(f"💡 **الإجابة:**\n\n{answer_text}", parse_mode="Markdown")
    else:
        await bot.copy_message(
            chat_id=callback.message.chat.id,
            from_chat_id=f"@{CHANNEL_USERNAME}",
            message_id=answer_msg_id
        )

    feedback_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔴 Again (10m)", callback_data=f"rate_again_{note[0]}"),
                InlineKeyboardButton(text="🟡 Hard (1d)", callback_data=f"rate_hard_{note[0]}")
            ],
            [
                InlineKeyboardButton(text="🔵 Good (3d)", callback_data=f"rate_good_{note[0]}"),
                InlineKeyboardButton(text="🟢 Easy (7d)", callback_data=f"rate_easy_{note[0]}")
            ]
        ]
    )
    
    await callback.message.edit_reply_markup(reply_markup=feedback_keyboard)

# =========================================================
# HANDLING EVALUATION
# =========================================================
@dp.callback_query(F.data.startswith("rate_"))
async def rate_question(callback: CallbackQuery):
    user_id = callback.from_user.id
    try:
        await callback.answer()
    except Exception:
        pass

    data_parts = callback.data.split("_")
    difficulty = data_parts[1]
    question_id = int(data_parts[2])

    update_user_progress(user_id, question_id, difficulty)

    if user_id in active_questions:
        del active_questions[user_id]

    status_messages = {
        "again": "🔴 مراجعة بعد 10 دقائق (Again)",
        "hard":  "🟡 مراجعة بعد يوم (Hard)",
        "good":  "🔵 مراجعة بعد 3 أيام (Good)",
        "easy":  "🟢 مراجعة بعد 7 أيام (Easy)"
    }
    
    status_text = status_messages.get(difficulty, "✅ تم تسجيل التقييم")
    
    next_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="➡️ السؤال التالي", callback_data="next_quiz")]]
    )
    
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.reply(status_text, reply_markup=next_keyboard)

# =========================================================
# MANUAL SYNC (PROTECTED)
# =========================================================
@dp.message(Command("sync"))
async def sync_notes_cmd(message: Message):
    if ADMIN_ID != 0 and message.from_user.id != ADMIN_ID:
        await message.answer("❌ عذراً، هذا الأمر مخصص للمسؤول فقط.")
        return

    if sync_lock.locked():
        await message.answer("⚠️ المزامنة قيد التشغيل بالفعل، يرجى الانتظار...")
        return
        
    async with sync_lock:
        await message.answer("🔄 بدأت المزامنة الذكية وتقسيم المحتوى...")
        await fetch_notes()
        await message.answer("✅ تمت المزامنة بنجاح!")

# =========================================================
# MAIN
# =========================================================
async def main():
    init_db()
    print("Bot started...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())