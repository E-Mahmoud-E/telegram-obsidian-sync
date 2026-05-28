import os
import json
import requests
import time
from telethon import TelegramClient
from telethon.sessions import StringSession
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

# ⭐ ضع هنا معرف القناة التي تريد التركيز عليها فقط (يمكنك وضع الرابط العام مثل '@اسم_القناة')
# أو إذا كانت قناة خاصة ضع رقم الـ ID الخاص بها مباشرة (بدون علامات تنصيص إذا كان رقماً)
TARGET_CHANNEL = "المنحة ELMIN7A" 

# 2. دالة الاتصال بـ OpenRouter لفرز وتعديل وتسمية المحتوى
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
        "model": "meta-llama/llama-3-8b-instruct:free", 
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text}
        ]
    }
    
    try:
        response = requests.post(url, headers=headers, json=payload)
        res_data = response.json()
        
        if 'error' in res_data:
            print(f"❌ OpenRouter API Error: {res_data['error'].get('message')}")
            return {"important": False}
            
        ai_reply = res_data['choices'][0]['message']['content'].strip()
        
        if ai_reply.startswith("```json"):
            ai_reply = ai_reply.replace("```json", "").replace("```", "").strip()
        elif ai_reply.startswith("```"):
            ai_reply = ai_reply.replace("```", "").strip()
            
        return json.loads(ai_reply)
    except Exception as e:
        print(f"❌ Error in OpenRouter processing: {e}")
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
        print(f" Successfully uploaded: {title}.md")
    except Exception as e:
        print(f"Error uploading to Drive: {e}")

# 4. المنطق الرئيسي للتركيز على قناة واحدة والسحب من البداية
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
    
    # الحصول على معلومات القناة المستهدفة لتأكيد وجودها ومعرفة اسمها الحقيقي
    try:
        entity = await client.get_entity(TARGET_CHANNEL)
        channel_id = str(entity.id)
        channel_title = entity.title
        print(f"🎯 Connected to Target Channel: {channel_title} (ID: {channel_id})")
    except Exception as e:
        print(f"❌ Cannot find or access the channel {TARGET_CHANNEL}: {e}")
        await client.disconnect()
        return

    # إذا كانت القناة لم تسجل من قبل، نبدأ تتبعها من المعرف 0 (البداية تماماً)
    if channel_id not in state:
        state[channel_id] = {
            "channel_name": channel_title,
            "last_processed_message_id": 0
        }
    
    ch_info = state[channel_id]
    last_id = ch_info['last_processed_message_id']
    
    print(f"⏳ Fetching new messages from historical ID: {last_id} (Chronological order)...")
    state_updated = False
    
    try:
        # حددنا الحد بـ 30 رسالة في الدورة الواحدة لتجنب تخطي حظور الاستهلاك (Rate Limits) لـ OpenRouter و Google
        # الخيار reverse=True يضمن جلب الرسائل من الأقدم إلى الأحدث تصاعدياً
        messages = await client.get_messages(entity, min_id=last_id, limit=30, reverse=True)
        
        for msg in messages:
            if msg.text and len(msg.text.strip()) > 5: # تجاهل النصوص القصيرة جداً كالرموز التعبيرية
                print(f"Processing message ID {msg.id}...")
                ai_result = process_with_openrouter(msg.text)
                
                if ai_result.get("important") and ai_result.get("title"):
                    upload_to_drive(ai_result["title"], ai_result["content"])
                    # تهدئة العمل لثانية واحدة لتفادي الضغط على السيرفرات
                    time.sleep(1)
                else:
                    print(f"Message ID {msg.id} skipped.")
                    
                ch_info['last_processed_message_id'] = msg.id
                state_updated = True
                
    except Exception as e:
        print(f"❌ Error pulling messages: {e}")
        
    await client.disconnect()
    
    if state_updated:
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        print("💾 Channels state updated successfully.")

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
