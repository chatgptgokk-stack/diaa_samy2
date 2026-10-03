import os
import sys
import io
import html
import logging
import threading
import textwrap
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
import psycopg2
from psycopg2 import pool
import fitz  # PyMuPDF
import cv2
from PIL import Image, ImageDraw, ImageFont
import arabic_reshaper
from bidi.algorithm import get_display

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

# ----------------- خادم الويب المصغر لمراقبة UptimeRobot -----------------
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
        # منع تسجيل طلبات الفحص الدورية للحفاظ على نظافة السجلات
        return

def run_http_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), SimpleHealthHandler)
    logger.info(f"تم تشغيل خادم المراقبة على المنفذ: {port}")
    server.serve_forever()

# ----------------- إدارة قاعدة بيانات Supabase (PostgreSQL) -----------------
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
    logger.info("تم التحقق من جاهزية جداول قاعدة البيانات بنجاح.")

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
        logger.error(f"خطأ أثناء تسجيل الإحصائية: {e}")

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

# ----------------- وظائف معالجة الوسائط وتوليد الأغلفة والخطوط -----------------
def ensure_arabic_font():
    """تحميل خط أميري العربي تلقائياً إذا لم يكن متوفراً في السيرفر"""
    if not os.path.exists(FONT_PATH):
        try:
            logger.info("جاري تحميل الخط العربي الأصيل Amiri-Bold...")
            urllib.request.urlretrieve(FONT_URL, FONT_PATH)
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
        _, buffer = cv2.imencode(".jpg", frame)
        return buffer.tobytes()
    return None

