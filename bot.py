import os
import sys
import io
import html
import logging
import threading
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
    InputMediaPhoto,
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

# ----------------- وظائف معالجة الوسائط وتوليد الأغلفة -----------------
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
    width, height = 1080, 1080
    image = Image.new("RGB", (width, height), color=(18, 30, 49))
    draw = ImageDraw.Draw(image)

    # إطار إسلامي ملكي مزدوج
    draw.rectangle([40, 40, width - 40, height - 40], outline=(212, 175, 55), width=8)
    draw.rectangle([60, 60, width - 60, height - 60], outline=(160, 130, 40), width=3)

    # معالجة النص العربي للرسم ثنائي الاتجاه
    reshaped_text = arabic_reshaper.reshape(title_text)
    bidi_text = get_display(reshaped_text)

    # محاولة جلب الخطوط القياسية الكبيرة المدعومة في سيرفرات لينكس
    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "arial.ttf"
    ]
    
    font = None
    footer_font = None
    for path in font_paths:
        if os.path.exists(path):
            try:
                font = ImageFont.truetype(path, 54)
                footer_font = ImageFont.truetype(path, 38)
                break
            except Exception:
                continue

    if font is None:
        try:
            font = ImageFont.load_default(size=48)
            footer_font = ImageFont.load_default(size=34)
        except Exception:
            font = ImageFont.load_default()
            footer_font = font

    # كتابة العنوان الرئيسي في منتصف البوستر
    draw.text((width // 2, height // 2), bidi_text, fill=(245, 245, 245), font=font, anchor="mm")

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
                "• إرسال فيديو: كادر سينمائي واعتماد الكابشن.\n"
                "• إرسال صوت: تصميم إسلامي ملكي بالعنوان المطلوب.\n"
                "• إرسال صورة: مسودة ومعاينة للبوسترات قبل النشر.\n"
                "• إرسال PDF: استخراج فوري للغلاف وزر التحميل المباشر."
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

    # استلام أسئلة الأعضاء الموجهة للشيخ
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

    # إرسال إجابة الشيخ إلى السائل
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

    # 1. إرسال كتاب PDF
    if msg.document and msg.document.mime_type == "application/pdf":
        status_msg = await msg.reply_text("⏳ جاري استخراج غلاف الكتاب بدقة عالية...")
        try:
            tg_file = await context.bot.get_file(msg.document.file_id)
            pdf_bytes = await tg_file.download_as_bytearray()
            cover_bytes = extract_pdf_cover(pdf_bytes)

            context.user_data["pending_pub"] = {
                "media_type": "pdf",
                "file_id": msg.document.file_id,
                "cover_bytes": cover_bytes,
                "caption": msg.caption or msg.document.file_name or "كتاب إلكتروني جديد"
            }

            keyboard = [[InlineKeyboardButton("🚀 اعتماد ونشر في القناة الآن", callback_data="publish_now")]]
            await status_msg.delete()
            await msg.reply_photo(
                photo=cover_bytes,
                caption=f"📚 <b>معاينة غلاف الكتاب:</b>\n\n{context.user_data['pending_pub']['caption']}",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )
        except Exception as e:
            await status_msg.edit_text(f"❌ تعذر استخراج الغلاف: {e}")

    # 2. إرسال مقطع فيديو
    elif msg.video:
        status_msg = await msg.reply_text("⏳ جاري التقاط الكادر السينمائي التلقائي من الفيديو...")
        try:
            tg_file = await context.bot.get_file(msg.video.file_id)
            temp_path = f"temp_vid_{msg.video.file_id[:8]}.mp4"
            await tg_file.download_to_drive(temp_path)
            frame_bytes = extract_video_frame(temp_path)
            if os.path.exists(temp_path):
                os.remove(temp_path)

            if not frame_bytes:
                await status_msg.edit_text("❌ تعذر التقاط الكادر من الفيديو.")
                return

            context.user_data["pending_pub"] = {
                "media_type": "video",
                "file_id": msg.video.file_id,
                "cover_bytes": frame_bytes,
                "caption": msg.caption or "مقطع مرئي جديد"
            }

            keyboard = [[InlineKeyboardButton("🚀 اعتماد ونشر في القناة الآن", callback_data="publish_now")]]
            await status_msg.delete()
            await msg.reply_photo(
                photo=frame_bytes,
                caption=f"🎬 <b>الكادر الملتقط للمعاينة:</b>\n\n{context.user_data['pending_pub']['caption']}",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )
        except Exception as e:
            await status_msg.edit_text(f"❌ تعذر معالجة الفيديو: {e}")

    # 3. إرسال مقطع صوتي (Audio / Voice)
    elif msg.audio or msg.voice:
        # استخراج العنوان الذكي (كابشن، ثم عنوان المقطع، ثم اسم الملف مع تنظيف الامتداد)
        title = msg.caption
        if not title and msg.audio:
            title = msg.audio.title
            if not title and msg.audio.file_name:
                title = os.path.splitext(msg.audio.file_name)[0].replace("_", " ")

        if not title:
            title = "خطبة ومحاضرة صوتية"

        status_msg = await msg.reply_text("⏳ جاري توليد وتصميم البوستر الملكي الصوتي...")
        try:
            poster_bytes = create_audio_poster(title)
            context.user_data["pending_pub"] = {
                "media_type": "voice" if msg.voice else "audio",
                "file_id": (msg.voice or msg.audio).file_id,
                "cover_bytes": poster_bytes,
                "caption": title
            }

            keyboard = [[InlineKeyboardButton("🚀 اعتماد ونشر في القناة الآن", callback_data="publish_now")]]
            await status_msg.delete()
            await msg.reply_photo(
                photo=poster_bytes,
                caption=f"🎙 <b>البوستر الملكي التلقائي:</b>\n\nالعنوان: {title}",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )
        except Exception as e:
            await status_msg.edit_text(f"❌ تعذر توليد تصميم الصوت: {e}")

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
            btn_text = "▶️ مشاهدة الفيديو كاملاً"
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
    # تشغيل خادم المراقبة لـ UptimeRobot في الخلفية
    server_thread = threading.Thread(target=run_http_server, daemon=True)
    server_thread.start()

    # تهيئة جداول Supabase PostgreSQL
    init_db()

    # تشغيل محرك تيليجرام
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_admin_media))

    logger.info("تم بدء استماع البوت رسمياً (Long Polling)...")
    app.run_polling()

if __name__ == "__main__":
    main()
