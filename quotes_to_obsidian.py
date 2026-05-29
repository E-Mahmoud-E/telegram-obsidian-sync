import os
import json
import asyncio
import logging
import requests
import time
import io
from telethon import TelegramClient
from telethon.sessions import StringSession
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload, MediaIoBaseDownload

# ─────────────────────────────────────────────
# 0. إعداد نظام السجلات (Logging)
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# 1. تحميل ملف .env إن وجد
# ─────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
    log.info("✅ تم تحميل ملف .env بنجاح.")
except ImportError:
    log.warning("⚠️ مكتبة python-dotenv غير مثبتة، سيتم قراءة متغيرات البيئة مباشرة.")

# ─────────────────────────────────────────────
# 2. قراءة والتحقق من المتغيرات البيئية
# ─────────────────────────────────────────────
def get_required_env(key: str) -> str:
    """يقرأ متغير بيئة مطلوب، ويرفع خطأ واضح إن لم يوجد."""
    value = os.getenv(key)
    if not value:
        raise EnvironmentError(f"❌ المتغير البيئي '{key}' غير موجود أو فارغ. تحقق من الإعدادات.")
    return value

API_ID          = int(get_required_env("TELEGRAM_API_ID"))
API_HASH        = get_required_env("TELEGRAM_API_HASH")
SESSION_STRING  = get_required_env("TELEGRAM_SESSION")
OPENROUTER_KEY  = get_required_env("OPENROUTER_API_KEY")
FOLDER_ID       = get_required_env("GDRIVE_FOLDER_ID")

# ─────────────────────────────────────────────
# 3. الإعدادات العامة للأداة (تستهدف قناة الأقوال)
# ─────────────────────────────────────────────
BASE_DIR         = os.path.dirname(os.path.abspath(__file__))
STATE_FILE       = os.path.join(BASE_DIR, "quotes_state.json") # ملف حالة منفصل تماماً
TARGET_CHANNEL   = "@uu66n6"                       # ⚠️ استبدله بمعرف قناة الأقوال هنا
MAX_UPLOADS      = int(os.getenv("MAX_UPLOADS", "1000"))
GDRIVE_SCOPES    = ["https://www.googleapis.com/auth/drive"]

# ─────────────────────────────────────────────
# 4. دالة الاتصال بـ OpenRouter (مخصصة للتصنيف والدمج)
# ─────────────────────────────────────────────
def process_quote_with_openrouter(text: str, retries: int = 3, delay: int = 5) -> dict:
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "http://localhost",
        "X-Title": "Obsidian Quotes Automation"
    }

    system_prompt = (
        "أنت مساعد ذكي متخصص في تصنيف الأقوال المأثورة، الحكم، والأمثال لبرنامج Obsidian.\n"
        "مهمتك هي فحص النص التالي:\n"
        "1. حدد ما إذا كان النص يحتوي على حكمة، قول مأثور، مثل شعبي، أو اقتباس مفيد وعميق. إذا كان مجرد إعلان أو كلام عشوائي، اجعل 'valid' يساوي false.\n"
        "2. صنف القول إلى فئة رئيسية واحدة تناسبه لتكون اسم الملف داخل أوبسيديان (مثل: 'حكم وأقوال'، 'أمثال شعبية LIGHT'، 'تطوير الذات'، 'اقتباسات كتب'). اجعل اسم الفئة مختصراً وبدون أي رموز خاصة.\n"
        "3. نسق القول بصيغة Markdown ليكون مناسباً كسطر مضاف في ملف، استخدم التنسيق التالي تماماً:\n"
        "- > \"القول هنا\" — **اسم القائل إن وجد** #وسم_الفئة\n\n"
        "يجب أن يكون ردك بصيغة JSON حصراً وبدون أي نص إضافي:\n"
        "{\n"
        "  \"valid\": true,\n"
        "  \"category\": \"اسم الفئة هنا\",\n"
        "  \"formatted_quote\": \"السطر المنسق هنا\"\n"
        "}\n"
        "لا تضف أي نص قبل أو بعد JSON. لا تستخدم ```json. الرد يجب أن يبدأ بـ { وينتهي بـ } فقط."
    )

    payload = {
        "model": "openrouter/owl-alpha",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": text}
        ]
    }

    for attempt in range(1, retries + 1):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=30)
            response.raise_for_status()
            res_data = response.json()

            if "error" in res_data:
                log.warning(f"⚠️ OpenRouter Error (محاولة {attempt}/{retries}): {res_data['error'].get('message')}")
                time.sleep(delay)
                continue

            ai_reply = res_data["choices"][0]["message"]["content"].strip()
            
            if ai_reply.startswith("```"):
                ai_reply = ai_reply.split("\n", 1)[-1]
                ai_reply = ai_reply.rsplit("```", 1)[0]
                ai_reply = ai_reply.strip()

            return json.loads(ai_reply, strict=False)

        except Exception as e:
            log.warning(f"⚠️ فشل تحليل رد AI (محاولة {attempt}/{retries}): {e}")
            time.sleep(delay)

    log.error("❌ فشلت جميع المحاولات مع OpenRouter.")
    return {"valid": False}

# ─────────────────────────────────────────────
# 5. إدارة اتصال Google Drive والتحديث الذكي
# ─────────────────────────────────────────────
OAUTH_FILE  = os.path.join(BASE_DIR, "oauth_credentials.json")
TOKEN_FILE  = os.path.join(BASE_DIR, "token.json")

