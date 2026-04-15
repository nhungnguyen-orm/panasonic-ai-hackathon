import os
import json
import boto3
from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from dotenv import load_dotenv
from dynamodb import get_extraction_schema, CONTRACT_REGISTRY
from validator import validate_contract

load_dotenv()

MODEL_ID         = os.getenv("MODEL_ID")
MODEL_ID_EXTRACT = os.getenv("MODEL_ID_EXTRACT")

client = boto3.client("bedrock-runtime", region_name='ap-southeast-2')

JSON_DIR = "json"

CONTRACT_TYPE_DESCRIPTIONS = {
    "mua_ban_don_gian": (
        "Hợp đồng mua bán hàng hóa trong nước (nội địa Việt Nam). "
        "Dấu hiệu nhận biết: tiêu đề 'HỢP ĐỒNG MUA BÁN', có 'Bên A'/'Bên B' hoặc 'Bên Bán'/'Bên Mua', "
        "điều khoản đánh số 'Điều 1/2/3...', đơn vị tiền VNĐ/đồng, không có Incoterms/L/C/USD."
    ),
    "mua_ban_quoc_te": (
        "Hợp đồng mua bán hàng hóa quốc tế (xuất nhập khẩu). "
        "Dấu hiệu nhận biết: có từ 'SELLER'/'BUYER' hoặc 'INTERNATIONAL', "
        "có Incoterms (FOB/CIF/EXW...), cảng xuất/nhập (Port of Loading/Discharge), "
        "ngoại tệ USD/EUR, có thể song ngữ Anh-Việt."
    ),
    "mua_ban_tieng_anh": (
        "Hợp đồng mua bán hàng hóa bằng tiếng Anh (thuần Anh ngữ). "
        "Dấu hiệu nhận biết: toàn bộ nội dung viết bằng tiếng Anh, có 'Seller'/'Customer' hoặc 'Buyer', "
        "các điều khoản như 'Governing Law', 'Termination', 'Shipping Method', "
        "KHÔNG có nội dung tiếng Việt, KHÔNG có Incoterms/cảng xuất nhập."
    ),
}


def read_docx(file_path: str) -> str:
    doc   = Document(file_path)
    parts = []

    for child in doc.element.body.iterchildren():
        if isinstance(child, Paragraph) or child.tag.endswith("p"):
            para = Paragraph(child, doc)
            if para.text.strip():
                parts.append(para.text.strip())
        elif isinstance(child, Table) or child.tag.endswith("tbl"):
            table = Table(child, doc)
            for row in table.rows:
                row_text = " | ".join(
                    cell.text.strip() for cell in row.cells if cell.text.strip()
                )
                if row_text and (not parts or row_text != parts[-1]):
                    parts.append(f"[TABLE]: {row_text}")

    return "\n".join(parts)


def detect_contract_type(contract_text: str) -> str:
    """
    Rule-based classification — fast, no LLM call needed for 3 known types.
    Falls back to LLM only if rules are inconclusive.
    """
    sample = contract_text[:3000].lower()

    # mua_ban_tieng_anh: pure English — no Vietnamese characters
    vietnamese_chars = "àáâãèéêìíòóôõùúýăđơưạảấầẩẫậắằẳẵặẹẻẽếềểễệỉịọỏốồổỗộớờởỡợụủứừửữựỳỵỷỹ"
    viet_count = sum(1 for c in sample if c in vietnamese_chars)
    if viet_count < 20 and ("seller" in sample or "customer" in sample or "buyer" in sample):
        print("  → Loại hợp đồng: mua_ban_tieng_anh (rule-based)")
        return "mua_ban_tieng_anh"

    # mua_ban_quoc_te: Vietnamese + international trade keywords
    intl_keywords = ["incoterms", "cif", "fob", "exw", "l/c", "port of", "cảng xếp", "cảng đích",
                     "letter of credit", "usd", "eur", "seller", "buyer"]
    intl_hits = sum(1 for kw in intl_keywords if kw in sample)
    if intl_hits >= 2:
        print("  → Loại hợp đồng: mua_ban_quoc_te (rule-based)")
        return "mua_ban_quoc_te"

    # mua_ban_don_gian: domestic Vietnamese contract
    domestic_keywords = ["bên a", "bên b", "bên bán", "bên mua", "hợp đồng mua bán", "vnđ", "đồng"]
    domestic_hits = sum(1 for kw in domestic_keywords if kw in sample)
    if domestic_hits >= 2:
        print("  → Loại hợp đồng: mua_ban_don_gian (rule-based)")
        return "mua_ban_don_gian"

    # Fallback to LLM if rules inconclusive
    print("  → Rule-based inconclusive, falling back to LLM...")
    return _detect_contract_type_llm(contract_text)


