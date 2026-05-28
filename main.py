import os
import json
import asyncio
import logging
import requests
import time
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import Channel
from google.oauth2 import service_account
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
# 1. قراءة والتحقق من المتغيرات البيئية من GitHub Secrets
# ─────────────────────────────────────────────
def get_required_env(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise EnvironmentError(f"❌ المتغير البيئي '{key}' غير موجود في GitHub Secrets.")
    return value

API_ID          = int(get_required_env("TELEGRAM_API_ID"))
API_HASH        = get_required_env("TELEGRAM_API_HASH")
SESSION_STRING  = get_required_env("TELEGRAM_SESSION")
OPENROUTER_KEY  = get_required_env("OPENROUTER_API_KEY")
FOLDER_ID       = get_required_env("GDRIVE_FOLDER_ID")
GDRIVE_JSON     = json.loads(get_required_env("GDRIVE_CREDENTIALS"))

# ─────────────────────────────────────────────
# 2. الإعدادات العامة
# ─────────────────────────────────────────────
STATE_FILE       = "channels_state.json"
GDRIVE_SCOPES    = ["https://www.googleapis.com/auth/drive"]

# ─────────────────────────────────────────────
# 3. دالة الاتصال بـ OpenRouter
# ─────────────────────────────────────────────
def process_with_openrouter(text: str, retries: int = 3, delay: int = 5) -> dict:
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com",
        "X-Title": "Obsidian Unlimited Multi-Channel Automation"
    }

    system_prompt = (
        "أنت مساعد ذكي متخصص في إدارة المعرفة لبرنامج Obsidian. مهمتك فرز Motes.\n"
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
        "model": "google/gemini-2.5-flash",
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
            
            if ai_reply.startswith("```json"):
                ai_reply = ai_reply.replace("```json", "").replace("```", "").strip()
            elif ai_reply.startswith("```"):
                ai_reply = ai_reply.replace("```", "").strip()

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

    return {"important": False}

# ─────────────────────────────────────────────
# 4. دالة رفع الملف إلى Google Drive
# ─────────────────────────────────────────────
def upload_to_drive(title: str, content: str) -> bool:
    try:
        creds = service_account.Credentials.from_service_account_info(GDRIVE_JSON, scopes=GDRIVE_SCOPES)
        creds.refresh(Request())
        service = build("drive", "v3", credentials=creds)

        file_metadata = {"name": f"{title}.md", "parents": [FOLDER_ID]}
        media = MediaInMemoryUpload(content.encode("utf-8"), mimetype='text/markdown')
        
        file = service.files().create(body=file_metadata, media_body=media, fields='id').execute()
        log.info(f"✅ تم رفع الملف: '{title}.md' (ID: {file.get('id')})")
        return True
    except Exception as e:
        log.error(f"❌ خطأ أثناء الرفع إلى Drive: {e}")
    return False

# ─────────────────────────────────────────────
# 5. حفظ وتحميل الحالة
# ─────────────────────────────────────────────
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            log.warning(f"⚠️ تعذّر قراءة ملف الحالة، سيتم البدء من جديد: {e