def get_drive_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, GDRIVE_SCOPES)
        except Exception as e:
            log.warning(f"⚠️ ملف التوكن تالف ({e}). سيتم حذفه.")
            os.remove(TOKEN_FILE)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                os.remove(TOKEN_FILE)
                creds = None

        if not creds:
            flow = InstalledAppFlow.from_client_secrets_file(OAUTH_FILE, GDRIVE_SCOPES)
            creds = flow.run_local_server(port=0)

        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    return build("drive", "v3", credentials=creds)

def find_file_in_folder(service, filename: str) -> str:
    """تبحث عن ملف محدد داخل المجلد وتعيد الـ ID الخاص به إذا وُجد."""
    query = f"name = '{filename}' and '{FOLDER_ID}' in parents and trashed = false"
    results = service.files().list(q=query, fields="files(id)").execute()
    files = results.get("files", [])
    return files[0]["id"] if files else None

def get_file_content(service, file_id: str) -> str:
    """تحميل محتوى الملف الحالي من الجوجل درايف لقراءته."""
    request = service.files().get_media(fileId=file_id)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return fh.getvalue().decode("utf-8")

def append_quote_to_file(category: str, formatted_quote: str) -> bool:
    """تبحث عن ملف الفئة: تحدثه بأسفله إن وجد، أو تنشئه من الصفر."""
    try:
        service = get_drive_service()
        if not service:
            return False

        filename = f"{category}.md"
        file_id = find_file_in_folder(service, filename)

        if file_id:
            # الملف موجود: نقرأ المحتوى القديم ونضيف الجديد أسفله
            current_content = get_file_content(service, file_id)
            if current_content and not current_content.endswith("\n"):
                current_content += "\n"
            new_content = current_content + formatted_quote + "\n"
            
            media = MediaInMemoryUpload(new_content.encode("utf-8"), mimetype="text/markdown")
            service.files().update(fileId=file_id, media_body=media).execute()
            log.info(f"🔄 تم تحديث وإضافة القول الجديد إلى الملف: '{filename}'")
        else:
            # الملف غير موجود: ننشئه لأول مرة
            new_content = f"# {category}\n\n" + formatted_quote + "\n"
            file_metadata = {"name": filename, "parents": [FOLDER_ID]}
            media = MediaInMemoryUpload(new_content.encode("utf-8"), mimetype="text/markdown")
            service.files().create(body=file_metadata, media_body=media, fields="id").execute()
            log.info(f"✨ تم إنشاء ملف فئة جديد بنجاح: '{filename}'")
        
        return True
    except Exception as e:
        log.error(f"❌ خطأ أثناء التعامل مع Google Drive: {e}")
    return False

# ─────────────────────────────────────────────
# 6. حفظ وتحميل الحالة والمنطق الرئيسي
# ─────────────────────────────────────────────
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except IOError as e:
        log.error(f"❌ فشل حفظ الحالة: {e}")

async def main():
    state = load_state()

    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    await client.connect()

    if not await client.is_user_authorized():
        log.error("❌ الجلسة (Session) غير صالحة أو منتهية.")
        await client.disconnect()
        return

    try:
        entity = await client.get_entity(TARGET_CHANNEL)
        channel_id = str(entity.id)
        log.info(f"🎯 متصل بقناة الأقوال والحكم: {entity.title}")
    except Exception as e:
        log.error(f"❌ تعذّر الوصول إلى القناة: {e}")
        await client.disconnect()
        return

    if channel_id not in state:
        state[channel_id] = {
            "channel_name": entity.title,
            "last_processed_message_id": 0
        }

    ch_info = state[channel_id]
    last_id = ch_info["last_processed_message_id"]
    quotes_processed = 0

    log.info(f"⏳ جاري المزامنة وفحص الرسائل الجديدة من ID: {last_id} ...")

    try:
        messages = await client.get_messages(
            entity,
            offset_id=last_id,
            limit=100,
            reverse=True # من الأقدم للأحدث لترتيب زمني صحيح داخل الملف
        )

        for msg in messages:
            if quotes_processed >= MAX_UPLOADS:
                log.info(f"🛑 تم الوصول للحد الأقصى ({MAX_UPLOADS}). إيقاف مؤقت.")
                break

            if not msg.text or len(msg.text.strip()) <= 4:
                ch_info["last_processed_message_id"] = msg.id
                continue

            log.info(f"🎬 تحليل رسالة رقم ID {msg.id} ...")
            ai_result = process_quote_with_openrouter(msg.text)

            if ai_result.get("valid") and ai_result.get("category"):
                success = append_quote_to_file(ai_result["category"], ai_result["formatted_quote"])
                if success:
                    quotes_processed += 1
                    time.sleep(2) # حماية من تخطي حدود الـ API لـ Google Drive
            else:
                log.info(f"⏭️ رسالة ID {msg.id} تم تجاوزها (إعلان أو محتوى غير مناسب).")

            # حفظ الحالة بعد كل رسالة لضمان عدم التكرار عند أي انقطاع
            ch_info["last_processed_message_id"] = msg.id
            save_state(state)

    except Exception as e:
        log.error(f"❌ خطأ أثناء معالجة الرسائل: {e}")
    finally:
        await client.disconnect()
        log.info("🔌 تم قطع الاتصال بتيليغرام وإنهاء العمل الجاري.")

if __name__ == "__main__":
    asyncio.run(main())
    
