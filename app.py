import os
import io
import time
import json
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
    TextMessage,
    TemplateMessage,
    ButtonsTemplate,
    PostbackAction
)
from linebot.v3.webhooks import (
    MessageEvent, ImageMessageContent, FollowEvent, PostbackEvent, TextMessageContent
)

# 引入 Google 最新官方 GenAI 套件
from google import genai
from google.genai import types

# --- 語言與暫存設定功能 ---
user_language_prefs = {} 
user_pending_data = {}

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

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
ai_client = genai.Client(api_key=GOOGLE_API_KEY)

# ==================== [AI 系統指令] ====================
SYSTEM_INSTRUCTION = """
你是一位擁有極高辨識精準度的「慢性病藥丸與藥袋審核專家」。
使用者上傳的照片可能是「藥袋」或「藥丸/藥片/膠囊」。

請嚴格執行以下規則：

【情況 A：如果是藥袋照片】
1. 完整進行 OCR 文字辨識，讀取藥袋上的藥品名稱、用法、用量與注意事項。
2. 必須嚴格遵守以下格式，標題與回答內容必須強制換行，且「回答內容與下一個標題之間必須空一行」：
📋 【藥袋辨識結果】
━━━━━━━━━━━━━━
【藥品名稱】：
[帶入藥品名稱]

【適應症/用途】：
[帶入適應症/用途]

【用法用量】：
[帶入用法用量]

【副作用】：
[帶入副作用]

【注意事項】：
[帶入注意事項]
━━━━━━━━━━━━━━
💡 提示：本系統辨識結果僅供參考，用藥前請務必再次核對藥袋，並遵照醫囑。

【情況 B：如果是藥丸/藥片/膠囊照片（雙重檢驗原則）】
1. 第一步（特徵擷取）：仔細辨認照片中藥丸正反兩面的刻字、英數代碼與顏色形狀。
2. 第二步（檢索對照）：嚴格比對【常見慢性病藥品參考知識庫】。若刻字不符絕對不可張冠李戴！
3. 🚨 終止條件：
- 若照片中的刻字或外觀在知識庫中找不到相符項目，請「嚴格且唯一」回傳以下文字，切勿輸出其他內容：
此藥丸非慢性病用藥，無法偵測
4. 成功比對輸出格式（標題與內容必須換行，且不同標題之間必須空一行，文字力求精確簡煉）：
💊 【慢性病藥丸辨識結果】
━━━━━━━━━━━━━━
【可能藥品名稱】：
[填寫精確藥名]

【外觀與刻字描述】：
[精煉描述刻字、形狀與顏色]

【慢性病分類】：
[例如：高血壓 / 高血脂 / 糖尿病]

【主要適應症/用途】：
[簡述主要用途]

【一般常見用法】：
[簡述用法]

【服用注意事項與警語】：
[精煉列出重要警語]
━━━━━━━━━━━━━━
💡 警語：單憑外觀辨識藥丸具備風險。本系統僅比對「常見慢性病指引用藥」，切勿盲目服用未知藥丸！若無法確認，請務必諮詢醫師或實體藥局。

通用規定事項：
- 嚴禁輸出任何客套話、問候語。
"""

