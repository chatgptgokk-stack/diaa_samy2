import os
import io
import fitz  # PyMuPDF
import cv2   # OpenCV
import sqlite3
import logging
import requests
import urllib.parse
from datetime import datetime
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler
from PIL import Image, ImageDraw, ImageFont

import arabic_reshaper

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError:
    psycopg2 = None

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
# 1. خادم ويب مصغر للحفاظ على استمرارية الخدمة
# -------------------------------------------------------------
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is Active and Ready!")

def run_web_server():
    try:
        port = int(os.environ.get("PORT", 8080))
        server = HTTPServer(("0.0.0.0", port), SimpleHandler)
        server.serve_forever()
    except Exception as e:
        logging.error(f"Web server error: {e}")

# -------------------------------------------------------------
# 2. الإعدادات وقاعدة البيانات الدائمة (PostgreSQL / SQLite)
# -------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", 
    level=logging.INFO
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID", "@diaa_samy2")
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL")
LOCAL_DB_NAME = "channel_bot_data.db"

def get_db_connection():
    if DATABASE_URL and psycopg2:
        return psycopg2.connect(DATABASE_URL)
    return sqlite3.connect(LOCAL_DB_NAME)

def init_db():
    is_pg = bool(DATABASE_URL and psycopg2)
    id_col = "id SERIAL PRIMARY KEY" if is_pg else "id INTEGER PRIMARY KEY AUTOINCREMENT"
    ts_default = "CURRENT_TIMESTAMP"

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS publications (
                {id_col},
                media_type TEXT NOT NULL,
                file_id TEXT NOT NULL,
                cover_file_id TEXT,
                caption TEXT,
                channel_msg_id INTEGER,
                created_at TIMESTAMP DEFAULT {ts_default}
            )
        """)
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS media_events (
                {id_col},
                lecture_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                user_id BIGINT NOT NULL,
                username TEXT,
                full_name TEXT,
                event_time TIMESTAMP DEFAULT {ts_default}
            )
        """)
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS comments (
                {id_col},
                lecture_id INTEGER NOT NULL,
                user_id BIGINT NOT NULL,
                username TEXT,
                full_name TEXT,
                comment_type TEXT NOT NULL,
                comment_content TEXT,
                created_at TIMESTAMP DEFAULT {ts_default}
            )
        """)
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS private_questions (
                {id_col},
                user_id BIGINT NOT NULL,
                username TEXT,
                full_name TEXT,
                question_type TEXT NOT NULL,
                question_content TEXT,
                created_at TIMESTAMP DEFAULT {ts_default}
            )
        """)
        conn.commit()

def log_event(lecture_id: int, event_type: str, user):
    is_pg = bool(DATABASE_URL and psycopg2)
    ph = "%s" if is_pg else "?"
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            INSERT INTO media_events (lecture_id, event_type, user_id, username, full_name)
            VALUES ({ph}, {ph}, {ph}, {ph}, {ph})
        """, (lecture_id, event_type, user.id, user.username or "بدون معرف", user.full_name or "مجهول"))
        conn.commit()

def save_publication(media_type: str, file_id: str, cover_file_id: str, caption: str) -> int:
    is_pg = bool(DATABASE_URL and psycopg2)
    with get_db_connection() as conn:
        cursor = conn.cursor()
        if is_pg:
            cursor.execute("""
                INSERT INTO publications (media_type, file_id, cover_file_id, caption)
                VALUES (%s, %s, %s, %s) RETURNING id
            """, (media_type, file_id, cover_file_id, caption))
            pub_id = cursor.fetchone()[0]
        else:
            cursor.execute("""
                INSERT INTO publications (media_type, file_id, cover_file_id, caption)
                VALUES (?, ?, ?, ?)
            """, (media_type, file_id, cover_file_id, caption))
            pub_id = cursor.lastrowid
        conn.commit()
        return pub_id

def update_publication_msg_id(publication_id: int, msg_id: int):
    is_pg = bool(DATABASE_URL and psycopg2)
    ph = "%s" if is_pg else "?"
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(f"UPDATE publications SET channel_msg_id = {ph} WHERE id = {ph}", (msg_id, publication_id))
        conn.commit()

def get_publication(publication_id: int):
    is_pg = bool(DATABASE_URL and psycopg2)
    ph = "%s" if is_pg else "?"
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(f"SELECT media_type, file_id, cover_file_id, caption, channel_msg_id FROM publications WHERE id = {ph}", (publication_id,))
        return cursor.fetchone()

def get_all_materials_by_type(media_type_filter: str):
    is_pg = bool(DATABASE_URL and psycopg2)
    ph = "%s" if is_pg else "?"
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT id, caption FROM publications 
            WHERE media_type LIKE {ph} 
            ORDER BY id DESC
        """, (f"%{media_type_filter}%",))
        return cursor.fetchall()

