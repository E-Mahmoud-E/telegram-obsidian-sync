import os
import sys
import requests
import yt_dlp
from telethon import TelegramClient
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload, MediaIoBaseDownload
import io

# --- جلب الإعدادات السرية من مستودع GitHub ---
API_ID = int(os.environ['TELEGRAM_API_ID'])
API_HASH = os.environ['TELEGRAM_API_HASH']
OPENROUTER_API_KEY = os.environ['OPENROUTER_API_KEY']
DRIVE_FOLDER_ID = os.environ['DRIVE_FOLDER_ID'] # يقرأ المتغير الممرر من الـ yml

# معرّف النموذج الصحيح في OpenRouter (تم استخدام qwen كمثال من Alibaba أو يمكنك إبقاء gemini-2.5-flash)
MODEL_NAME = "Alibaba:HappyHorse 1.1" 
STATE_FILE_NAME = "last_id.txt"

# --- 1. الاتصال بجوجل درايف ---
def get_drive_service():
    if not os.path.exists('token.json') and 'DRIVE_TOKEN_JSON' in os.environ:
        with open('token.json', 'w') as f:
            f.write(os.environ['DRIVE_TOKEN_JSON'])
            
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', ['https://www.googleapis.com/auth/drive'])
        return build('drive', 'v3', credentials=creds)
    else:
        print("❌ خطأ: لم يتم العثور على صلاحيات Drive Token")
        sys.exit(1)

# دالة جلب رقم آخر رسالة معالجة من الدرايف
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

# دالة تحديث أو إنشاء ملف الرقم في الدرايف
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

# دالة رفع ملف الـ Markdown
def upload_to_drive(service, filename, content):
    file_metadata = {'name': f"{filename}.md", 'parents': [DRIVE_FOLDER_ID]}
    media = MediaInMemoryUpload(content.encode('utf-8'), mimetype='text/markdown')
    service.files().create(body=file_metadata, media_body=media, fields='id').execute()

# --- 2. سحب تفريغ الفيديو ---
def get_video_transcript(url):
    ydl_opts = {'format': 'bestaudio/best', 'skip_download': True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        return f"عنوان الفيديو: {info.get('title', 'بدون عنوان')}\nالوصف: {info.get('description', 'لا يوجد وصف')}"

# --- 3. المعالجة عبر OpenRouter ---
def analyze_with_openrouter(text_content):
    prompt = (
        "أنت مساعد محترف في إدارة المعرفة لبرنامج Obsidian.\n"
        "قم بتحليل هذا المحتوى المستخرج من فيديو واستخرج الأفكار والخطوات والنصائح "
        "في ملف Markdown (.md) منظم ونظيف مع الـ Tags المناسبة.\n\n"
        f"المحتوى:\n{text_content}"
    )
    response = requests.post(
        url="https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
        json={"model": MODEL_NAME, "messages": [{"role": "user", "content": prompt}]}
    )
    if response.status_code == 200:
        return response.json()['choices'][0]['message']['content']
    raise Exception(f"خطأ OpenRouter: {response.text}")

# --- 4. الدالة الأساسية ---
async def main():
    service = get_drive_service()
    last_id = get_last_processed_id(service)
    print(f"🔍 آخر ID تم معالجته سابقاً ومخزن في الدرايف هو: {last_id}")

    # الاعتماد على ملف الجلسة المشحون مسبقاً للحساب الشخصي
    async with TelegramClient('gh_user_session', API_ID, API_HASH) as client:
        # تسجيل الدخول المباشر دون طلب توكن بوت
        await client.connect()
        
        new_messages = []
        async for message in client.iter_messages('Links', limit=50):
            if message.text and message.text.strip().startswith("http") and message.id > last_id:
                new_messages.append(message)
        
        new_messages.reverse()
        
        if not new_messages:
            print("✨ لا توجد روابط جديدة لمعالجتها.")
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
                print(f"✅ تم حفظ الملف بنجاح.")
            except Exception as e:
                print(f"❌ خطأ أثناء المعالجة: {e}")
        
        update_last_processed_id(service, highest_id)
        print(f"💾 تم تحديث ملف الحالة في Google Drive إلى الـ ID الجديد: {highest_id}")

if __name__ == '__main__':
    import asyncio
    asyncio.run(main())
