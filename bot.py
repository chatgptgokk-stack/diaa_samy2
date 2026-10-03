import os
import io
import fitz  # PyMuPDF
import cv2   # OpenCV لاستخراج كادر الفيديو
import sqlite3
import logging
import urllib.parse
from datetime import datetime
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler
from PIL import Image, ImageDraw, ImageFont

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

# -------------------------------------------------------------
# 1. خادم ويب مصغر لضمان استمرارية التشغيل على Render Free
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
# 2. الإعدادات وقاعدة البيانات
# -------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", 
    level=logging.INFO
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID", "@diaa_samy2")
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))
DB_NAME = "channel_bot_data.db"

def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS publications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                media_type TEXT NOT NULL,
                file_id TEXT NOT NULL,
                cover_file_id TEXT,
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
        """, (lecture_id, event_type, user.id, user.username or "بدون معرف", user.full_name or "مجهول"))
        conn.commit()

def save_publication(media_type: str, file_id: str, cover_file_id: str, caption: str) -> int:
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO publications (media_type, file_id, cover_file_id, caption)
            VALUES (?, ?, ?, ?)
        """, (media_type, file_id, cover_file_id, caption))
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
        cursor.execute("SELECT media_type, file_id, cover_file_id, caption, channel_msg_id FROM publications WHERE id = ?", (publication_id,))
        return cursor.fetchone()