def _detect_contract_type_llm(contract_text: str) -> str:
    """LLM fallback for contract type classification."""
    type_list = "\n".join(
        f'- "{k}": {desc}'
        for k, desc in CONTRACT_TYPE_DESCRIPTIONS.items()
        if k in CONTRACT_REGISTRY
    )

    prompt = f"""Bạn là chuyên gia phân loại hợp đồng. Đọc đoạn đầu hợp đồng và phân loại vào đúng 1 loại.

    Các loại hợp đồng:
    {type_list}

    Chỉ trả về đúng key (ví dụ: mua_ban_don_gian), không giải thích, không markdown, không dấu ngoặc kép.

    Nội dung hợp đồng:
    {contract_text[:3000]}

    Loại hợp đồng:
    """

    response = client.converse(
        modelId=MODEL_ID_EXTRACT,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"temperature": 0, "maxTokens": 64},
    )
    detected = response["output"]["message"]["content"][0]["text"].strip().strip('"').strip()

    if detected not in CONTRACT_REGISTRY:
        fallback = next(iter(CONTRACT_REGISTRY))
        print(f"  [WARN] Không nhận dạng được '{detected}', fallback → '{fallback}'")
        detected = fallback

    print(f"  → Loại hợp đồng: {detected}")
    return detected


def _parse_json_from_response(raw_text: str) -> dict:
    from json_repair import repair_json

    if "```" in raw_text:
        raw_text = raw_text.split("```")[1]
        if raw_text.startswith("json"):
            raw_text = raw_text[4:]
        raw_text = raw_text.strip()

    start = raw_text.find("{")
    end   = raw_text.rfind("}") + 1
    if start == -1 or end == 0:
        raise ValueError(f"No JSON object in response: {raw_text[:200]}")

    candidate = raw_text[start:end]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        repaired = repair_json(candidate, return_objects=True)
        if isinstance(repaired, dict):
            return repaired
        raise ValueError(f"Cannot repair JSON: {candidate[:200]}")


# ── Extraction rules for mua_ban_quoc_te ─────────────────────────────────────

EXTRACTION_RULES_QUOC_TE = """
QUY TẮC TRÍCH XUẤT CHO HỢP ĐỒNG MUA BÁN QUỐC TẾ:

1. TRƯỜNG Số tiền / Giá trị / Giá cả (type=number):
   - Chỉ lấy phần số, bỏ đơn vị tiền tệ (USD, VNĐ...) và điều kiện giao hàng (CIF, FOB...)
   - Ví dụ: "100.000 USD CIF Hải Phòng" → 100000
   - Ví dụ: "2.000 USD/bộ" → 2000
   - Bỏ dấu chấm/phẩy phân cách hàng nghìn
   -> TRẢ VỀ NUMBER

2. TRƯỜNG TỶ LỆ % (type=number):
   - Chỉ lấy số, bỏ ký hiệu %, 
   - Ví dụ: "0.5% một tuần" → 0.5
   - Ví dụ: "110% giá trị hợp đồng" → 110
   -> TRẢ VỀ NUMBER

3. TRƯỜNG THỜI GIAN / SỐ NGÀY / SỐ THÁNG (type=number):
   - Chỉ lấy con số, bỏ đơn vị phía sau
   - Ví dụ: "60 ngày kể từ ngày bên bán nhận được L/C" → 60
   - Ví dụ: "06 tháng" → 6
   -> TRẢ VỀ NUMBER

4. TRƯỜNG PHỤ LỤC (type=string):
   - Lấy text sau chữ "Phụ lục" hoặc "phụ lục"
   - Ví dụ: "Phụ lục 01" → "01"
   -> TRẢ VỀ STRING

5. TRƯỜNG ĐIỀU (type=string):
   - Lấy text sau chữ "điều" hoặc "Điều"
   - Ví dụ: "Điều 5" → "5"
   - Ví dụ: "điều 5" → "5"
    - Ví dụ: "Điều 01" → "1"
   - Ví dụ: "điều 01" → "1"
   -> TRẢ VỀ STRING

6. TRƯỜNG NGÀY THÁNG NĂM (type=string):
   - Giữ nguyên định dạng đầy đủ
   -> TRẢ VỀ STRING

7. TRƯỜNG TÊN, ĐỊA CHỈ, MÔ TẢ (type=string):
   - Lấy nguyên văn từ tài liệu
   -> TRẢ VỀ STRING

8. TRƯỜNG THỜI GIAN (type=number):
    - Chỉ lấy con số, bỏ các đơn vị phía sau
    - Ví dụ: "15 ngày kể từ khi nhận được khiếu nại" -> 15
    -> TRẢ VỀ NUMBER

8. Tài khoản số, Mã số công ty, Giá, Thời gian → TRẢ VỀ NUMBER (bỏ dấu phân cách)

9. TRÍCH XUẤT TRUNG THỰC KHI THÔNG TIN BỊ THIẾU/CẮT ĐỨT:
   - LUÔN lấy giá trị thực tế, KỂ CẢ KHI SAI
   - Ví dụ: "Tổng giá trị hợp đồng là: abc USD" → "abc"
   - Ví dụ: "Giá cả: bộ" → "bộ"
   - Ví dụ: "theo quy định tại điều trong hợp đồng này" → "điều"
   - CHỈ để "" khi thực sự không có thông tin nào liên quan
"""


