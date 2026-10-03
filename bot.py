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
        self.wfile.write(b"Bot and Web Server are Healthy & Running.")

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    server.serve_forever()

# -------------------------------------------------------------
# 2. الإعدادات وسجلات النظام
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
# 3. إدارة قاعدة البيانات (SQLite)
# -------------------------------------------------------------
def init_db():
    """تهيئة الجداول المطلوبة لتخزين المنشورات، التفاعلات، والتعليقات."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        # جدول المنشورات والوسائط
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
        # جدول إحصائيات وأحداث النقر (media_events)
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
        # جدول التعليقات
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
    """تسجيل حدث تفاعلي في جدول media_events."""
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
    """حفظ منشور جديد وإرجاع الـ ID الخاص به."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO publications (media_type, file_id, caption)
            VALUES (?, ?, ?)
        """, (media_type, file_id, caption))
        conn.commit()
        return cursor.lastrowid

def update_publication_msg_id(publication_id: int, msg_id: int):
    """تحديث معرّف الرسالة داخل القناة لتمكين رابط المشاركة المباشر."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE publications SET channel_msg_id = ? WHERE id = ?", (msg_id, publication_id))
        conn.commit()

def get_publication(publication_id: int):
    """استرجاع بيانات المنشور عبر الـ ID."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT media_type, file_id, caption, channel_msg_id FROM publications WHERE id = ?", (publication_id,))
        return cursor.fetchone()

# -------------------------------------------------------------
# 4. بناء الأزرار والسيمترية البصرية (Layout Alignment)
# -------------------------------------------------------------
def build_custom_keyboard(bot_uname: str, lecture_id: int, primary_text: str, primary_action_prefix: str, channel_msg_id: int = None):
    """
    سيمترية الواجهة العربية في تيليجرام:
    - جهة اليمين: الزر الأساسي (المشاهدة / الاستماع / التحميل / فتح البطاقة).
    - جهة اليسار: زر التفاعل الثابت [ 💬 سجل تعليقك ].
    - الصف الثاني: زر منفرد بعرض الشاشة [ 📲 انشر تؤجر ].
    """
    primary_url = f"https://t.me/{bot_uname}?start={primary_action_prefix}_{lecture_id}"
    comment_url = f"https://t.me/{bot_uname}?start=comment_{lecture_id}"

    # إنشاء رابط المشاركة الرسمي المباشر
    clean_chan = CHANNEL_ID.replace("@", "")
    if channel_msg_id:
        share_post_url = f"https://t.me/{clean_chan}/{channel_msg_id}"
    else:
        share_post_url = f"https://t.me/{clean_chan}"

    share_text = urllib.parse.quote("انضم وتابع معنا المنشورات والفوائد:")
    share_url = f"https://t.me/share/url?url={urllib.parse.quote(share_post_url)}&text={share_text}"

    keyboard = [
        # في تيليجرام باللغة العربية: العنصر الثاني يظهر في أقصى اليمين، والعنصر الأول في أقصى اليسار
        [
            InlineKeyboardButton("💬 سجل تعليقك", url=comment_url),
            InlineKeyboardButton(primary_text, url=primary_url)
        ],
        [
            InlineKeyboardButton("📲 انشر تؤجر", url=share_url)
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

# -------------------------------------------------------------
# 5. معالجة أوامر الروابط العميقة (Deep Linking)
# -------------------------------------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة الأمر /start سواءً العادي أو عبر الروابط العميقة (Deep Linking)."""
    user = update.effective_user
    args = context.args

    # 1. إذا كان /start بدون وسائط وكان المستخدم هو المشرف
    if not args:
        if user.id == ADMIN_USER_ID:
            msg = (
                "👋 **مرحباً بك يا مدير القناة في النظام الهندسي المتكامل:**\n\n"
                "• **نشر فوري متقدم:** أرسل (PDF أو فيديو أو صوت أو صورة) مع الكابشن وسينشره البوت بنظامه الهندسي فوراً.\n"
                "• **/post [النص]** - نشر بطاقة نصية تفاعلية.\n"
                "• البوت مربوط بقاعدة بيانات `SQLite` ويسجل النقرات والتعليقات ويخطر بها لحظياً."
            )
            await update.message.reply_text(msg, parse_mode="Markdown")
        else:
            await update.message.reply_text(f"مرحباً بك يا {user.first_name} في بوت خدمة وإدارة القناة.")
        return

    # 2. تحليل الرابط العميق: payload
    payload = args[0]

    # --- أ) زر فتح / تحميل الكتاب ---
    if payload.startswith("doc_"):
        lecture_id = int(payload.split("_")[1])
        pub = get_publication(lecture_id)
        if not pub:
            await update.message.reply_text("عذراً، هذا الملف غير متوفر أو تم حذفه.")
            return

        log_event(lecture_id, "download_doc", user)
        # إشعار سري للإدارة
        await context.bot.send_message(
            chat_id=ADMIN_USER_ID,
            text=f"🔒 **إشعار فتح / تحميل كتاب (سري)**\n"
                 f"• المنشور رقم: `{lecture_id}`\n"
                 f"• المستخدم: {user.full_name} (@{user.username or 'بدون'}) | ID: `{user.id}`\n"
                 f"• الوقت: {datetime.now().strftime('%Y-%m-%d %I:%M:%S %p')}",
            parse_mode="Markdown"
        )
        # إرسال المستند كاملاً في الخاص
        await update.message.reply_document(
            document=pub[1],
            caption=pub[2] or "إليك نسخة الكتاب المطلوبة:",
            parse_mode="HTML"
        )

    # --- ب) زر مشاهدة الفيديو ---
    elif payload.startswith("watch_"):
        lecture_id = int(payload.split("_")[1])
        pub = get_publication(lecture_id)
        if not pub:
            await update.message.reply_text("عذراً، هذا المقطع غير متوفر.")
            return

        log_event(lecture_id, "watch_video", user)
        await context.bot.send_message(
            chat_id=ADMIN_USER_ID,
            text=f"🔒 **إشعار مشاهدة فيديو (سري)**\n"
                 f"• المنشور رقم: `{lecture_id}`\n"
                 f"• المستخدم: {user.full_name} (@{user.username or 'بدون'}) | ID: `{user.id}`",
            parse_mode="Markdown"
        )
        await update.message.reply_video(
            video=pub[1],
            caption=pub[2] or "",
            parse_mode="HTML"
        )

    # --- ج) زر استمع للخطبة ---
    elif payload.startswith("listen_"):
        lecture_id = int(payload.split("_")[1])
        pub = get_publication(lecture_id)
        if not pub:
            await update.message.reply_text("عذراً، هذا التسجيل غير متوفر.")
            return

        log_event(lecture_id, "listen_audio", user)
        await context.bot.send_message(
            chat_id=ADMIN_USER_ID,
            text=f"🔒 **إشعار استماع لمقطع صوتي (سري)**\n"
                 f"• المنشور رقم: `{lecture_id}`\n"
                 f"• المستخدم: {user.full_name} (@{user.username or 'بدون'}) | ID: `{user.id}`",
            parse_mode="Markdown"
        )
        await update.message.reply_audio(
            audio=pub[1],
            caption=pub[2] or "",
            parse_mode="HTML"
        )

    # --- د) زر عرض البطاقة كاملة (للصور أو النصوص) ---
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

    # --- هـ) زر سجل تعليقك ---
    elif payload.startswith("comment_"):
        lecture_id = int(payload.split("_")[1])
        # حفظ جلسة المستخدم
        context.user_data["action"] = "comment"
        context.user_data["lecture_id"] = lecture_id

        prompt = (
            "💬 **أهلاً بك!**\n"
            f"أنت بصدد تسجيل تعليقك على المنشور رقم `{lecture_id}`.\n"
            "تفضل بإرسال تعليقك الآن (سواءً كان رسالة نصية أو رسالة صوتية 🎙):"
        )
        await update.message.reply_text(prompt, parse_mode="Markdown")

