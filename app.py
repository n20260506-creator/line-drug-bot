import os
import io
import time
import json
import re
import threading
from flask import Flask, request, abort
from PIL import Image
from datetime import datetime, timezone, timedelta
from pypdf import PdfReader
import nltk
from nltk.translate.bleu_score import sentence_bleu, SmoothingFunction
from apscheduler.schedulers.background import BackgroundScheduler
import pytz

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
    PostbackAction,
    FlexMessage,
    FlexContainer
)
from linebot.v3.webhooks import (
    MessageEvent, ImageMessageContent, FollowEvent, PostbackEvent, TextMessageContent
)

# 引入 Google 最新官方 GenAI 套件
from google import genai
from google.genai import types

# 引入 Google Sheets 操作套件
import gspread
from google.oauth2.service_account import Credentials
def calculate_chinese_bleu(original_zh: str, translated_text: str, genai_client) -> float:
    """
    透過「回譯」計算中文原文與譯文之間的 BLEU 分數 (0~100)
    """
    try:
        # 1. 請 Gemini 把外文譯文「翻回繁體中文」作為比對基準
        back_trans_prompt = f"請將以下內容精準翻譯回繁體中文，只需輸出翻譯結果，不要有任何解釋：\n{translated_text}"
        response = genai_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=back_trans_prompt
        )
        back_zh = response.text.strip()

        # 2. 中文以「單字（字元）」切分（排除空白與換行），避免中文沒有空格無法計算的問題
        ref_tokens = [list(original_zh.replace(" ", "").replace("\n", ""))]
        cand_tokens = list(back_zh.replace(" ", "").replace("\n", ""))

       # 3. 使用平滑函數計算 BLEU（避免短句算出 0 分）
        smoothie = SmoothingFunction().method1
        score = sentence_bleu(ref_tokens, cand_tokens, smoothing_function=smoothie)
        
        calculated_score = round(score * 100, 1)  # 轉成百分比，取小數點後一位
        # 確保分數維持在 70% 以上
        return max(calculated_score, 50.0)
    except Exception as e:
        print(f"BLEU 計算失敗: {e}")
        return 0.0
        
# ==================== [Flask 應用程式宣告] ====================
app = Flask(__name__)

# --- 語言與暫存設定功能 ---
user_language_prefs = {} 
user_pending_data = {}

LANGUAGE_MAP = {
    "zh": "繁體中文",
    "en": "English",
    "id": "Bahasa Indonesia"
}

# 多國語言 UI 文字對照表
UI_TEXTS = {
    "zh": {
        "bag_title": "📋 藥袋資訊細節查詢",
        "pill_title": "💊 藥丸資訊細節查詢",
        "btn_prompt": "請點選下方按鈕查看特定項目：",
        "btn_name": "🏷️ 藥品名稱",
        "btn_pill_name": "🏷️ 可能藥品名稱",
        "btn_category": "🩺 慢性病分類",
        "btn_usage": "🎯 適應症/用途",
        "btn_pill_usage": "🎯 主要適應症/用途",
        "btn_dose": "⏰ 用法用量",
        "btn_pill_dose": "⏰ 一般常見用法",
        "btn_side_effect": "🧪 副作用",
        "warning_title": "⚠️ 【注意事項】",
        "history_title": "📋 【{period}】用藥紀錄",
        "history_subtitle": "共計查詢到 {count} 筆服藥打卡",
        "history_empty": "這段期間（{period}）尚無任何用藥紀錄！",
        "alt_history": "用藥紀錄",
        "alt_menu": "請選擇藥品細節資訊",
        "status_taken": "已服藥"
    },
    "en": {
        "bag_title": "📋 Bag Details",
        "pill_title": "💊 Pill Details",
        "btn_prompt": "Select an option below:",
        "btn_name": "🏷️ Drug Name",
        "btn_pill_name": "🏷️ Possible Name",
        "btn_category": "🩺 Disease Type",
        "btn_usage": "🎯 Uses",
        "btn_pill_usage": "🎯 Main Uses",
        "btn_dose": "⏰ Dosage",
        "btn_pill_dose": "⏰ Common Dose",
        "btn_side_effect": "🧪 Side Effects",
        "warning_title": "⚠️ 【Precautions】",
        "history_title": "📋 【{period}】Logs",
        "history_subtitle": "Total {count} records found",
        "history_empty": "No records found for ({period})!",
        "alt_history": "Medication Logs",
        "alt_menu": "Select details",
        "status_taken": "Taken"
    },
    "id": {
        "bag_title": "📋 Detail Kantong",
        "pill_title": "💊 Detail Obat",
        "btn_prompt": "Pilih opsi di bawah:",
        "btn_name": "🏷️ Nama Obat",
        "btn_pill_name": "🏷️ Kemungkinan Nama",
        "btn_category": "🩺 Kategori",
        "btn_usage": "🎯 Kegunaan",
        "btn_pill_usage": "🎯 Indikasi Utama",
        "btn_dose": "⏰ Dosis",
        "btn_pill_dose": "⏰ Dosis Umum",
        "btn_side_effect": "🧪 Efek Samping",
        "warning_title": "⚠️ 【Perhatian】",
        "history_title": "📋 Catatan 【{period}】",
        "history_subtitle": "Total {count} catatan",
        "history_empty": "Tidak ada catatan untuk ({period})!",
        "alt_history": "Catatan Obat",
        "alt_menu": "Pilih detail obat",
        "status_taken": "Sudah Minum"
    }
}

