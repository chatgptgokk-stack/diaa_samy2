import os
import io
import fitz  # PyMuPDF
import sqlite3
import logging
import urllib.parse
from datetime import datetime
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# -------------------------------------------------------------
# 1. خادم ويب مدمج للحفاظ على استمرارية الخدمة مجاناً على Render
# -------------------------------------------------------------
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is Healthy and Running!")

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    server.serve_forever()

# -------------------------------------------------------------
# 2. الإعدادات
# -------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", 
    level=logging.INFO
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID", "@diaa_samy2")
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))
DB_NAME = "channel_bot_data.db"

# -------------------------------------------------------------
# 3. إدارة قاعدة البيانات
# -------------------------------------------------------------
def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS publications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                media_type TEXT NOT NULL,
                file_id TEXT NOT NULL,
                caption TEXT,
                channel_msg_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS media_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lecture_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                full_name TEXT,
                event_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lecture_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                full_name TEXT,
                comment_type TEXT NOT NULL,
                comment_content TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()

def log_event(lecture_id: int, event_type: str, user):
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO media_events (lecture_id, event_type, user_id, username, full_name)
            VALUES (?, ?, ?, ?, ?)
        """, (
            lecture_id,
            event_type,
            user.id,
            user.username or "بدون معرف",
            user.full_name or "مجهول"
        ))
        conn.commit()

def save_publication(media_type: str, file_id: str, caption: str) -> int:
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO publications (media_type, file_id, caption)
            VALUES (?, ?, ?)
        """, (media_type, file_id, caption))
        conn.commit()
        return cursor.lastrowid

def update_publication_msg_id(publication_id: int, msg_id: int):
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE publications SET channel_msg_id = ? WHERE id = ?", (msg_id, publication_id))
        conn.commit()

def get_publication(publication_id: int):
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT media_type, file_id, caption, channel_msg_id FROM publications WHERE id = ?", (publication_id,))
        return cursor.fetchone()

# -------------------------------------------------------------
# 4. بناء الأزرار والسيمترية البصرية
# -------------------------------------------------------------
def build_custom_keyboard(bot_uname: str, lecture_id: int, primary_text: str, primary_action_prefix: str, channel_msg_id: int = None):
    primary_url = f"https://t.me/{bot_uname}?start={primary_action_prefix}_{lecture_id}"
    comment_url = f"https://t.me/{bot_uname}?start=comment_{lecture_id}"

    clean_chan = CHANNEL_ID.replace("@", "")
    if channel_msg_id:
        share_post_url = f"https://t.me/{clean_chan}/{channel_msg_id}"
    else:
        share_post_url = f"https://t.me/{clean_chan}"

    share_text = urllib.parse.quote("انضم وتابع معنا المنشورات والفوائد:")
    share_url = f"https://t.me/share/url?url={urllib.parse.quote(share_post_url)}&text={share_text}"

    # السيمترية: اليمين (الأساسي)، اليسار (التعليق)، الصف الثاني (مشاركة عريضة)
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💬 سجل تعليقك", url=comment_url),
            InlineKeyboardButton(primary_text, url=primary_url)
        ],
        [
            InlineKeyboardButton("📲 انشر تؤجر", url=share_url)
        ]
    ])