# -------------------------------------------------------------
# 6. استقبال التعليقات وإعادة توجيهها للمشرف
# -------------------------------------------------------------
async def handle_user_responses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة ما يرسله المستخدم في المحادثة الخاصة (تسجيل التعليق)."""
    user = update.effective_user

    # التأكد من أن المستخدم داخل خطوة كتابة تعليق
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

        # حفظ التعليق في قاعدة البيانات
        with sqlite3.connect(DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO comments (lecture_id, user_id, username, full_name, comment_type, comment_content)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                lecture_id,
                user.id,
                user.username or "بدون معرف",
                user.full_name or "مجهول",
                c_type,
                comment_content
            ))
            conn.commit()

        # إشعار سري فوري للمدير بالتعليق ومحتواه
        admin_notice = (
            f"🔔 **تعليق جديد وارد!**\n"
            f"• على المنشور رقم: `{lecture_id}`\n"
            f"• صاحب التعليق: {user.full_name} (@{user.username or 'بدون'}) | ID: `{user.id}`\n"
            f"• نوع التعليق: `{c_type}`\n"
        )
        await context.bot.send_message(chat_id=ADMIN_USER_ID, text=admin_notice, parse_mode="Markdown")

        # إعادة توجيه المحتوى ذاته للمدير
        if c_type == "text":
            await context.bot.send_message(
                chat_id=ADMIN_USER_ID, 
                text=f"📝 نص التعليق:\n{comment_content}"
            )
        elif c_type == "voice":
            await context.bot.send_voice(chat_id=ADMIN_USER_ID, voice=comment_content)
        elif c_type == "audio":
            await context.bot.send_audio(chat_id=ADMIN_USER_ID, audio=comment_content)

        # إنهاء حالة الجلسة
        context.user_data.clear()
        await update.message.reply_text("✅ جزاك الله خيراً، تم استلام تعليقك وحفظه بنجاح.")

# -------------------------------------------------------------
# 7. معالجة الوسائط ونشرها وفق السيمترية البصرية وإدارة الذاكرة
# -------------------------------------------------------------
async def handle_admin_media_and_publishing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    استقبال المواد من المشرف، تخزينها، إنشاء الأزرار المنضبطة هندسياً، والنشر في القناة.
    """
    if update.effective_user.id != ADMIN_USER_ID:
        return

    msg = update.message
    caption = msg.caption or ""
    bot_info = await context.bot.get_me()
    bot_uname = bot_info.username

    # ---------------- 1. قسم المستندات والكتب (PDF) ----------------
    if msg.document and msg.document.mime_type == "application/pdf":
        file_id = msg.document.file_id
        pub_id = save_publication("pdf", file_id, caption)

        # استخراج الغلاف بدقة مع تفريغ فوري للذاكرة
        cover_img_bytes = None
        pdf_file = await context.bot.get_file(file_id)
        pdf_bytes = await pdf_file.download_as_bytearray()

        try:
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            if len(doc) > 0:
                page = doc.load_page(0)
                pix = page.get_pixmap()
                cover_img_bytes = pix.tobytes("jpeg")
            doc.close()
        except Exception as e:
            logging.error(f"خطأ أثناء استخراج غلاف الكتاب: {e}")
        finally:
            del pdf_bytes  # تفريغ فوري للذاكرة لتجنب استهلاك موارد السيرفر

        # تجهيز الأزرار الهندسية
        reply_markup = build_custom_keyboard(
            bot_uname=bot_uname,
            lecture_id=pub_id,
            primary_text="📥 فتح / تحميل الكتاب",
            primary_action_prefix="doc"
        )

        # النشر في القناة: صورة الغلاف إذا توفرت، أو إرسال المستند
        if cover_img_bytes:
            sent_msg = await context.bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=io.BytesIO(cover_img_bytes),
                caption=caption or "كتاب جديد متاح للتحميل والقراءة الآن:",
                reply_markup=reply_markup,
                parse_mode="HTML"
            )
        else:
            sent_msg = await context.bot.send_document(
                chat_id=CHANNEL_ID,
                document=file_id,
                caption=caption,
                reply_markup=reply_markup,
                parse_mode="HTML"
            )

        update_publication_msg_id(pub_id, sent_msg.message_id)
        # تحديث الأزرار لتضمن رابط المشاركة المباشر للمنشور
        updated_markup = build_custom_keyboard(
            bot_uname, pub_id, "📥 فتح / تحميل الكتاب", "doc", sent_msg.message_id
        )
        await sent_msg.edit_reply_markup(reply_markup=updated_markup)
        await msg.reply_text(f"✅ تم نشر الكتاب في القناة بنجاح (رقم المعرف: `{pub_id}`).", parse_mode="Markdown")

    # ---------------- 2. قسم مقاطع الفيديو ----------------
    elif msg.video:
        file_id = msg.video.file_id
        pub_id = save_publication("video", file_id, caption)

        reply_markup = build_custom_keyboard(
            bot_uname=bot_uname,
            lecture_id=pub_id,
            primary_text="🎬 مشاهدة الفيديو",
            primary_action_prefix="watch"
        )
        sent_msg = await context.bot.send_video(
            chat_id=CHANNEL_ID,
            video=file_id,
            caption=caption,
            reply_markup=reply_markup,
            parse_mode="HTML"
        )
        update_publication_msg_id(pub_id, sent_msg.message_id)
        updated_markup = build_custom_keyboard(
            bot_uname, pub_id, "🎬 مشاهدة الفيديو", "watch", sent_msg.message_id
        )
        await sent_msg.edit_reply_markup(reply_markup=updated_markup)
        await msg.reply_text(f"✅ تم نشر الفيديو في القناة بنجاح (رقم المعرف: `{pub_id}`).", parse_mode="Markdown")

    # ---------------- 3. قسم المقاطع الصوتية والخطب ----------------
    elif msg.audio or msg.voice:
        file_id = (msg.audio or msg.voice).file_id
        pub_id = save_publication("audio", file_id, caption)

        reply_markup = build_custom_keyboard(
            bot_uname=bot_uname,
            lecture_id=pub_id,
            primary_text="🎧 استمع للخطبة",
            primary_action_prefix="listen"
        )
        if msg.audio:
            sent_msg = await context.bot.send_audio(
                chat_id=CHANNEL_ID,
                audio=file_id,
                caption=caption,
                reply_markup=reply_markup,
                parse_mode="HTML"
            )
        else:
            sent_msg = await context.bot.send_voice(
                chat_id=CHANNEL_ID,
                voice=file_id,
                caption=caption,
                reply_markup=reply_markup
            )
        update_publication_msg_id(pub_id, sent_msg.message_id)
        updated_markup = build_custom_keyboard(
            bot_uname, pub_id, "🎧 استمع للخطبة", "listen", sent_msg.message_id
        )
        await sent_msg.edit_reply_markup(reply_markup=updated_markup)
        await msg.reply_text(f"✅ تم نشر المقطع الصوتي بنجاح (رقم المعرف: `{pub_id}`).", parse_mode="Markdown")

    # ---------------- 4. قسم الصور والبطاقات ----------------
    elif msg.photo:
        file_id = msg.photo[-1].file_id
        pub_id = save_publication("photo", file_id, caption)

        reply_markup = build_custom_keyboard(
            bot_uname=bot_uname,
            lecture_id=pub_id,
            primary_text="🖼 عرض البطاقة كاملة",
            primary_action_prefix="view"
        )
        sent_msg = await context.bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=file_id,
            caption=caption,
            reply_markup=reply_markup,
            parse_mode="HTML"
        )
        update_publication_msg_id(pub_id, sent_msg.message_id)
        updated_markup = build_custom_keyboard(
            bot_uname, pub_id, "🖼 عرض البطاقة كاملة", "view", sent_msg.message_id
        )
        await sent_msg.edit_reply_markup(reply_markup=updated_markup)
        await msg.reply_text(f"✅ تم نشر البطاقة المصورة بنجاح (رقم المعرف: `{pub_id}`).", parse_mode="Markdown")

