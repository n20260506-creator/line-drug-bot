import re

def extract_section(text, keyword):
    try:
        # 同義關鍵字對照表（涵蓋 中文 / 英文 / 印尼文 / 常見 AI 翻譯標籤）
        synonyms = {
            "藥品名稱": [
                "藥品名稱", "可能藥品名稱",
                "drug name", "possible drug name", "name of drug", "medicine name",
                "nama obat", "kemungkinan nama obat"
            ],
            "慢性病分類": [
                "慢性病分類",
                "chronic disease category", "disease category", "category",
                "kategori penyakit kronis", "kategori penyakit", "kategori"
            ],
            "適應症/用途": [
                "適應症/用途", "主要適應症/用途", "適應症", "用途",
                "indications/uses", "main indications/uses", "indications", "uses", "indication",
                "indikasi/kegunaan", "indikasi utama", "indikasi", "kegunaan"
            ],
            "用法用量": [
                "用法用量", "一般常見用法", "用法", "用量",
                "dosage and administration", "general dosage", "dosage & usage", "dosage", "usage", "common dosage",
                "dosis dan penggunaan", "dosis umum", "dosis", "cara pakai"
            ],
            "副作用": [
                "副作用",
                "side effects", "side effect",
                "efek samping"
            ],
            "注意事項": [
                "注意事項", "服用注意事項與警語", "注意事項與警語", "警語",
                "precautions", "warnings", "precautions and warnings", "precautions & warnings",
                "peringatan dan perhatian", "peringatan", "perhatian"
            ]
        }

        targets = synonyms.get(keyword, [keyword])
        lines = text.split('\n')
        capturing = False
        result = []

        for line in lines:
            clean_line = line.strip()
            if not clean_line:
                continue

            # 檢查這一行是否包含目標關鍵字（忽略大小寫）
            line_lower = clean_line.lower()
            is_match = any(t.lower() in line_lower for t in targets)

            # 如果已經在擷取狀態，遇到下一個大標題（包含【、[ 或 ━━）就停止
            if capturing:
                if any(clean_line.startswith(prefix) for prefix in ['【', '[', '━━', '📋', '💊', '💡', '⚠️']):
                    # 避免把同一行剛匹配到的標題當成終止符號
                    if not is_match:
                        break

            if is_match:
                capturing = True
                # 嘗試以冒號拆分（支援全形 ： 與半形 :）
                parts = re.split(r'[:：]', clean_line, maxsplit=1)
                if len(parts) > 1 and parts[1].strip():
                    # 濾除開頭可能殘留的方括號或裝飾符號
                    val = parts[1].strip().lstrip(']').strip()
                    if val and val != "[帶入藥品名稱]":
                        result.append(val)
                continue

            if capturing:
                # 排除純標題行與分隔線
                if not any(clean_line.startswith(prefix) for prefix in ['━━━━━━━━━━━━━━', '📋', '💊']):
                    result.append(clean_line)

        final_text = "\n".join(result).strip()
        return final_text if final_text else "（未標示 / Not specified）"

    except Exception as e:
        print(f"❌ [extract_section 解析失敗] ➔ {e}")
        return "（未標示 / Not specified）"