def create_audio_poster(title_text: str) -> bytes:
    ensure_arabic_font()
    width, height = 1080, 1080
    image = Image.new("RGB", (width, height), color=(18, 30, 49))
    draw = ImageDraw.Draw(image)

    # إطار إسلامي ملكي مزدوج
    draw.rectangle([45, 45, width - 45, height - 45], outline=(212, 175, 55), width=7)
    draw.rectangle([65, 65, width - 65, height - 65], outline=(160, 130, 40), width=2)

    # تحميل الخط العربي المتخصص
    try:
        title_font = ImageFont.truetype(FONT_PATH, 55)
        footer_font = ImageFont.truetype(FONT_PATH, 38)
    except Exception:
        title_font = ImageFont.load_default()
        footer_font = title_font

    # تقسيم النص الطويل تلقائياً لأسطر متناسقة حتى لا يخرج عن الإطار
    wrapped_lines = textwrap.wrap(title_text, width=28)
    if not wrapped_lines:
        wrapped_lines = [title_text]

    processed_lines = []
    for line in wrapped_lines:
        reshaped = arabic_reshaper.reshape(line)
        bidi_line = get_display(reshaped)
        processed_lines.append(bidi_line)

    # حساب موضع البداية لمركزة النص رأسياً
    line_spacing = 25
    line_height = 65
    total_text_height = len(processed_lines) * line_height + (len(processed_lines) - 1) * line_spacing
    start_y = (height - total_text_height) // 2

    # كتابة الأسطر في المنتصف
    for i, line in enumerate(processed_lines):
        y = start_y + i * (line_height + line_spacing)
        draw.text((width // 2, y), line, fill=(245, 245, 245), font=title_font, anchor="mm")

    # توقيع المنصة في أسفل البوستر
    footer_text = get_display(arabic_reshaper.reshape("منصة القناة الرسمية"))
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
                "• إرسال فيديو: كادر سينمائي ثم كتابة الكابشن يدوياً.\n"
                "• إرسال صوت: إرسال المقطع ثم كتابة العنوان بيدك ليظهر في البوستر الملكي.\n"
                "• إرسال PDF: استخراج الغلاف ثم كتابة وصف الكتاب بيدك قبل النشر."
            )
            await update.message.reply_text(admin_panel)
        else:
            await update.message.reply_text("أهلاً بك! يمكنك تصفح مواد القناة والخدمات عبر الأزرار المتاحة فيها.")
        return

    payload = args[0]

    # خدمة إسأل الشيخ
    if payload == "ask_admin":
        await update.message.reply_text(
            "🔒 مرحباً بك في خدمة (إسأل الشيخ) الخاصة:\n\n"
            "هذه المحادثة سرية ومشفرة بالكامل ولا يطّلع عليها أحد.\n"
            "تفضل بكتابة سؤالك الآن نصياً أو أرسله كتسجيل صوتي (🎙):"
        )
        context.user_data["awaiting_question"] = True
        return

    # استماع لتسجيل صوتي
    if payload.startswith("listen_"):
        try:
            pub_id = int(payload.split("_")[1])
        except (IndexError, ValueError):
            await update.message.reply_text("عذراً، الرابط غير صالح.")
            return

        log_event(pub_id, "listen_audio", user)
        await notify_admin_event(context, "إشعار استماع لخطبة (سري)", pub_id, user)

        pub = get_publication(pub_id)
        if not pub:
            await update.message.reply_text("عذراً، هذا التسجيل غير متاح حالياً.")
            return

        media_type, file_id, _, cap = pub
        if media_type == "voice":
            await update.message.reply_voice(voice=file_id, caption=cap or "", parse_mode="HTML")
        else:
            await update.message.reply_audio(audio=file_id, caption=cap or "", parse_mode="HTML")

    # تحميل أو فتح كتاب PDF
    elif payload.startswith("doc_"):
        try:
            pub_id = int(payload.split("_")[1])
        except (IndexError, ValueError):
            await update.message.reply_text("عذراً، الرابط غير صالح.")
            return

        log_event(pub_id, "download_doc", user)
        await notify_admin_event(context, "إشعار فتح / تحميل كتاب (سري)", pub_id, user)

        pub = get_publication(pub_id)
        if not pub:
            await update.message.reply_text("عذراً، هذا الكتاب غير متاح حالياً.")
            return

        _, file_id, _, cap = pub
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

        _, file_id, _, cap = pub
        await update.message.reply_video(video=file_id, caption=cap or "", parse_mode="HTML")

# ----------------- نشر وتحديث لوحة الأزرار /menu -----------------
async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        return

    bot_me = await context.bot.get_me()
    keyboard = [
        [
            InlineKeyboardButton("🎬 الفيديوهات المرئية", url=f"https://t.me/{bot_me.username}?start=videos"),
            InlineKeyboardButton("🎙 الخطب والمحاضرات", url=f"https://t.me/{bot_me.username}?start=lectures"),
        ],
        [
            InlineKeyboardButton("🔎 البحث في المحتوى", url=f"https://t.me/{bot_me.username}?start=search"),
            InlineKeyboardButton("📚 الكتب والرسائل", url=f"https://t.me/{bot_me.username}?start=books"),
        ],
        [
            InlineKeyboardButton("📩 إسأل الشيخ (استشارة خاصة وسرية)", url=f"https://t.me/{bot_me.username}?start=ask_admin")
        ]
    ]

    menu_text = (
        "🌿 <b>مرحباً بكم في منصة القناة الرسمية</b> 🌿\n\n"
        "يمكنكم عبر اللوحة التفاعلية أدناه تصفح كافة الفوائد والمحتويات، "
        "أو التواصل وإرسال استشاراتكم وأسئلتكم الخاصة في سرية تامة:"
    )

    try:
        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=menu_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
        await update.message.reply_text("✅ تم تحديث ونشر لوحة الأزرار في القناة بنجاح!\nيمكنك تثبيتها (Pin) في أعلى القناة الآن.")
    except Exception as e:
        await update.message.reply_text(f"❌ تعذر نشر اللوحة في القناة: {e}")

# ----------------- استقبال وتجهيز المواد للنشر من المشرف -----------------
async def handle_admin_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.message

    # 1. استلام أسئلة الأعضاء الموجهة للشيخ
    if context.user_data.get("awaiting_question") and user.id != ADMIN_USER_ID:
        context.user_data["awaiting_question"] = False
        clean_name = html.escape(user.full_name or "بدون اسم")
        uname = f"@{user.username}" if user.username else "بدون معرف"

        admin_alert = (
            f"❓ <b>سؤال واستشارة خاصة واردة للشيخ (سري جداً)</b>\n"
            f"• من: {clean_name} ({uname})\n"
            f"• المعرف (ID): <code>{user.id}</code>\n"
            f"• النوع: {'صوت' if msg.voice or msg.audio else 'نص'}\n\n"
        )

        reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("✍️ الإجابة على سؤال العضو", callback_data=f"reply_{user.id}")]])

        if msg.text:
            admin_alert += f"📝 <b>نص السؤال:</b>\n{html.escape(msg.text)}"
            await context.bot.send_message(chat_id=ADMIN_USER_ID, text=admin_alert, parse_mode="HTML", reply_markup=reply_markup)
        elif msg.voice:
            await context.bot.send_voice(chat_id=ADMIN_USER_ID, voice=msg.voice.file_id, caption=admin_alert, parse_mode="HTML", reply_markup=reply_markup)
        elif msg.audio:
            await context.bot.send_audio(chat_id=ADMIN_USER_ID, audio=msg.audio.file_id, caption=admin_alert, parse_mode="HTML", reply_markup=reply_markup)

        await msg.reply_text("✅ تم استلام سؤالك في سرية تامة، وسيقوم الشيخ بالاطلاع عليه والإجابة عن استشارتك قريباً بإذن الله.")
        return

    # 2. إرسال إجابة الشيخ إلى السائل
    if user.id == ADMIN_USER_ID and context.user_data.get("answering_user_id"):
        target_id = context.user_data["answering_user_id"]
        try:
            if msg.text:
                await context.bot.send_message(
                    chat_id=target_id,
                    text=f"📨 <b>إجابة واردة من الشيخ على استشارتك:</b>\n\n{html.escape(msg.text)}",
                    parse_mode="HTML"
                )
            elif msg.voice:
                await context.bot.send_voice(
                    chat_id=target_id,
                    voice=msg.voice.file_id,
                    caption="🎙 <b>إجابة صوتية واردة من الشيخ على استشارتك.</b>",
                    parse_mode="HTML"
                )
            await msg.reply_text("✅ تم إرسال الإجابة إلى السائل بنجاح في الخاص.")
        except Exception as e:
            await msg.reply_text(f"❌ تعذر إرسال الإجابة للسائل: {e}")
        finally:
            context.user_data["answering_user_id"] = None
        return

    # فحص صلاحية الإدارة لباقي العمليات
    if user.id != ADMIN_USER_ID:
        return

    # 3. إذا أرسل المشرف نصاً وهو في حالة انتظار كتابة الكابشن يدوياً
    if context.user_data.get("awaiting_custom_caption") and msg.text:
        custom_caption = msg.text.strip()
        pending_type = context.user_data.get("temp_media_type")
        file_id = context.user_data.get("temp_file_id")
        context.user_data["awaiting_custom_caption"] = False

        status_msg = await msg.reply_text("⏳ جاري تجهيز المعاينة والتصميم بالعنوان المكتوب...")

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
            await msg.reply_photo(
                photo=poster_bytes,
                caption=f"🎙 <b>البوستر الملكي بالعنوان المطلوب:</b>\n\n{custom_caption}",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )

        elif pending_type == "video":
            cover_bytes = context.user_data.get("temp_cover_bytes")
            context.user_data["pending_pub"] = {
                "media_type": "video",
                "file_id": file_id,
                "cover_bytes": cover_bytes,
                "caption": custom_caption
            }
            keyboard = [[InlineKeyboardButton("🚀 اعتماد ونشر في القناة الآن", callback_data="publish_now")]]
            await status_msg.delete()
            await msg.reply_photo(
                photo=cover_bytes,
                caption=f"🎬 <b>معاينة الفيديو بالكابشن المطلوب:</b>\n\n{custom_caption}",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )

        elif pending_type == "pdf":
            cover_bytes = context.user_data.get("temp_cover_bytes")
            context.user_data["pending_pub"] = {
                "media_type": "pdf",
                "file_id": file_id,
                "cover_bytes": cover_bytes,
                "caption": custom_caption
            }
            keyboard = [[InlineKeyboardButton("🚀 اعتماد ونشر في القناة الآن", callback_data="publish_now")]]
            await status_msg.delete()
            await msg.reply_photo(
                photo=cover_bytes,
                caption=f"📚 <b>معاينة الكتاب بالكابشن المطلوب:</b>\n\n{custom_caption}",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )
        return

    # 4. استقبال الملف الصوتي وانتظار الكابشن
    if msg.audio or msg.voice:
        context.user_data["temp_media_type"] = "voice" if msg.voice else "audio"
        context.user_data["temp_file_id"] = (msg.voice or msg.audio).file_id
        context.user_data["awaiting_custom_caption"] = True
        await msg.reply_text("✍️ <b>تم استلام المقطع الصوتي بنجاح.</b>\n\nتفضل الآن بكتابة العنوان والكابشن المطلوب رسمه على البوستر الملكي:")
        return

    # 5. استقبال الفيديو وانتظار الكابشن
    elif msg.video:
        status_msg = await msg.reply_text("⏳ جاري التقاط الكادر السينمائي التلقائي من الفيديو...")
        try:
            tg_file = await context.bot.get_file(msg.video.file_id)
            temp_path = f"temp_vid_{msg.video.file_id[:8]}.mp4"
            await tg_file.download_to_drive(temp_path)
            frame_bytes = extract_video_frame(temp_path)
            if os.path.exists(temp_path):
                os.remove(temp_path)

            context.user_data["temp_media_type"] = "video"
            context.user_data["temp_file_id"] = msg.video.file_id
            context.user_data["temp_cover_bytes"] = frame_bytes
            context.user_data["awaiting_custom_caption"] = True

            await status_msg.delete()
            await msg.reply_text("✍️ <b>تم التقاط الكادر بنجاح.</b>\n\nتفضل الآن بكتابة الكابشن والشرح المطلوب نشره مع الفيديو:")
        except Exception as e:
            await status_msg.edit_text(f"❌ تعذر معالجة الفيديو: {e}")
        return

    # 6. استقبال كتاب PDF وانتظار الكابشن
    elif msg.document and msg.document.mime_type == "application/pdf":
        status_msg = await msg.reply_text("⏳ جاري استخراج غلاف الكتاب...")
        try:
            tg_file = await context.bot.get_file(msg.document.file_id)
            pdf_bytes = await tg_file.download_as_bytearray()
            cover_bytes = extract_pdf_cover(pdf_bytes)

            context.user_data["temp_media_type"] = "pdf"
            context.user_data["temp_file_id"] = msg.document.file_id
            context.user_data["temp_cover_bytes"] = cover_bytes
            context.user_data["awaiting_custom_caption"] = True

            await status_msg.delete()
            await msg.reply_text("✍️ <b>تم استخراج الغلاف بنجاح.</b>\n\nتفضل الآن بكتابة اسم الكتاب والشرح المطلوب نشره معه:")
        except Exception as e:
            await status_msg.edit_text(f"❌ تعذر استخراج الغلاف: {e}")
        return