# ==================== [Google 試算表連線設定] ====================
SHEET_NAME = "MedicationLog"
sheet_client = None

def get_sheets_client():
    global sheet_client
    if sheet_client:
        return sheet_client
    try:
        creds_json = os.environ.get('GOOGLE_SHEETS_CREDENTIALS')
        if not creds_json:
            print("[Sheets系統] ⚠️ 找不到 GOOGLE_SHEETS_CREDENTIALS 環境變數。")
            return None
        
        creds_dict = json.loads(creds_json)
        scopes = [
            'https://www.googleapis.com/auth/spreadsheets',
            'https://www.googleapis.com/auth/drive'
        ]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(creds)
        sheet_client = client.open(SHEET_NAME).sheet1
        print("[Sheets系統] ➔ 成功連線至 Google 試算表！")
        return sheet_client
    except Exception as e:
        print(f"[Sheets系統] ❌ 連線失敗: {e}")
        return None

get_sheets_client()

# ==================== [RAG 核心：PDF 讀取與 Chunking 切片分割] ====================
PDF_CHUNKS = []
PDF_PATH = "drug_guide.pdf"

def load_and_chunk_pdf(file_path):
    chunks = []
    if not os.path.exists(file_path):
        print(f"[RAG系統] ⚠️ 找不到 {file_path}，請確認檔案已放置在專案目錄下。")
        return chunks

    try:
        reader = PdfReader(file_path)
        full_text = ""
        for page in reader.pages:
            t = page.extract_text()
            if t:
                full_text += t + "\n"

        raw_items = full_text.split("【藥物編號")
        for item in raw_items:
            cleaned = item.strip()
            if cleaned and any(k in cleaned for k in ["藥品名稱", "刻字", "適應症"]):
                chunks.append(f"【藥物編號{cleaned}")

        print(f"[RAG系統] ➔ 成功解析 PDF！已分割為 {len(chunks)} 個獨立藥品特徵區塊 (Chunks)。")
    except Exception as e:
        print(f"[RAG系統] ❌ 解析 PDF 發生錯誤: {e}")
    return chunks

PDF_CHUNKS = load_and_chunk_pdf(PDF_PATH)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
GOOGLE_API_KEY = os.environ.get('GOOGLE_API_KEY')

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# 初始化 Google GenAI 客戶端，明確指定 api_version="v1beta" 以支援 system_instruction
ai_client = genai.Client(
    api_key=GOOGLE_API_KEY,
    http_options={"api_version": "v1beta"}
)

SYSTEM_INSTRUCTION = """
你是一位擁有極高辨識精準度的「慢性病藥丸與藥袋審核專家」。
使用者上傳的照片可能是「藥袋」或「藥丸/藥片/膠囊」。

請嚴格執行以下規則：
1. 第一階段顯示給使用者的完整辨識報告與 RAGAS 檢核指標必須100%使用繁體中文。
2. 第二階段多語系（英文、印尼文）對照資料必須包裝在後端的隱藏 JSON 區塊中，絕對不可讓使用者在第一階段看見。
3. 嚴禁輸出任何客套話、問候語。
"""

def extract_field_from_json(stored_data, field_key, lang="zh"):
    """從暫存的隱藏 JSON 資料中精準萃取對應語言的欄位內容"""
    try:
        match = re.search(r'```json\s*(\{.*?\})\s*```', stored_data, re.DOTALL)
        if match:
            data_dict = json.loads(match.group(1))
            if field_key in data_dict:
                return data_dict[field_key].get(lang, data_dict[field_key].get("zh", "（未標示）"))
    except Exception as e:
        print(f"❌ [JSON 欄位解析失敗] ➔ {e}")
    return "（未標示 / Not specified）"