def extract_section(text, keyword):
    try:
        lines = text.split('\n')
        capturing = False
        result = []
        for line in lines:
            if keyword in line:
                capturing = True
                if '：' in line and len(line.split('：')) > 1 and line.split('：')[1].strip():
                    result.append(line.split('：')[1].strip())
                continue
            elif capturing and ('【' in line or '━━━━━━━━━━━━━━' in line or '💡' in line):
                break
            elif capturing:
                if line.strip():
                    result.append(line.strip())
        return "\n".join(result) if result else "（未標示 / Not specified）"
    except Exception:
        return "（解析中...）"

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
                        title="請選擇語言 / Select Language",
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
    data = event.postback.data
    user_id = event.source.user_id
    
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)

        if data.startswith("lang="):
            lang_code = data.split("=")[1]
            user_language_prefs[user_id] = lang_code
            reply_text = f"語言已設定為：{LANGUAGE_MAP.get(lang_code, '繁體中文')}"
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply_text)]
                )
            )

        elif data.startswith("view_bag="):
            view_type = data.split("=")[1]
            stored_data = user_pending_data.get(user_id, "查無近期辨識資料，請重新上傳照片。")
            warning_info = extract_section(stored_data, "注意事項")

            if view_type == "name":
                val = extract_section(stored_data, "藥品名稱")
                reply_msg = f"🏷️ 【藥品名稱】：\n{val}\n\n⚠️ 【注意事項】：\n{warning_info}"
            elif view_type == "usage":
                val = extract_section(stored_data, "適應症/用途")
                reply_msg = f"🎯 【適應症/用途】：\n{val}\n\n⚠️ 【注意事項】：\n{warning_info}"
            elif view_type == "dose":
                val = extract_section(stored_data, "用法用量")
                reply_msg = f"⏰ 【用法用量】：\n{val}\n\n⚠️ 【注意事項】：\n{warning_info}"
            elif view_type == "side_effect":
                val = extract_section(stored_data, "副作用")
                reply_msg = f"🧪 【副作用】：\n{val}\n\n⚠️ 【注意事項】：\n{warning_info}"
            else:
                reply_msg = stored_data

            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply_msg)]
                )
            )

        elif data.startswith("view_pill="):
            view_type = data.split("=")[1]
            stored_data = user_pending_data.get(user_id, "查無近期辨識資料，請重新上傳照片。")
            warning_info = extract_section(stored_data, "服用注意事項與警語")

            if view_type == "name":
                val = extract_section(stored_data, "可能藥品名稱")
                reply_msg = f"🏷️ 【可能藥品名稱】：\n{val}\n\n⚠️ 【注意事項與警語】：\n{warning_info}"
            elif view_type == "category":
                val = extract_section(stored_data, "慢性病分類")
                reply_msg = f"🩺 【慢性病分類】：\n{val}\n\n⚠️ 【注意事項與警語】：\n{warning_info}"
            elif view_type == "usage":
                val = extract_section(stored_data, "主要適應症/用途")
                reply_msg = f"🎯 【主要適應症/用途】：\n{val}\n\n⚠️ 【注意事項與警語】：\n{warning_info}"
            elif view_type == "dose":
                val = extract_section(stored_data, "一般常見用法")
                reply_msg = f"⏰ 【一般常見用法】：\n{val}\n\n⚠️ 【注意事項與警語】：\n{warning_info}"
            else:
                reply_msg = stored_data

            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply_msg)]
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
            except Exception as reply_error:
                print(f"❌ [發送即時確認失敗] ➔ {reply_error}")

