import os
import json
import logging
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

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
# 1. قراءة والتحقق من المتغيرات البيئية
# ─────────────────────────────────────────────
def get_required_env(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise EnvironmentError(f"❌ المتغير البيئي '{key}' غير موجود أو فارغ.")
    return value

MAIN_FOLDER_ID  = get_required_env("GDRIVE_FOLDER_ID")
GDRIVE_SCOPES    = ["https://www.googleapis.com/auth/drive"]
BASE_DIR         = os.path.dirname(os.path.abspath(__file__))

# اسم المجلد الرئيسي الذي نبحث داخله عن المكررات
MEDIA_BASE_FOLDER_NAME = "Telegram_Media"

# ─────────────────────────────────────────────
# 2. إدارة اتصال Google Drive عبر الـ Secret
# ─────────────────────────────────────────────
def get_drive_service():
    creds = None
    gdrive_token_env = os.getenv("GOOGLE_TOKEN_JSON")
    
    if gdrive_token_env:
        try:
            token_data = json.loads(gdrive_token_env)
            creds = Credentials.from_authorized_user_info(token_data, GDRIVE_SCOPES)
            log.info("🔐 تم تحميل صلاحيات Google Drive من الـ Secret بنجاح.")
        except Exception as e:
            log.error(f"❌ خطأ في تحميل التوكن من الـ Secret: {e}")
            
    local_token_file = os.path.join(BASE_DIR, "..", "token.json")
    if not creds and os.path.exists(local_token_file):
        try: creds = Credentials.from_authorized_user_file(local_token_file, GDRIVE_SCOPES)
        except Exception: pass

    if creds and creds.expired and creds.refresh_token:
        try: creds.refresh(Request())
        except Exception: creds = None

    if not creds:
        log.error("❌ لا تتوفر صلاحيات تشغيل Google Drive.")
        return None

    return build("drive", "v3", credentials=creds)

# ─────────────────────────────────────────────
# 3. منطق فحص وحذف الملفات المكررة
# ─────────────────────────────────────────────
def find_folder_id(service, folder_name: str, parent_id: str) -> str:
    """تبحث عن ID مجلد محدد باسمه داخل مجلد أب."""
    query = f"name = '{folder_name}' and '{parent_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    results = service.files().list(q=query, fields="files(id)").execute()
    files = results.get("files", [])
    return files[0]["id"] if files else None

def clean_duplicates_in_folder(service, folder_id: str, folder_name: str):
    """تفحص الملفات داخل مجلد محدد وتحذف المتطابقة في الاسم والحجم."""
    log.info(f"🔎 جاري فحص المجلد الفرعي: [{folder_name}] ...")
    
    # جلب جميع الملفات داخل المجلد مع أسمائها، أحجامها، وتاريخ إنشائها
    query = f"'{folder_id}' in parents and mimeType != 'application/vnd.google-apps.folder' and trashed = false"
    results = service.files().list(
        q=query, 
        fields="files(id, name, size, createdTime)",
        orderBy="createdTime" # الترتيب من الأقدم للأحدث لنضمن الحفاظ على النسخة الأولى
    ).execute()
    
    files = results.get("files", [])
    if not files:
        log.info(f"📭 المجلد [{folder_name}] فارغ تماماً.")
        return

    # قاموس لتتبع الملفات الفريدة المفتاح بتاعه: (اسم الملف، حجم الملف)
    seen_files = {}
    duplicates_count = 0

    for file in files:
        file_id = file.get("id")
        file_name = file.get("name")
        file_size = file.get("size") # الحجم بالبايت

        # إذا لم يكن للملف حجم (مجلد أو ملف مجهول) نتخطاه
        if file_size is None:
            continue

        # المفتاح الفريد للدمج والتحقق
        file_key = (file_name, file_size)

        if file_key in seen_files:
            # إذا وجدنا نفس الاسم والحجم مسبقاً، فهذه نسخة مكررة نقوم بنقلها لسلة المهملات
            log.info(f"🗑️ تم العثور على ملف مكرر: '{file_name}' (الحجم: {file_size} Bytes) -> جاري نقله للمهملات.")
            try:
                service.files().update(fileId=file_id, body={"trashed": True}).execute()
                duplicates_count += 1
            except Exception as e:
                log.error(f"❌ فشل نقل الملف {file_name} للمهملات: {e}")
        else:
            # إذا كانت أول مرة نرى فيها الملف، نسجله كنسخة أصلية نعتمد عليها
            seen_files[file_key] = file_id

    log.info(f"✨ انتهى فحص [{folder_name}]. تم التخلص من ({duplicates_count}) ملف مكرر.")

def main():
    service = get_drive_service()
    if not service:
        return

    # 1. البحث عن المجلد الرئيسي للوسائط Telegram_Media
    base_media_folder_id = find_folder_id(service, MEDIA_BASE_FOLDER_NAME, MAIN_FOLDER_ID)
    if not base_media_folder_id:
        log.warning(f"⚠️ لم يتم العثور على المجلد الشامل '{MEDIA_BASE_FOLDER_NAME}' في حسابك بعد. لا يوجد شيء لتنظيفه.")
        return

    # 2. جلب كافة المجلدات الفرعية الموجودة بداخله (Photos, Videos, Documents, Audio)
    query = f"'{base_media_folder_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    results = service.files().list(q=query, fields="files(id, name)").execute()
    sub_folders = results.get("files", [])

    if not sub_folders:
        log.info("ℹ️ لا توجد مجلدات فرعية داخل مجلد الوسائط لتنظيفها حالياً.")
        return

    # 3. تشغيل التنظيف على كل مجلد فرعي منفصلاً
    for folder in sub_folders:
        clean_duplicates_in_folder(service, folder["id"], folder["name"])

    log.info("🎉 اكتملت عملية تنظيف وتطهير المجلدات من الملفات المتطابقة تماماً بنجاح!")

if __name__ == "__main__":
    main()
      