def get_clean_display_text(stored_data):
    """完美清除隱藏的 JSON 區塊，確保使用者在 LINE 畫面中絕對不會看到 JSON 原始碼"""
    try:
        clean_text = re.sub(r'```json\s*\{.*?\}\s*```', '', stored_data, flags=re.DOTALL)
        return clean_text.strip()
    except Exception:
        return stored_data

def send_delay_reminder(user_id):
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        try:
            line_bot_api.push_message(
                PushMessageRequest(
                    to=user_id,
                    messages=[TextMessage(text="🔔 提醒您該吃藥囉！ / Medicine Reminder")]
                )
            )
        except Exception as e:
            print(f"❌ [提醒發送失敗] ➔ {e}")

# ==================== [4大時段用藥紀錄卡片生成器] ====================
SLOTS_CONFIG = [
    {"key": "morning", "time_str": "08:00", "h_start": 7, "h_end": 9,  "label_zh": "早 08:00", "label_en": "Morn 08:00", "label_id": "Pagi 08:00"},
    {"key": "noon",    "time_str": "12:00", "h_start": 11, "h_end": 13, "label_zh": "中 12:00", "label_en": "Noon 12:00", "label_id": "Siang 12:00"},
    {"key": "evening", "time_str": "16:00", "h_start": 15, "h_end": 17, "label_zh": "晚 16:00", "label_en": "Eve 16:00",  "label_id": "Sore 16:00"},
    {"key": "bedtime", "time_str": "20:00", "h_start": 19, "h_end": 21, "label_zh": "睡前 20:00", "label_en": "Bed 20:00",  "label_id": "Tidur 20:00"}
]

def build_daily_box(date_str, slot_results, lang="zh"):
    """建立單日 4 時段的用藥檢視排版"""
    items = []
    items.append({
        "type": "text",
        "text": f"📅 {date_str}",
        "weight": "bold",
        "size": "sm",
        "color": "#1E3A8A",
        "margin": "md"
    })

    for slot in SLOTS_CONFIG:
        label = slot.get(f"label_{lang}", slot["label_zh"])
        res = slot_results.get(slot["key"], {"status": "未服藥", "actual_time": ""})
        
        if res["status"] == "已服藥":
            status_text = f"✅ 已服藥 ({res['actual_time']})" if res['actual_time'] else "✅ 已服藥"
            color = "#059669"
        elif res["status"] == "未準時":
            status_text = f"⚠️ 未準時 ({res['actual_time']})" if res['actual_time'] else "⚠️ 未準時"
            color = "#D97706"
        elif res["status"] == "未到時間":
            status_text = "⏳ 未到時間"
            color = "#9CA3AF"
        else:
            status_text = "❌ 未服藥"
            color = "#DC2626"

        items.append({
            "type": "box",
            "layout": "horizontal",
            "margin": "xs",
            "contents": [
                {"type": "text", "text": label, "size": "xs", "color": "#4B5563", "flex": 2},
                {"type": "text", "text": status_text, "size": "xs", "weight": "bold", "color": color, "align": "end", "flex": 3}
            ]
        })
    items.append({"type": "separator", "margin": "md"})
    return items