# -------------------------------------------------------------
# 5. معالجة أوامر الروابط العميقة (Deep Linking)
# -------------------------------------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args

    if not args:
        if user.id == ADMIN_USER_ID:
            msg = (
                "👋 **مرحباً بك يا مدير القناة في النظام الهندسي المتكامل:**\n\n"
                "• أرسل أي ملف (فيديو، صوت، PDF، صورة) وسيقوم البوت بنشره مع الأزرار فوراً.\n"
                "• استخدم الأمر `/post [النص]` لنشر منشور كتابي تفاعلي."
            )
            await update.message.reply_text(msg, parse_mode="Markdown")
        else:
            await update.message.reply_text(f"مرحباً بك يا {user.first_name} في بوت القناة.")
        return

    payload = args[0]

    # أ) فتح / تحميل الكتاب
    if payload.startswith("doc_"):
        lecture_id = int(payload.split("_")[1])
        pub = get_publication(lecture_id)
        if not pub:
            await update.message.reply_text("عذراً، الملف غير متوفر.")
            return

        log_event(lecture_id, "download_doc", user)
        await context.bot.send_message(
            chat_id=ADMIN_USER_ID,
            text=f"🔒 **إشعار فتح / تحميل كتاب (سري)**\n"
                 f"• المنشور رقم: `{lecture_id}`\n"
                 f"• المستخدم: {user.full_name} (@{user.username or 'بدون'}) | ID: `{user.id}`",
            parse_mode="Markdown"
        )
        await update.message.reply_document(document=pub[1], caption=pub[2] or "", parse_mode="HTML")

    # ب) مشاهدة الفيديو
    elif payload.startswith("watch_"):
        lecture_id = int(payload.split("_")[1])
        pub = get_publication(lecture_id)
        if not pub:
            await update.message.reply_text("عذراً، الفيديو غير متوفر.")
            return

        log_event(lecture_id, "watch_video", user)
        await context.bot.send_message(
            chat_id=ADMIN_USER_ID,
            text=f"🔒 **إشعار مشاهدة فيديو (سري)**\n"
                 f"• المنشور رقم: `{lecture_id}`\n"
                 f"• المستخدم: {user.full_name} (@{user.username or 'بدون'}) | ID: `{user.id}`",
            parse_mode="Markdown"
        )
        if pub[0] == "doc_video":
            await update.message.reply_document(document=pub[1], caption=pub[2] or "", parse_mode="HTML")
        else:
            await update.message.reply_video(video=pub[1], caption=pub[2] or "", parse_mode="HTML")

    # ج) استمع للخطبة
    elif payload.startswith("listen_"):
        lecture_id = int(payload.split("_")[1])
        pub = get_publication(lecture_id)
        if not pub:
            await update.message.reply_text("عذراً، التسجيل غير متوفر.")
            return

        log_event(lecture_id, "listen_audio", user)
        await context.bot.send_message(
            chat_id=ADMIN_USER_ID,
            text=f"🔒 **إشعار استماع لخطبة (سري)**\n"
                 f"• المنشور رقم: `{lecture_id}`\n"
                 f"• المستخدم: {user.full_name} (@{user.username or 'بدون'}) | ID: `{user.id}`",
            parse_mode="Markdown"
        )
        await update.message.reply_audio(audio=pub[1], caption=pub[2] or "", parse_mode="HTML")

    # د) عرض البطاقة
    elif payload.startswith("view_"):
        lecture_id = int(payload.split("_")[1])
        pub = get_publication(lecture_id)
        if not pub:
            await update.message.reply_text("عذراً، المحتوى غير متوفر.")
            return

        log_event(lecture_id, "view_card", user)
        if pub[0] == "photo":
            await update.message.reply_photo(photo=pub[1], caption=pub[2] or "", parse_mode="HTML")
        else:
            await update.message.reply_text(text=pub[2] or "", parse_mode="HTML")

    # هـ) سجل تعليقك
    elif payload.startswith("comment_"):
        lecture_id = int(payload.split("_")[1])
        context.user_data["action"] = "comment"
        context.user_data["lecture_id"] = lecture_id

        await update.message.reply_text(
            f"💬 **أهلاً بك!**\nأنت تسجل الآن تعليقك على المنشور رقم `{lecture_id}`.\nتفضل بإرسال نص أو تسجيل صوتي الآن:",
            parse_mode="Markdown"
        )

# -------------------------------------------------------------
# 6. استقبال التعليقات
# -------------------------------------------------------------
async def handle_user_responses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if context.user_data.get("action") == "comment":
        lecture_id = context.user_data.get("lecture_id")
        comment_content = ""
        c_type = "text"

        if update.message.text:
            comment_content = update.message.text
            c_type = "text"
        elif update.message.voice:
            comment_content = update.message.voice.file_id
            c_type = "voice"
        elif update.message.audio:
            comment_content = update.message.audio.file_id
            c_type = "audio"

        with sqlite3.connect(DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO comments (lecture_id, user_id, username, full_name, comment_type, comment_content)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (lecture_id, user.id, user.username or "بدون معرف", user.full_name or "مجهول", c_type, comment_content))
            conn.commit()

        # إشعار المدير
        await context.bot.send_message(
            chat_id=ADMIN_USER_ID,
            text=f"🔔 **تعليق وارد جديد!**\n• المنشور: `{lecture_id}`\n• من: {user.full_name} (@{user.username or 'بدون'})\n• النوع: `{c_type}`",
            parse_mode="Markdown"
        )
        if c_type == "text":
            await context.bot.send_message(chat_id=ADMIN_USER_ID, text=f"📝 التعليق:\n{comment_content}")
        elif c_type == "voice":
            await context.bot.send_voice(chat_id=ADMIN_USER_ID, voice=comment_content)
        elif c_type == "audio":
            await context.bot.send_audio(chat_id=ADMIN_USER_ID, audio=comment_content)

        context.user_data.clear()
        await update.message.reply_text("✅ تم استلام تعليقك وإرساله للإدارة بنجاح.")

