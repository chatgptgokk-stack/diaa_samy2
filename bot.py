import os
import sys
import io
import html
import logging
import threading
import urllib.request
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
import psycopg2
from psycopg2 import pool
import fitz  # PyMuPDF
import cv2
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
    ContextTypes,
    filters,
)

# ----------------- تسجيل الأحداث (Logging) -----------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ----------------- المتغيرات وإعدادات البيئة -----------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8943828841:AAE6hpSO8e0_UwpXtk6mUQWKsw0OTNY6i8Y")
ADMIN_USER_ID = int(os.environ.get("ADMIN_USER_ID", "8389850706"))
CHANNEL_ID = os.environ.get("CHANNEL_ID", "@DiaaEldinSamy4")
DATABASE_URL = os.environ.get("DATABASE_URL")

FONT_URL = "https://github.com/google/fonts/raw/main/ofl/amiri/Amiri-Bold.ttf"
FONT_PATH = "Amiri-Bold.ttf"

# ----------------- خادم الويب لمراقبة UptimeRobot -----------------
class SimpleHealthHandler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Bot is Active and Running!")

    def log_message(self, format, *args):
        return

def run_http_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), SimpleHealthHandler)
    logger.info(f"تم تشغيل خادم المراقبة على المنفذ: {port}")
    server.serve_forever()

# ----------------- قاعدة بيانات Supabase (PostgreSQL) -----------------
def get_db_connection():
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL غير محدد في المتغيرات!")
    return psycopg2.connect(DATABASE_URL)