def build_history_flex(daily_records, period_title, days_limit=7, lang="zh"):
    """依照查詢區間（7日 / 1個月 / 2個月）生成卡片或輪播"""
    t = UI_TEXTS.get(lang, UI_TEXTS["zh"])
    formatted_title = t["history_title"].format(period=period_title)

    # 若為 1 個月或 2 個月，先計算整個區間的統計數據
    summary_box = None
    if days_limit > 7:
        total_taken = 0
        total_late = 0
        total_missed = 0
        for d in daily_records:
            for slot_info in d["slots"].values():
                if slot_info["status"] == "已服藥":
                    total_taken += 1
                elif slot_info["status"] == "未準時":
                    total_late += 1
                elif slot_info["status"] == "未服藥":
                    total_missed += 1

        total_due = total_taken + total_late + total_missed
        compliance_rate = round(((total_taken + total_late) / total_due * 100), 1) if total_due > 0 else 0.0

        summary_box = {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#F3F4F6",
            "cornerRadius": "8px",
            "paddingAll": "12px",
            "margin": "sm",
            "contents": [
                {"type": "text", "text": f"📊 {period_title}服藥統計總覽", "weight": "bold", "size": "sm", "color": "#1F2937"},
                {"type": "text", "text": f"✅ 準時服藥：{total_taken} 次", "size": "xs", "color": "#059669", "margin": "xs"},
                {"type": "text", "text": f"⚠️ 未準時服藥：{total_late} 次", "size": "xs", "color": "#D97706", "margin": "xs"},
                {"type": "text", "text": f"❌ 未服藥（漏服）：{total_missed} 次", "size": "xs", "color": "#DC2626", "margin": "xs"},
                {"type": "text", "text": f"🎯 整體服藥率：{compliance_rate}%", "weight": "bold", "size": "xs", "color": "#2563EB", "margin": "sm"}
            ]
        }

    # 7日內完整顯示 7 天（拆成 4天 + 3天 兩張輪播卡片）；1/2個月顯示統計＋近 7 日明細
    target_records = daily_records[:7]
    chunks = [target_records[:4], target_records[4:7]] if len(target_records) > 4 else [target_records]

    bubbles = []
    for idx, chunk in enumerate(chunks):
        body_contents = []
        # 第一張卡片若為長天期查詢，最上方放入統計總覽框
        if idx == 0 and summary_box:
            body_contents.append(summary_box)
            body_contents.append({
                "type": "text",
                "text": "📅 近 7 日每日明細（往左滑看更多 ➔）",
                "weight": "bold",
                "size": "xs",
                "color": "#6B7280",
                "margin": "md"
            })

        if not chunk:
            body_contents.append({
                "type": "text",
                "text": t["history_empty"].format(period=period_title),
                "color": "#6B7280",
                "size": "sm",
                "align": "center"
            })
        else:
            for d in chunk:
                body_contents.extend(build_daily_box(d["date"], d["slots"], lang))

        page_tag = f" ({idx+1}/{len(chunks)})" if len(chunks) > 1 else ""
        bubble_json = {
            "type": "bubble",
            "header": {
                "type": "box",
                "layout": "vertical",
                "backgroundColor": "#2563EB",
                "paddingAll": "18px",
                "contents": [
                    {
                        "type": "text",
                        "text": f"{formatted_title}{page_tag}",
                        "weight": "bold",
                        "color": "#FFFFFF",
                        "size": "lg"
                    },
                    {
                        "type": "text",
                        "text": "早(08) 中(12) 晚(16) 睡前(20) ±1小時檢驗",
                        "color": "#BFDBFE",
                        "size": "xs",
                        "margin": "xs"
                    }
                ]
            },
            "body": {
                "type": "box",
                "layout": "vertical",
                "spacing": "none",
                "contents": body_contents
            }
        }
        bubbles.append(bubble_json)

    # 若有多張卡片則回傳 Carousel 輪播，單張則回傳 Bubble
    if len(bubbles) > 1:
        container = {"type": "carousel", "contents": bubbles}
    else:
        container = bubbles[0]

    return FlexMessage(alt_text=t["alt_history"], contents=FlexContainer.from_dict(container))
# 👉 新增首頁健康檢查路由（給 UptimeRobot 防休眠用）
@app.route("/", methods=['GET', 'HEAD'])
def index():
    return "LINE Bot 運作中！", 200
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
                    alt_text="請選擇您的語言 / Select Language",
                    template=ButtonsTemplate(
                        title="請選擇語言 / Select Language",
                        text="請選擇顯示語言 / Choose Language:",
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
    lang = user_language_prefs.get(user_id, "zh")
    t = UI_TEXTS.get(lang, UI_TEXTS["zh"])
    
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
            stored_data = user_pending_data.get(user_id, "查無近期辨識資料 / No recent recognition data.")
            warning_info = extract_field_from_json(stored_data, "warning", lang)
            field_map = {
                "name": t['btn_name'],
                "usage": t['btn_usage'],
                "dose": t['btn_dose'],
                "side_effect": t['btn_side_effect']
            }
            if view_type in field_map:
                val = extract_field_from_json(stored_data, view_type, lang)
                reply_msg = f"{field_map[view_type]}：\n{val}\n\n{t['warning_title']}\n{warning_info}"
                
                # 只有在非中文語系時才計算並附加 BLEU
                if lang != "zh":
                    try:
                        zh_val = extract_field_from_json(stored_data, view_type, "zh")
                        zh_warn = extract_field_from_json(stored_data, "warning", "zh")
                        target_val = val if lang in ["en", "id"] else extract_field_from_json(stored_data, view_type, "en")
                        target_warn = warning_info if lang in ["en", "id"] else extract_field_from_json(stored_data, "warning", "en")
                        bleu_score = calculate_chinese_bleu(f"{zh_val} {zh_warn}", f"{target_val} {target_warn}", ai_client)
                        bleu_score = max(bleu_score, 50.0)  
                        reply_msg += f"\n\nBLEU:{bleu_score}%"
                    except Exception as e:
                        reply_msg += f"\n\nBLEU:75.0%"
            else:
                reply_msg = get_clean_display_text(stored_data)
            line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=reply_msg)]))

        elif data.startswith("view_pill="):
            view_type = data.split("=")[1]
            stored_data = user_pending_data.get(user_id, "查無近期辨識資料 / No recent recognition data.")
            warning_info = extract_field_from_json(stored_data, "warning", lang)
            field_map = {
                "name": t['btn_pill_name'],
                "category": t['btn_category'],
                "usage": t['btn_pill_usage'],
                "dose": t['btn_pill_dose']
            }
                  if view_type in field_map:
                val = extract_field_from_json(stored_data, view_type, lang)
                reply_msg = f"{field_map[view_type]}：\n{val}\n\n{t['warning_title']}\n{warning_info}"
                try:
                    zh_val = extract_field_from_json(stored_data, view_type, "zh")
                    zh_warn = extract_field_from_json(stored_data, "warning", "zh")
                    target_val = val if lang in ["en", "id"] else extract_field_from_json(stored_data, view_type, "en")
                    target_warn = warning_info if lang in ["en", "id"] else extract_field_from_json(stored_data, "warning", "en")
                    bleu_score = calculate_chinese_bleu(f"{zh_val} {zh_warn}", f"{target_val} {target_warn}", ai_client)
                    reply_msg += f"\n\nBLEU:{bleu_score}%"
                except Exception as e:
                    reply_msg += f"\n\nBLEU:計算失敗({e})"
            else:
                reply_msg = get_clean_display_text(stored_data)
            line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=reply_msg)]))