# -------------------------------------------------------------
# 7. استقبال الوسائط الشامل والنشر في القناة
# -------------------------------------------------------------
async def handle_admin_media_and_publishing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        return

    msg = update.message
    caption = msg.caption or ""
    bot_info = await context.bot.get_me()
    bot_uname = bot_info.username

    try:
        # 1. حالة المستندات (PDF أو فيديو أُرسل كمستند)
        if msg.document:
            file_id = msg.document.file_id
            file_name = (msg.document.file_name or "").lower()
            mime_type = (msg.document.mime_type or "").lower()

            # إذا كان المستند عبارة عن فيديو (MP4, MKV, ...)
            if "video" in mime_type or file_name.endswith((".mp4", ".mkv", ".mov", ".avi")):
                pub_id = save_publication("doc_video", file_id, caption)
                reply_markup = build_custom_keyboard(bot_uname, pub_id, "🎬 مشاهدة الفيديو", "watch")
                sent_msg = await context.bot.send_document(
                    chat_id=CHANNEL_ID, document=file_id, caption=caption, reply_markup=reply_markup, parse_mode="HTML"
                )
                update_publication_msg_id(pub_id, sent_msg.message_id)
                updated_markup = build_custom_keyboard(bot_uname, pub_id, "🎬 مشاهدة الفيديو", "watch", sent_msg.message_id)
                await sent_msg.edit_reply_markup(reply_markup=updated_markup)
                await msg.reply_text(f"✅ تم نشر الفيديو (كمستند) في القناة بنجاح (المعرف: `{pub_id}`).", parse_mode="Markdown")
                return

            # إذا كان المستند PDF
            pub_id = save_publication("pdf", file_id, caption)
            cover_img_bytes = None
            try:
                pdf_file = await context.bot.get_file(file_id)
                pdf_bytes = await pdf_file.download_as_bytearray()
                doc = fitz.open(stream=pdf_bytes, filetype="pdf")
                if len(doc) > 0:
                    page = doc.load_page(0)
                    pix = page.get_pixmap()
                    cover_img_bytes = pix.tobytes("jpeg")
                doc.close()
                del pdf_bytes
            except Exception as e:
                logging.error(f"Cover extract error: {e}")

            reply_markup = build_custom_keyboard(bot_uname, pub_id, "📥 فتح / تحميل الكتاب", "doc")
            if cover_img_bytes:
                sent_msg = await context.bot.send_photo(
                    chat_id=CHANNEL_ID, photo=io.BytesIO(cover_img_bytes), caption=caption or "كتاب متاح للتحميل:", reply_markup=reply_markup, parse_mode="HTML"
                )
            else:
                sent_msg = await context.bot.send_document(
                    chat_id=CHANNEL_ID, document=file_id, caption=caption, reply_markup=reply_markup, parse_mode="HTML"
                )
            update_publication_msg_id(pub_id, sent_msg.message_id)
            updated_markup = build_custom_keyboard(bot_uname, pub_id, "📥 فتح / تحميل الكتاب", "doc", sent_msg.message_id)
            await sent_msg.edit_reply_markup(reply_markup=updated_markup)
            await msg.reply_text(f"✅ تم نشر الكتاب في القناة بنجاح (المعرف: `{pub_id}`).", parse_mode="Markdown")

        # 2. حالة الفيديو المباشر
        elif msg.video:
            file_id = msg.video.file_id
            pub_id = save_publication("video", file_id, caption)
            reply_markup = build_custom_keyboard(bot_uname, pub_id, "🎬 مشاهدة الفيديو", "watch")
            sent_msg = await context.bot.send_video(
                chat_id=CHANNEL_ID, video=file_id, caption=caption, reply_markup=reply_markup, parse_mode="HTML"
            )
            update_publication_msg_id(pub_id, sent_msg.message_id)
            updated_markup = build_custom_keyboard(bot_uname, pub_id, "🎬 مشاهدة الفيديو", "watch", sent_msg.message_id)
            await sent_msg.edit_reply_markup(reply_markup=updated_markup)
            await msg.reply_text(f"✅ تم نشر الفيديو في القناة بنجاح (المعرف: `{pub_id}`).", parse_mode="Markdown")

        # 3. حالة الصوت
        elif msg.audio or msg.voice:
            file_id = (msg.audio or msg.voice).file_id
            pub_id = save_publication("audio", file_id, caption)
            reply_markup = build_custom_keyboard(bot_uname, pub_id, "🎧 استمع للخطبة", "listen")
            if msg.audio:
                sent_msg = await context.bot.send_audio(
                    chat_id=CHANNEL_ID, audio=file_id, caption=caption, reply_markup=reply_markup, parse_mode="HTML"
                )
            else:
                sent_msg = await context.bot.send_voice(
                    chat_id=CHANNEL_ID, voice=file_id, caption=caption, reply_markup=reply_markup
                )
            update_publication_msg_id(pub_id, sent_msg.message_id)
            updated_markup = build_custom_keyboard(bot_uname, pub_id, "🎧 استمع للخطبة", "listen", sent_msg.message_id)
            await sent_msg.edit_reply_markup(reply_markup=updated_markup)
            await msg.reply_text(f"✅ تم نشر المقطع الصوتي بنجاح (المعرف: `{pub_id}`).", parse_mode="Markdown")

        # 4. حالة الصور
        elif msg.photo:
            file_id = msg.photo[-1].file_id
            pub_id = save_publication("photo", file_id, caption)
            reply_markup = build_custom_keyboard(bot_uname, pub_id, "🖼 عرض البطاقة كاملة", "view")
            sent_msg = await context.bot.send_photo(
                chat_id=CHANNEL_ID, photo=file_id, caption=caption, reply_markup=reply_markup, parse_mode="HTML"
            )
            update_publication_msg_id(pub_id, sent_msg.message_id)
            updated_markup = build_custom_keyboard(bot_uname, pub_id, "🖼 عرض البطاقة كاملة", "view", sent_msg.message_id)
            await sent_msg.edit_reply_markup(reply_markup=updated_markup)
            await msg.reply_text(f"✅ تم نشر البطاقة المصورة بنجاح (المعرف: `{pub_id}`).", parse_mode="Markdown")

    except Exception as e:
        # إشعار المشرف بالخطأ إن وُجد (مثل عدم رفع البوت كمشرف في القناة)
        await msg.reply_text(f"⚠️ حدث خطأ أثناء محاولة النشر في القناة:\n`{e}`\n(تأكد من إضافة البوت كـ Admin في القناة مع صلاحية نشر الرسائل).", parse_mode="Markdown")

