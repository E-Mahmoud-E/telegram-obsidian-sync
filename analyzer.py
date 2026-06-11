import os
import json
import csv
import asyncio
import logging
from datetime import datetime
import urllib.request

# محاولة استيراد مكتبات تيليجرام
try:
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon.tl.types import MessageMediaDocument, MessageMediaWebPage
except ImportError:
    print("❌ يرجى تثبيت مكتبة Telethon أولاً عبر: pip install telethon")

# ────────────────────────────────────────────────────────
# ⚙️ إعدادات النظام والبيئة
# ────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

API_ID = int(os.getenv("TELEGRAM_API_ID", 0))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
SESSION_STRING = os.getenv("TELEGRAM_SESSION", "")
OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY", "")

# إعداد مسارات التصدير داخل الـ Repo
GITHUB_DIR = "./github_dashboard"
OBSIDIAN_DIR = "./obsidian_vault/Telegram_Channels"
os.makedirs(GITHUB_DIR, exist_ok=True)
os.makedirs(OBSIDIAN_DIR, exist_ok=True)

STATE_FILE = "analyzer_state.json"
LIMIT_PER_RUN = 1000  # الحد الأقصى المطلوب لمعالجة الرسائل في الجلسة الواحدة

# ────────────────────────────────────────────────────────
# 🧠 محرك التخاطب مع الذكاء الاصطناعي عبر OpenRouter
# ────────────────────────────────────────────────────────
def ask_openrouter(prompt: str) -> dict:
    """إرسال البيانات لـ OpenRouter لتحليل القناة واستخراج تصنيفها والملخص باللغة العربية."""
    if not OPENROUTER_KEY:
        log.warning("⚠️ سيكرت OPENROUTER_API_KEY غير متوفر، سيتم استخدام تصنيف احتياطي.")
        return {"categories": ["Other"], "summary": "لم يتم التحليل لغياب مفتاح الـ API"}

    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/MahmoudElmahdy", # معرف مخصص
        "X-Title": "Telegram KM Assistant"
    }
    
    # استخدام نموذج سريع ومجاني/رخيص وممتاز في النصوص مثل Llama 3
    data = {
        "model": "openrouter/owl-alpha",
        "messages": [
            {
                "role": "user",
                "content": prompt
            }
        ],
        "response_format": { "type": "json_object" } # إجبار السيرفر على الرد بصيغة JSON
    }

    try:
        req = urllib.request.Request(url, data=json.dumps(data).encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=30) as response:
            res_body = json.loads(response.read().decode("utf-8"))
            content = res_body['choices'][0]['message']['content']
            return json.loads(content)
    except Exception as e:
        log.error(f"❌ خطأ أثناء الاتصال بـ OpenRouter: {e}")
        return {"categories": ["Other"], "summary": "حدث خطأ أثناء معالجة البيانات ذكياً."}

# ────────────────────────────────────────────────────────
# 💾 إدارة ذاكرة الحالة (State Management)
# ────────────────────────────────────────────────────────
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f: return json.load(f)
        except Exception: pass
    return {}

def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