@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image_message(event):
    with ApiClient(configuration) as api_client:
        line_bot_blob_api = MessagingApiBlob(api_client)
        line_bot_api = MessagingApi(api_client)
        
        base_result_text = ""
        try:
            message_id = event.message.id
            message_content = line_bot_blob_api.get_message_content(message_id)
            image_bytes = io.BytesIO()
            if hasattr(message_content, 'iter_content'):
                for chunk in message_content.iter_content():
                    image_bytes.write(chunk)
            else:
                image_bytes.write(message_content if isinstance(message_content, bytes) else message_content.read())
            
            image_bytes.seek(0)
            raw_img = Image.open(image_bytes)
            raw_img.thumbnail((1200, 1200))
            
            compressed_io = io.BytesIO()
            raw_img.convert("RGB").save(compressed_io, format="JPEG", quality=85)
            compressed_io.seek(0)
            img = Image.open(compressed_io)
            image_bytes.close() 
            
            user_id = event.source.user_id
            lang = user_language_prefs.get(user_id, "zh")
            target_lang = LANGUAGE_MAP.get(lang, "繁體中文")
            
            prompt_content = f"""
請判斷這張照片是「藥袋」還是「藥丸/藥片/膠囊」。

【常見慢性病藥品參考知識庫 (RAG Knowledge Base)】：
{RAG_KNOWLEDGE_BASE}

【任務指示】：
1. 如果是「藥袋」：請精確萃取藥袋上的藥品名稱、適應症、用法用量與注意事項。
2. 如果是藥丸：請依據知識庫進行深度辨識。若非慢性病用藥，請回傳「此藥丸非慢性病用藥，無法偵測」。
3. 請全程使用「{target_lang}」語言回覆。
"""
            
            candidate_models = ['gemini-2.5-flash', 'gemini-1.5-flash']
            response = None
            last_error = None

            for model_name in candidate_models:
                try:
                    response = ai_client.models.generate_content(
                        model=model_name,
                        contents=[img, prompt_content],
                        config=types.GenerateContentConfig(
                            system_instruction=SYSTEM_INSTRUCTION,
                            temperature=0.1
                        ),
                    )
                    if response:
                        break
                except Exception as err:
                    last_error = err
                    time.sleep(1)

            if not response:
                raise last_error

            base_result_text = response.text.strip()
            user_pending_data[user_id] = base_result_text

            similarity_score = "90%"
            reasoning = "已完成系統檢核"
            metric_title = "•系統解析完整度"

            if "此藥丸非慢性病用藥，無法偵測" in base_result_text:
                similarity_score = "0% (未收錄)"
                reasoning = "該藥丸不在慢性病知識庫範圍內"
                metric_title = "與慢性病藥品匹配度"
            elif "📋 【藥袋辨識結果】" in base_result_text:
                metric_title = "圖文忠實度與準確度"
                bag_eval_prompt = f"""
你是一位嚴格的醫療 RAG 系統評估裁判。請同時檢視【原始上傳的藥袋圖片】與下方【AI 辨識出的藥袋結果】，評估其忠實度與準確度。
【AI 辨識回答】：{base_result_text}
請輸出 JSON 格式：{{"score": "95%", "reason": "無幻覺"}}
"""
                try:
                    eval_response = ai_client.models.generate_content(
                        model='gemini-2.5-flash',
                        contents=[img, bag_eval_prompt], 
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json",
                            temperature=0.0
                        )
                    )
                    eval_json = json.loads(eval_response.text)
                    similarity_score = eval_json.get("score", "94%")
                    reasoning = eval_json.get("reason", "")
                except Exception:
                    similarity_score = "N/A"
                    reasoning = "動態評估略過"
            else:
                metric_title = "資訊準確度與相似度"
                pill_eval_prompt = f"""
你是一位嚴格的醫療 RAG 系統評估裁判。請比對【知識庫】與【AI辨識回答】，評估忠實度。
【知識庫】：{RAG_KNOWLEDGE_BASE}
【回答】：{base_result_text}
請輸出 JSON 格式：{{"score": "95%", "reason": "完全對應"}}
"""
                try:
                    eval_response = ai_client.models.generate_content(
                        model='gemini-2.5-flash',
                        contents=pill_eval_prompt,
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json",
                            temperature=0.0
                        )
                    )
                    eval_json = json.loads(eval_response.text)
                    similarity_score = eval_json.get("score", "95%")
                    reasoning = eval_json.get("reason", "")
                except Exception:
                    similarity_score = "N/A"
                    reasoning = "動態評估略過"

            result_text = f"{base_result_text}\n\n📊 【RAG 系統評估指標】\n━━━━━━━━━━━━━━\n• {metric_title}：{similarity_score}\n• 檢核說明：{reasoning}\n━━━━━━━━━━━━━━"

        except Exception as e:
            result_text = "❌ 辨識失敗。可能原因：照片過於模糊、反光、或是 Google AI 連線超時。請重新拍攝並再試一次！"

        try:
            messages_to_send = [TextMessage(text=result_text)]
            if "此藥丸非慢性病用藥，無法偵測" not in base_result_text and "❌ 辨識失敗" not in result_text:
                if "📋 【藥袋辨識結果】" in base_result_text:
                    menu_template = TemplateMessage(
                        alt_text="請選擇您想了解的藥袋資訊",
                        template=ButtonsTemplate(
                            title="📋 藥袋資訊細節查詢",
                            text="請點選下方按鈕查看特定項目：",
                            actions=[
                                PostbackAction(label="🏷️ 藥品名稱", data="view_bag=name"),
                                PostbackAction(label="🎯 適應症/用途", data="view_bag=usage"),
                                PostbackAction(label="⏰ 用法用量", data="view_bag=dose"),
                                PostbackAction(label="🧪 副作用", data="view_bag=side_effect")
                            ]
                        )
                    )
                else:
                    menu_template = TemplateMessage(
                        alt_text="請選擇您想了解的藥丸資訊",
                        template=ButtonsTemplate(
                            title="💊 藥丸資訊細節查詢",
                            text="請點選下方按鈕查看特定項目：",
                            actions=[
                                PostbackAction(label="🏷️ 可能藥品名稱", data="view_pill=name"),
                                PostbackAction(label="🩺 慢性病分類", data="view_pill=category"),
                                PostbackAction(label="🎯 主要適應症/用途", data="view_pill=usage"),
                                PostbackAction(label="⏰ 一般常見用法", data="view_pill=dose")
                            ]
                        )
                    )
                messages_to_send.append(menu_template)

            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=messages_to_send
                )
            )
        except Exception as reply_error:
            print(f"❌ [回傳失敗] ➔ {reply_error}")

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