def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute("""
    CREATE TABLE IF NOT EXISTS publications (
        id SERIAL PRIMARY KEY,
        media_type VARCHAR(50),
        file_id TEXT,
        cover_file_id TEXT,
        caption TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS analytics (
        id SERIAL PRIMARY KEY,
        publication_id INT,
        action_type VARCHAR(50),
        user_id BIGINT,
        user_name TEXT,
        username TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)
    conn.commit()
    cur.close()
    conn.close()
    logger.info("تم التحقق من جداول قاعدة البيانات بنجاح.")

def save_publication(media_type, file_id, cover_file_id, caption):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO publications (media_type, file_id, cover_file_id, caption) VALUES (%s, %s, %s, %s) RETURNING id;",
        (media_type, file_id, cover_file_id, caption)
    )
    pub_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return pub_id

def get_publication(pub_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT media_type, file_id, cover_file_id, caption FROM publications WHERE id = %s;", (pub_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row

def log_event(pub_id, action_type, user):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO analytics (publication_id, action_type, user_id, user_name, username) VALUES (%s, %s, %s, %s, %s);",
            (pub_id, action_type, user.id, user.full_name, user.username)
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f"خطأ في تسجيل الإحصائيات: {e}")

# ----------------- دالة إرسال إشعار سري آمن للمشرف بصيغة HTML -----------------
async def notify_admin_event(context: ContextTypes.DEFAULT_TYPE, title: str, pub_id: int, user):
    clean_name = html.escape(user.full_name or "بدون اسم")
    uname = f"@{user.username}" if user.username else "بدون معرف"
    admin_text = (
        f"🔒 <b>{title}</b>\n"
        f"• المنشور رقم: <code>{pub_id}</code>\n"
        f"• المتابع: {clean_name} ({uname})\n"
        f"• المعرف (ID): <code>{user.id}</code>"
    )
    try:
        await context.bot.send_message(
            chat_id=ADMIN_USER_ID,
            text=admin_text,
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"فشل إرسال إشعار المشرف: {e}")

# ----------------- معالجة الوسائط والأغلفة -----------------
def ensure_arabic_font():
    if not os.path.exists(FONT_PATH) or os.path.getsize(FONT_PATH) < 10000:
        try:
            logger.info("جاري تحميل الخط العربي Amiri-Bold...")
            headers = {'User-Agent': 'Mozilla/5.0'}
            req = urllib.request.Request(FONT_URL, headers=headers)
            with urllib.request.urlopen(req) as response, open(FONT_PATH, 'wb') as out_file:
                out_file.write(response.read())
            logger.info("تم تحميل الخط العربي بنجاح.")
        except Exception as e:
            logger.error(f"فشل تحميل الخط العربي: {e}")

def extract_pdf_cover(pdf_bytes: bytes) -> bytes:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = doc.load_page(0)
    pix = page.get_pixmap(dpi=150)
    img_data = pix.tobytes("jpg")
    doc.close()
    return img_data

def extract_video_frame(video_path: str) -> bytes:
    cap = cv2.VideoCapture(video_path)
    cap.set(cv2.CAP_PROP_POS_MSEC, 2000)
    success, frame = cap.read()
    if not success:
        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        success, frame = cap.read()
    cap.release()
    if success:
        _, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
        return buffer.tobytes()
    return None

def split_arabic_text(text: str, max_chars_per_line: int = 28) -> list:
    words = text.split()
    lines = []
    current_line = []
    current_length = 0

    for word in words:
        if current_length + len(word) + 1 <= max_chars_per_line:
            current_line.append(word)
            current_length += len(word) + 1
        else:
            if current_line:
                lines.append(" ".join(current_line))
            current_line = [word]
            current_length = len(word)

    if current_line:
        lines.append(" ".join(current_line))
    return lines

def create_audio_poster(title_text: str) -> bytes:
    ensure_arabic_font()
    width, height = 1080, 1080
    image = Image.new("RGB", (width, height), color=(18, 30, 49))
    draw = ImageDraw.Draw(image)

    # إطار إسلامي ذهبي
    draw.rectangle([45, 45, width - 45, height - 45], outline=(212, 175, 55), width=7)
    draw.rectangle([65, 65, width - 65, height - 65], outline=(160, 130, 40), width=2)

    corner_len = 40
    for cx, cy in [(75, 75), (width - 75, 75), (75, height - 75), (width - 75, height - 75)]:
        draw.rectangle([cx - 5, cy - 5, cx + 5, cy + 5], fill=(212, 175, 55))

    try:
        title_font = ImageFont.truetype(FONT_PATH, 52)
        footer_font = ImageFont.truetype(FONT_PATH, 36)
    except Exception:
        title_font = ImageFont.load_default()
        footer_font = title_font

    raw_lines = split_arabic_text(title_text, max_chars_per_line=26)

    line_spacing = 25
    line_height = 65
    total_text_height = len(raw_lines) * line_height + (len(raw_lines) - 1) * line_spacing
    start_y = (height - total_text_height) // 2

    for i, line in enumerate(raw_lines):
        y = start_y + i * (line_height + line_spacing)
        try:
            draw.text((width // 2, y), line, fill=(245, 245, 245), font=title_font, anchor="mm", direction="rtl")
        except TypeError:
            draw.text((width // 2, y), line, fill=(245, 245, 245), font=title_font, anchor="mm")

    footer_text = "منصة القناة الرسمية"
    try:
        draw.text((width // 2, height - 120), footer_text, fill=(212, 175, 55), font=footer_font, anchor="mm", direction="rtl")
    except TypeError:
        draw.text((width // 2, height - 120), footer_text, fill=(212, 175, 55), font=footer_font, anchor="mm")

    out_buffer = io.BytesIO()
    image.save(out_buffer, format="JPEG", quality=95)
    return out_buffer.getvalue()

# ----------------- معالجات الأوامر والروابط -----------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args

    if not args:
        if user.id == ADMIN_USER_ID:
            admin_panel = (
                "👋 مرحباً بك يا مدير القناة في لوحة التحكم الإدارية:\n\n"
                "• لتحديث لوحة الأزرار في القناة: أرسل الأمر /menu.\n"
                "• إرسال صوتيات: تصميم إسلامي ملكي بعد كتابة الكابشن بيدك.\n"
                "• إرسال فيديو (مباشر أو كملف): كادر نقي مع طلب الكابشن.\n"
                "• إرسال PDF: استخراج صفحة الغلاف مع طلب الكابشن.\n"
                "• إرسال صورة / بوستر: نشر دعوي مباشر مع طلب الكابشن."
            )
            await update.message.reply_text(admin_panel)
        else:
            await update.message.reply_text("أهلاً بك! يمكنك تصفح مواد القناة وخدماتها من خلال الأزرار التفاعلية المرفقة بالمنشورات.")
        return

    payload = args[0]

    # خدمة إسأل الشيخ
    if payload == "ask_admin":
        await update.message.reply_text(
            "🔒 مرحباً بك في خدمة (إسأل الشيخ) الخاصة:\n\n"
            "هذه المحادثة سرية ومشفرة تماماً بينك وبين الشيخ.\n"
            "تفضل بكتابة سؤالك الآن نصياً أو سجّل مقطعاً صوتياً (🎙):"
        )
        context.user_data["awaiting_question"] = True
        return

    # خدمة سجل تعليقك
    if payload.startswith("comment_"):
        try:
            pub_id = int(payload.split("_")[1])
        except (IndexError, ValueError):
            await update.message.reply_text("عذراً، الرابط غير صحيح.")
            return

        pub = get_publication(pub_id)
        pub_title = pub[3] if pub and pub[3] else f"المنشور رقم {pub_id}"

        context.user_data["awaiting_material_comment"] = pub_id
        await update.message.reply_text(
            f"💬 <b>تسجيل تعليق حول المادة:</b>\n"
            f"« {pub_title} »\n\n"
            f"تفضل بكتابة تعليقك أو انطباعك الآن (نصاً أو تسجيلاً صوتياً 🎙)، وسيصل مباشرة للإدارة في سرية تامة:",
            parse_mode="HTML"
        )
        return

    # استماع لتسجيل صوتي
    if payload.startswith("listen_"):
        try:
            pub_id = int(payload.split("_")[1])
        except (IndexError, ValueError):
            await update.message.reply_text("عذراً، الرابط غير صالح.")
            return

        log_event(pub_id, "listen_audio", user)
        await notify_admin_event(context, "إشعار استماع لمادة صوتية (سري)", pub_id, user)

        pub = get_publication(pub_id)
        if not pub:
            await update.message.reply_text("عذراً، هذا المقطع الصوتي غير متاح حالياً.")
            return

        media_type, file_id, _, cap = pub
        if media_type == "voice":
            await update.message.reply_voice(voice=file_id, caption=cap or "", parse_mode="HTML")
        else:
            await update.message.reply_audio(audio=file_id, caption=cap or "", parse_mode="HTML")

    # فتح / تحميل ملف أو صورة
    elif payload.startswith("doc_"):
        try:
            pub_id = int(payload.split("_")[1])
        except (IndexError, ValueError):
            await update.message.reply_text("عذراً، الرابط غير صالح.")
            return

        log_event(pub_id, "download_doc", user)
        await notify_admin_event(context, "إشعار مشاهدة / تنزيل مادة (سري)", pub_id, user)

        pub = get_publication(pub_id)
        if not pub:
            await update.message.reply_text("عذراً، هذه المادة غير متاحة حالياً.")
            return

        media_type, file_id, _, cap = pub
        if media_type == "image":
            await update.message.reply_photo(photo=file_id, caption=cap or "", parse_mode="HTML")
        else:
            await update.message.reply_document(document=file_id, caption=cap or "", parse_mode="HTML")

    # مشاهدة مقطع فيديو
    elif payload.startswith("watch_"):
        try:
            pub_id = int(payload.split("_")[1])
        except (IndexError, ValueError):
            await update.message.reply_text("عذراً، الرابط غير صالح.")
            return

        log_event(pub_id, "watch_video", user)
        await notify_admin_event(context, "إشعار مشاهدة فيديو (سري)", pub_id, user)

        pub = get_publication(pub_id)
        if not pub:
            await update.message.reply_text("عذراً، هذا الفيديو غير متاح.")
            return

        media_type, file_id, _, cap = pub
        if media_type == "video_doc":
            await update.message.reply_document(document=file_id, caption=cap or "", parse_mode="HTML")
        else:
            await update.message.reply_video(video=file_id, caption=cap or "", parse_mode="HTML")

# ----------------- نشر وتحديث لوحة /menu في القناة -----------------
async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        return

    bot_me = await context.bot.get_me()

    keyboard = [
        [
            InlineKeyboardButton("🎙 الخطب", url=f"https://t.me/{bot_me.username}?start=lectures"),
            InlineKeyboardButton("🎬 الفيديوهات", url=f"https://t.me/{bot_me.username}?start=videos"),
        ],
        [
            InlineKeyboardButton("📚 الكتب", url=f"https://t.me/{bot_me.username}?start=books"),
            InlineKeyboardButton("🔎 البحث", url=f"https://t.me/{bot_me.username}?start=search"),
        ],
        [
            InlineKeyboardButton("📩 اسأل الشيخ", url=f"https://t.me/{bot_me.username}?start=ask_admin")
        ]
    ]

    menu_text = (
        "🌿 <b>مرحباً بكم في منصة القناة الرسمية</b> 🌿\n\n"
        "يمكنكم عبر اللوحة التفاعلية أدناه تصفح كافة أقسام المحتوى الدعوي، "
        "أو التواصل وإرسال استفساراتكم الخاصة في سرية تامة:"
    )

    try:
        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=menu_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
        await update.message.reply_text("✅ تم نشر لوحة الأزرار في القناة بنجاح بالتقسيم المعتمد!")
    except Exception as e:
        await update.message.reply_text(f"❌ تعذر نشر اللوحة في القناة: {e}")

# ----------------- استقبال وتجهيز المواد للنشر من المشرف والردود -----------------
async def handle_admin_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.message

    # 1. استلام سؤال خاص للشيخ
    if context.user_data.get("awaiting_question") and user.id != ADMIN_USER_ID:
        context.user_data["awaiting_question"] = False
        clean_name = html.escape(user.full_name or "بدون اسم")
        uname = f"@{user.username}" if user.username else "بدون معرف"

        admin_alert = (
            f"❓ <b>سؤال واستشارة خاصة للشيخ (سري)</b>\n"
            f"• السائل: {clean_name} ({uname})\n"
            f"• المعرف (ID): <code>{user.id}</code>\n"
            f"• النوع: {'صوت' if msg.voice or msg.audio else 'نص'}\n\n"
        )

        reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("✍️ الإجابة على السؤال", callback_data=f"reply_{user.id}")]])

        if msg.text:
            admin_alert += f"📝 <b>نص السؤال:</b>\n{html.escape(msg.text)}"
            await context.bot.send_message(chat_id=ADMIN_USER_ID, text=admin_alert, parse_mode="HTML", reply_markup=reply_markup)
        elif msg.voice:
            await context.bot.send_voice(chat_id=ADMIN_USER_ID, voice=msg.voice.file_id, caption=admin_alert, parse_mode="HTML", reply_markup=reply_markup)
        elif msg.audio:
            await context.bot.send_audio(chat_id=ADMIN_USER_ID, audio=msg.audio.file_id, caption=admin_alert, parse_mode="HTML", reply_markup=reply_markup)

        await msg.reply_text("✅ تم استلام سؤالك في سرية تامة، وسيجيب عليه الشيخ قريباً بإذن الله.")
        return

    # 2. استلام تعليق العضو حول مادة محددة
    if context.user_data.get("awaiting_material_comment") and user.id != ADMIN_USER_ID:
        pub_id = context.user_data["awaiting_material_comment"]
        context.user_data["awaiting_material_comment"] = None

        clean_name = html.escape(user.full_name or "بدون اسم")
        uname = f"@{user.username}" if user.username else "بدون معرف"

        comment_alert = (
            f"💬 <b>تعليق وارد حول مادة بالقناة (سري)</b>\n"
            f"• على المنشور رقم: <code>{pub_id}</code>\n"
            f"• من: {clean_name} ({uname})\n"
            f"• المعرف (ID): <code>{user.id}</code>\n"
            f"• النوع: {'صوت' if msg.voice or msg.audio else 'نص'}\n\n"
        )

        reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("✍️ الرد على المعلق", callback_data=f"reply_{user.id}")]])

        if msg.text:
            comment_alert += f"📝 <b>نص التعليق:</b>\n{html.escape(msg.text)}"
            await context.bot.send_message(chat_id=ADMIN_USER_ID, text=comment_alert, parse_mode="HTML", reply_markup=reply_markup)
        elif msg.voice:
            await context.bot.send_voice(chat_id=ADMIN_USER_ID, voice=msg.voice.file_id, caption=comment_alert, parse_mode="HTML", reply_markup=reply_markup)
        elif msg.audio:
            await context.bot.send_audio(chat_id=ADMIN_USER_ID, audio=msg.audio.file_id, caption=comment_alert, parse_mode="HTML", reply_markup=reply_markup)

        await msg.reply_text("✅ شكراً لك، تم تسجيل تعليقك وإرساله للإدارة بنجاح.")
        return

    # 3. إرسال رد المشرف للسائل أو المعلق
    if user.id == ADMIN_USER_ID and context.user_data.get("answering_user_id"):
        target_id = context.user_data["answering_user_id"]
        try:
            if msg.text:
                await context.bot.send_message(
                    chat_id=target_id,
                    text=f"📨 <b>رد وارد من الإدارة على رسالتك:</b>\n\n{html.escape(msg.text)}",
                    parse_mode="HTML"
                )
            elif msg.voice:
                await context.bot.send_voice(
                    chat_id=target_id,
                    voice=msg.voice.file_id,
                    caption="🎙 <b>تسجيل صوتي وارد من الإدارة رداً على رسالتك.</b>",
                    parse_mode="HTML"
                )
            await msg.reply_text("✅ تم إرسال الرد للشخص المعني بنجاح.")
        except Exception as e:
            await msg.reply_text(f"❌ تعذر توصيل الرد: {e}")
        finally:
            context.user_data["answering_user_id"] = None
        return

    # التحقق من صلاحية الإدارة
    if user.id != ADMIN_USER_ID:
        return

    # 4. استقبال ملف صوتي جديد (تصفير أي انتظار سابق فوراً)
    if msg.audio or msg.voice:
        context.user_data.clear()
        context.user_data["temp_media_type"] = "voice" if msg.voice else "audio"
        context.user_data["temp_file_id"] = (msg.voice or msg.audio).file_id
        context.user_data["awaiting_custom_caption"] = True
        await msg.reply_text("✍️ <b>تم استلام المقطع الصوتي.</b>\n\nماذا تحب أن نكتب في العنوان والكابشن على البوستر الملكي؟\nتفضل بإرسال النص الآن:")
        return

    # 5. استقبال مقطع فيديو مباشر
    if msg.video:
        context.user_data.clear()
        status_msg = await msg.reply_text("⏳ جاري استخراج كادر نقي بأبعاده الطبيعية من الفيديو...")
        frame_bytes = None
        try:
            if msg.video.thumbnail:
                thumb_file = await context.bot.get_file(msg.video.thumbnail.file_id)
                frame_bytes = await thumb_file.download_as_bytearray()

            if not frame_bytes and (msg.video.file_size or 0) < 20 * 1024 * 1024:
                tg_file = await context.bot.get_file(msg.video.file_id)
                temp_path = f"temp_vid_{msg.video.file_id[:8]}.mp4"
                await tg_file.download_to_drive(temp_path)
                frame_bytes = extract_video_frame(temp_path)
                if os.path.exists(temp_path):
                    os.remove(temp_path)

            if not frame_bytes:
                await status_msg.edit_text("❌ تعذر استخراج كادر الفيديو، يرجى إرسال صورة كغلاف للفيديو أولاً.")
                return

            context.user_data["temp_media_type"] = "video"
            context.user_data["temp_file_id"] = msg.video.file_id
            context.user_data["temp_cover_bytes"] = bytes(frame_bytes)
            context.user_data["awaiting_custom_caption"] = True

            await status_msg.delete()
            photo_file = io.BytesIO(bytes(frame_bytes))
            photo_file.name = "cover.jpg"
            await msg.reply_photo(
                photo=photo_file,
                caption="✍️ <b>تم التقاط كادر الفيديو بنجاح.</b>\n\nماذا تحب أن نكتب في الكابشن الخاص بهذا الفيديو؟\nتفضل بإرسال النص الآن:"
            )
        except Exception as e:
            logger.error(f"خطأ في معالجة الفيديو: {e}")
            await status_msg.edit_text(f"❌ تعذر معالجة الفيديو: {e}")
        return

    # 6. استقبال المستندات (فيديوهات ملفات أو كتب PDF)
    if msg.document:
        doc = msg.document
        mime = (doc.mime_type or "").lower()
        file_name = (doc.file_name or "").lower()
        video_extensions = ('.mp4', '.mkv', '.avi', '.mov', '.wmv', '.rmvb', '.flv', '.3gp')

        # أ- ملف فيديو مرسل كمستند
        if mime.startswith("video/") or file_name.endswith(video_extensions):
            context.user_data.clear()
            status_msg = await msg.reply_text("⏳ جاري معالجة ملف الفيديو واستخراج الكادر...")
            frame_bytes = None
            try:
                if doc.thumbnail:
                    thumb_file = await context.bot.get_file(doc.thumbnail.file_id)
                    frame_bytes = await thumb_file.download_as_bytearray()

                if not frame_bytes and (doc.file_size or 0) < 20 * 1024 * 1024:
                    tg_file = await context.bot.get_file(doc.file_id)
                    ext = os.path.splitext(file_name)[1] or ".mp4"
                    temp_path = f"temp_doc_{doc.file_id[:8]}{ext}"
                    await tg_file.download_to_drive(temp_path)
                    frame_bytes = extract_video_frame(temp_path)
                    if os.path.exists(temp_path):
                        os.remove(temp_path)

                if not frame_bytes:
                    await status_msg.edit_text("❌ تعذر استخراج كادر الفيديو لكبر حجم الملف بدون غلاف مصغر.")
                    return

                context.user_data["temp_media_type"] = "video_doc"
                context.user_data["temp_file_id"] = doc.file_id
                context.user_data["temp_cover_bytes"] = bytes(frame_bytes)
                context.user_data["awaiting_custom_caption"] = True

                await status_msg.delete()
                photo_file = io.BytesIO(bytes(frame_bytes))
                photo_file.name = "cover.jpg"
                await msg.reply_photo(
                    photo=photo_file,
                    caption="✍️ <b>تم استخراج كادر ملف الفيديو بنجاح.</b>\n\nماذا تحب أن نكتب في الكابشن الخاص به؟\nتفضل بإرسال النص الآن:"
                )
            except Exception as e:
                logger.error(f"خطأ في معالجة ملف الفيديو: {e}")
                await status_msg.edit_text(f"❌ تعذر معالجة ملف الفيديو: {e}")
            return

        # ب- كتاب PDF
        elif mime == "application/pdf" or file_name.endswith('.pdf'):
            context.user_data.clear()
            status_msg = await msg.reply_text("⏳ جاري استخراج الصفحة الأولى لغلاف الكتاب...")
            cover_bytes = None
            try:
                if doc.thumbnail:
                    thumb_file = await context.bot.get_file(doc.thumbnail.file_id)
                    cover_bytes = await thumb_file.download_as_bytearray()
                
                if not cover_bytes and (doc.file_size or 0) < 20 * 1024 * 1024:
                    tg_file = await context.bot.get_file(doc.file_id)
                    pdf_bytes = await tg_file.download_as_bytearray()
                    cover_bytes = extract_pdf_cover(pdf_bytes)

                if not cover_bytes:
                    await status_msg.edit_text("❌ تعذر سحب غلاف الـ PDF من تيليجرام.")
                    return

                context.user_data["temp_media_type"] = "pdf"
                context.user_data["temp_file_id"] = doc.file_id
                context.user_data["temp_cover_bytes"] = bytes(cover_bytes)
                context.user_data["awaiting_custom_caption"] = True

                await status_msg.delete()
                photo_file = io.BytesIO(bytes(cover_bytes))
                photo_file.name = "cover.jpg"
                await msg.reply_photo(
                    photo=photo_file,
                    caption="✍️ <b>تم استخراج الصفحة الأولى كغلاف للملف بنجاح.</b>\n\nماذا تحب أن نكتب في الكابشن والوصف الخاص بهذا الكتاب؟\nتفضل بإرسال النص الآن:"
                )
            except Exception as e:
                logger.error(f"خطأ في استخراج غلاف الـ PDF: {e}")
                await status_msg.edit_text(f"❌ تعذر استخراج الغلاف: {e}")
            return

    # 7. استقبال صورة أو بوستر
    if msg.photo:
        context.user_data.clear()
        photo_obj = msg.photo[-1]
        tg_file = await context.bot.get_file(photo_obj.file_id)
        img_bytes = await tg_file.download_as_bytearray()

        context.user_data["temp_media_type"] = "image"
        context.user_data["temp_file_id"] = photo_obj.file_id
        context.user_data["temp_cover_bytes"] = bytes(img_bytes)
        context.user_data["awaiting_custom_caption"] = True

        await msg.reply_text("✍️ <b>تم استلام التصميم الدعوي.</b>\n\nماذا تحب أن نكتب في الكابشن الخاص بهذا المنشور؟\nتفضل بإرسال النص الآن:")
        return

    # 8. إدخال الكابشن يدوياً من المشرف بعد طلبه
    if context.user_data.get("awaiting_custom_caption") and msg.text:
        custom_caption = msg.text.strip()
        pending_type = context.user_data.get("temp_media_type")
        file_id = context.user_data.get("temp_file_id")
        context.user_data["awaiting_custom_caption"] = False

        status_msg = await msg.reply_text("⏳ جاري تجهيز المعاينة والتصميم بالعنوان الذي كتبته...")

        if pending_type in ["audio", "voice"]:
            poster_bytes = create_audio_poster(custom_caption)
            context.user_data["pending_pub"] = {
                "media_type": pending_type,
                "file_id": file_id,
                "cover_bytes": poster_bytes,
                "caption": custom_caption
            }
            keyboard = [[InlineKeyboardButton("🚀 اعتماد ونشر في القناة الآن", callback_data="publish_now")]]
            await status_msg.delete()
            photo_file = io.BytesIO(poster_bytes)
            photo_file.name = "poster.jpg"
            await msg.reply_photo(
                photo=photo_file,
                caption=f"🎙 <b>معاينة البوستر الإسلامي بالعنوان المطلوب:</b>\n\n{custom_caption}",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )

        elif pending_type in ["video", "video_doc", "pdf", "image"]:
            cover_bytes = context.user_data.get("temp_cover_bytes")
            context.user_data["pending_pub"] = {
                "media_type": pending_type,
                "file_id": file_id,
                "cover_bytes": cover_bytes,
                "caption": custom_caption
            }
            keyboard = [[InlineKeyboardButton("🚀 اعتماد ونشر في القناة الآن", callback_data="publish_now")]]
            await status_msg.delete()
            photo_file = io.BytesIO(cover_bytes)
            photo_file.name = "cover.jpg"
            await msg.reply_photo(
                photo=photo_file,
                caption=f"📋 <b>معاينة المادة بالكابشن المطلوب:</b>\n\n{custom_caption}",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )
        return

# ----------------- معالجة الأزرار التفاعلية والنشر في القناة -----------------
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data.startswith("reply_"):
        target_id = int(query.data.split("_")[1])
        context.user_data["answering_user_id"] = target_id
        await query.message.reply_text(
            f"✍️ <b>وضع الرد السري على المعني (ID: <code>{target_id}</code>):</b>\n\nتفضل بإرسال ردك نصياً أو سجّل مقطعاً صوتياً (🎙)",
            parse_mode="HTML"
        )
        return

    if query.data == "publish_now":
        pending = context.user_data.get("pending_pub")
        if not pending:
            await query.message.reply_text("❌ لم يتم العثور على مادة معلقة للنشر.")
            return

        bot_me = await context.bot.get_me()

        pub_id = save_publication(
            media_type=pending["media_type"],
            file_id=pending["file_id"],
            cover_file_id="",
            caption=pending["caption"]
        )

        encoded_caption = urllib.parse.quote(pending["caption"])

        # توزيع الأزرار المعتمد لواجهات RTL:
        # 1. المواد الصوتية:
        if pending["media_type"] in ["audio", "voice"]:
            share_url = f"https://t.me/share/url?url=https://t.me/{bot_me.username}?start=listen_{pub_id}&text={encoded_caption}"
            channel_markup = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🎧 الاستماع إلى المادة", url=f"https://t.me/{bot_me.username}?start=listen_{pub_id}"),
                    InlineKeyboardButton("💬 سجّل تعليقك", url=f"https://t.me/{bot_me.username}?start=comment_{pub_id}")
                ],
                [
                    InlineKeyboardButton("📢 انشر تؤجر", url=share_url)
                ]
            ])

        # 2. مقاطع الفيديو:
        elif pending["media_type"] in ["video", "video_doc"]:
            share_url = f"https://t.me/share/url?url=https://t.me/{bot_me.username}?start=watch_{pub_id}&text={encoded_caption}"
            channel_markup = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("▶️ مشاهدة الفيديو", url=f"https://t.me/{bot_me.username}?start=watch_{pub_id}"),
                    InlineKeyboardButton("💬 سجّل تعليقك", url=f"https://t.me/{bot_me.username}?start=comment_{pub_id}")
                ],
                [
                    InlineKeyboardButton("📢 انشر تؤجر", url=share_url)
                ]
            ])

        # 3. ملفات الـ PDF أو الصور (صف واحد):
        else:
            channel_markup = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("📥 فتح / تنزيل الملف", url=f"https://t.me/{bot_me.username}?start=doc_{pub_id}"),
                    InlineKeyboardButton("💬 سجّل تعليقك", url=f"https://t.me/{bot_me.username}?start=comment_{pub_id}")
                ]
            ])

        try:
            publish_photo = io.BytesIO(pending["cover_bytes"])
            publish_photo.name = "channel_cover.jpg"
            await context.bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=publish_photo,
                caption=pending["caption"],
                reply_markup=channel_markup
            )
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(f"✅ تم النشر في القناة بنجاح بالأزرار المتفق عليها!\nرقم المنشور: <code>{pub_id}</code>", parse_mode="HTML")
            context.user_data["pending_pub"] = None
        except Exception as e:
            logger.error(f"خطأ في النشر بالقناة: {e}")
            await query.message.reply_text(f"❌ تعذر النشر في القناة: {e}")

# ----------------- الدالة الرئيسية -----------------
def main():
    server_thread = threading.Thread(target=run_http_server, daemon=True)
    server_thread.start()

    init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_admin_media))

    logger.info("تم بدء استماع البوت رسمياً (Long Polling)...")
    app.run_polling()

if __name__ == "__main__":
    main()