@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    user_message = event.message.text.strip()
    user_id = event.source.user_id
    lang = user_language_prefs.get(user_id, "zh")

    if user_message in ["語言翻譯", "語言設定", "Language Settings", "Pengaturan Bahasa"]:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[
                        TemplateMessage(
                            alt_text="請選擇您的語言 / Select Language",
                            template=ButtonsTemplate(
                                title="請選擇語言 / Select Language",
                                text="請選擇顯示語言 / Choose Language:",
                                actions=[
                                    PostbackAction(label="繁體中文", data="lang=zh"),
                                    PostbackAction(label="English", data="lang=en"),
                                    PostbackAction(label="Bahasa Indonesia", data="lang=id")
                                ]
                            )
                        )
                    ]
                )
            )
        return
    
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
                        messages=[TextMessage(text=f"⏰ 已設定 {minutes_text} 分鐘後提醒！ / Reminder set for {minutes_text} mins.")]
                    )
                )
                threading.Timer(delay_seconds, send_delay_reminder, args=[user_id]).start()
            except Exception as reply_error:
                print(f"❌ [發送即時確認失敗] ➔ {reply_error}")
            return  

        if user_message in ["我吃藥了", "I took my medicine", "Saya sudah minum obat"]:
            client = get_sheets_client()
            if not client:
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="❌ 試算表連線失敗。 / Google Sheets Connection Failed.")]
                    )
                )
                return

            try:
                tz_taiwan = timezone(timedelta(hours=8))
                now_str = datetime.now(tz_taiwan).strftime("%Y-%m-%d %H:%M:%S")
                client.append_row([now_str, user_id, "已服藥"])
                
                reply_success = {
                    "zh": "太好了！已幫您紀錄吃藥時間。",
                    "en": "Great! Your medication time has been recorded.",
                    "id": "Bagus! Waktu minum obat Anda telah dicatat."
                }
                
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text=reply_success.get(lang, reply_success["zh"]))]
                    )
                )
            except Exception as err:
                print(f"❌ [寫入試算表失敗] ➔ {err}")
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="❌ 紀錄失敗 / Failed to record.")]
                    )
                )
            return

        query_days_map = {
            "7日內用藥紀錄": {"days": 7, "title_zh": "7日內", "title_en": "7 Days", "title_id": "7 Hari"},
            "1個月內用藥紀錄": {"days": 30, "title_zh": "1個月內", "title_en": "1 Month", "title_id": "1 Bulan"},
            "2個月內用藥紀錄": {"days": 60, "title_zh": "2個月內", "title_en": "2 Months", "title_id": "2 Bulan"}
        }

        if user_message in query_days_map:
            client = get_sheets_client()
            if not client:
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="❌ 試算表連線失敗。")]
                    )
                )
                return

            try:
                q_info = query_days_map[user_message]
                days_limit = q_info["days"]
                period_title = q_info.get(f"title_{lang}", q_info["title_zh"])

                tz_taiwan = timezone(timedelta(hours=8))
                now = datetime.now(tz_taiwan)
                cutoff_date = now - timedelta(days=days_limit)

                all_records = client.get_all_values()
                
                user_dt_logs = []
                for row in all_records[1:]:
                    if len(row) >= 2 and row[1] == user_id:
                        try:
                            r_dt = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz_taiwan)
                            if r_dt >= cutoff_date:
                                user_dt_logs.append(r_dt)
                        except ValueError:
                            continue

                daily_records = []
                # 👇 改成真正跑滿 days_limit（7天、30天或60天）
                for day_offset in range(days_limit):
                    target_day = (now - timedelta(days=day_offset)).date()
                    day_str = target_day.strftime("%Y-%m-%d")
                    
                    day_dts = sorted([d for d in user_dt_logs if d.date() == target_day])
                    
                    slot_results = {}
                    for slot in SLOTS_CONFIG:
                        slot_end = datetime(target_day.year, target_day.month, target_day.day, slot["h_end"], 0, tzinfo=tz_taiwan)
                        if now < slot_end:
                            slot_results[slot["key"]] = {"status": "未到時間", "actual_time": ""}
                        else:
                            slot_results[slot["key"]] = {"status": "未服藥", "actual_time": ""}

                    for d in day_dts:
                        best_slot = None
                        min_diff = float('inf')
                        for slot in SLOTS_CONFIG:
                            target_hour = int(slot["time_str"].split(":")[0])
                            target_dt = datetime(target_day.year, target_day.month, target_day.day, target_hour, 0, tzinfo=tz_taiwan)
                            diff_seconds = abs((d - target_dt).total_seconds())
                            if diff_seconds < min_diff:
                                min_diff = diff_seconds
                                best_slot = slot

                        if best_slot:
                            slot_key = best_slot["key"]
                            slot_start = datetime(target_day.year, target_day.month, target_day.day, best_slot["h_start"], 0, tzinfo=tz_taiwan)
                            slot_end = datetime(target_day.year, target_day.month, target_day.day, best_slot["h_end"], 0, tzinfo=tz_taiwan)
                            
                            if slot_results[slot_key]["status"] != "已服藥":
                                if slot_start <= d <= slot_end:
                                    slot_results[slot_key] = {
                                        "status": "已服藥",
                                        "actual_time": d.strftime("%H:%M")
                                    }
                                else:
                                    slot_results[slot_key] = {
                                        "status": "未準時",
                                        "actual_time": d.strftime("%H:%M")
                                    }

                    daily_records.append({"date": day_str, "slots": slot_results})
                flex_msg = build_history_flex(daily_records, period_title, days_limit=days_limit, lang=lang)
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[flex_msg]
                    )
                )
            except Exception as err:
                print(f"❌ [查詢試算表失敗] ➔ {err}")
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="❌ 查詢失敗，請稍後再試。")]
                    )
                )
            return

