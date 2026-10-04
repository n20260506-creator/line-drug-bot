import os
import io
import time
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

# 引入 Google 最新官方 GenAI 套件
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

# ==================== [RAG 慢性病知識庫預載 (純文字檔)] ====================
RAG_KNOWLEDGE_BASE = ""
TXT_PATH = "drug_guide.txt"

try:
    if os.path.exists(TXT_PATH):
        with open(TXT_PATH, "r", encoding="utf-8") as f:
            RAG_KNOWLEDGE_BASE = f.read()
        print(f"[RAG系統] ➔ 成功載入慢性病藥品知識庫 TXT，共讀取 {len(RAG_KNOWLEDGE_BASE)} 字。")
    else:
        print(f"[RAG系統] ⚠️ 找不到 {TXT_PATH}，將使用通用醫學知識進行辨識。")
except Exception as e:
    print(f"[RAG系統] ❌ 讀取 TXT 知識庫失敗: {e}")

# 讀取環境變數
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
GOOGLE_API_KEY = os.environ.get('GOOGLE_API_KEY')

# 初始化 LINE SDK
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# 初始化 Google GenAI Client
ai_client = genai.Client(api_key=GOOGLE_API_KEY)

# ==================== [核心升級：AI 系統指令 (高精準比對)] ====================
SYSTEM_INSTRUCTION = """
你是一位擁有極高辨識精準度的「慢性病藥丸與藥袋審核專家」。
使用者上傳的照片可能是「藥袋」或「藥丸/藥片/膠囊」。

請嚴格執行以下規則：

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

【情況 B：如果是藥丸/藥片/膠囊照片（雙重檢驗原則）】
1. 第一步（特徵擷取）：請仔細辨認照片中藥丸正反兩面的「刻字、英數代碼、標記、切痕」、幾何形狀（八邊形、心形、橢圓、圓形雙凸）與顏色。
2. 第二步（檢索對照）：嚴格比對提供的【常見慢性病藥品參考知識庫】。
   - ⚠️ 關鍵判斷：比對的黃金標準是「表面刻字標記」與「特殊形狀」。若刻字代碼不相符，絕對不可張冠李戴！
3. 🚨 終止條件：
   - 若照片中的刻字或外觀在知識庫中找不到相符的項目，請「嚴格且唯一」回傳以下這行文字，切勿猜測、切勿輸出任何其他內容：
   此藥丸非慢性病用藥，無法偵測
4. 成功比對輸出格式：
💊 【慢性病藥丸辨識結果】
━━━━━━━━━━━━━━━━━━
【可能藥品名稱】：
【外觀與刻字描述】：(請詳列您在照片中觀察到的刻字與形狀，並說明與手冊吻合之處)
【慢性病分類】：
【主要適應症/用途】：
【一般常見用法】：
【服用注意事項與警語】：
━━━━━━━━━━━━━━━━━━
💡 警語：單憑外觀辨識藥丸具備風險。本系統僅比對「常見慢性病指引用藥」，切勿盲目服用未知藥丸！若無法確認，請務必諮詢醫師或實體藥局。

通用規定事項：
- 嚴禁輸出任何客套話、問候語。
"""

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
        
        reply_text = f"語言已設定為：{LANGUAGE_MAP.get(lang_code, '繁體中文')}"
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
    user_id = event.source.user_id
    
    timer_options = {
        "5分鐘後提醒我": {"seconds": 300, "text": "5"},
        "10分鐘後提醒我": {"seconds": 600, "text": "10"},
        "15分鐘後提醒我": {"seconds": 900, "text": "15"}
    }
    
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        
        if user_message in timer_options:
            selected_option = timer_options[user_message]
            delay_seconds = selected_option["seconds"]
            minutes_text = selected_option["text"]
            
            try:
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text=f"⏰ 好的！已為您設定 {minutes_text} 分鐘後的吃藥提醒。")]
                    )
                )
                
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
            
            # 1. 下載圖片 binary 資料
            message_content = line_bot_blob_api.get_message_content(message_id)
            image_bytes = io.BytesIO()
            if hasattr(message_content, 'iter_content'):
                for chunk in message_content.iter_content():
                    image_bytes.write(chunk)
            else:
                image_bytes.write(message_content if isinstance(message_content, bytes) else message_content.read())
            
            image_bytes.seek(0)
            print("[系統] ➔ 成功下載圖片。")
            
            # 2. 高解析度壓縮優化（放寬至 1200x1200，品質 85，清晰保留藥丸細微刻字）
            raw_img = Image.open(image_bytes)
            raw_img.thumbnail((1200, 1200))
            
            compressed_io = io.BytesIO()
            raw_img.convert("RGB").save(compressed_io, format="JPEG", quality=85)
            compressed_io.seek(0)
            img = Image.open(compressed_io)
            
            image_bytes.close()  # 釋放大圖記憶體
            print("[系統] ➔ 高清特徵保留壓縮成功，正在傳送給 Gemini AI 進行精確比對...")
            
            # 3. 根據語言與純文字 RAG 知識庫生成 Prompt
            user_id = event.source.user_id
            lang = user_language_prefs.get(user_id, "zh")
            target_lang = LANGUAGE_MAP.get(lang, "繁體中文")
            
            prompt_content = f"""
請判斷這張照片是「藥袋」還是「藥丸/藥片/膠囊」。

【常見慢性病藥品參考知識庫 (RAG Knowledge Base)】：
{RAG_KNOWLEDGE_BASE}

【藥丸深度辨識步驟（請依序比對）】：
1. 觀察刻字細節：仔細放大檢查藥丸表面是否有英文字母、數字（如 AML 5, NVR, NV, C15, 500, 277, NMK, S10, ZD4522, 10, ATV, 20, YSP, BKCP, 75, 1171, 80, 100, 0.2 等）或切痕。
2. 比對形狀與顏色：八角形、圓形雙凸、長橢圓形、心形，或是雙色膠囊。
3. 嚴格檢索比對：
   - 只有在「表面刻字」與「外觀」均能精確匹配上方知識庫中的某一項藥物時，才回傳【慢性病藥丸辨識結果】。
   - ⚠️ 若照片中的刻字與知識庫不符，或無法在知識庫中找到對應藥物，請停止分析，直接且僅回傳：
     此藥丸非慢性病用藥，無法偵測
4. 請全程使用「{target_lang}」語言回覆。
"""
            
            # 4. 呼叫 Gemini (使用 gemini-2.5-flash 與 gemini-1.5-flash 備援)
            candidate_models = [
                'gemini-2.5-flash',
                'gemini-1.5-flash'
            ]
            response = None
            last_error = None

            for model_name in candidate_models:
                try:
                    print(f"[系統] ➔ 嘗試使用模型 {model_name}...")
                    response = ai_client.models.generate_content(
                        model=model_name,
                        contents=[img, prompt_content],
                        config=types.GenerateContentConfig(
                            system_instruction=SYSTEM_INSTRUCTION,
                            temperature=0.1  # 調低隨機性，讓 OCR 與知識庫匹配更精準嚴謹
                        ),
                    )
                    if response:
                        break
                except Exception as err:
                    last_error = err
                    print(f"⚠️ [{model_name} 呼叫異常，立即切換備援] ➔ {err}")
                    time.sleep(1)

            if not response:
                raise last_error

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
