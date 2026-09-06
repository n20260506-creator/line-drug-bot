import os
import io
import threading
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
    PushMessageRequest,
    TextMessage
)
from linebot.v3.webhooks import MessageEvent, ImageMessageContent, FollowEvent, PostbackEvent, TextMessageContent
from linebot.v3.messaging import TemplateMessage, ButtonsTemplate, PostbackAction

# 引入 Google 2026 最新官方 GenAI 套件
from google import genai
from google.genai import types

# --- 語言設定功能 ---
user_language_prefs = {} 
LANGUAGE_MAP = {
    "zh": "繁體中文",
    "en": "English",
    "id": "Bahasa Indonesia"
}

app = Flask(__name__)

# 讀取環境變數
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
GOOGLE_API_KEY = os.environ.get('GOOGLE_API_KEY')

# 初始化 LINE SDK
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# 初始化 Google GenAI Client
ai_client = genai.Client(api_key=GOOGLE_API_KEY)

# ==================== [核心升級：AI 系統指令] ====================
# 調整系統指令，讓 Gemini 具備「自動分類」與「藥丸識別」的能力
SYSTEM_INSTRUCTION = """
你是一位專業、嚴謹的醫療院所藥劑師。
請仔細分析使用者上傳的照片，照片可能是「藥袋」或「單顆/多顆藥丸（裸藥、排裝藥、罐裝藥）」。

請根據照片類型，嚴格依照以下規定的格式回傳資訊：

【情況 A：如果是藥袋照片】
必須嚴格遵守以下格式標籤：
📋 【藥袋辨識結果】
━━━━━━━━━━━━━━━━━━
【藥品名稱】：
【適應症/用途】：
【用法用量】：
【副作用】：
【注意事項】：
━━━━━━━━━━━━━━━━━━
💡 提示：本系統辨識結果僅供參考，用藥前請務必再次核對藥袋，並遵照醫囑。

【情況 B：如果是藥丸/藥片/膠囊照片】
必須嚴格識別外觀特徵（外觀、顏色、形狀、刻字/標記），並尋找可能的藥品匹配。遵守以下格式標籤：
💊 【藥丸外觀辨識結果】
━━━━━━━━━━━━━━━━━━
【可能藥品名稱】：(如果無法百分之百確定，可列出1-3個最可能的藥名並註明機率)
【外觀特徵描述】：(例如：白色圓形錠劑、一面刻有ABC、另一面有十字一字刻痕)
【主要適應症/用途】：
【一般常見用法】：
【服用注意事項與副作用】：
━━━━━━━━━━━━━━━━━━
💡 警語：單憑外觀辨識藥丸具備高度風險。本結果僅供參考，請勿盲目服用未知藥丸！若無法確認，請諮詢實體藥局或醫師。

通用規定事項：
1. 必須根據照片種類，精確選擇對應的格式（藥袋或藥丸），不得混用。
2. 嚴禁包含任何額外的解釋、問候語或格式以外的文字。
3. 如果完全找不到該項資訊或無法識別，請填寫「無法明確辨識」。
"""
# 負責在 5 分鐘（300秒）後主動推播提醒的背景任務
def send_delay_reminder(user_id):
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        try:
            line_bot_api.push_message(
                PushMessageRequest(
                    to=user_id,
                    messages=[TextMessage(text="🔔 提醒您該吃藥囉！")]
                )
            )
            print(f"[提醒系統] ➔ 已成功發送提醒給用戶 {user_id}")
        except Exception as e:
            print(f"❌ [提醒發送失敗] ➔ {e}")

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(FollowEvent)
def handle_follow(event):
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TemplateMessage(
                    alt_text="請選擇您的語言",
                    template=ButtonsTemplate(
                        title="請選擇語言 / Please select language",
                        text="請選擇藥物資訊的顯示語言：",
                        actions=[
                            PostbackAction(label="繁體中文", data="lang=zh"),
                            PostbackAction(label="English", data="lang=en"),
                            PostbackAction(label="Bahasa Indonesia", data="lang=id")
                        ]
                    )
                )]
            )
        )

@handler.add(PostbackEvent)
def handle_postback(event):
    if event.postback.data.startswith("lang="):
        lang_code = event.postback.data.split("=")[1]
        user_id = event.source.user_id
        user_language_prefs[user_id] = lang_code
        
        reply_text = f"語言已設定為：{LANGUAGE_MAP.get(lang_code)}"
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply_text)]
                )
            )