# -------------------------------------------------------------
# 3. إدارة الخط والتصميم الزخرفي الإسلامي
# -------------------------------------------------------------
FONT_FILE = "Amiri-Bold.ttf"

def ensure_font_downloaded():
    if not os.path.exists(FONT_FILE):
        try:
            url = "https://raw.githubusercontent.com/google/fonts/main/ofl/amiri/Amiri-Bold.ttf"
            resp = requests.get(url, timeout=20)
            if resp.status_code == 200:
                with open(FONT_FILE, "wb") as f:
                    f.write(resp.content)
                logging.info("تم تحميل خط Amiri-Bold بنجاح.")
        except Exception as e:
            logging.error(f"خطأ أثناء جلب الخط: {e}")

def get_font(size=64):
    if os.path.exists(FONT_FILE):
        try:
            return ImageFont.truetype(FONT_FILE, size)
        except Exception:
            pass
    return ImageFont.load_default()

def create_islamic_audio_card(title_text: str) -> io.BytesIO:
    ensure_font_downloaded()

    width, height = 1080, 1080
    bg_color = (15, 23, 42)
    gold_color = (212, 175, 55)
    gold_light = (245, 222, 130)

    img = Image.new("RGB", (width, height), color=bg_color)
    draw = ImageDraw.Draw(img)

    draw.rectangle([(25, 25), (width - 25, height - 25)], outline=gold_color, width=3)
    draw.rectangle([(42, 42), (width - 42, height - 42)], outline=gold_light, width=2)
    draw.rectangle([(58, 58), (width - 58, height - 58)], outline=gold_color, width=1)

    for cx, cy in [(58, 58), (width - 58, 58), (58, height - 58), (width - 58, height - 58)]:
        draw.line([(cx - 18, cy), (cx + 30, cy)], fill=gold_light, width=2)
        draw.line([(cx, cy - 18), (cx + 30, cy)], fill=gold_light, width=2)
        draw.rectangle([(cx - 8, cy - 8), (cx + 8, cy + 8)], outline=gold_color, width=2)

    center_x = width // 2
    draw.arc([(center_x - 45, 115), (center_x + 45, 205)], start=25, end=275, fill=gold_light, width=4)
    draw.ellipse([(center_x - 10, 149), (center_x + 10, 171)], fill=gold_color)
    draw.arc([(center_x - 70, 90), (center_x + 70, 230)], start=320, end=40, fill=gold_color, width=3)
    draw.arc([(center_x - 70, 90), (center_x + 70, 230)], start=140, end=220, fill=gold_color, width=3)

    raw_lines = [line.strip() for line in (title_text or "تسجيل صوتي مبارك").split("\n") if line.strip()]
    if not raw_lines:
        raw_lines = ["تسجيل صوتي مبارك"]

    reshaped_lines = []
    for line in raw_lines:
        try:
            reshaped_lines.append(arabic_reshaper.reshape(line))
        except Exception:
            reshaped_lines.append(line)

    max_allowed_width = 940
    max_allowed_height = 540

    font_size = 62 if len(reshaped_lines) == 1 else (52 if len(reshaped_lines) == 2 else 44)
    min_font_size = 24

    while font_size > min_font_size:
        test_font = get_font(font_size)
        fits_width = True
        for line in reshaped_lines:
            bbox = draw.textbbox((0, 0), line, font=test_font)
            line_w = bbox[2] - bbox[0]
            if line_w > max_allowed_width:
                fits_width = False
                break
        
        line_h = int(font_size * 1.5)
        total_h = len(reshaped_lines) * line_h
        fits_height = (total_h <= max_allowed_height)

        if fits_width and fits_height:
            break
        font_size -= 2

    chosen_font = get_font(font_size)
    line_height = int(font_size * 1.52)
    total_text_height = len(reshaped_lines) * line_height

    center_y = 560
    start_y = center_y - (total_text_height // 2) + (line_height // 2)

    top_separator_y = max(260, start_y - (line_height // 2) - 45)
    bottom_separator_y = min(860, start_y + total_text_height - (line_height // 2) + 45)

    draw.line([(95, top_separator_y), (width - 95, top_separator_y)], fill=gold_color, width=3)
    draw.ellipse([(center_x - 8, top_separator_y - 8), (center_x + 8, top_separator_y + 8)], fill=gold_light)

    for i, line in enumerate(reshaped_lines):
        cur_y = start_y + (i * line_height)
        draw.text((center_x, cur_y), line, fill=gold_light, font=chosen_font, anchor="mm")

    draw.line([(95, bottom_separator_y), (width - 95, bottom_separator_y)], fill=gold_color, width=3)
    draw.ellipse([(center_x - 8, bottom_separator_y - 8), (center_x + 8, bottom_separator_y + 8)], fill=gold_light)

    output = io.BytesIO()
    img.save(output, format="JPEG", quality=95)
    output.seek(0)
    return output

def extract_video_frame(video_path: str) -> io.BytesIO:
    try:
        cap = cv2.VideoCapture(video_path)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        target_frame = max(0, int(total_frames * 0.15))
        cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame)
        success, frame = cap.read()
        if not success:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            success, frame = cap.read()
        cap.release()

        if success:
            is_success, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
            if is_success:
                return io.BytesIO(buffer.tobytes())
    except Exception as e:
        logging.error(f"Error frame: {e}")
    return None

# -------------------------------------------------------------
# 4. لوحات الأزرار
# -------------------------------------------------------------
def build_channel_control_panel(bot_uname: str):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎙 الخطب والمحاضرات", url=f"https://t.me/{bot_uname}?start=cat_audio"),
            InlineKeyboardButton("🎬 الفيديوهات المرئية", url=f"https://t.me/{bot_uname}?start=cat_video")
        ],
        [
            InlineKeyboardButton("📚 الكتب والرسائل", url=f"https://t.me/{bot_uname}?start=cat_pdf"),
            InlineKeyboardButton("🔍 البحث في المحتوى", url=f"https://t.me/{bot_uname}?start=act_search")
        ],
        [
            InlineKeyboardButton("📩 إسأل الشيخ (استشارة خاصة وسرية)", url=f"https://t.me/{bot_uname}?start=ask_sheikh")
        ]
    ])

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
                "👋 **مرحباً بك يا مدير القناة في لوحة التحكم الإدارية:**\n\n"
                "• لتحديث لوحة الأزرار في القناة: أرسل الأمر `/menu`.\n"
                "• **إرسال فيديو:** كادر سينمائي واعتماد الكابشن.\n"
                "• **إرسال صوت:** تصميم إسلامي ملكي بالعنوان المطلوب.\n"
                "• **إرسال صورة:** مسودة ومعاينة للبوسترات قبل النشر.\n"
                "• **إرسال PDF:** استخراج فوري وحتمي للصفحة الأولى كغلاف."
            )
            await update.message.reply_text(msg, parse_mode="Markdown")
        else:
            await update.message.reply_text(f"مرحباً بك يا {user.first_name} في بوت الخدمة والتواصل.")
        return

    payload = args[0]

    if payload == "ask_sheikh":
        context.user_data["action"] = "awaiting_private_question"
        await update.message.reply_text(
            "🔒 **مرحباً بك في خدمة (إسأل الشيخ) الخاصة:**\n\n"
            "هذه المحادثة سرية ومشفرة بالكامل ولا يطّلع عليها أحد.\n"
            "تفضل بكتابة سؤالك الآن نصياً أو أرسله كتسجيل صوتي (🎙):",
            parse_mode="Markdown"
        )
        return

    elif payload.startswith("cat_"):
        c_type = payload.replace("cat_", "")
        type_names = {"audio": "الخطب والمحاضرات الصوتية", "video": "المقاطع المرئية (الفيديو)", "pdf": "الكتب والمستندات (PDF)"}
        materials = get_all_materials_by_type(c_type)
        
        if not materials:
            await update.message.reply_text(f"لا توجد مواد منشورة حالياً في قسم {type_names.get(c_type, '')}.")
            return

        kb = []
        action_prefix = "watch" if c_type == "video" else ("listen" if c_type == "audio" else "doc")
        for m_id, cap in materials:
            clean_title = (cap or f"المادة رقم {m_id}").split("\n")[0][:45]
            kb.append([InlineKeyboardButton(f"🔹 {clean_title}", callback_data=f"get_{action_prefix}_{m_id}")])

        await update.message.reply_text(
            f"📂 **جميع مواد قسم ({type_names.get(c_type, '')}):**\n"
            f"عدد المواد المتاحة: ({len(materials)})\n"
            f"اضغط على أي مادة لاستلامها في الخاص فوراً:",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
        return

    elif payload == "act_search":
        context.user_data["action"] = "awaiting_search_query"
        await update.message.reply_text(
            "🔍 **البحث في جميع محتويات القناة:**\n\n"
            "أرسل الآن كلمة البحث للبحث في عناوين الخطب، الفيديوهات، والكتب وسأوافيك بالنتائج فوراً:"
        )
        return

    elif payload.startswith("watch_"):
        lecture_id = int(payload.split("_")[1])
        pub = get_publication(lecture_id)
        if not pub:
            await update.message.reply_text("عذراً، هذا المقطع غير متاح.")
            return

        log_event(lecture_id, "watch_video", user)
        if user.id != ADMIN_USER_ID:
            await context.bot.send_message(
                chat_id=ADMIN_USER_ID,
                text=f"🔒 **إشعار مشاهدة فيديو (سري)**\n• المنشور: `{lecture_id}`\n• المتابع: {user.full_name} (@{user.username or 'بدون'}) | ID: `{user.id}`",
                parse_mode="Markdown"
            )
        if pub[0] == "doc_video":
            await update.message.reply_document(document=pub[1], caption=pub[3] or "", parse_mode="HTML")
        else:
            await update.message.reply_video(video=pub[1], caption=pub[3] or "", parse_mode="HTML")

    elif payload.startswith("listen_"):
        lecture_id = int(payload.split("_")[1])
        pub = get_publication(lecture_id)
        if not pub:
            await update.message.reply_text("عذراً، هذا التسجيل غير متاح.")
            return

        log_event(lecture_id, "listen_audio", user)
        if user.id != ADMIN_USER_ID:
            await context.bot.send_message(
                chat_id=ADMIN_USER_ID,
                text=f"🔒 **إشعار استماع لخطبة (سري)**\n• المنشور: `{lecture_id}`\n• المتابع: {user.full_name} (@{user.username or 'بدون'}) | ID: `{user.id}`",
                parse_mode="Markdown"
            )
        if pub[0] == "voice":
            await update.message.reply_voice(voice=pub[1], caption=pub[3] or "")
        else:
            await update.message.reply_audio(audio=pub[1], caption=pub[3] or "", parse_mode="HTML")

    elif payload.startswith("doc_"):
        lecture_id = int(payload.split("_")[1])
        pub = get_publication(lecture_id)
        if not pub:
            await update.message.reply_text("عذراً، الملف غير متوفر.")
            return

        log_event(lecture_id, "download_doc", user)
        if user.id != ADMIN_USER_ID:
            await context.bot.send_message(
                chat_id=ADMIN_USER_ID,
                text=f"🔒 **إشعار فتح / تحميل كتاب (سري)**\n• المنشور: `{lecture_id}`\n• المتابع: {user.full_name} (@{user.username or 'بدون'}) | ID: `{user.id}`",
                parse_mode="Markdown"
            )
        await update.message.reply_document(document=pub[1], caption=pub[3] or "", parse_mode="HTML")

    elif payload.startswith("view_"):
        lecture_id = int(payload.split("_")[1])
        pub = get_publication(lecture_id)
        if not pub:
            await update.message.reply_text("عذراً، الصورة غير متوفرة.")
            return

        log_event(lecture_id, "view_photo", user)
        if user.id != ADMIN_USER_ID:
            await context.bot.send_message(
                chat_id=ADMIN_USER_ID,
                text=f"🔒 **إشعار استعراض صورة (سري)**\n• المنشور: `{lecture_id}`\n• المتابع: {user.full_name} (@{user.username or 'بدون'}) | ID: `{user.id}`",
                parse_mode="Markdown"
            )
        await update.message.reply_photo(photo=pub[1], caption=pub[3] or "", parse_mode="HTML")

    elif payload.startswith("comment_"):
        lecture_id = int(payload.split("_")[1])
        context.user_data["action"] = "comment"
        context.user_data["lecture_id"] = lecture_id
        await update.message.reply_text(
            f"💬 **تسجيل تعليق على المنشور رقم `{lecture_id}`:**\nتفضل بإرسال تعليقك الآن (نصياً أو تسجيلاً صوتياً 🎙):",
            parse_mode="Markdown"
        )

# -------------------------------------------------------------
# 6. أمر إرسال لوحة التحكم والتنقل إلى القناة (`/menu`)
# -------------------------------------------------------------
async def post_menu_to_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        return

    bot_info = await context.bot.get_me()
    panel_markup = build_channel_control_panel(bot_info.username)

    menu_text = (
        "🌿 **مرحباً بكم في منصة القناة الرسمية** 🌿\n\n"
        "يمكنكم عبر اللوحة التفاعلية أدناه تصفح كافة الفوائد والمحتويات، "
        "أو التواصل وإرسال استشاراتكم وأسئلتكم الخاصة في سرية تامة:"
    )

    await context.bot.send_message(
        chat_id=CHANNEL_ID,
        text=menu_text,
        reply_markup=panel_markup,
        parse_mode="Markdown"
    )
    await update.message.reply_text("✅ تم تحديث ونشر لوحة الأزرار في القناة بنجاح! يمكنك تثبيتها (Pin) في أعلى القناة الآن.")

# -------------------------------------------------------------
# 7. استخراج غلاف الصفحة الأولى من PDF بشكل قاطع
# -------------------------------------------------------------
def render_pdf_first_page(file_path: str) -> io.BytesIO:
    """استخراج الصفحة الأولى كصورة عالية الجودة بشكل قطعي"""
    doc = fitz.open(file_path)
    if len(doc) > 0:
        page = doc[0]
        # مصفوفة تكبير 2x لدقة ووضوح فائق
        zoom = 2.0
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        img_bytes = pix.tobytes("jpeg")
        doc.close()
        return io.BytesIO(img_bytes)
    doc.close()
    return None

# -------------------------------------------------------------
# 8. تجهيز الوسائط والمسودات التحريرية
# -------------------------------------------------------------
async def handle_admin_media_preparation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        return

    msg = update.message
    bot_msg = await msg.reply_text("⏳ جاري سحب الملف واستخراج الصفحة الأولى كغلاف...")

    # الصور والبوسترات
    if msg.photo:
        file_obj = msg.photo[-1]
        context.user_data["draft"] = {
            "media_type": "photo",
            "file_id": file_obj.file_id,
            "caption": msg.caption or ""
        }
        context.user_data["action"] = "awaiting_caption"

        await bot_msg.delete()
        confirm_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("🚀 اعتماد ونشر في القناة الآن", callback_data="confirm_publish")],
            [InlineKeyboardButton("❌ إلغاء", callback_data="cancel_publish")]
        ])
        await msg.reply_photo(
            photo=file_obj.file_id,
            caption="🖼 **تم استلام الصورة وتجهيز المسودة!**\n\n"
                    "✏️ **أرسل الآن نص الكابشن** المطلوب إدراجه بالقناة،\n"
                    "أو اضغط زر الاعتماد أدناه للنشر بالوصف الحالي:",
            reply_markup=confirm_markup
        )
        return

    # مقاطع الفيديو
    is_video_doc = (
        msg.document and (
            "video" in (msg.document.mime_type or "").lower() or
            (msg.document.file_name or "").lower().endswith((".mp4", ".mkv", ".mov", ".avi"))
        )
    )
    if msg.video or is_video_doc:
        file_obj = msg.video or msg.document
        m_type = "video" if msg.video else "doc_video"

        frame_bytes = None
        try:
            temp_vpath = f"temp_{file_obj.file_unique_id}.mp4"
            tg_file = await context.bot.get_file(file_obj.file_id)
            await tg_file.download_to_drive(temp_vpath)
            frame_bytes = extract_video_frame(temp_vpath)
            if os.path.exists(temp_vpath):
                os.remove(temp_vpath)
        except Exception as e:
            logging.error(f"Video thumb error: {e}")

        context.user_data["draft"] = {
            "media_type": m_type,
            "file_id": file_obj.file_id,
            "cover_bytes": frame_bytes.getvalue() if frame_bytes else None,
            "caption": msg.caption or "مقطع مرئي مميز"
        }
        context.user_data["action"] = "awaiting_caption"
        await bot_msg.delete()

        confirm_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("🚀 اعتماد ونشر في القناة الآن", callback_data="confirm_publish")],
            [InlineKeyboardButton("❌ إلغاء", callback_data="cancel_publish")]
        ])

        if frame_bytes:
            frame_bytes.seek(0)
            await msg.reply_photo(
                photo=frame_bytes,
                caption="🎬 **تم استخراج كادر الفيديو بنجاح!**\n\n✏️ أرسل الكابشن الآن أو اضغط اعتماد:",
                reply_markup=confirm_markup
            )
        else:
            await msg.reply_text("🎬 تم استلام الفيديو! أرسل الكابشن أو اضغط اعتماد:", reply_markup=confirm_markup)
        return

    # المقاطع الصوتية
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
            "🎧 **تم استلام المقطع الصوتي!**\n\n"
            "✍️ أرسل الآن **العنوان** الذي ترغب في وضعه داخل التصميم الزخرفي الإسلامي وفي الكابشن:"
        )
        return

    # ملفات PDF والمستندات (استخراج حتمي للصفحة الأولى كغلاف)
    elif msg.document:
        doc = msg.document
        doc_name = doc.file_name or "كتاب ومستند علمي"
        clean_title = doc_name.replace(".pdf", "").replace("_", " ")

        temp_pdf_path = f"temp_{doc.file_unique_id}.pdf"
        cover_stream = None

        try:
            tg_file = await context.bot.get_file(doc.file_id)
            await tg_file.download_to_drive(temp_pdf_path)
            cover_stream = render_pdf_first_page(temp_pdf_path)
        except Exception as e:
            logging.error(f"فشل استخراج صفحة الـ PDF: {e}")
        finally:
            if os.path.exists(temp_pdf_path):
                os.remove(temp_pdf_path)

        context.user_data["draft"] = {
            "media_type": "pdf",
            "file_id": doc.file_id,
            "cover_bytes": cover_stream.getvalue() if cover_stream else None,
            "caption": msg.caption or f"📚 {clean_title}"
        }
        context.user_data["action"] = "awaiting_caption"
        await bot_msg.delete()

        confirm_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("🚀 اعتماد ونشر في القناة الآن", callback_data="confirm_publish")],
            [InlineKeyboardButton("❌ إلغاء", callback_data="cancel_publish")]
        ])

        if cover_stream:
            cover_stream.seek(0)
            await msg.reply_photo(
                photo=cover_stream,
                caption=f"📖 **تم استخراج الصفحة الأولى من الملف كغلاف بنجاح قاطع!**\n\n"
                        f"• **الكابشن المقترح:**\n{context.user_data['draft']['caption']}\n\n"
                        f"✏️ أرسل كابشن جديد للتعديل، أو اضغط **اعتماد ونشر في القناة الآن**:",
                reply_markup=confirm_markup
            )
        else:
            await msg.reply_text(
                f"📚 **تم حفظ ملف الـ PDF!**\n\n"
                f"• الكابشن المقترح: {clean_title}\n\n"
                f"أرسل الكابشن المطلوب أو اضغط اعتماد:",
                reply_markup=confirm_markup
            )

