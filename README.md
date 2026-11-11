# Video Downloader Bot V2

بوت تليجرام لتنزيل **الفيديو** و**الصوت (MP3)** من يوتيوب وتيك توك وإنستجرام وتويتر ولينكدإن وأكثر من 1000 موقع.

## المميزات
- فيديو أو صوت MP3 مع اختيار الجودة
- **بلاي ليست يوتيوب** (حتى `MAX_PLAYLIST_ITEMS` فيديو)
- دعم أفضل لإنستجرام (منشورات / ريلز / ستوريز عند توفر Cookies)
- نظام **Cookies** للفيديوهات المقيّدة بالعمر أو شبه الخاصة
- صورة مصغّرة + عنوان + اسم القناة
- أوامر: `/start` `/help` `/status` `/cookies`
- Redis اختياري، ويمكن تشغيله كـ Worker على Railway

## التشغيل المحلي
```bash
git clone https://github.com/sadman8987-dev/Video-for-some-one.git
cd Video-for-some-one
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# ثبّت FFmpeg
export BOT_TOKEN="توكن_البوت"
python main.py
```

## Cookies (ميزة معروفة)
بعض المحتوى يحتاج جلسة متصفح:
1. صدّر `cookies.txt` بصيغة Netscape من متصفحك
2. حوّله Base64
3. ضعه في:
   - `YOUTUBE_COOKIES_BASE64` ليوتيوب
   - `SOCIAL_COOKIES_BASE64` لإنستجرام/تيك توك/تويتر

الأمر `/cookies` يعرض حالة التفعيل.

## حدود
- إرسال Telegram السحابي ≈ 49MB
- DRM غير مدعوم
- المحتوى الخاص تمامًا يحتاج حساب يملك صلاحية المشاهدة

## Railway
Worker + Dockerfile + متغير `BOT_TOKEN` (مطلوب).

## Cookies لكل منصة على Railway

يدعم البوت ملفات Cookies بصيغة Netscape بعد تحويلها إلى Base64. أضف المتغيرات التي تحتاجها فقط في Railway → Service → Variables:

- `YOUTUBE_COOKIES_BASE64` — يوتيوب (موجود دعمه بالفعل في `youtube_handler.py`).
- `TIKTOK_COOKIES_BASE64` — تيك توك.
- `INSTAGRAM_COOKIES_BASE64` — إنستجرام.
- `TWITTER_COOKIES_BASE64` أو `X_COOKIES_BASE64` — إكس.
- `FACEBOOK_COOKIES_BASE64` — فيسبوك.
- `SOCIAL_COOKIES_BASE64` — ملف اجتماعي عام احتياطي إذا لم يوجد متغير خاص بالمنصة.

لا يحتوي أرشيف الإعدادات المرفق على Cookies فعلية؛ إنه يحتوي على أدوات مساعدة وأمثلة فقط. يجب تصدير ملف Cookies من حسابك بنفسك، ثم تحويله إلى Base64 وإضافته كمتغير سري في Railway. لا ترفع ملفات Cookies أو قيم Base64 إلى GitHub ولا تشاركها في المحادثات. استخدم جلسات حسابات تملكها، واحمِها لأنها تمنح وصولًا إلى حسابك.

بعد إضافة المتغيرات، أعد تشغيل/نشر الخدمة حتى تُقرأ الإعدادات الجديدة.
