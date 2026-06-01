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
MAIN_FOLDER_ID  = get_required_env("GDRIVE_FOLDER_ID") # مجلد الأوبسيديان الرئيسي

# ─────────────────────────────────────────────
# 3. الإعدادات العامة للأداة
# ─────────────────────────────────────────────
BASE_DIR         = os.path.dirname(os.path.abspath(__file__))
STATE_FILE       = os.path.join(BASE_DIR, "media_state.json")
TARGET_CHANNEL   = "@L_alnader22"
MAX_UPLOADS      = int(os.getenv("MAX_UPLOADS", "30"))
GDRIVE_SCOPES    = ["https://www.googleapis.com/auth/drive"]

# اسم المجلد الثابت الشامل لكافة الوسائط داخل أوبسيديان
MEDIA_BASE_FOLDER_NAME = "Telegram_Media"

# ─────────────────────────────────────────────
# 4. إدارة اتصال Google Drive والمجلدات الهيكلية
# ─────────────────────────────────────────────
OAUTH_FILE  = os.path.join(BASE_DIR, "oauth_credentials.json")
TOKEN_FILE  = os.path.join(BASE_DIR, "token.json")

def get_drive_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        try: creds = Credentials.from_authorized_user_file(TOKEN_FILE, GDRIVE_SCOPES)
        except Exception: os.remove(TOKEN_FILE)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try: creds.refresh(Request())
            except Exception: os.remove(TOKEN_FILE); creds = None
        if not creds:
            flow = InstalledAppFlow.from_client_secrets_file(OAUTH_FILE, GDRIVE_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f: f.write(creds.to_json())

    return build("drive", "v3", credentials=creds)

def get_or_create_folder(service, folder_name: str, parent_id: str) -> str:
    """دالة عامة للبحث عن مجلد أو إنشائه داخل مجلد أب محدد."""
    query = f"name = '{folder_name}' and '{parent_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    results = service.files().list(q=query, fields="files(id)").execute()
    files = results.get("files", [])
    
    if files:
        return files[0]["id"]
    
    folder_metadata = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id]
    }
    sub_folder = service.files().create(body=folder_metadata, fields="id").execute()
    log.info(f"📁 تم إنشاء مجلد جديد باسم: {folder_name}")
    return sub_folder.get("id")

def get_target_subfolder_id(service, base_media_folder_id: str, mime_type: str) -> str:
    """تحديد المجلد الفرعي المناسب (Photos, Videos, Documents, Audio) بناءً على نوع الملف."""
    if mime_type.startswith("image/"):
        sub_folder_name = "Photos"
    elif mime_type.startswith("video/"):
        sub_folder_name = "Videos"
    elif mime_type.startswith("audio/") or mime_type.startswith("voice/"):
        sub_folder_name = "Audio"
    else:
        sub_folder_name = "Documents" # للملفات المضغوطة، الـ PDF، وخلافه
        
    return get_or_create_folder(service, sub_folder_name, base_media_folder_id)

def upload_media_to_drive(service, file_bytes: bytes, filename: str, mime_type: str, target_folder_id: str) -> bool:
    try:
        file_metadata = {"name": filename, "parents": [target_folder_id]}
        media = MediaInMemoryUpload(file_bytes, mimetype=mime_type, resumable=True)
        
        file = service.files().create(
            body=file_metadata,
            media_body=media,
            fields="id"
        ).execute()
        
        log.info(f"✅ تم رفع الملف: '{filename}' بنجاح.")
        return True
    except Exception as e:
        log.error(f"❌ خطأ أثناء رفع الملف '{filename}': {e}")
        return False

# ─────────────────────────────────────────────
# 5. استخراج تفاصيل الميديا ونوع الـ Mime لقناة تيليجرام
# ─────────────────────────────────────────────
def get_media_details(msg) -> tuple:
    filename = f"media_{msg.id}"
    mime_type = "application/octet-stream"

    if msg.file:
        if msg.file.name:
            filename = msg.file.name
        else:
            ext = msg.file.ext if msg.file.ext else ""
            filename = f"media_{msg.id}{ext}"
        if msg.file.mime_type:
            mime_type = msg.file.mime_type

    return filename, mime_type

# ─────────────────────────────────────────────
# 6. إدارة الحالة والمنطق الرئيسي
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

    # 1. جلب أو إنشاء المجلد الرئيسي الثابت للوسائط
    base_media_folder_id = get_or_create_folder(service, MEDIA_BASE_FOLDER_NAME, MAIN_FOLDER_ID)

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

            if not msg.media:
                ch_info["last_processed_message_id"] = msg.id
                continue

            log.info(f"🎬 جاري معالجة وسائط الرسالة رقم ID {msg.id} ...")
            filename, mime_type = get_media_details(msg)

            # 2. تحديد المجلد الفرعي النوعي المناسب تلقائياً (Photos, Videos، إلخ) داخل المجلد الرئيسي
            target_subfolder_id = get_target_subfolder_id(service, base_media_folder_id, mime_type)

            try:
                file_buffer = io.BytesIO()
                await client.download_media(msg.media, file_buffer)
                file_bytes = file_buffer.getvalue()

                if len(file_bytes) > 0:
                    success = upload_media_to_drive(service, file_bytes, filename, mime_type, target_subfolder_id)
                    if success:
                        files_uploaded += 1
                        time.sleep(1)
                else:
                    log.warning(f"⚠️ الملف في الرسالة {msg.id} فارغ.")

            except Exception as media_err:
                log.error(f"❌ خطأ أثناء تحميل الوسائط للرسالة {msg.id}: {media_err}")

            ch_info["last_processed_message_id"] = msg.id
            save_state(state)

    except Exception as e:
        log.error(f"❌ خطأ عام أثناء معالجة الرسائل: {e}")
    finally:
        await client.disconnect()
        log.info("🔌 تم قطع الاتصال وإنهاء الجلسة.")

if __name__ == "__main__":
    asyncio.run(main())