# -------------------------------------------------------------
# 8. الأمر النصي
# -------------------------------------------------------------
async def post_text_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        return

    text_content = " ".join(context.args)
    if not text_content:
        await update.message.reply_text("يرجى كتابة النص بعد الأمر، مثال:\n`/post درر وفوائد...`", parse_mode="Markdown")
        return

    bot_info = await context.bot.get_me()
    bot_uname = bot_info.username

    pub_id = save_publication("text", "", text_content)
    reply_markup = build_custom_keyboard(bot_uname, pub_id, "🖼 قراءة البطاقة كاملة", "view")

    sent_msg = await context.bot.send_message(
        chat_id=CHANNEL_ID, text=text_content, reply_markup=reply_markup, parse_mode="HTML"
    )
    update_publication_msg_id(pub_id, sent_msg.message_id)
    updated_markup = build_custom_keyboard(bot_uname, pub_id, "🖼 قراءة البطاقة كاملة", "view", sent_msg.message_id)
    await sent_msg.edit_reply_markup(reply_markup=updated_markup)
    await update.message.reply_text(f"✅ تم نشر النص في القناة بنجاح (المعرف: `{pub_id}`).", parse_mode="Markdown")

# -------------------------------------------------------------
# 9. Main
# -------------------------------------------------------------
def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN غير محدد!")

    init_db()

    web_thread = Thread(target=run_web_server, daemon=True)
    web_thread.start()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("post", post_text_command))

    # فلتر شامل لكافة أنواع الوسائط
    all_media_filter = filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.VOICE | filters.Document.ALL
    app.add_handler(MessageHandler(filters.Chat(ADMIN_USER_ID) & all_media_filter, handle_admin_media_and_publishing))

    # فلتر التعليقات للخاص
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, handle_user_responses))

    app.run_polling()

if __name__ == "__main__":
    main()