@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    user_message = event.message.text.strip()
    user_id = event.source.user_id  # 取得用戶的 LINE ID，供 5、10、15 分鐘後推播使用
    
    # 建立「訊息文字」對應「秒數」與「回覆文字」的對照表
    # 5分鐘 = 300秒 / 10分鐘 = 600秒 / 15分鐘 = 900秒
    timer_options = {
        "5分鐘後提醒我": {"seconds": 300, "text": "5"},
        "10分鐘後提醒我": {"seconds": 600, "text": "10"},
        "15分鐘後提醒我": {"seconds": 900, "text": "15"}
    }
    
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        
        # 檢查用戶傳來的訊息有沒有在我們的對照表裡
        if user_message in timer_options:
            selected_option = timer_options[user_message]
            delay_seconds = selected_option["seconds"]
            minutes_text = selected_option["text"]
            
            try:
                # 1. 立刻回覆用戶確認訊息（確保 5 秒內回應，避免超時）
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text=f"⏰ 好的！已為您設定 {minutes_text} 分鐘後的吃藥提醒。")]
                    )
                )
                
                # 2. 根據對應的秒數，在背景啟動計時器，時間到就執行最上方設定好的 send_delay_reminder 函式
                threading.Timer(delay_seconds, send_delay_reminder, args=[user_id]).start()
                print(f"[系統] ➔ 已為用戶 {user_id} 建立 {minutes_text} 分鐘後的吃藥提醒。")
                
            except Exception as reply_error:
                print(f"❌ [發送即時確認失敗] ➔ {reply_error}")

@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image_message(event):
    with ApiClient(configuration) as api_client:
        line_bot_blob_api = MessagingApiBlob(api_client)
        line_bot_api = MessagingApi(api_client)
        
        try:
            print("\n[系統] ➔ 收到來自 LINE 的圖片訊息！開始處理...")
            message_id = event.message.id
            
            # 1. 下載圖片
            message_content = line_bot_blob_api.get_message_content(message_id)
            image_bytes = io.BytesIO()
            if hasattr(message_content, 'iter_content'):
                for chunk in message_content.iter_content():
                    image_bytes.write(chunk)
            else:
                image_bytes.write(message_content if isinstance(message_content, bytes) else message_content.read())
            
            image_bytes.seek(0)
            print("[系統] ➔ 成功下載圖片。")
            
            # 2. 轉換圖片格式（強制壓縮並釋放記憶體）
            with Image.open(image_bytes) as raw_img:
                raw_img.thumbnail((1024, 1024))
                compressed_io = io.BytesIO()
                raw_img.convert("RGB").save(compressed_io, format="JPEG", quality=75)
                compressed_io.seek(0)
                img = Image.open(compressed_io)
            
            image_bytes.close()  # 強制關閉大圖暫存，立刻把記憶體還給系統
            print("[系統] ➔ 圖片壓縮轉換成功，正在傳送給 Gemini AI 辨識...")
            
            # 3. 根據使用者選擇的語言動態生成 Prompt
            user_id = event.source.user_id
            lang = user_language_prefs.get(user_id, "zh")
            
            # 提示 AI 先判斷照片種類，再用指定語言回答
            prompt_content = (
                f"請先判斷這張照片是「藥袋」還是「藥丸/藥片/膠囊」。\n"
                f"接著，請全程使用「{LANGUAGE_MAP[lang]}」語言，並嚴格依照系統指令（System Instruction）中對應的格式標籤進行回覆。\n"
                f"如果是藥丸，請仔細放大觀察上面的刻字、顏色和形狀進行比對。"
            )
            
            # 4. 呼叫 Gemini 3.5 Flash
            response = ai_client.models.generate_content(
                model='gemini-3.5-flash',
                contents=[img, prompt_content],
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_INSTRUCTION
                )
            )
            
            result_text = response.text.strip()
            print("[系統] ➔ Gemini 辨識完成！準備回傳給 LINE。")
            
        except Exception as e:
            print(f"\n❌ [錯誤原因] ➔ {e}\n")
            result_text = "❌ 辨識失敗。可能原因：照片過於模糊、反光、或是 Google AI 連線超時。請重新拍攝並再試一次！"
            
        # 5. 回傳給使用者
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

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