# -------------------------------------------------------------
# 8. أمر النشر النصي المخصص
# -------------------------------------------------------------
async def post_text_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نشر بطاقة نصية تفاعلية بنفس السيمترية والأزرار."""
    if update.effective_user.id != ADMIN_USER_ID:
        return

    text_content = " ".join(context.args)
    if not text_content:
        await update.message.reply_text("يرجى كتابة نص المنشور بعد الأمر. مثال:\n`/post درر وفوائد اليوم...`", parse_mode="Markdown")
        return

    bot_info = await context.bot.get_me()
    bot_uname = bot_info.username

    pub_id = save_publication("text", "", text_content)
    reply_markup = build_custom_keyboard(
        bot_uname=bot_uname,
        lecture_id=pub_id,
        primary_text="🖼 قراءة البطاقة كاملة",
        primary_action_prefix="view"
    )

    sent_msg = await context.bot.send_message(
        chat_id=CHANNEL_ID,
        text=text_content,
        reply_markup=reply_markup,
        parse_mode="HTML"
    )
    update_publication_msg_id(pub_id, sent_msg.message_id)
    updated_markup = build_custom_keyboard(
        bot_uname, pub_id, "🖼 قراءة البطاقة كاملة", "view", sent_msg.message_id
    )
    await sent_msg.edit_reply_markup(reply_markup=updated_markup)
    await update.message.reply_text(f"✅ تم نشر النص في القناة بنجاح (رقم المنشور: `{pub_id}`).", parse_mode="Markdown")

# -------------------------------------------------------------
# 9. نقطة الانطلاق الرئيسية (Main)
# -------------------------------------------------------------
def main():
    if not BOT_TOKEN:
        raise ValueError("خطأ: المتغير BOT_TOKEN غير محدد!")

    # 1. تهيئة جداول قاعدة البيانات
    init_db()

    # 2. إطلاق خادم الويب في Thread مستقل لمنع تعليق Render
    web_thread = Thread(target=run_web_server, daemon=True)
    web_thread.start()

    # 3. بناء تطبيق التيليجرام
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # تسجيل المعالجات
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("post", post_text_command))

    # معالجة الوسائط المنشورة من المشرف
    admin_media_filter = filters.Chat(ADMIN_USER_ID) & (
        filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.VOICE | filters.Document.ALL
    )
    app.add_handler(MessageHandler(admin_media_filter, handle_admin_media_and_publishing))

    # معالجة تعليقات المستخدمين الواردة في الخاص
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, handle_user_responses))

    logging.info("البوت المطور يعمل بكامل وظائفه الهندسية...")
    app.run_polling()

if __name__ == "__main__":
    main()
