import os
import io
from flask import Flask, request, abort
from PIL import Image

# 引入 LINE SDK
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    MessagingApiBlob,
    ReplyMessageRequest,
    TextMessage
)
from linebot.v3.webhooks import MessageEvent, ImageMessageContent

# 修正：引入 Google 2026 最新官方 GenAI 套件
from google import genai
from google.genai import types

app = Flask(__name__)


# 💡 正確寫法（告訴程式去讀取 Render 後台填寫的環境變數）
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
GOOGLE_API_KEY = os.environ.get('GOOGLE_API_KEY')
# ==========================================================

# 初始化 LINE SDK
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# 初始化 Google GenAI Client (將 API Key 直接帶入)
ai_client = genai.Client(api_key=GOOGLE_API_KEY)

# 系統指令：嚴格規範格式
SYSTEM_INSTRUCTION = """
你是一位專業、嚴謹的醫療院所藥劑師。
請仔細分析使用者上傳的藥袋照片，並「嚴格」依照以下指定的格式回傳資訊。

規定事項：
1. 必須嚴格遵守下方提供的格式標籤，不得自行修改標籤名稱。
2. 如果藥袋上完全找不到該項資訊，請填寫「未明確標示」。
3. 嚴禁包含任何額外的解釋、問候語或格式以外的文字。
"""

PROMPT_TEMPLATE = """
📋 【藥袋辨識結果】
━━━━━━━━━━━━━━━━━━
【藥品名稱】：
【適應症/用途】：
【用法用量】：
【副作用】：
【注意事項】：
━━━━━━━━━━━━━━━━━━
💡 提示：本系統辨識結果僅供參考，用藥前請務必再次核對藥袋，並遵照醫囑。
"""

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image_message(event):
    with ApiClient(configuration) as api_client:
        line_bot_blob_api = MessagingApiBlob(api_client)
        line_bot_api = MessagingApi(api_client)
        
       try:
            print("\n[系統] ➔ 收到來自 LINE 的圖片訊息！開始處理...")
            message_id = event.message.id
            
            # 1. 下載圖片（新版正確寫法：使用讀取或迭代器獲取 binary 資料）
            message_content = line_bot_blob_api.get_message_content(message_id)
            
            # 💡 【關鍵修改】用 BytesIO 把圖片內容完整寫入記憶體
            image_bytes = io.BytesIO()
            if hasattr(message_content, 'iter_content'):
                for chunk in message_content.iter_content():
                    image_bytes.write(chunk)
            else:
                # 備用方案：如果不是串流，直接讀取整個 body
                image_bytes.write(message_content if isinstance(message_content, bytes) else message_content.read())
            
            # ✨ 確保將讀取位置歸零，雲端環境（Linux）非常需要這行！
            image_bytes.seek(0)
            print("[系統] ➔ 成功下載圖片。")
            
            # 2. 轉換圖片格式
            img = Image.open(image_bytes)
            print("[系統] ➔ 圖片轉換成功，正在傳送給 Gemini AI 辨識...")
            
            # 3. 使用最新版套件呼叫 Gemini
            # 💡 提示：如果 gemini-2.5-flash 在你們的環境噴 404，可以改回 'models/gemini-1.5-flash'
            response = ai_client.models.generate_content(
                model='gemini-2.5-flash',
                contents=[img, f"請填寫以下這份表單，將藥袋中的資訊填入對應的括號中：\n{PROMPT_TEMPLATE}"],
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_INSTRUCTION
                )
            )
            result_text = response.text.strip()
            print("[系統] ➔ Gemini 辨識完成！準備回傳給 LINE。")
            
        except Exception as e:
            print(f"\n❌ [錯誤原因] ➔ {e}\n")
            result_text = "❌ 辨識失敗。可能原因：照片過於模糊、反光、或是 Google AI 連線超時。請重新拍攝並再試一次！"
            
        # 4. 回傳給使用者
        try:
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=result_text)]
                )
            )
            print("[系統] ➔ 成功將結果送回使用者的 LINE！")
        except Exception as reply_error:
            print(f"❌ [回傳失敗] ➔ {reply_error}")

if __name__ == "__main__":
    app.run(port=5000, debug=True)