# ── Static prompt builders ────────────────────────────────────────────────────

def _build_prompt_static_don_gian(schema_template: str) -> str:
    return f"""Bạn là chuyên gia phân tích hợp đồng. Hãy đọc kỹ nội dung hợp đồng và điền vào schema JSON.

QUY TẮC TRÍCH XUẤT:

1. TRƯỜNG SỐ (type=number): chỉ lấy số, bỏ đơn vị, bỏ dấu phân cách hàng nghìn
   - "25.000.000 đồng" → 25000000
   - "10 cái" → 10
   - "0,5%" → 0.5
   - "500.000 đồng/ngày" → 500000
   - "12 tháng" → 12
   - "2 năm" → 2
   - TUYỆT ĐỐI không trả về string cho trường number, chỉ trả về con số thuần túy

2. TRƯỜNG NGÀY THÁNG NĂM (type=string): giữ nguyên định dạng đầy đủ

3. TRƯỜNG TÊN, ĐỊA CHỈ, MÔ TẢ (type=string): lấy nguyên văn

4. TRƯỜNG "Điều 1 - Hàng hóa" (type=object) — QUAN TRỌNG:
   - Trích xuất TOÀN BỘ các dòng hàng hóa trong bảng thành object với "items" array
   - Mỗi item gồm: stt (number), description (string), unit (string), quantity (number), unit_price (number), total_price (number), note (string hoặc "")
   - Bỏ dấu phân cách hàng nghìn trong số
   - Ví dụ:
     {{
       "items": [
         {{"stt": 1, "description": "Máy tính Dell XPS 13", "unit": "Cái", "quantity": 10, "unit_price": 25000000, "total_price": 250000000, "note": "Mới 100%"}}
       ]
     }}

5. TRÍCH XUẤT TRUNG THỰC: lấy giá trị thực tế kể cả khi sai, chỉ để "" khi thực sự không có thông tin

Chỉ trả về JSON thuần túy, KHÔNG markdown, KHÔNG giải thích.

Schema:
{schema_template}"""


def _build_prompt_static_quoc_te(schema_template: str) -> str:
    return f"""Bạn là chuyên gia phân tích hợp đồng. Hãy đọc kỹ nội dung hợp đồng và điền vào schema JSON.

{EXTRACTION_RULES_QUOC_TE}

QUY TẮC CHUNG:
- Giữ nguyên tên trường (key), chỉ thay thế giá trị (value)
- Nếu không có thông tin → để ""
- Trường type=number: trả về số (integer hoặc float), KHÔNG có dấu ngoặc kép
- Trường type=string: trả về chuỗi có dấu ngoặc kép
- Chỉ trả về JSON thuần túy, KHÔNG markdown, KHÔNG giải thích

Schema:
{schema_template}"""