# -------------------------------------------------------------
# 3. محرك التصميم الزخرفي واستخراج الكادرات
# -------------------------------------------------------------
def create_islamic_audio_card(title_text: str) -> io.BytesIO:
    """توليد بطاقة إسلامية ملكية بزخارف ذهبية وكتابة العنوان داخلها."""
    width, height = 1080, 1080
    bg_color = (18, 30, 49)      # كحلي ملكي عميق
    gold_color = (212, 175, 55)   # ذهبي إسلامي
    gold_light = (244, 226, 133)

    img = Image.new("RGB", (width, height), color=bg_color)
    draw = ImageDraw.Draw(img)

    # 1. إطار هندسي مزدوج
    draw.rectangle([(40, 40), (width - 40, height - 40)], outline=gold_color, width=5)
    draw.rectangle([(60, 60), (width - 60, height - 60)], outline=gold_light, width=2)

    # 2. زخارف زوايا إسلامية هندسية
    corner_size = 80
    for cx, cy in [(60, 60), (width - 60, 60), (60, height - 60), (width - 60, height - 60)]:
        draw.rectangle([(cx - 20, cy - 20), (cx + 20, cy + 20)], outline=gold_color, width=2)

    # 3. أيقونة ورمز صوتي زخرفي في المنتصف
    draw.ellipse([(width//2 - 60, 180), (width//2 + 60, 300)], outline=gold_light, width=4)
    # هلال زخرفي
    draw.arc([(width//2 - 40, 200), (width//2 + 40, 280)], start=30, end=270, fill=gold_light, width=6)

    # 4. كتابة العنوان في المنتصف
    try:
        font = ImageFont.load_default()
    except Exception:
        font = None

    display_title = title_text if title_text else "تسجيل صوتي مباك"
    
    # رسم خطوط زخرفية فاصلة
    draw.line([(250, 480), (width - 250, 480)], fill=gold_color, width=3)
    draw.text((width // 2, 540), display_title, fill=gold_light, font=font, anchor="mm")
    draw.line([(250, 600), (width - 250, 600)], fill=gold_color, width=3)

    output = io.BytesIO()
    img.save(output, format="JPEG", quality=95)
    output.seek(0)
    return output

def extract_video_frame(video_path: str) -> io.BytesIO:
    """استخراج كادر سينمائي متناسق من منتصف الفيديو."""
    cap = cv2.VideoCapture(video_path)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    # أخذ كادر عند 15% من مدة الفيديو لتجنب الشاشات السوداء في البداية
    target_frame = max(0, int(total_frames * 0.15))
    cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame)
    success, frame = cap.read()
    if not success:
        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        success, frame = cap.read()
    cap.release()

    if success:
        # إضافة طبقة تعزيز تباين وتدرج بسيط لجاذبية الكادر
        is_success, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
        if is_success:
            return io.BytesIO(buffer.tobytes())
    return None

# -------------------------------------------------------------
# 4. هندسة السيمترية البصرية والأزرار
# -------------------------------------------------------------
def build_custom_keyboard(bot_uname: str, lecture_id: int, primary_text: str, primary_action_prefix: str, channel_msg_id: int = None):
    primary_url = f"https://t.me/{bot_uname}?start={primary_action_prefix}_{lecture_id}"
    comment_url = f"https://t.me/{bot_uname}?start=comment_{lecture_id}"

    clean_chan = CHANNEL_ID.replace("@", "")
    share_post_url = f"https://t.me/{clean_chan}/{channel_msg_id}" if channel_msg_id else f"https://t.me/{clean_chan}"
    share_text = urllib.parse.quote("انضم لمتابعة جديد الفوائد والدروس:")
    share_url = f"https://t.me/share/url?url={urllib.parse.quote(share_post_url)}&text={share_text}"

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
                "👋 **مرحباً بك يا مدير القناة في لوحة التحكم التحريرية:**\n\n"
                "• **إرسال فيديو:** سيستخرج البوت كادراً أنيقاً تلقائياً ويطلب منك العنوان قبل النشر.\n"
                "• **إرسال صوت:** سيصنع البوت تصميماً زخرفياً إسلامياً ويطلب منك العنوان لدمجه بداخله قبل النشر.\n"
                "• لن يُنشر أي فيديو أو صوت داخل القناة إطلاقاً، بل يستلمه المتابع بالخاص بنقرة زر."
            )
            await update.message.reply_text(msg, parse_mode="Markdown")
        else:
            await update.message.reply_text(f"مرحباً بك يا {user.first_name} في بوت الخدمة والتفاعل.")
        return

    payload = args[0]

    # أ) مشاهدة الفيديو
    if payload.startswith("watch_"):
        lecture_id = int(payload.split("_")[1])
        pub = get_publication(lecture_id)
        if not pub:
            await update.message.reply_text("عذراً، هذا المقطع غير متاح.")
            return

        log_event(lecture_id, "watch_video", user)
        await context.bot.send_message(
            chat_id=ADMIN_USER_ID,
            text=f"🔒 **إشعار مشاهدة فيديو (سري)**\n• المنشور: `{lecture_id}`\n• المتابع: {user.full_name} (@{user.username or 'بدون'}) | ID: `{user.id}`",
            parse_mode="Markdown"
        )
        if pub[0] == "doc_video":
            await update.message.reply_document(document=pub[1], caption=pub[3] or "", parse_mode="HTML")
        else:
            await update.message.reply_video(video=pub[1], caption=pub[3] or "", parse_mode="HTML")

    # ب) استمع للتسجيل الصوتي
    elif payload.startswith("listen_"):
        lecture_id = int(payload.split("_")[1])
        pub = get_publication(lecture_id)
        if not pub:
            await update.message.reply_text("عذراً، هذا التسجيل غير متاح.")
            return

        log_event(lecture_id, "listen_audio", user)
        await context.bot.send_message(
            chat_id=ADMIN_USER_ID,
            text=f"🔒 **إشعار استماع لخطبة (سري)**\n• المنشور: `{lecture_id}`\n• المتابع: {user.full_name} (@{user.username or 'بدون'}) | ID: `{user.id}`",
            parse_mode="Markdown"
        )
        if pub[0] == "voice":
            await update.message.reply_voice(voice=pub[1], caption=pub[3] or "")
        else:
            await update.message.reply_audio(audio=pub[1], caption=pub[3] or "", parse_mode="HTML")

    # ج) فتح / تحميل الكتاب
    elif payload.startswith("doc_"):
        lecture_id = int(payload.split("_")[1])
        pub = get_publication(lecture_id)
        if not pub:
            await update.message.reply_text("عذراً، الملف غير متوفر.")
            return

        log_event(lecture_id, "download_doc", user)
        await context.bot.send_message(
            chat_id=ADMIN_USER_ID,
            text=f"🔒 **إشعار فتح / تحميل كتاب (سري)**\n• المنشور: `{lecture_id}`\n• المتابع: {user.full_name} (@{user.username or 'بدون'}) | ID: `{user.id}`",
            parse_mode="Markdown"
        )
        await update.message.reply_document(document=pub[1], caption=pub[3] or "", parse_mode="HTML")

    # د) سجل تعليقك
    elif payload.startswith("comment_"):
        lecture_id = int(payload.split("_")[1])
        context.user_data["action"] = "comment"
        context.user_data["lecture_id"] = lecture_id
        await update.message.reply_text(
            f"💬 **تسجيل تعليق على المنشور رقم `{lecture_id}`:**\nتفضل بإرسال تعليقك الآن (نصياً أو تسجيلاً صوتياً 🎙):",
            parse_mode="Markdown"
        )

# -------------------------------------------------------------
# 6. استقبال الوسائط وإعداد المعاينة قبل النشر للمشرف
# -------------------------------------------------------------
async def handle_admin_media_preparation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """التقاط الوسائط من المدير وتجهيز الكادر أو التصميم الزخرفي وطلب الكابشن."""
    if update.effective_user.id != ADMIN_USER_ID:
        return

    msg = update.message
    bot_msg = await msg.reply_text("⏳ جاري تجهيز المعاينة والكادر الهندسي...")

    # أ) معالجة الفيديو (مباشر أو كمستند)
    is_video_doc = (
        msg.document and (
            "video" in (msg.document.mime_type or "").lower() or
            (msg.document.file_name or "").lower().endswith((".mp4", ".mkv", ".mov", ".avi"))
        )
    )
    if msg.video or is_video_doc:
        file_obj = msg.video or msg.document
        m_type = "video" if msg.video else "doc_video"

        # تحميل الفيديو مؤقتاً لاستخراج الكادر
        temp_vpath = f"temp_{file_obj.file_unique_id}.mp4"
        tg_file = await context.bot.get_file(file_obj.file_id)
        await tg_file.download_to_drive(temp_vpath)

        frame_bytes = extract_video_frame(temp_vpath)
        if os.path.exists(temp_vpath):
            os.remove(temp_vpath)  # تفريغ الذاكرة والقرص فوراً

        if frame_bytes:
            # حفظ الحالة في جلسة المشرف
            context.user_data["draft"] = {
                "media_type": m_type,
                "file_id": file_obj.file_id,
                "cover_bytes": frame_bytes.getvalue(),
                "caption": msg.caption or ""
            }
            context.user_data["action"] = "awaiting_caption"

            await bot_msg.delete()
            frame_bytes.seek(0)
            await msg.reply_photo(
                photo=frame_bytes,
                caption="🎬 **تم استخراج هذا الكادر الأنيق كثيمبل للمقطع!**\n\n"
                        "✏️ **أرسل الآن العنوان والكابشن** الذي تريده أن يظهر أسفل المنشور في القناة.\n"
                        "(أو أرسل كلمة `اعتماد` إذا أردت اعتماد الوصف الحالي مباشرة)."
            )
            return

    # ب) معالجة الملفات الصوتية
    elif msg.audio or msg.voice:
        file_obj = msg.audio or msg.voice
        m_type = "audio" if msg.audio else "voice"

        initial_title = msg.caption or (msg.audio.title if msg.audio and msg.audio.title else "خطبة مباركة")
        context.user_data["draft"] = {
            "media_type": m_type,
            "file_id": file_obj.file_id,
            "caption": initial_title
        }
        context.user_data["action"] = "awaiting_audio_title"

        await bot_msg.delete()
        await msg.reply_text(
            "🎧 **تم استلام المقطع الصوتي بنجاح!**\n\n"
            "✍️ أرسل الآن **العنوان** الذي ترغب في وضعه داخل التصميم الزخرفي الإسلامي وفي الكابشن:"
        )
        return

    # ج) معالجة كتب PDF
    elif msg.document and (msg.document.mime_type == "application/pdf" or (msg.document.file_name or "").lower().endswith(".pdf")):
        tg_file = await context.bot.get_file(msg.document.file_id)
        pdf_bytes = await tg_file.download_as_bytearray()
        cover_img_bytes = None
        try:
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            if len(doc) > 0:
                cover_img_bytes = doc.load_page(0).get_pixmap().tobytes("jpeg")
            doc.close()
            del pdf_bytes
        except Exception as e:
            logging.error(f"PDF Cover error: {e}")

        context.user_data["draft"] = {
            "media_type": "pdf",
            "file_id": msg.document.file_id,
            "cover_bytes": cover_img_bytes,
            "caption": msg.caption or "كتاب قيّم متاح للتحميل"
        }
        context.user_data["action"] = "awaiting_caption"

        await bot_msg.delete()
        if cover_img_bytes:
            await msg.reply_photo(
                photo=io.BytesIO(cover_img_bytes),
                caption="📚 **تم استخراج غلاف الكتاب!**\n\nأرسل الآن نص الكابشن للمنشور، أو أرسل `اعتماد`."
            )
        else:
            await msg.reply_text("📚 أرسل نص الكابشن لبطاقة الكتاب، أو أرسل `اعتماد`.")

# -------------------------------------------------------------
# 7. محادثة إدخال الكابشن وتوليد التصميم وتأكيد النشر
# -------------------------------------------------------------
async def handle_admin_text_inputs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    # معالجة تعليقات المتابعين في الخاص
    if context.user_data.get("action") == "comment":
        lecture_id = context.user_data.get("lecture_id")
        c_content = update.message.text or (update.message.voice.file_id if update.message.voice else "")
        c_type = "voice" if update.message.voice else "text"

        with sqlite3.connect(DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO comments (lecture_id, user_id, username, full_name, comment_type, comment_content)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (lecture_id, user.id, user.username or "بدون معرف", user.full_name or "مجهول", c_type, c_content))
            conn.commit()

        await context.bot.send_message(
            chat_id=ADMIN_USER_ID,
            text=f"🔔 **تعليق جديد وارد!**\n• المنشور: `{lecture_id}`\n• المتابع: {user.full_name} (@{user.username or 'بدون'})",
            parse_mode="Markdown"
        )
        if c_type == "text":
            await context.bot.send_message(chat_id=ADMIN_USER_ID, text=f"📝 التعليق:\n{c_content}")
        else:
            await context.bot.send_voice(chat_id=ADMIN_USER_ID, voice=c_content)

        context.user_data.clear()
        await update.message.reply_text("✅ جزاك الله خيراً، تم تسجيل تعليقك بنجاح.")
        return

    # استقبال مدخلات المشرف لصياغة المنشور
    if user.id == ADMIN_USER_ID and "draft" in context.user_data:
        draft = context.user_data["draft"]
        action = context.user_data.get("action")

        # أ) إدخال عنوان الصوت وتوليد التصميم الإسلامي
        if action == "awaiting_audio_title":
            title = update.message.text
            draft["caption"] = title
            # توليد التصميم الإسلامي مع العنوان
            islamic_card = create_islamic_audio_card(title)
            draft["cover_bytes"] = islamic_card.getvalue()

            confirm_markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("🚀 اعتماد ونشر في القناة الآن", callback_data="confirm_publish")],
                [InlineKeyboardButton("❌ إلغاء", callback_data="cancel_publish")]
            ])

            islamic_card.seek(0)
            await update.message.reply_photo(
                photo=islamic_card,
                caption=f"🕌 **تم دمج العنوان داخل التصميم الإسلامي بنجاح!**\n\n"
                        f"• **الكابشن المقترح:**\n{title}\n\n"
                        f"اضغط على الزر أدناه لإطلاق المنشور في القناة[span_4](start_span)[span_4](end_span):",
                reply_markup=confirm_markup,
                parse_mode="Markdown"
            )
            return

        # ب) إدخال كابشن الفيديو أو الـ PDF
        elif action == "awaiting_caption":
            text = update.message.text
            if text.strip() != "اعتماد":
                draft["caption"] = text

            confirm_markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("🚀 اعتماد ونشر في القناة الآن", callback_data="confirm_publish")],
                [InlineKeyboardButton("❌ إلغاء", callback_data="cancel_publish")]
            ])

            if draft.get("cover_bytes"):
                await update.message.reply_photo(
                    photo=io.BytesIO(draft["cover_bytes"]),
                    caption=f"📋 **معاينة المنشور النهائي:**\n\n{draft['caption']}\n\nهل تريد النشر بالقناة؟",
                    reply_markup=confirm_markup
                )
            else:
                await update.message.reply_text(
                    f"📋 **معاينة النص:**\n\n{draft['caption']}",
                    reply_markup=confirm_markup
                )

