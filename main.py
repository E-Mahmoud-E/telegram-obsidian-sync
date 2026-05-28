import os
import json
import requests
import time
from telethon import TelegramClient
from telethon.sessions import StringSession
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload

# 1. إعداد المتغيرات الأمنية من بيئة جيت هاب
API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
SESSION_STRING = os.environ["TELEGRAM_SESSION"]
OPENROUTER_KEY = os.environ["OPENROUTER_API_KEY"]
FOLDER_ID = os.environ["GDRIVE_FOLDER_ID"]
GDRIVE_JSON = json.loads(os.environ["GDRIVE_CREDENTIALS"])

STATE_FILE = "channels_state.json"

# القناة المستهدفة
TARGET_CHANNEL = "@elmin7a" 

# 2. دالة الاتصال بـ OpenRouter مع ميزة إعادة المحاولة التلقائية عند الفشل
def process_with_openrouter(text, retries=3, delay=5):
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
        "model": "openrouter/owl-alpha", 
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text}
        ]
    }
    
    for attempt in range(retries):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=30)
            res_data = response.json()
            
            if 'error' in res_data:
                print(f"⚠️ OpenRouter Error (Attempt {attempt+1}/{retries}): {res_data['error'].get('message')}")
                time.sleep(delay)
                continue
                
            ai_reply = res_data['choices'][0]['message']['content'].strip()
            
            if ai_reply.startswith("```json"):
                ai_reply = ai_reply.replace("```json", "").replace("```", "").strip()
            elif ai_reply.startswith("```"):
                ai_reply = ai_reply.replace("```", "").strip()
                
            return json.loads(ai_reply)
            
        except Exception as e:
            print(f"⚠️ Connection Error (Attempt {attempt+1}/{retries}): {e}")
            time.sleep(delay)
            
    print("❌ Failed to process message after all retries.")
    return {"important": False}

# 3. دالة رفع الملف إلى Google Drive (المحدثة والمصلحة لأخطاء الـ Token)
def upload_to_drive(title, content):
    # استخدام النطاق الأوسع والأشمل لحل مشكلة No access token
    SCOPES = ["[https://www.googleapis.com/auth/drive](https://www.googleapis.com/auth/drive)"]
    
    try:
        # بناء التوثيق بشكل صريح وإجباري مع النطاق الصحيح
        creds = service_account.Credentials.from_service_account_info(GDRIVE_JSON, scopes=SCOPES)
        service = build('drive', 'v3', credentials=creds)
        
        file_metadata = {
            'name': f"{title}.md",
            'parents': [FOLDER_ID]
        }
        
        media = MediaInMemoryUpload(content.encode('utf-8'), mimetype='text/markdown')
        
        # تنفيذ أمر الإنشاء والرفع
        file = service.files().create(body=file_metadata, media_body=media, fields='id').execute()
        print(f" Successfully uploaded to Drive: '{title}.md' (File ID: {file.get('id')})")
        return True
    except Exception as e:
        print(f"❌ Error uploading to Drive: {e}")
        return False

# 4. المنطق الرئيسي المستمر
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
    
    try:
        entity = await client.get_entity(TARGET_CHANNEL)
        channel_id = str(entity.id)
        channel_title = entity.title
        print(f"🎯 Target Channel Connected: {channel_title} (ID: {channel_id})")
    except Exception as e:
        print(f"❌ Access Denied to channel {TARGET_CHANNEL}: {e}")
        await client.disconnect()
        return

    if channel_id not in state:
        state[channel_id] = {
            "channel_name": channel_title,
            "last_processed_message_id": 0
        }
    
    ch_info = state[channel_id]
    last_id = ch_info['last_processed_message_id']
    
    print(f"⏳ Syncing archive starting from message ID: {last_id}...")
    state_updated = False
    
    try:
        # جلب 50 رسالة بالتوالي في الدورة الواحدة من الأقدم للأحدث
        messages = await client.get_messages(entity, min_id=last_id, limit=50, reverse=True)
        
        for msg in messages:
            if msg.text and len(msg.text.strip()) > 5:
                print(f"\n🎬 Processing message ID {msg.id}...")
                
                # 1. معالجة عبر الذكاء الاصطناعي والانتظار
                ai_result = process_with_openrouter(msg.text)
                
                if ai_result.get("important") and ai_result.get("title"):
                    # 2. الرفع الآمن على Drive والانتظار
                    success = upload_to_drive(ai_result["title"], ai_result["content"])
                    
                    if success:
                        print("⏳ Sleeping for 3 seconds before next message...")
                        time.sleep(3)
                else:
                    print(f" Message ID {msg.id} marked as Unimportant/Ad and skipped.")
                    
                # تحديث ملف التتبع فوراً لكل رسالة تنتهي بنجاح
                ch_info['last_processed_message_id'] = msg.id
                state_updated = True
                
    except Exception as e:
        print(f"❌ Error during historical loop: {e}")
        
    await client.disconnect()
    
    if state_updated:
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        print("\n💾 Progress saved successfully in channels_state.json.")

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