def _build_prompt_static_tieng_anh(schema_template: str) -> str:
    return f"""You are a contract analysis expert. Read the contract carefully and fill in the JSON schema below.

EXTRACTION RULES FOR ENGLISH SALES CONTRACT:

1. MONETARY / AMOUNT FIELDS (type=number):
   - Extract the numeric value only, strip currency symbols (USD, $, etc.)
   - Example: "$10,000.00" → 10000, "USD 5,000" → 5000
   - Remove thousand separators
   -> RETURN NUMBER

2. PERCENTAGE FIELDS (type=number):
   - Extract the number only, strip the % symbol
   -> RETURN NUMBER

3. TIME / DAYS / MONTHS FIELDS (type=number):
   - Extract the number only, strip units (days, months, weeks...)
   - Example: "30 days written notice" → 30
   -> RETURN NUMBER

4. QUANTITY FIELD (inside "Goods and price" items) — CRITICAL:
   - Extract the EXACT value as written — do NOT convert or interpret
   - If written as a word (e.g. "Fifty", "Ten") → keep as string: "Fifty"
   - If written as a number (e.g. 100, -50) → keep as-is: 100 or -50
   - Do NOT convert "Fifty" → 50, do NOT fix negative values
   - The validator will check correctness — extract honestly only

5. DATE FIELDS (type=string):
   - Keep the full date as written: "April 7, 2026" → "April 7, 2026"
   -> RETURN STRING

6. NAME, ADDRESS, DESCRIPTION FIELDS (type=string):
   - Extract verbatim from the document
   -> RETURN STRING

7. "Goods and price" FIELD (type=object):
   - Extract as a JSON object with an "items" array
   - Each item: description (string), quantity (as-is), price_per_unit (number), total_price (number)
   - Example:
     {{
       "items": [
         {{"description": "Laptop Model X", "quantity": 10, "price_per_unit": 500, "total_price": 5000}}
       ]
     }}
   -> RETURN OBJECT (nested JSON)

GENERAL RULES:
- Keep field names (keys) unchanged, only replace the values
- If information is not found → use ""
- Fields with type=number: return a number (integer or float), NO quotes
- Fields with type=string: return a quoted string
- The "Goods and price" field must be a JSON object (not a string)
- Return pure JSON only, NO markdown, NO explanation

Schema:
{schema_template}"""


# ── Main extraction ───────────────────────────────────────────────────────────

def extract_contract_info(contract_text: str, schema: dict, contract_type: str = "") -> dict:
    from time import perf_counter
    from concurrent.futures import ThreadPoolExecutor

    schema_template = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))

    if contract_type == "mua_ban_don_gian":
        static_text = _build_prompt_static_don_gian(schema_template)
    elif contract_type == "mua_ban_tieng_anh":
        static_text = _build_prompt_static_tieng_anh(schema_template)
    else:
        static_text = _build_prompt_static_quoc_te(schema_template)

    # For large schemas, split into 2 parallel calls
    schema_items = list(schema.items())
    if len(schema_items) > 30 and contract_type not in ("mua_ban_don_gian", "mua_ban_tieng_anh"):
        mid = len(schema_items) // 2
        schema_a = dict(schema_items[:mid])
        schema_b = dict(schema_items[mid:])

        def _extract_chunk(chunk_schema: dict) -> dict:
            chunk_template = json.dumps(chunk_schema, ensure_ascii=False, separators=(",", ":"))
            static = _build_prompt_static_quoc_te(chunk_template)
            dynamic = f"\nNội dung hợp đồng:\n{contract_text}\n\nJSON trích xuất:"
            msgs = [{"role": "user", "content": [
                {"text": static},
                {"cachePoint": {"type": "default"}},
                {"text": dynamic},
            ]}]
            for attempt in range(1, 4):
                t0 = perf_counter()
                stream_resp = client.converse_stream(
                    modelId=MODEL_ID, messages=msgs,
                    inferenceConfig={"temperature": 0},
                )
                chunks = []
                for event in stream_resp["stream"]:
                    if "contentBlockDelta" in event:
                        delta = event["contentBlockDelta"]["delta"]
                        if "text" in delta:
                            chunks.append(delta["text"])
                raw = "".join(chunks).strip()
                print(f"    [4] chunk {len(chunk_schema)} fields: {perf_counter()-t0:.2f}s")
                try:
                    return _parse_json_from_response(raw)
                except (ValueError, json.JSONDecodeError) as e:
                    print(f"  [WARN] chunk attempt {attempt}/3 — {e}")
                    if attempt < 3:
                        msgs.append({"role": "assistant", "content": [{"text": raw}]})
                        msgs.append({"role": "user", "content": [{"text": f"JSON lỗi: {e}. Trả lại JSON hợp lệ, không markdown."}]})
            raise RuntimeError("Không thể parse JSON sau 3 lần thử.")

        t_start = perf_counter()
        with ThreadPoolExecutor(max_workers=2) as executor:
            future_a = executor.submit(_extract_chunk, schema_a)
            future_b = executor.submit(_extract_chunk, schema_b)
            result_a = future_a.result()
            result_b = future_b.result()

        result = {**result_a, **result_b}
        print(f"    [4] parallel extract done: {perf_counter()-t_start:.2f}s, {len(result)} fields")
        return result

    # Single call for small schemas
    dynamic_text = f"\nNội dung hợp đồng:\n{contract_text}\n\nJSON trích xuất:"
    messages = [{"role": "user", "content": [
        {"text": static_text},
        {"cachePoint": {"type": "default"}},
        {"text": dynamic_text},
    ]}]

    for attempt in range(1, 4):
        t_call = perf_counter()
        stream_resp = client.converse_stream(
            modelId=MODEL_ID, messages=messages,
            inferenceConfig={"temperature": 0},
        )
        raw_chunks  = []
        first_token = None
        for event in stream_resp["stream"]:
            if "contentBlockDelta" in event:
                delta = event["contentBlockDelta"]["delta"]
                if "text" in delta:
                    if first_token is None:
                        first_token = perf_counter()
                        print(f"    [4] time to first token: {first_token - t_call:.2f}s")
                    raw_chunks.append(delta["text"])

        raw_text = "".join(raw_chunks).strip()
        print(f"    [4] stream complete ({len(raw_text)} chars): {perf_counter() - t_call:.2f}s")

        try:
            result = _parse_json_from_response(raw_text)
            print(f"    [4] extracted {len(result)} fields")
            return result
        except (ValueError, json.JSONDecodeError) as e:
            print(f"  [WARN] Attempt {attempt}/3 — {e}")
            if attempt < 3:
                messages.append({"role": "assistant", "content": [{"text": raw_text}]})
                messages.append({"role": "user", "content": [{"text": (
                    f"JSON bị lỗi: {e}. Trả lại JSON hợp lệ, chỉ sửa cú pháp, không đổi nội dung, không markdown."
                )}]})

    raise RuntimeError("Không thể parse JSON sau 3 lần thử.")