# -------------------------------------------------------------
# 9. التفاعل والأسئلة السرية والبحث
# -------------------------------------------------------------
async def handle_user_interactions_and_inputs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    action = context.user_data.get("action")
    is_pg = bool(DATABASE_URL and psycopg2)
    ph = "%s" if is_pg else "?"

    if user.id == ADMIN_USER_ID and action == "answering_member_question":
        target_uid = context.user_data.get("target_user_id")
        target_uname = context.user_data.get("target_user_name", "المتابع")

        try:
            if update.message.text:
                await context.bot.send_message(
                    chat_id=target_uid,
                    text=f"🔒 **إجابة واستشارة خاصة من الشيخ:**\n\n{update.message.text}",
                    parse_mode="Markdown"
                )
            elif update.message.voice:
                await context.bot.send_voice(
                    chat_id=target_uid,
                    voice=update.message.voice.file_id,
                    caption="🔒 تسجيل صوتي خاص من الشيخ رداً على استشارتك."
                )

            await update.message.reply_text(
                f"✅ **تم إرسال الإجابة إلى ({target_uname}) في سرية تامة ومطلقة.**",
                parse_mode="Markdown"
            )
        except Exception as e:
            await update.message.reply_text(f"⚠️ تعذر تسليم الرد للمستخدم: {e}")

        context.user_data.clear()
        return

    if action == "awaiting_private_question":
        q_content = update.message.text or (update.message.voice.file_id if update.message.voice else "")
        q_type = "voice" if update.message.voice else "text"

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"""
                INSERT INTO private_questions (user_id, username, full_name, question_type, question_content)
                VALUES ({ph}, {ph}, {ph}, {ph}, {ph})
            """, (user.id, user.username or "بدون معرف", user.full_name or "مجهول", q_type, q_content))
            conn.commit()

        reply_member_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("✍️ الإجابة على سؤال العضو", callback_data=f"answer_user_{user.id}")]
        ])

        await context.bot.send_message(
            chat_id=ADMIN_USER_ID,
            text=f"❓ **سؤال واستشارة خاصة واردة للشيخ (سري جداً)**\n"
                 f"• من: {user.full_name} (@{user.username or 'بدون'})\n"
                 f"• المعرف (ID): `{user.id}`\n"
                 f"• النوع: `{q_type}`",
            parse_mode="Markdown"
        )
        if q_type == "text":
            await context.bot.send_message(
                chat_id=ADMIN_USER_ID, 
                text=f"📝 **نص السؤال:**\n{q_content}",
                reply_markup=reply_member_markup
            )
        else:
            await context.bot.send_voice(
                chat_id=ADMIN_USER_ID, 
                voice=q_content,
                caption="🎙 تسجيل صوتي من السائل.",
                reply_markup=reply_member_markup
            )

        context.user_data.clear()
        await update.message.reply_text("✅ تم استلام سؤالك في سرية تامة، وسيقوم الشيخ بالاطلاع عليه والإجابة عن استشارتك قريباً بإذن الله.")
        return

    elif action == "comment":
        lecture_id = context.user_data.get("lecture_id")
        c_content = update.message.text or (update.message.voice.file_id if update.message.voice else "")
        c_type = "voice" if update.message.voice else "text"

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"""
                INSERT INTO comments (lecture_id, user_id, username, full_name, comment_type, comment_content)
                VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph})
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
        await update.message.reply_text("✅ تم استلام تعليقك بنجاح.")
        return

    elif action == "awaiting_search_query":
        query = update.message.text
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"SELECT id, caption, media_type FROM publications WHERE caption LIKE {ph} ORDER BY id DESC", (f"%{query}%",))
            results = cursor.fetchall()

        if not results:
            await update.message.reply_text(f"لم يتم العثور على أي نتائج مطابقة لـ: `{query}`.", parse_mode="Markdown")
        else:
            kb = []
            for r_id, r_cap, r_type in results:
                title = (r_cap or f"المادة {r_id}").split("\n")[0][:45]
                action_pfx = "watch" if "video" in r_type else ("listen" if "audio" in r_type or r_type == "voice" else ("doc" if r_type == "pdf" else "view"))
                kb.append([InlineKeyboardButton(f"🔗 {title}", callback_data=f"get_{action_pfx}_{r_id}")])

            await update.message.reply_text(
                f"🔎 **نتائج البحث عن:** `{query}` (عدد النتائج: {len(results)})\nاضغط على أي مادة لاستلامها فوراً:",
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode="Markdown"
            )
        context.user_data.clear()
        return

    if user.id == ADMIN_USER_ID and "draft" in context.user_data:
        draft = context.user_data["draft"]
        draft_action = context.user_data.get("action")

        if draft_action == "awaiting_audio_title":
            title = update.message.text
            draft["caption"] = title
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
                        f"اضغط على الزر أدناه لإطلاق المنشور في القناة:",
                reply_markup=confirm_markup,
                parse_mode="Markdown"
            )
            return

        elif draft_action == "awaiting_caption":
            text = update.message.text
            if text.strip() != "اعتماد":
                draft["caption"] = text

            confirm_markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("🚀 اعتماد ونشر في القناة الآن", callback_data="confirm_publish")],
                [InlineKeyboardButton("❌ إلغاء", callback_data="cancel_publish")]
            ])

            if draft.get("media_type") == "photo":
                await update.message.reply_photo(
                    photo=draft["file_id"],
                    caption=f"📋 **معاينة المنشور النهائي للبوستر:**\n\n{draft['caption']}\n\nهل تعتمد النشر في القناة الآن؟",
                    reply_markup=confirm_markup
                )
            elif draft.get("cover_bytes"):
                await update.message.reply_photo(
                    photo=io.BytesIO(draft["cover_bytes"]),
                    caption=f"📋 **معاينة غلاف المنشور:**\n\n{draft['caption']}\n\nهل تعتمد النشر في القناة؟",
                    reply_markup=confirm_markup
                )
            else:
                await update.message.reply_text(
                    f"📋 **معاينة النص للمنشور:**\n\n{draft['caption']}",
                    reply_markup=confirm_markup
                )

# -------------------------------------------------------------
# 10. أزرار القوائم والردود
# -------------------------------------------------------------
async def handle_callback_queries(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("answer_user_"):
        target_uid = int(data.replace("answer_user_", ""))
        context.user_data["action"] = "answering_member_question"
        context.user_data["target_user_id"] = target_uid
        context.user_data["target_user_name"] = f"المعرف {target_uid}"

        cancel_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ إلغاء الإجابة", callback_data="cancel_answer")]
        ])

        await query.message.reply_text(
            f"✍️ **وضع الرد السري على السائل (ID: `{target_uid}`):**\n\n"
            f"تفضل الآن بإرسال إجابتك **نصياً أو سجّل مقطعاً صوتياً (🎙)**.\n"
            f"سيقوم البوت بنقلها إليه فوراً في سرية تامة.",
            reply_markup=cancel_kb,
            parse_mode="Markdown"
        )
        return

    if data == "cancel_answer":
        context.user_data.clear()
        await query.edit_message_text("❌ تم إلغاء وضع الرد.")
        return

    if data == "cancel_publish":
        context.user_data.clear()
        try:
            await query.edit_message_caption("❌ تم إلغاء المسودة.")
        except Exception:
            await query.edit_message_text("❌ تم إلغاء المسودة.")
        return

    if data.startswith("get_"):
        parts = data.split("_")
        action_pfx = parts[1]
        lecture_id = int(parts[2])
        pub = get_publication(lecture_id)
        if not pub:
            await query.message.reply_text("عذراً، المادة غير متاحة.")
            return

        if action_pfx == "watch":
            if pub[0] == "doc_video":
                await query.message.reply_document(document=pub[1], caption=pub[3] or "", parse_mode="HTML")
            else:
                await query.message.reply_video(video=pub[1], caption=pub[3] or "", parse_mode="HTML")
        elif action_pfx == "listen":
            if pub[0] == "voice":
                await query.message.reply_voice(voice=pub[1], caption=pub[3] or "")
            else:
                await query.message.reply_audio(audio=pub[1], caption=pub[3] or "", parse_mode="HTML")
        elif action_pfx == "doc":
            await query.message.reply_document(document=pub[1], caption=pub[3] or "", parse_mode="HTML")
        elif action_pfx == "view":
            await query.message.reply_photo(photo=pub[1], caption=pub[3] or "", parse_mode="HTML")
        return

    if data == "confirm_publish":
        draft = context.user_data.get("draft")
        if not draft:
            try:
                await query.edit_message_caption("⚠️ انتهت المسودة.")
            except Exception:
                await query.edit_message_text("⚠️ انتهت المسودة.")
            return

        bot_info = await context.bot.get_me()
        bot_uname = bot_info.username

        media_type = draft["media_type"]
        file_id = draft["file_id"]
        caption = draft["caption"]
        cover_bytes = draft.get("cover_bytes")

        pub_id = save_publication(media_type, file_id, "", caption)

        if "video" in media_type:
            btn_text = "🎬 مشاهدة الفيديو"
            action_pfx = "watch"
        elif "audio" in media_type or media_type == "voice":
            btn_text = "🎧 استمع للخطبة"
            action_pfx = "listen"
        elif media_type == "photo":
            btn_text = "🖼 عرض الصورة الأصلية"
            action_pfx = "view"
        else:
            btn_text = "📥 فتح / تحميل الكتاب"
            action_pfx = "doc"

        reply_markup = build_custom_keyboard(bot_uname, pub_id, btn_text, action_pfx)

        if media_type == "photo":
            sent_msg = await context.bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=file_id,
                caption=caption,
                reply_markup=reply_markup,
                parse_mode="HTML"
            )
        elif cover_bytes:
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

        update_publication_msg_id(pub_id, sent_msg.message_id)
        updated_markup = build_custom_keyboard(bot_uname, pub_id, btn_text, action_pfx, sent_msg.message_id)
        await sent_msg.edit_reply_markup(reply_markup=updated_markup)

        context.user_data.clear()
        try:
            await query.edit_message_caption(f"✅ **تم النشر في القناة بنجاح (المعرف: `{pub_id}`).**")
        except Exception:
            await query.edit_message_text(f"✅ **تم النشر في القناة بنجاح (المعرف: `{pub_id}`).**")

# -------------------------------------------------------------
# 11. تشغيل التطبيق
# -------------------------------------------------------------
def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN غير مضبوط!")

    init_db()
    ensure_font_downloaded()

    web_thread = Thread(target=run_web_server, daemon=True)
    web_thread.start()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("menu", post_menu_to_channel))
    app.add_handler(CallbackQueryHandler(handle_callback_queries))

    admin_media_filter = filters.Chat(ADMIN_USER_ID) & (
        filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.VOICE | filters.Document.ALL
    )
    app.add_handler(MessageHandler(admin_media_filter, handle_admin_media_preparation))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, handle_user_interactions_and_inputs))

    app.run_polling()

if __name__ == "__main__":
    main()
