import os
import json
import asyncio
import logging
import requests
import time
from telethon import TelegramClient
from telethon.sessions import StringSession
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload

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
        raise EnvironmentError(f"❌ المتغير البيئي '{key}' غير موجود أو فارغ. تحقق من ملف .env")
    return value

API_ID          = int(get_required_env("TELEGRAM_API_ID"))
API_HASH        = get_required_env("TELEGRAM_API_HASH")
SESSION_STRING  = get_required_env("TELEGRAM_SESSION")
OPENROUTER_KEY  = get_required_env("OPENROUTER_API_KEY")
FOLDER_ID       = get_required_env("GDRIVE_FOLDER_ID")

# ─────────────────────────────────────────────
# 3. الإعدادات العامة (قابلة للتعديل)
# ─────────────────────────────────────────────
STATE_FILE       = "channels_state.json"
CREDENTIALS_FILE = "oauth_credentials.json"
TARGET_CHANNEL   = "@elmin7a"
MAX_UPLOADS      = int(os.getenv("MAX_UPLOADS", "1000"))   # قابل للتعديل عبر .env
GDRIVE_SCOPES    = ["https://www.googleapis.com/auth/drive"]

# ─────────────────────────────────────────────
# 4. دالة الاتصال بـ OpenRouter
# ─────────────────────────────────────────────
def process_with_openrouter(text: str, retries: int = 3, delay: int = 5) -> dict:
    """
    ترسل النص إلى OpenRouter وتعيد dict يحتوي على:
    - important (bool)
    - title (str)
    - content (str)
    """
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "http://localhost",
        "X-Title": "Obsidian Local Automation"
    }

    system_prompt = (
        "أنت مساعد ذكي متخصص في إدارة المعرفة لبرنامج Obsidian. مهمتك فرز الملاحظات.\n"
        "قيّم الرسالة التالية: إذا كانت إعلاناً أو رسالة ترويجية أو تافهة ولا تقدم قيمة معرفية، "
        "اجعل حقل 'important' يساوي false.\n"
        "إذا كانت مهمة وقيّمة، صغها وتنسّقها بلغة Markdown احترافية (عناوين، نقاط، وسوم مناسبة).\n"
        "ابتكر عنواناً مختصراً جداً يصف المحتوى (3-5 كلمات كحد أقصى) "
        "وبدون أي رموز خاصة تمنع حفظ الملفات مثل: \\ / : * ? \" < > |\n"
        "يجب أن يكون ردك بصيغة JSON حصراً وبدون أي نص إضافي:\n"
        "{\n"
        "  \"important\": true,\n"
        "  \"title\": \"العنوان المختصر هنا\",\n"
        "  \"content\": \"المحتوى المنسق بالكامل بـ Markdown هنا\"\n"
        "لا تضف أي نص قبل أو بعد JSON. لا تستخدم ```json. الرد يجب أن يبدأ بـ { وينتهي بـ } فقط."
        "}"
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
            
            log.info(f"🔍 الرد الخام من AI:\n{ai_reply}")  # ← أضف هذا
            
            # تنظيف أي Markdown code fences إن وُجدت
            if ai_reply.startswith("```"):
                ai_reply = ai_reply.split("\n", 1)[-1]   # احذف السطر الأول ```json أو ```
                ai_reply = ai_reply.rsplit("```", 1)[0]  # احذف الإغلاق ```
                ai_reply = ai_reply.strip()

            return json.loads(ai_reply, strict=False)

        except json.JSONDecodeError as e:
            log.warning(f"⚠️ فشل تحليل JSON (محاولة {attempt}/{retries}): {e}")
            time.sleep(delay)
        except requests.RequestException as e:
            log.warning(f"⚠️ خطأ في الاتصال (محاولة {attempt}/{retries}): {e}")
            time.sleep(delay)
        except Exception as e:
            log.error(f"❌ خطأ غير متوقع (محاولة {attempt}/{retries}): {e}")
            time.sleep(delay)

    log.error("❌ فشلت جميع المحاولات مع OpenRouter.")
    return {"important": False}

# ─────────────────────────────────────────────
# 5. دالة رفع الملف إلى Google Drive
# ─────────────────────────────────────────────
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError

OAUTH_FILE  = "oauth_credentials.json"
TOKEN_FILE  = "token.json"

def get_drive_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, GDRIVE_SCOPES)
        except ValueError as e:
            log.warning(f"⚠️ ملف التوكن '{TOKEN_FILE}' تالف ({e}). سيتم حذفه وطلب مصادقة جديدة.")
            os.remove(TOKEN_FILE)
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError as e:
                log.warning(f"⚠️ فشل تحديث توكن Google Drive ({e}). سيتم حذف التوكن القديم وطلب مصادقة جديدة.")
                os.remove(TOKEN_FILE)
                creds = None # Force re-authentication

        if not creds:
            try:
                flow = InstalledAppFlow.from_client_secrets_file(OAUTH_FILE, GDRIVE_SCOPES)
                creds = flow.run_local_server(port=0)
            except FileNotFoundError:
                log.error(f"❌ ملف مصادقة OAuth '{OAUTH_FILE}' غير موجود. لا يمكن المتابعة مع Google Drive.")
                return None

        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    return build("drive", "v3", credentials=creds)


