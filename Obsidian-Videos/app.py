import os
import sys
import json
import requests
import yt_dlp
import io
import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload, MediaIoBaseDownload

# --- 1. قراءة المتغيرات البيئية من GitHub Secrets ---
def get_required_env(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise EnvironmentError(f"❌ المتغير البيئي '{key}' غير موجود أو فارغ.")
    return value

API_ID          = int(get_required_env("TELEGRAM_API_ID"))
API_HASH        = get_required_env("TELEGRAM_API_HASH")
SESSION_STRING  = get_required_env("TELEGRAM_SESSION")      # سطر الجلسة الذكي الخاص بك
OPENROUTER_KEY  = get_required_env("OPENROUTER_API_KEY")
DRIVE_FOLDER_ID = get_required_env("DRIVE_FOLDER_ID")       # سيقرأ القيمة الممررة من DRIVE_FOLDER_ID2

MODEL_NAME = "google/gemini-2.5-flash" 
STATE_FILE_NAME = "last_id.txt"

# --- 2. إدارة اتصال ومستندات Google Drive ---
def get_drive_service():
    # معالجة التوكن القادم من السيكرتس كـ JSON صحيح
    gdrive_token_env = os.getenv("DRIVE_TOKEN_JSON")
    if gdrive_token_env:
        try:
            token_data = json.loads(gdrive_token_env)
            creds = Credentials.from_authorized_user_info(token_data, ['https://www.googleapis.com/auth/drive'])
            return build("drive", "v3", credentials=creds)
        except Exception as e:
            print(f"❌ خطأ في تحليل JSON الخاص بتوكن جوجل درايف: {e}")
            sys.exit(1)
    else:
        print("❌ خطأ: لم يتم العثور على المتغير DRIVE_TOKEN_JSON")
        sys.exit(1)

# جلب رقم آخر رسالة تم تحليلها من الدرايف
def get_last_processed_id(service):
    try:
        results = service.files().list(
            q=f"'{DRIVE_FOLDER_ID}' in parents and name='{STATE_FILE_NAME}' and trashed=false",
            fields="files(id)"
        ).execute()
        files = results.get('files', [])
        
        if not files:
            return 0 
            
        file_id = files[0]['id']
        request = service.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while done is False:
            status, done = downloader.next_chunk()
        return int(fh.getvalue().decode('utf-8').strip())
    except Exception as e:
        print(f"تنبيه: تعذر قراءة ملف الحالة، سيتم البدء من 0. السبب: {e}")
        return 0

# تحديث ملف الحالة بالـ ID الجديد في الدرايف
def update_last_processed_id(service, last_id):
    file_metadata = {'name': STATE_FILE_NAME, 'parents': [DRIVE_FOLDER_ID]}
    media = MediaInMemoryUpload(str(last_id).encode('utf-8'), mimetype='text/plain')
    
    results = service.files().list(
        q=f"'{DRIVE_FOLDER_ID}' in parents and name='{STATE_FILE_NAME}' and trashed=false",
        fields="files(id)"
    ).execute()
    files = results.get('files', [])
    
    if files:
        service.files().update(fileId=files[0]['id'], media_body=media).execute()
    else:
        service.files().create(body=file_metadata, media_body=media).execute()

# رفع ملف الملاحظة بصيغة Markdown
def upload_to_drive(service, filename, content):
    file_metadata = {'name': f"{filename}.md", 'parents': [DRIVE_FOLDER_ID]}
    media = MediaInMemoryUpload(content.encode('utf-8'), mimetype='text/markdown')
    service.files().create(body=file_metadata, media_body=media, fields='id').execute()

# --- 3. استخراج بيانات الفيديو وتحليله ---
def get_video_transcript(url):
    ydl_opts = {'format': 'bestaudio/best', 'skip_download': True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        return f"عنوان الفيديو: {info.get('title', 'بدون عنوان')}\nالوصف: {info.get('description', 'لا يوجد وصف')}"

def analyze_with_openrouter(text_content):
    prompt = (
        "أنت مساعد محترف في إدارة المعرفة لبرنامج Obsidian.\n"
        "قم بتحليل هذا المحتوى المستخرج من فيديو واستخرج الأفكار والخطوات والنصائح "
        "في ملف Markdown (.md) منظم ونظيف مع الـ Tags المناسبة.\n\n"
        f"المحتوى:\n{text_content}"
    )
    response = requests.post(
        url="https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json"},
        json={"model": MODEL_NAME, "messages": [{"role": "user", "content": prompt}]}
    )
    if response.status_code == 200:
        return response.json()['choices'][0]['message']['content']
    raise Exception(f"خطأ OpenRouter: {response.text}")

# --- 4. المنطق الأساسي للتشغيل المجدول ---
async def main():
    service = get_drive_service()
    last_id = get_last_processed_id(service)
    print(f"🔍 آخر ID تم معالجته ومخزن في الدرايف هو: {last_id}")

    # استخدام الاتصال النصي الصامت المباشر لحسابك الشخصي
    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    await client.connect()

    if not await client.is_user_authorized():
        print("❌ خطأ: سلسلة الجلسة TELEGRAM_SESSION غير صالحة أو منتهية.")
        await client.disconnect()
        return

    new_messages = []
    # جلب آخر رسائل من القناة وفلترتها
    async for message in client.iter_messages('Links', limit=50):
        if message.text and message.text.strip().startswith("http") and message.id > last_id:
            new_messages.append(message)
    
    new_messages.reverse() # الترتيب من الأقدم للأحدث
    
    if not new_messages:
        print("✨ لا توجد روابط جديدة لمعالجتها حالياً.")
        await client.disconnect()
        return

    highest_id = last_id
    for message in new_messages:
        url = message.text.strip()
        print(f"🔄 جاري معالجة الرابط الجديد [ID: {message.id}]: {url}")
        try:
            video_data = get_video_transcript(url)
            markdown_output = analyze_with_openrouter(video_data)
            
            filename = f"Note_{message.id}"
            upload_to_drive(service, filename, markdown_output)
            highest_id = max(highest_id, message.id)
            print(f"✅ تم حفظ الملاحظة بنجاح في Google Drive.")
        except Exception as e:
            print(f"❌ خطأ أثناء المعالجة: {e}")
    
    # تحديث الـ ID الأخير في ملف الحالة
    update_last_processed_id(service, highest_id)
    print(f"💾 تم تحديث ملف الحالة في الدرايف إلى الـ ID: {highest_id}")
    
    await client.disconnect()

if __name__ == '__main__':
    asyncio.run(main())
