import os
import asyncio
import logging
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# 1. خادم ويب وهمي بسيط لتجاوز فحص Render المجاني
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive and running!")

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    server.serve_forever()

# 2. إعدادات التيليجرام
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", 
    level=logging.INFO
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID", "@diaa_samy2")
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        return
    welcome_text = (
        "مرحباً بك في لوحة تحكم القناة:\n\n"
        "1. `/post [النص]` - لنشر رسالة فورية مع زر رابط القناة.\n"
        "2. `/schedule [ثواني] [النص]` - لجدولة رسالة بعد ثوانٍ."
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")

async def post_to_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        return
    text_content = " ".join(context.args)
    if not text_content:
        await update.message.reply_text("يرجى كتابة الرسالة بعد الأمر، مثال:\n`/post أهلاً بكم`", parse_mode="Markdown")
        return
    keyboard = [
        [InlineKeyboardButton("زيارة القناة", url=f"https://t.me/{CHANNEL_ID.replace('@', '')}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    try:
        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=text_content,
            reply_markup=reply_markup,
            parse_mode="HTML"
        )
        await update.message.reply_text("تم نشر الرسالة في القناة بنجاح.")
    except Exception as e:
        await update.message.reply_text(f"فشل النشر: {e}")

async def send_scheduled_message(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data
    await context.bot.send_message(
        chat_id=CHANNEL_ID,
        text=job_data["text"],
        parse_mode="HTML"
    )

async def schedule_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        return
    if len(context.args) < 2:
        await update.message.reply_text("الاستخدام: `/schedule 60 نص الرسالة`", parse_mode="Markdown")
        return
    try:
        delay = float(context.args[0])
        message_to_send = " ".join(context.args[1:])
        context.job_queue.run_once(
            send_scheduled_message, 
            when=delay, 
            data={"text": message_to_send}
        )
        await update.message.reply_text(f"تمت جدولة المنشور بنجاح ليُنشر بعد {delay} ثانية.")
    except ValueError:
        await update.message.reply_text("يرجى إدخال عدد ثوانٍ صحيح.")

def main():
    if not BOT_TOKEN:
        raise ValueError("خطأ: لم يتم ضبط BOT_TOKEN!")
    
    # تشغيل خادم الويب في خلفية مستقلة (Thread)
    web_thread = Thread(target=run_web_server, daemon=True)
    web_thread.start()

    # تشغيل البوت
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("post", post_to_channel))
    app.add_handler(CommandHandler("schedule", schedule_post))
    
    print("البوت يعمل الآن بنجاح على الخطة المجانية...")
    app.run_polling()

if __name__ == "__main__":
    main()
