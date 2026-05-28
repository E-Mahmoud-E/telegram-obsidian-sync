import os
import json
import requests
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import Channel
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload

# 1. إعداد المتغيرات من بيئة جيت هاب الأمنية
API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
SESSION_STRING = os.environ["TELEGRAM_SESSION"]
OPENROUTER_KEY = os.environ["OPENROUTER_API_KEY"]
FOLDER_ID = os.environ["GDRIVE_FOLDER_ID"]
GDRIVE_JSON = json.loads(os.environ["GDRIVE_CREDENTIALS"])

STATE_FILE = "channels_state.json"

# 2. دالة الاتصال بـ OpenRouter (نسخة مطورة ومحمية من الأخطاء)
def process_with_openrouter(text):
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_KEY}",
        "Content-Type": "application/json"
    }
    
    system_prompt = (
        "أنت مساعد ذكي متخصص في إدارة المعرفة لبرنامج Obsidian. مهمتك فرز الملاحظات.\n"
        "قيم الرسالة التالية: إذا كانت إعلاناً، أو رسالة ترويجية، أو تافهة ولا تقدم قيمة معرفية، اجعل حقل 'important' يساوي false.\n"
        "إذا كانت مهمة وقيمة، قم بصياغتها وتنسيقها بلغة Markdown احترافية (عناوين، نقاط، وسوم مناسبة للسياق).\n"
        "ابتكر عنواناً مختصراً جداً يصف المحتوى (3-5 كلمات كحد أقصى) وبدون أي رموز خاصة تمنع حفظ الملفات مثل (\\, /, :, *, ?, \", <, >, |).\n"
        "يجب أن يكون ردك بصيغة JSON حصراً كالتالي:\n"
        "{\n"
        "  \"important\": true,\n"
        "  \"title\": \"العنوان المختصر هنا\",\n"
        "  \"content\": \"المحتوى المنسق بالكامل بـ Markdown هنا\"\n"
        "}"
    )
    
    payload = {
        "model": "meta-llama/llama-3-8b-instruct:free", # نموذج مجاني تماماً ومستقر لتفادي خطأ الرصيد
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text}
        ]
    }
    
    try:
        response = requests.post(url, headers=headers, json=payload)
        res_data = response.json()
        
        # حماية في حال أرسل OpenRouter خطأ في الحساب أو الرصيد
        if 'error' in res_data:
            print(f"❌ OpenRouter API Error Message: {res_data['error'].get('message')}")
            return {"important": False}
            
        if 'choices' not in res_data:
            print(f"❌ Unexpected OpenRouter Response structure: {res_data}")
            return {"important": False}
            
        ai_reply = res_data['choices'][0]['message']['content'].strip()
        
        # تنظيف علامات الاقتباس البرمجية إذا أضافها النموذج تلقائياً
        if ai_reply.startswith("```json"):
            ai_reply = ai_reply.replace("```json", "").replace("```", "").strip()
        elif ai_reply.startswith("```"):
            ai_reply = ai_reply.replace("```", "").strip()
            
        return json.loads(ai_reply)
    except json.JSONDecodeError:
        print(f"⚠️ Failed to parse AI reply as JSON. Raw reply was: {ai_reply}")
        return {"important": False}
    except Exception as e:
        print(f"❌ General Error in OpenRouter processing: {e}")
        return {"important": False}

# 3. دالة رفع الملف إلى Google Drive
def upload_to_drive(title, content):
    creds = Credentials.from_service_account_info(GDRIVE_JSON, scopes=["[https://www.googleapis.com/auth/drive.file](https://www.googleapis.com/auth/drive.file)"])
    service = build('drive', 'v3', credentials=creds)
    
    file_metadata = {
        'name': f"{title}.md",
        'parents': [FOLDER_ID]
    }
    media = MediaInMemoryUpload(content.encode('utf-8'), mimetype='text/markdown')
    
    try:
        file = service.files().create(body=file_metadata, media_body=media, fields='id').execute()
        print(f" Successfully uploaded: {title}.md (ID: {file.get('id')})")
    except Exception as e:
        print(f"Error uploading to Drive: {e}")

# 4. المنطق الرئيسي لتشغيل البوت سحابياً والتعرف التلقائي
async def main():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            try:
                state = json.load(f)
            except json.JSONDecodeError:
                state = {}
    else:
        state = {}
        
    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    await client.connect()
    
    print("Scanning your Telegram account for channels...")
    state_updated = False
    
    async for dialog in client.iter_dialogs():
        if isinstance(dialog.entity, Channel) and dialog.entity.broadcast:
            channel_id = str(dialog.entity.id)
            channel_title = dialog.title
            
            if channel_id not in state:
                print(f" New channel detected and added: {channel_title} (ID: {channel_id})")
                state[channel_id] = {
                    "channel_name": channel_title,
                    "last_processed_message_id": 0
                }
                state_updated = True
                
            ch_info = state[channel_id]
            last_id = ch_info['last_processed_message_id']
            
            print(f"Checking messages for: {channel_title} (From ID: {last_id})")
            
            try:
                # جلب آخر 10 رسائل فقط في الدورة الواحدة لتجنب الضغط على الـ API وحظر الحساب
                messages = await client.get_messages(dialog.entity, min_id=last_id, limit=10, reverse=True)
                
                for msg in messages:
                    if msg.text:
                        print(f"Processing message ID {msg.id} in {channel_title}...")
                        ai_result = process_with_openrouter(msg.text)
                        
                        if ai_result.get("important") and ai_result.get("title"):
                            upload_to_drive(ai_result["title"], ai_result["content"])
                        else:
                            print(f"Message ID {msg.id} skipped (Unimportant/Ad/Info).")
                            
                        ch_info['last_processed_message_id'] = msg.id
                        state_updated = True
            except Exception as e:
                print(f"Error pulling messages from {channel_title}: {e}")
                
    await client.disconnect()
    
    if state_updated:
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        print("Channels state updated successfully.")

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