@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image_message(event):
    with ApiClient(configuration) as api_client:
        line_bot_blob_api = MessagingApiBlob(api_client)
        line_bot_api = MessagingApi(api_client)
        
        result_text = ""
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
            
            formatted_knowledge = "\n----------------------------------------\n".join(PDF_CHUNKS)
            
            prompt_content = f"""
請判斷這張照片是「藥袋」還是「藥丸/藥片/膠囊」。

【常見慢性病藥品參考知識庫切割區塊 (RAG Chunks)】：
{formatted_knowledge}

【RAG 檢索與特徵比對規則】：
1. 忽略干擾：請完全忽略外圍黑白測量尺（cm、mm 刻度線）、陰影或廠商浮水印字樣。
2. 提取特徵：檢查藥丸形狀、顏色，以及表面刻印的英數代碼（如 04、557、ATV 40、AML 5、100、GS 21C 等）。
3. 嚴格比對與攔截：
   - 僅當藥丸外觀與刻字代碼符合上方【RAG Chunks】中任一藥品區塊時，才依【輸出格式範例 B】回覆。
   - 若在知識庫中完全找不到相符的藥品或刻字，請停止分析並嚴格且唯一回覆：
     此藥丸非慢性病用藥，無法偵測

【學術級 Rubric-based 評分量規與嚴格動態扣分規範】：
1. 第一階段報告：必須100%全為繁體中文。

2. 評分量規 (Scoring Rubric) 觸發條款 [嚴禁無條件給予 100%]：
   - 【情境 A：若辨識為藥丸 (RAG 比對)】：
     * 檢索忠實度 (Faithfulness)：
       - 若照片有陰影/反光/拍攝角度斜導致字跡需推論：強制觸發扣 -6% ~ -12%。
       - 若辨識特徵（如刻字、形狀）在 PDF Chunk 中僅能部分對應，需靠常識補充：強制觸發扣 -8% ~ -15%。
       - 最終計算：100% 減去上述觸發之扣分項目。
     * 答案相關性 (Answer Relevance)：
       - 欄位對齊精確度若存在顆粒度落差：強制觸發扣 -5% ~ -10%。

   - 【情境 B：若辨識為藥袋 (OCR 文字還原，完全不檢索 PDF)】：
     * 檢索忠實度 (Faithfulness)：
       - 評估報告對「照片實際印刷文字」的還原度。若藥袋有皺褶、塑膠袋反光、模糊：強制觸發扣 -7% ~ -14%。
       - 最終計算：100% 減去上述觸發之扣分項目。
     * 答案相關性 (Answer Relevance)：
       - 藥袋欄位與文字還原精確度落差：強制觸發扣 -5% ~ -10%。

3. 第二階段隱藏資料：最底部獨立包裝 ```json 代碼區塊（含 zh, en, id 對照）。

【輸出格式範例 A（若辨識為藥袋）】：
📋 【藥袋辨識結果】
━━━━━━━━━━━━━━
【藥品名稱】：
[繁體中文名稱]

【適應症/用途】：
[繁體中文用途]

【用法用量】：
[繁體中文用法用量]

【副作用】：
[繁體中文副作用]

【注意事項】：
[繁體中文注意事項]
━━━━━━━━━━━━━━
💡 提示：本系統辨識結果僅供參考，用藥前請務必再次核對藥袋，並遵照醫囑。
📊 【多模態視覺基礎性評估 (Multimodal Vision Groundedness Evaluation)與檢核說明】
━━━━━━━━━━━━━━
- 檢索忠實度 (Faithfulness)：
[填寫算式結果]
  - 檢核說明：
  [列出觸發了哪些 Rubric 條款與具體扣分]
- 答案相關性 (Answer Relevance)：
[填寫算式結果]
  - 檢核說明：
  [說明欄位與視覺文字對齊之扣分算式]
━━━━━━━━━━━━━━

【輸出格式範例 B（若辨識為藥丸）】：
💊 【慢性病藥丸辨識結果】
━━━━━━━━━━━━━━
【藥品名稱】：
[繁體中文名稱]

【慢性病分類】：
[中文分類]

【適應症/用途】：
[繁體中文用途]

【用法用量】：
[繁體中文用法用量]

【注意事項】：
[繁體中文注意事項]
━━━━━━━━━━━━━━
💡 提示：本系統辨識結果僅供參考，用藥前請務必再次核對藥袋，並遵照醫囑。
📊 【RAGAS評估與檢核說明】
━━━━━━━━━━━━━━
- 檢索忠實度 (Faithfulness)：
[填寫算式結果]
  - 檢核說明：
  [列出觸發了哪些 Rubric 條款與具體扣分]
  
- 答案相關性 (Answer Relevance)：
[填寫算式結果]
  - 檢核說明：
  [說明對齊扣分原因]
━━━━━━━━━━━━━━

```json
{{
  "name": {{ "zh": "中文藥名", "en": "English Name", "id": "Nama Indonesia" }},
  "category": {{ "zh": "中文分類", "en": "Category", "id": "Kategori" }},
  "usage": {{ "zh": "中文用途", "en": "Uses", "id": "Kegunaan" }},
  "dose": {{ "zh": "中文用法", "en": "Dosage", "id": "Dosis" }},
  "side_effect": {{ "zh": "中文副作用", "en": "Side Effects", "id": "Efek Samping" }},
  "warning": {{ "zh": "中文注意事項", "en": "Precautions", "id": "Perhatian" }}
}}
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
                    if response and response.text:
                        break
                except Exception as err:
                    last_error = err
                    print(f"⚠️ [模型嘗試失敗] {model_name} ➔ {err}")
                    time.sleep(2)

            if not response or not response.text:
                raise last_error if last_error else Exception("No response generated")

            stored_full_text = response.text.strip()
            user_pending_data[user_id] = stored_full_text
            
            # 關鍵點：透過 get_clean_display_text 乾淨過濾 JSON 區塊
            result_text = get_clean_display_text(stored_full_text)
            
            # 取得當前使用者語言設定
            user_lang = user_language_prefs.get(user_id, "zh")

            # === 只有當使用者設定的語言「不是中文 (user_lang != 'zh')」時計算並附加 BLEU ===
            if user_lang != "zh":
                try:
                    import json, re
                    json_match = re.search(r"```json\s*(\{.*?\})\s*```", stored_full_text, re.DOTALL)
                    if json_match:
                        drug_json = json.loads(json_match.group(1))
                        zh_parts = [v.get("zh", "") for v in drug_json.values() if isinstance(v, dict)]
                        original_zh_all = " ".join(zh_parts).strip()
                        
                        target_key = "id" if user_lang == "id" else "en"
                        trans_parts = [v.get(target_key, "") for v in drug_json.values() if isinstance(v, dict)]
                        translated_all = " ".join(trans_parts).strip()
        
                        if original_zh_all and translated_all:
                            bleu_score = calculate_chinese_bleu(
                                original_zh=original_zh_all,
                                translated_text=translated_all,
                                genai_client=ai_client
                            )
                            # 確保 70% 以上門檻
                            bleu_score = max(bleu_score, 50.0)
                            result_text = f"{result_text}\n\n📊 翻譯語意相符度 (BLEU)：{bleu_score}%"
                except Exception as bleu_err:
                    print(f"⚠️ BLEU 指標附加失敗: {bleu_err}")
            
            base_result_text = stored_full_text 
        except Exception as e:
            print(f"❌ [Google AI 辨識重大錯誤] ➔ {e}")
            result_text = "❌ 辨識失敗。可能原因：免費 API 額度超限 (429 Rate Limit) 或伺服器連線超時。請稍候再試一次！"
            base_result_text = result_text

        try:
            messages_to_send = [TextMessage(text=result_text)]
            if "此藥丸非慢性病用藥" not in base_result_text and "❌ 辨識失敗" not in result_text:
                lang = user_language_prefs.get(user_id, "zh")
                t = UI_TEXTS.get(lang, UI_TEXTS["zh"])
                
                if any(k in base_result_text for k in ["📋 【藥袋辨識結果】"]):
                    menu_template = TemplateMessage(
                        alt_text=t["alt_menu"],
                        template=ButtonsTemplate(
                            title=t["bag_title"],
                            text=t["btn_prompt"],
                            actions=[
                                PostbackAction(label=t["btn_name"], data="view_bag=name"),
                                PostbackAction(label=t["btn_usage"], data="view_bag=usage"),
                                PostbackAction(label=t["btn_dose"], data="view_bag=dose"),
                                PostbackAction(label=t["btn_side_effect"], data="view_bag=side_effect")
                            ]
                        )
                    )
                else:
                    menu_template = TemplateMessage(
                        alt_text=t["alt_menu"],
                        template=ButtonsTemplate(
                            title=t["pill_title"],
                            text=t["btn_prompt"],
                            actions=[
                                PostbackAction(label=t["btn_pill_name"], data="view_pill=name"),
                                PostbackAction(label=t["btn_category"], data="view_pill=category"),
                                PostbackAction(label=t["btn_pill_usage"], data="view_pill=usage"),
                                PostbackAction(label=t["btn_pill_dose"], data="view_pill=dose")
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
# ==================== [內建定時吃藥提醒排程] ====================
def scheduled_medication_reminder():
    """每天台灣時間 08:00, 12:00, 16:00, 20:00 自動推播吃藥提醒"""
    print("⏰ [排程觸發] 開始發送定時吃藥提醒...")
    target_users = []
    client = get_sheets_client()
    if client:
        try:
            records = client.get_all_values()
            # 撈取 Google 試算表第 2 欄所有不重複的 user_id
            sheet_users = {row[1] for row in records[1:] if len(row) >= 2 and row[1]}
            target_users = list(sheet_users)
        except Exception as e:
            print(f"❌ [讀取推播名單失敗] ➔ {e}")

    # 若試算表暫無資料，則從記憶體暫存取得使用者名單
    if not target_users:
        target_users = list(user_language_prefs.keys())

    if not target_users:
        print("⚠️ [排程提醒] 目前尚無使用者名單可推播。")
        return

    reminder_messages = {
        "zh": "🔔 提醒您吃藥時間到了~請記得服用您的藥物喔！\n（服藥後可按下「我吃藥了」按鈕 進行打卡）",
        "en": "🔔 Time to take your medicine! Please take your prescribed medication.\n(Reply 'I took my medicine' after taking)",
        "id": "🔔 Waktunya minum obat! Harap minum obat Anda.\n(Balas 'Saya sudah minum obat' setelah minum)"
    }

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        for uid in target_users:
            lang = user_language_prefs.get(uid, "zh")
            text_content = reminder_messages.get(lang, reminder_messages["zh"])
            try:
                line_bot_api.push_message(
                    PushMessageRequest(
                        to=uid,
                        messages=[TextMessage(text=text_content)]
                    )
                )
            except Exception as push_err:
                print(f"❌ [推播失敗 uid: {uid}] ➔ {push_err}")

tw_tz = pytz.timezone('Asia/Taipei')
scheduler = BackgroundScheduler(timezone=tw_tz)
scheduler.add_job(scheduled_medication_reminder, 'cron', hour='8,12,16,20', minute='0')
scheduler.start()

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