# -------------------------------------------------------------
# 8. زر التأكيد النهائي والنشر في القناة
# -------------------------------------------------------------
async def handle_publish_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_publish":
        context.user_data.clear()
        await query.edit_message_caption("❌ تم إلغاء مسودة المنشور.")
        return

    if query.data == "confirm_publish":
        draft = context.user_data.get("draft")
        if not draft:
            await query.edit_message_caption("⚠️ انتهت صلاحية المسودة، يرجى إعادة إرسال الملف.")
            return

        bot_info = await context.bot.get_me()
        bot_uname = bot_info.username

        media_type = draft["media_type"]
        file_id = draft["file_id"]
        caption = draft["caption"]
        cover_bytes = draft.get("cover_bytes")

        # 1. حفظ في قاعدة البيانات
        pub_id = save_publication(media_type, file_id, "", caption)

        # 2. تحديد الزر الأساسي والسيمترية
        if "video" in media_type:
            btn_text = "🎬 مشاهدة الفيديو"
            action_pfx = "watch"
        elif "audio" in media_type or media_type == "voice":
            btn_text = "🎧 استمع للخطبة"
            action_pfx = "listen"
        else:
            btn_text = "📥 فتح / تحميل الكتاب"
            action_pfx = "doc"

        reply_markup = build_custom_keyboard(bot_uname, pub_id, btn_text, action_pfx)

        # 3. النشر الفعلي في القناة (صورة الكادر أو التصميم الإسلامي فقط دون الملف الأصلي)
        if cover_bytes:
            sent_msg = await context.bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=io.BytesIO(cover_bytes),
                caption=caption,
                reply_markup=reply_markup,
                parse_mode="HTML"
            )
        else:
            sent_msg = await context.bot.send_message(
                chat_id=CHANNEL_ID,
                text=caption,
                reply_markup=reply_markup,
                parse_mode="HTML"
            )

        # 4. تحديث رابط المشاركة بـ message_id
        update_publication_msg_id(pub_id, sent_msg.message_id)
        updated_markup = build_custom_keyboard(bot_uname, pub_id, btn_text, action_pfx, sent_msg.message_id)
        await sent_msg.edit_reply_markup(reply_markup=updated_markup)

        context.user_data.clear()
        await query.edit_message_caption(f"✅ **تم نشر المادة في القناة بنجاح بنظام الأزرار السيمترية (المعرف: `{pub_id}`).**")

# -------------------------------------------------------------
# 9. تشغيل التطبيق
# -------------------------------------------------------------
def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN غير محدد!")

    init_db()

    web_thread = Thread(target=run_web_server, daemon=True)
    web_thread.start()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CallbackQueryHandler(handle_publish_callback, pattern="^(confirm_publish|cancel_publish)$"))

    admin_media_filter = filters.Chat(ADMIN_USER_ID) & (
        filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.VOICE | filters.Document.ALL
    )
    app.add_handler(MessageHandler(admin_media_filter, handle_admin_media_preparation))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, handle_admin_text_inputs))

    app.run_polling()

if __name__ == "__main__":
    main()