def upload_to_drive(title: str, content: str) -> bool:
    try:
        service = get_drive_service()
        if not service:
            return False

        file_metadata = {"name": f"{title}.md", "parents": [FOLDER_ID]}
        media = MediaInMemoryUpload(content.encode("utf-8"), mimetype="text/markdown")
        file = service.files().create(
            body=file_metadata,
            media_body=media,
            fields="id"
        ).execute()
        
        log.info(f"✅ تم رفع الملف: '{title}.md' (ID: {file.get('id')})")
        return True
    except Exception as e:
        log.error(f"❌ خطأ أثناء الرفع إلى Drive: {e}")
    return False
# ─────────────────────────────────────────────
# 6. حفظ وتحميل الحالة
# ─────────────────────────────────────────────
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            log.warning(f"⚠️ تعذّر قراءة ملف الحالة، سيتم البدء من جديد: {e}")
    return {}

def save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        log.info("💾 تم حفظ الحالة محلياً.")
    except IOError as e:
        log.error(f"❌ فشل حفظ الحالة: {e}")

# ─────────────────────────────────────────────
# 7. المنطق الرئيسي
# ─────────────────────────────────────────────
async def main():
    state = load_state()

    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    await client.connect()

    if not await client.is_user_authorized():
        log.error("❌ الجلسة (Session) غير صالحة أو منتهية. أعد إنشاءها.")
        await client.disconnect()
        return

    # جلب معلومات القناة
    try:
        entity      = await client.get_entity(TARGET_CHANNEL)
        channel_id  = str(entity.id)
        channel_title = entity.title
        log.info(f"🎯 متصل بالقناة: {channel_title}")
    except Exception as e:
        log.error(f"❌ تعذّر الوصول إلى القناة: {e}")
        await client.disconnect()
        return

    # تهيئة الحالة إن لم تكن موجودة
    if channel_id not in state:
        state[channel_id] = {
            "channel_name": channel_title,
            "last_processed_message_id": 0
        }

    ch_info = state[channel_id]
    last_id = ch_info["last_processed_message_id"]
    files_uploaded = 0

    log.info(f"⏳ جاري المزامنة من رسالة ID: {last_id} ...")

    try:
        messages = await client.get_messages(
            entity,
            min_id=last_id,
            limit=200,
            reverse=True   # من الأقدم للأحدث
        )

        for msg in messages:
            # توقف فوري عند بلوغ الحد الأقصى
            if files_uploaded >= MAX_UPLOADS:
                log.info(f"🛑 تم الوصول للحد الأقصى ({MAX_UPLOADS} ملفات). إيقاف البرنامج.")
                break

            if not msg.text or len(msg.text.strip()) <= 5:
                # تحديث الحالة حتى للرسائل الفارغة لتجنب إعادة معالجتها
                ch_info["last_processed_message_id"] = msg.id
                continue

            log.info(f"🎬 معالجة رسالة ID {msg.id} ...")
            ai_result = process_with_openrouter(msg.text)

            if ai_result.get("important") and ai_result.get("title"):
                success = upload_to_drive(ai_result["title"], ai_result["content"])
                if success:
                    files_uploaded += 1
                    log.info(f"📈 المرفوع في هذه الجلسة: {files_uploaded}/{MAX_UPLOADS}")
                    time.sleep(3)  # تجنب تجاوز حدود API
            else:
                log.info(f"⏭️ رسالة ID {msg.id} تجاوزناها (غير مهمة).")

            # ✅ حفظ الحالة بعد كل رسالة لضمان الاستمرارية عند أي انقطاع
            ch_info["last_processed_message_id"] = msg.id
            save_state(state)

    except Exception as e:
        log.error(f"❌ خطأ أثناء معالجة الرسائل: {e}")

    finally:
        await client.disconnect()
        log.info("🔌 تم قطع الاتصال بتيليغرام.")

    log.info(f"✅ انتهت الجلسة. إجمالي الملفات المرفوعة: {files_uploaded}")

# ─────────────────────────────────────────────
if __name__ == "__main__":
    asyncio.run(main())