# ────────────────────────────────────────────────────────
# 🚀 المحرك البرمجي الرئيسي للفلترة والكشط المتدرج
# ────────────────────────────────────────────────────────
async def main():
    if not SESSION_STRING:
        log.error("❌ متغير الجلسة TELEGRAM_SESSION غير موجود!")
        return

    state = load_state()
    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    await client.connect()
    
    inventory = []
    log.info("📡 جاري بدء فحص القنوات واستخراج البيانات المستهدفة...")

    async for dialog in client.iter_dialogs():
        if dialog.is_channel and not dialog.is_group:
            channel = dialog.entity
            ch_id = str(channel.id)
            
            # تهيئة حالة القناة في الذاكرة إذا كانت جديدة
            if ch_id not in state:
                state[ch_id] = {"last_scraped_id": 0, "is_fully_scraped": False, "accumulated_text": ""}
            
            if state[ch_id]["is_fully_scraped"]:
                log.info(f"⏭️ تخطي القناة [{dialog.name}] - تم التهام تاريخها بالكامل مسبقاً.")
                continue

            log.info(f"📥 فحص متدرج للقناة [{dialog.name}] | بدءاً من الرسالة: {state[ch_id]['last_scraped_id']}")
            
            collected_messages = []
            last_processed_id = state[ch_id]["last_scraped_id"]
            
            # جلب الرسائل عكسياً (من الأقدم للأحدث reverse=True)
            async for msg in client.iter_messages(channel, limit=LIMIT_PER_RUN, offset_id=last_processed_id, reverse=True):
                last_processed_id = msg.id
                
                # شروط التصفية النوعية: (روابط، أو ملفات ومستندات، أو نصوص طويلة غنية بالمعلومات)
                is_file = msg.media and isinstance(msg.media, MessageMediaDocument)
                is_link = msg.media and isinstance(msg.media, MessageMediaWebPage) or (msg.text and "http" in msg.text)
                is_long_text = msg.text and len(msg.text) >= 150 # تضمين الرسائل الطويلة والشروحات
                
                if msg.text and (is_file or is_link or is_long_text):
                    collected_messages.append(msg.text)

            # تحديث معرف آخر رسالة وصلنا إليها في هذا التشغيل
            state[ch_id]["last_scraped_id"] = last_processed_id
            
            # إذا أرجع تيليجرام رسائل أقل من الحد، فهذا يعني أننا وصلنا لليوم الحالي واكتمل الكشط التاريخي
            if len(collected_messages) < LIMIT_PER_RUN:
                state[ch_id]["is_fully_scraped"] = True

            if collected_messages:
                # دمج النصوص الجديدة مع النصوص السابقة لتحديث رؤية الذكاء الاصطناعي للقناة
                new_texts = " \n ".join(collected_messages)
                state[ch_id]["accumulated_text"] = (state[ch_id]["accumulated_text"] + " " + new_texts)[:8000] # نحددها بـ 8000 حرف لتجنب تخطي الـ Context Window
                
                # بناء الـ Prompt للذكاء الاصطناعي ليعطينا مخرجات JSON مصححة
                ai_prompt = f"""
                Analyze the following data extracted from a Telegram channel.
                You must respond with a strictly valid JSON object following this template:
                {{
                  "categories": ["Choose from: GIS & Remote Sensing, Programming & Automation, AI & Machine Learning, Novels & Manga, Education & Research"],
                  "summary": "Write a concise summary in Arabic explaining the primary value of this channel, the kind of tools shared, and how the user can benefit from it in their study or career growth."
                }}
                
                Channel Title: {channel.title}
                Content Sample: {state[ch_id]['accumulated_text']}
                """
                
                log.info(f"🧠 جاري استشارة OpenRouter لتحليل تخصص قناة [{channel.title}]...")
                ai_analysis = ask_openrouter(ai_prompt)
                
                categories = ai_analysis.get("categories", ["Other"])
                summary = ai_analysis.get("summary", "تعذر استخراج الملخص الذكي.")
            else:
                categories = ["Other"]
                summary = "لم يتم العثور على رسائل مستهدفة جديدة في هذه الدفعة."

            # إعداد البيانات النهائية للتصدير
            username = f"@{channel.username}" if channel.username else f"id/{channel.id}"
            channel_data = {
                "id": channel.id,
                "title": channel.title,
                "username": username,
                "link": f"https://t.me/{channel.username}" if channel.username else "رابط خاص",
                "categories": categories,
                "summary": summary,
                "is_completed_history": state[ch_id]["is_fully_scraped"]
            }
            inventory.append(channel_data)
            save_state(state) # حفظ التقدم فوراً خطوة بخطوة لضمان عدم ضياع الجهد

    # كتابة وحفظ ملفات التقارير النهائية
    if inventory:
        export_artifacts(inventory)

    await client.disconnect()

# ────────────────────────────────────────────────────────
# 💾 دوال تصدير ملفات الجداول و الـ Markdown لـ Obsidian
# ────────────────────────────────────────────────────────
def export_artifacts(inventory):
    # تصدير ملف إكسيل/CSV الرئيسي في جيت هاب
    with open(f"{GITHUB_DIR}/channels.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["ID", "Title", "Username", "Categories", "History Scraped Completely"])
        for c in inventory:
            writer.writerow([c["id"], c["title"], c["username"], ", ".join(c["categories"]), c["is_completed_history"]])

    # تصدير واستبدال نوتات أوبسيديان الذكية
    for c in inventory:
        filename = f"{OBSIDIAN_DIR}/{c['title'].replace(' ', '_').replace('/', '_')}.md"
        tags_str = "\n  - ".join(c["categories"])
        
        content = f"""---
id: {c['id']}
title: "{c['title']}"
username: "{c['username']}"
link: "{c['link']}"
history_scraped_completely: {c['is_completed_history']}
tags:
  - {tags_str}
---

# {c['title']}

## 📊 معلومات الوصول
- **الرابط المباشر:** [{c['username']}]({c['link']})
- **حالة الأرشفة التاريخية كاملة:** {'✅ نعم، تم التهام الأرشيف' if c['is_completed_history'] else '⏳ جاري السحب تدريجياً كل ساعة'}

## 🧠 التحليل الذكي المحدث عبر (OpenRouter AI)
> {c['summary']}

## 🎯 الفرص الاستراتيجية وبناء المهارات
* **التصنيف المهني الممنوح:** {", ".join(c['categories'])}
* **كيفية توظيف المحتوى:** راجع الملاحظة الملحقة بملخص الذكاء الاصطناعي أعلاه، واستخرج الأدوات والملفات الهندسية أو البرمجية لتحديث مشاريعك الشخصية بانتظام.

## 🔗 روابط التنقل السريع في الخزنة
- [[لوحة_التحكم_الرئيسية]]
- [[مستودع_خرائط_المساحة_والـ_GIS]]
"""
        with open(filename, "w", encoding="utf-8") as f:
            f.write(content)
    log.info("🎉 تم تحديث لوحات تحكم GitHub وخزنة Obsidian الذكية بنجاح!")

if __name__ == "__main__":
    import sys
    if "asyncio" in sys.modules:
        asyncio.run(main())
