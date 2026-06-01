import os
import json
import asyncio
import logging
import time
import io
from telethon import TelegramClient
from telethon.sessions import StringSession
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
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
    value = os.getenv(key)
    if not value:
        raise EnvironmentError(f"❌ المتغير البيئي '{key}' غير موجود أو فارغ.")
    return value

API_ID          = int(get_required_env("TELEGRAM_API_ID"))
API_HASH        = get_required_env("TELEGRAM_API_HASH")
SESSION_STRING  = get_required_env("TELEGRAM_SESSION")
GDRIVE_FOLDER_ID = get_required_env("GDRIVE_FOLDER_ID")

# ─────────────────────────────────────────────
# 3. الإعدادات العامة للأداة الجديدة
# ─────────────────────────────────────────────
BASE_DIR         = os.path.dirname(os.path.abspath(__file__))
STATE_FILE       = os.path.join(BASE_DIR, "media_state.json") # ملف حالة منفصل للوسائط
TARGET_CHANNEL   = "@L_alnader22"                                   # ⚠️ ضع معرف قناتك الجديدة هنا
MAX_UPLOADS      = int(os.getenv("MAX_UPLOADS", "50"))         # يفضل تقليله للوسائط الكبيرة لتجنب ميعاد الـ Timeout
GDRIVE_SCOPES    = ["https://www.googleapis.com/auth/drive"]

# ─────────────────────────────────────────────
# 4. إدارة اتصال Google Drive والرفع
# ─────────────────────────────────────────────
OAUTH_FILE  = os.path.join(BASE_DIR, "oauth_credentials.json")
TOKEN_FILE  = os.path.join(BASE_DIR, "token.json")

def get_drive_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, GDRIVE_SCOPES)
        except Exception:
            os.remove(TOKEN_FILE)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try: creds.refresh(Request())
            except Exception: os.remove(TOKEN_FILE); creds = None

        if not creds:
            flow = InstalledAppFlow.from_client_secrets_file(OAUTH_FILE, GDRIVE_SCOPES)
            creds = flow.run_local_server(port=0)

        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    return build("drive", "v3", credentials=creds)

def upload_media_to_drive(service, file_bytes: bytes, filename: str, mime_type: str) -> bool:
    """ترفع ملف الوسائط من الذاكرة مباشرة إلى المجلد المحدد."""
    try:
        file_metadata = {"name": filename, "parents": [GDRIVE_FOLDER_ID]}
        media = MediaInMemoryUpload(file_bytes, mimetype=mime_type, resumable=True)
        
        file = service.files().create(
            body=file_metadata,
            media_body=media,
            fields="id"
        ).execute()
        
        log.info(f"✅ تم رفع الملف بنجاح: '{filename}' (ID: {file.get('id')})")
        return True
    except Exception as e:
        log.error(f"❌ خطأ أثناء رفع الملف '{filename}' إلى Drive: {e}")
        return False

# ─────────────────────────────────────────────
# 5. استخراج اسم الملف ونوع الـ Mime تلقائياً
# ─────────────────────────────────────────────
def get_media_details(msg) -> tuple:
    """تستخرج اسم الملف المناسب ونوع الـ Mime للوسائط المختلفة."""
    # افتراضات أولية
    filename = f"media_{msg.id}"
    mime_type = "application/octet-stream"

    if msg.file:
        if msg.file.name:
            filename = msg.file.name
        else:
            # توليد اسم بناءً على الامتداد إذا لم يتوفر اسم صريح (مثل بعض الصور والفيديوهات)
            ext = msg.file.ext if msg.file.ext else ""
            filename = f"media_{msg.id}{ext}"
        
        if msg.file.mime_type:
            mime_type = msg.file.mime_type

    return filename, mime_type

# ─────────────────────────────────────────────
# 6. حفظ وتحميل الحالة والمنطق الرئيسي
# ─────────────────────────────────────────────
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f: return json.load(f)
        except Exception: pass
    return {}

def save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f: json.dump(state, f, ensure_ascii=False, indent=2)
    except IOError as e: log.error(f"❌ فشل حفظ الحالة: {e}")

async def main():
    state = load_state()
    service = get_drive_service()
    if not service:
        log.error("❌ تعذّر الاتصال بـ Google Drive.")
        return

    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    await client.connect()

    if not await client.is_user_authorized():
        log.error("❌ الجلسة غير صالحة.")
        await client.disconnect()
        return

    try:
        entity = await client.get_entity(TARGET_CHANNEL)
        channel_id = str(entity.id)
        log.info(f"🎯 متصل بقناة الوسائط: {entity.title}")
    except Exception as e:
        log.error(f"❌ تعذّر الوصول إلى القناة: {e}"); await client.disconnect(); return

    if channel_id not in state:
        state[channel_id] = {"channel_name": entity.title, "last_processed_message_id": 0}

    ch_info = state[channel_id]
    last_id = ch_info["last_processed_message_id"]
    files_uploaded = 0

    log.info(f"⏳ جاري فحص الوسائط الجديدة من رسالة ID: {last_id} ...")

    try:
        messages = await client.get_messages(entity, offset_id=last_id, limit=100, reverse=True)

        for msg in messages:
            if files_uploaded >= MAX_UPLOADS:
                log.info(f"🛑 تم الوصول للحد الأقصى المسموح به في الجلسة ({MAX_UPLOADS} ملف).")
                break

            # التحقق مما إذا كانت الرسالة تحتوي على ملف أو وسائط (صورة، فيديو، مستند، صوت)
            if not msg.media:
                ch_info["last_processed_message_id"] = msg.id
                continue

            log.info(f"🎬 جاري معالجة وسائط الرسالة رقم ID {msg.id} ...")
            filename, mime_type = get_media_details(msg)

            try:
                # تحميل الملف مباشرة إلى الذاكرة لتجنب استهلاك مساحة القرص على سيرفر الـ Action
                file_buffer = io.BytesIO()
                await client.download_media(msg.media, file_buffer)
                file_bytes = file_buffer.getvalue()

                if len(file_bytes) > 0:
                    success = upload_media_to_drive(service, file_bytes, filename, mime_type)
                    if success:
                        files_uploaded += 1
                        time.sleep(1) # تأخير طفيف بين الملفات لحماية الاتصال
                else:
                    log.warning(f"⚠️ الملف في الرسالة {msg.id} فارغ أو فشل تحميله.")

            except Exception as media_err:
                log.error(f"❌ خطأ أثناء تحميل الوسائط من تيليجرام للرسالة {msg.id}: {media_err}")

            ch_info["last_processed_message_id"] = msg.id
            save_state(state)

    except Exception as e:
        log.error(f"❌ خطأ عام أثناء معالجة الرسائل: {e}")
    finally:
        await client.disconnect()
        log.info("🔌 تم قطع الاتصال وإنهاء الجلسة.")

if __name__ == "__main__":
    asyncio.run(main())
      