# ── Full pipeline ─────────────────────────────────────────────────────────────

def process_contract(docx_path: str, contract_type: str = None) -> tuple[list[dict], dict, str, str]:
    from time import perf_counter

    def _step(label: str, t0: float) -> float:
        t1 = perf_counter()
        print(f"{t1 - t0:.2f}s")
        print(label)
        return t1

    os.makedirs(JSON_DIR, exist_ok=True)
    filename        = os.path.basename(docx_path)
    extracted_path  = os.path.join(JSON_DIR, filename.replace(".docx", "_extracted.json"))
    validation_path = os.path.join(JSON_DIR, filename.replace(".docx", "_validation.json"))

    total_t0 = perf_counter()
    print(f"\n{'='*50}")

    print(f"[1] Parse: {filename}")
    t0            = perf_counter()
    contract_text = read_docx(docx_path)
    t0            = _step(f"[2] Classify contract_type...", t0)

    if not contract_type:
        contract_type = detect_contract_type(contract_text)

    t0     = _step(f"[3] Load schema + [4] LLM extract...", t0)
    schema = get_extraction_schema(contract_type)
    print(f"    → {len(schema)} fields loaded")
    t0 = _step(f"[4] Generate dynamic prompt + LLM extract...", t0)

    extracted = extract_contract_info(contract_text, schema, contract_type)

    with open(extracted_path, "w", encoding="utf-8") as f:
        json.dump(extracted, f, ensure_ascii=False, indent=2)
    print(f"    → Saved: {extracted_path}")
    t0 = _step(f"[5] Validate against DynamoDB schema...", t0)

    results = validate_contract(extracted, contract_type, validation_path)
    t0      = _step(f"[6] Done", t0)

    errors = sum(1 for r in results if not r["corrected"])
    total  = perf_counter() - total_t0
    print(f"    → {len(results)} fields, {errors} errors")
    print(f"    Total: {total:.2f}s")
    print(f"{'='*50}")

    return results, extracted, contract_text, contract_type


if __name__ == "__main__":
    FILES_DIR = "files/hop-dong-mua-ban-don-gian"

    for filename in os.listdir(FILES_DIR):
        if filename.endswith(".docx"):
            process_contract(os.path.join(FILES_DIR, filename))
