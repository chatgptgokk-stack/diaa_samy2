import arabic_reshaper
from bidi.algorithm import get_display
import requests

def get_arabic_font(size=48):
    """تحميل خط عربي أصيل (Amiri) تلقائياً واستخدامه في التصميم"""
    font_path = "Amiri-Bold.ttf"
    if not os.path.exists(font_path):
        try:
            url = "https://raw.githubusercontent.com/google/fonts/main/ofl/amiri/Amiri-Bold.ttf"
            r = requests.get(url, timeout=15)
            with open(font_path, "wb") as f:
                f.write(r.content)
        except Exception:
            pass
    if os.path.exists(font_path):
        return ImageFont.truetype(font_path, size)
    return ImageFont.load_default()

def create_islamic_audio_card(title_text: str) -> io.BytesIO:
    """توليد بطاقة إسلامية ملكية بزخارف ثرية وكتابة عربية أصيلة مشكّلة."""
    width, height = 1080, 1080
    bg_color = (15, 23, 42)       # كحلي لؤلؤي ملكي غامق
    gold_color = (212, 175, 55)   # ذهبي ملكي
    gold_light = (245, 222, 130)  # لمعان ذهبي فاتح

    img = Image.new("RGB", (width, height), color=bg_color)
    draw = ImageDraw.Draw(img)

    # 1. إطارات هندسية متعددة
    draw.rectangle([(35, 35), (width - 35, height - 35)], outline=gold_color, width=4)
    draw.rectangle([(55, 55), (width - 55, height - 55)], outline=gold_light, width=2)
    draw.rectangle([(75, 75), (width - 75, height - 75)], outline=gold_color, width=1)

    # 2. زخارف الأركان الإسلامية الأربعة
    corner_len = 70
    for cx, cy in [(75, 75), (width - 75, 75), (75, height - 75), (width - 75, height - 75)]:
        # زوايا هندسية متداخلة
        draw.line([(cx - 15, cy), (cx + 35, cy)], fill=gold_light, width=2)
        draw.line([(cx, cy - 15), (cx, cy + 35)], fill=gold_light, width=2)
        draw.rectangle([(cx - 10, cy - 10), (cx + 10, cy + 10)], outline=gold_color, width=2)

    # 3. أيقونة المقطع الصوتي والهلال العلوي
    center_x = width // 2
    draw.arc([(center_x - 50, 160), (center_x + 50, 260)], start=25, end=275, fill=gold_light, width=5)
    draw.ellipse([(center_x - 12, 198), (center_x + 12, 222)], fill=gold_color)

    # رسم أقواس صوتية محيطة بالهلال
    draw.arc([(center_x - 80, 130), (center_x + 80, 290)], start=320, end=40, fill=gold_color, width=3)
    draw.arc([(center_x - 80, 130), (center_x + 80, 290)], start=140, end=220, fill=gold_color, width=3)

    # 4. معالجة وتشكيل النص العربي
    display_title = title_text if title_text else "تسجيل صوتي مبارك"
    reshaped_text = arabic_reshaper.reshape(display_title)
    bidi_text = get_display(reshaped_text)

    font = get_arabic_font(size=52)

    # 5. خطوط فاصلة بنقاط ذهبية
    draw.line([(200, 480), (width - 200, 480)], fill=gold_color, width=3)
    draw.ellipse([(center_x - 6, 474), (center_x + 6, 486)], fill=gold_light)

    # كتابة العنوان العربي المتناسق
    draw.text((center_x, 560), bidi_text, fill=gold_light, font=font, anchor="mm")

    draw.line([(200, 640), (width - 200, 640)], fill=gold_color, width=3)
    draw.ellipse([(center_x - 6, 634), (center_x + 6, 646)], fill=gold_light)

    # وسم القناة الترويجي في الأسفل
    chan_font = get_arabic_font(size=30)
    chan_reshaped = get_display(arabic_reshaper.reshape("قناة ضياء الدين سامي"))
    draw.text((center_x, 920), chan_reshaped, fill=gold_color, font=chan_font, anchor="mm")

    output = io.BytesIO()
    img.save(output, format="JPEG", quality=95)
    output.seek(0)
    return output