# ----------------- معالجة أزرار الكولباك (Callback Queries) -----------------
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data.startswith("reply_"):
        target_id = int(query.data.split("_")[1])
        context.user_data["answering_user_id"] = target_id
        await query.message.reply_text(
            f"✍️ <b>وضع الرد السري على السائل (ID: <code>{target_id}</code>):</b>\n\nتفضل الآن بإرسال إجابتك نصياً أو سجّل مقطعاً صوتياً (🎙)",
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

        if pending["media_type"] == "pdf":
            btn_text = "📥 قراءة وتحميل الكتاب"
            start_param = f"doc_{pub_id}"
        elif pending["media_type"] in ["audio", "voice"]:
            btn_text = "🎧 استماع للمقطع الآن"
            start_param = f"listen_{pub_id}"
        else:
            btn_text = "▶️️ مشاهدة الفيديو كاملاً"
            start_param = f"watch_{pub_id}"

        channel_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton(btn_text, url=f"https://t.me/{bot_me.username}?start={start_param}")
        ]])

        try:
            await context.bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=pending["cover_bytes"],
                caption=pending["caption"],
                reply_markup=channel_markup
            )
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(f"✅ تم اعتماد المادة ونشرها في القناة بنجاح برقم تعريف: <code>{pub_id}</code>", parse_mode="HTML")
            context.user_data["pending_pub"] = None
        except Exception as e:
            await query.message.reply_text(f"❌ تعذر النشر في القناة: {e}")

# ----------------- نقطة انطلاق التطبيق الرئيسية -----------------
def main():
    # 1. تشغيل سيرفر فحص الحياة (UptimeRobot)
    server_thread = threading.Thread(target=run_http_server, daemon=True)
    server_thread.start()

    # 2. تهيئة جداول قاعدة البيانات
    init_db()

    # 3. تشغيل تطبيق التيليجرام
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_admin_media))

    logger.info("تم بدء استماع البوت رسمياً (Long Polling)...")
    app.run_polling()

if __name__ == "__main__":
    main()
