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

# AWS_PROFILE = os.getenv("AWS_PROFILE")
# AWS_REGION  = os.getenv("AWS_REGION")
MODEL_ID         = os.getenv("MODEL_ID")
MODEL_ID_EXTRACT = os.getenv("MODEL_ID_EXTRACT")

# session = boto3.Session(profile_name=AWS_PROFILE, region_name=AWS_REGION)
client  = boto3.client("bedrock-runtime", region_name='ap-southeast-2')

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


# Step 2: LLM classify contract_type
def detect_contract_type(contract_text: str) -> str:
    """
    Send first 3000 chars to Bedrock to classify into a known contract_type.
    Returns a key from CONTRACT_REGISTRY.
    """
    type_list = "\n".join(
        f'- "{k}": {desc}'
        for k, desc in CONTRACT_TYPE_DESCRIPTIONS.items()
        if k in CONTRACT_REGISTRY
    )

    prompt = f"""Bạn là chuyên gia phân loại hợp đồng. Đọc đoạn đầu hợp đồng và phân loại vào đúng 1 loại.

    Các loại hợp đồng:
    {type_list}

    Quy trình:
    1. Tìm các từ khóa đặc trưng trong văn bản (Bên A/B, Seller/Buyer, Incoterms, đơn vị tiền tệ...)
    2. Đối chiếu với mô tả từng loại
    3. Chọn loại phù hợp nhất

    Chỉ trả về đúng key (ví dụ: mua_ban_don_gian), không giải thích, không markdown, không dấu ngoặc kép.

    Nội dung hợp đồng:
    {contract_text[:3000]}

    Loại hợp đồng:
    """

    response = client.converse(
        modelId=MODEL_ID_EXTRACT,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
    )
    detected = response["output"]["message"]["content"][0]["text"].strip().strip('"').strip()

    if detected not in CONTRACT_REGISTRY:
        fallback = next(iter(CONTRACT_REGISTRY))
        print(f"  [WARN] Không nhận dạng được '{detected}', fallback → '{fallback}'")
        detected = fallback

    print(f"  → Loại hợp đồng: {detected}")
    return detected


# Step 3: Load ContractSchema from DynamoDB → Generate dynamic prompt

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


# Step 4: LLM extract
EXTRACTION_RULES = {
    """
QUY TẮC TRÍCH XUẤT CHO HỢP ĐỒNG MUA BÁN QUỐC TẾ:

1. TRƯỜNG Số tiền / Giá trị / Giá cả (type=number):
   - Chỉ lấy phần số, bỏ đơn vị tiền tệ (USD, VNĐ...) và điều kiện giao hàng (CIF, FOB...)
   - Ví dụ: "100.000 USD CIF Hải Phòng" → 100000
   - Ví dụ: "2.000 USD/bộ" → 2000
   - Ví dụ: "10.000 USD" → 10000
   - Bỏ dấu chấm/phẩy phân cách hàng nghìn
   -> TRẢ VỀ NUMBER

2. TRƯỜNG TỶ LỆ % (type=number):
   - Chỉ lấy số, bỏ ký hiệu %
   - Ví dụ: "10% giá trị hợp đồng" → 10
   - Ví dụ: "0.5% một tuần" → 0.5
   - Ví dụ: "5%" → 5
   -> TRẢ VỀ NUMBER

3. TRƯỜNG THỜI GIAN / SỐ NGÀY / SỐ THÁNG (type=number):
   - Chỉ lấy con số, bỏ đơn vị (ngày, tháng, tuần...)
   - Ví dụ: "60 ngày kể từ ngày bên bán nhận được L/C" → 60
   - Ví dụ: "15 ngày kể từ khi nhận được khiếu nại" → 15
   - Ví dụ: "10 ngày sau khi ký hợp đồng" → 10
   - Ví dụ: "06 tháng" → 6
   - Ví dụ: "24 tháng" → 24
   -> TRẢ VỀ NUMBER

4. TRƯỜNG PHỤ LỤC (type=string):
    - Lấy text sau chữ "Phụ lục" hoặc "phụ lục"
     Ví dụ: "Phụ lục 01" → "01"
     Ví dụ: "phụ lục 02" -> "02"
     -> TRẢ VỀ STRING

5. TRƯỜNG ĐIỀU (type=string):
   - Lấy text sau chữ "điều" hoặc "Điều"
   - Ví dụ: "điều 01" → "01"
   - Ví dụ: "Điều 5" → "5"
   -> TRẢ VỀ STRING

6. TRƯỜNG NGÀY THÁNG NĂM (type=string):
   - Giữ nguyên định dạng đầy đủ
   - Ví dụ: "07 tháng 04 năm 2026" → "07 tháng 04 năm 2026"
   -> TRẢ VỀ STRING

7. TRƯỜNG TÊN, ĐỊA CHỈ, MÔ TẢ (type=string):
   - Lấy nguyên văn từ tài liệu
   -> TRẢ VỀ STRING

8. TRƯỜNG LIÊN QUAN ĐẾN Tài khoản số, Mã số công ty, Giá, Thời gian → TRẢ VỀ NUMBER (bỏ dấu phân cách)

9. TRÍCH XUẤT TRUNG THỰC KHI THÔNG TIN BỊ THIẾU/CẮT ĐỨT:
   - Nếu câu văn bị cắt đứt hoặc thiếu thông tin (ví dụ: "danh mục vật tư ở Phụ lục" không có số),
     hãy điền giá trị gần nhất có thể suy ra từ ngữ cảnh xung quanh.
   - Ví dụ: "danh mục vật tư ở Phụ lục" (thiếu số) → tìm số phụ lục được đề cập gần nhất trong Điều đó
   - KHÔNG để trống ("") nếu có thể suy ra giá trị từ ngữ cảnh
   - Nếu thực sự không thể suy ra → để ""
"""
}

def _build_prompt_static_don_gian(schema_template: str) -> str:
    """Static portion of the mua_ban_don_gian prompt — suitable for caching."""
    return f"""Bạn là chuyên gia phân tích hợp đồng. Hãy đọc kỹ nội dung hợp đồng và thực hiện 2 bước:

BƯỚC 1 - TRÍCH XUẤT TỰ DO:
Đọc toàn bộ hợp đồng và liệt kê TẤT CẢ thông tin có trong tài liệu.

BƯỚC 2 - MAP VÀO SCHEMA:
Điền giá trị vào đúng các trường trong schema JSON bên dưới.
- Giữ nguyên tên trường (key), chỉ thay thế giá trị (value)
- Nếu tên trường trong tài liệu hơi khác → vẫn map vào trường phù hợp nhất
- Nếu không có thông tin → để ""

QUAN TRỌNG - TRÍCH XUẤT TRUNG THỰC:
- LUÔN lấy giá trị thực tế trong tài liệu, kể cả khi sai định dạng
- KHÔNG bỏ trống nếu ô đó có nội dung (dù sai)

Quy tắc kiểu dữ liệu:
- Ngày tháng năm đầy đủ → STRING
- Số nguyên thuần túy (số lượng, tiền, mã số, SĐT, chi phí, thời gian) → NUMBER (bỏ dấu phân cách)
- Tỷ lệ % → NUMBER (chỉ lấy số)
- Tên, địa chỉ, mô tả → STRING

Chỉ trả về JSON thuần túy, KHÔNG markdown, KHÔNG giải thích.

Schema:
{schema_template}"""


def _build_prompt_static_quoc_te(schema_template: str, extraction_rules: str) -> str:
    """Static portion of the mua_ban_quoc_te prompt — suitable for caching."""
    return f"""Bạn là chuyên gia phân tích hợp đồng. Hãy đọc kỹ nội dung hợp đồng và điền vào schema JSON.

{extraction_rules}

QUY TẮC CHUNG:
- Giữ nguyên tên trường (key), chỉ thay thế giá trị (value)
- Nếu không có thông tin → để ""
- Trường type=number: trả về số (integer hoặc float), KHÔNG có dấu ngoặc kép
- Trường type=string: trả về chuỗi có dấu ngoặc kép
- Chỉ trả về JSON thuần túy, KHÔNG markdown, KHÔNG giải thích

Schema:
{schema_template}"""


def _build_prompt_static_tieng_anh(schema_template: str) -> str:
    """Static portion of the mua_ban_tieng_anh prompt — suitable for caching."""
    return f"""You are a contract analysis expert. Read the contract carefully and fill in the JSON schema below.

    EXTRACTION RULES FOR ENGLISH SALES CONTRACT:

    1. MONETARY / AMOUNT FIELDS (type=number):
    - Extract the numeric value only, strip currency symbols (USD, $, etc.)
    - Example: "$10,000.00" → 10000
    - Example: "USD 5,000" → 5000
    - Remove thousand separators
    -> RETURN NUMBER

    2. PERCENTAGE FIELDS (type=number):
    - Extract the number only, strip the % symbol
    - Example: "10% of contract value" → 10
    -> RETURN NUMBER

    3. TIME / DAYS / MONTHS FIELDS (type=number):
    - Extract the number only, strip units (days, months, weeks...)
    - Example: "30 days written notice" → 30
    - Example: "6 months" → 6
    -> RETURN NUMBER

    4. DATE FIELDS (type=string):
    - Keep the full date as written in the document
    - Example: "April 7, 2026" → "April 7, 2026"
    -> RETURN STRING

    5. NAME, ADDRESS, DESCRIPTION FIELDS (type=string):
    - Extract verbatim from the document
    -> RETURN STRING

    6. "Goods and price" FIELD (type=object):
    - Extract as a JSON object with an "items" array
    - Each item must have: description, quantity (integer), price_per_unit (number), total_price (number)
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


def _build_prompt(contract_text: str, schema_template: str, contract_type: str) -> str:
    if contract_type == "mua_ban_don_gian":
        return f"""
    Bạn là chuyên gia phân tích hợp đồng. Hãy đọc kỹ nội dung hợp đồng và thực hiện 2 bước:

    BƯỚC 1 - TRÍCH XUẤT TỰ DO:
    Đọc toàn bộ hợp đồng và liệt kê TẤT CẢ thông tin có trong tài liệu.

    BƯỚC 2 - MAP VÀO SCHEMA:
    Điền giá trị vào đúng các trường trong schema JSON bên dưới.
    - Giữ nguyên tên trường (key), chỉ thay thế giá trị (value)
    - Nếu tên trường trong tài liệu hơi khác → vẫn map vào trường phù hợp nhất
    - Nếu không có thông tin → để ""

    QUAN TRỌNG - TRÍCH XUẤT TRUNG THỰC:
    - LUÔN lấy giá trị thực tế trong tài liệu, kể cả khi sai định dạng
    - KHÔNG bỏ trống nếu ô đó có nội dung (dù sai)
    - Hệ thống khác sẽ kiểm tra đúng/sai, bạn chỉ cần trích xuất trung thực

    Quy tắc kiểu dữ liệu (chỉ áp dụng khi giá trị RÕ RÀNG đúng loại):
    - Ngày tháng năm đầy đủ → STRING
    - Số nguyên thuần túy (số lượng, tiền, mã số, SĐT, chi phí, thời gian) → NUMBER (bỏ dấu phân cách)
    - Tỷ lệ % → NUMBER (chỉ lấy số)
    - Tên, địa chỉ, mô tả → STRING
    - Giá trị hỗn hợp → STRING
    - Chi phí -> NUMBER (bỏ dấu phân cách và chỉ lấy số)

    Chỉ trả về JSON thuần túy, KHÔNG markdown, KHÔNG giải thích.

    Schema (giữ nguyên key, chỉ điền value):
    {schema_template}

    Nội dung hợp đồng:
    {contract_text}

    JSON trích xuất:
    """

    # mua_ban_quoc_te — rules chi tiết theo từng pattern field
    extraction_rules = EXTRACTION_RULES.get(contract_type, "")

    if contract_type == "mua_ban_tieng_anh":
        return f"""
    You are a contract analysis expert. Read the contract carefully and fill in the JSON schema.

    {extraction_rules}

    GENERAL RULES:
    - Keep field names (keys) unchanged, only replace the values
    - If information is not found → use ""
    - Fields with type=number: return a number (integer or float), NO quotes
    - Fields with type=string: return a quoted string
    - The "Goods and price" field must be a JSON object (not a string)
    - Return pure JSON only, NO markdown, NO explanation

    Schema (keep keys, fill values only):
    {schema_template}

    Contract content:
    {contract_text}

    Extracted JSON:
    """

    return f"""
    Bạn là chuyên gia phân tích hợp đồng. Hãy đọc kỹ nội dung hợp đồng và thực hiện 2 bước:

    BƯỚC 1 - TRÍCH XUẤT TỰ DO:
    Đọc toàn bộ hợp đồng và liệt kê TẤT CẢ thông tin có trong tài liệu.

    BƯỚC 2 - MAP VÀO SCHEMA:
    Điền giá trị vào đúng các trường trong schema JSON bên dưới.
    - Giữ nguyên tên trường (key), chỉ thay thế giá trị (value)
    - Nếu tên trường trong tài liệu hơi khác → vẫn map vào trường phù hợp nhất
    - Nếu không có thông tin → để ""

    QUAN TRỌNG - TRÍCH XUẤT TRUNG THỰC:
    - LUÔN lấy giá trị thực tế trong tài liệu, kể cả khi sai định dạng
    - KHÔNG bỏ trống nếu ô đó có nội dung (dù sai)
    - Hệ thống khác sẽ kiểm tra đúng/sai, bạn chỉ cần trích xuất trung thực

    {extraction_rules}

    QUY TẮC CHUNG:
    - Giữ nguyên tên trường (key), chỉ thay thế giá trị (value)
    - Nếu tên trường trong tài liệu hơi khác → vẫn map vào trường phù hợp nhất
    - Nếu không có thông tin → để ""
    - Trường type=number: trả về số (integer hoặc float), KHÔNG có dấu ngoặc kép
    - Trường type=string: trả về chuỗi có dấu ngoặc kép
    - Chỉ trả về JSON thuần túy, KHÔNG markdown, KHÔNG giải thích

    Schema (giữ nguyên key, chỉ điền value):
    {schema_template}

    Nội dung hợp đồng:
    {contract_text}

    JSON trích xuất:
    """




def extract_contract_info(contract_text: str, schema: dict, contract_type: str = "") -> dict:
    """
    Single LLM call with full schema — avoids missing fields caused by context split.
    Retries up to 3 times on JSON parse failure.
    """
    from time import perf_counter

    schema_template  = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    extraction_rules = EXTRACTION_RULES

    if contract_type == "mua_ban_don_gian":
        static_text = _build_prompt_static_don_gian(schema_template)
    elif contract_type == "mua_ban_tieng_anh":
        static_text = _build_prompt_static_tieng_anh(schema_template)
    else:
        static_text = _build_prompt_static_quoc_te(schema_template, extraction_rules)

    dynamic_text = f"\nNội dung hợp đồng:\n{contract_text}\n\nJSON trích xuất:"

    messages = [
        {
            "role": "user",
            "content": [
                {"text": static_text},
                {"cachePoint": {"type": "default"}},
                {"text": dynamic_text},
            ],
        }
    ]

    for attempt in range(1, 4):
        t_call = perf_counter()
        stream_resp = client.converse_stream(modelId=MODEL_ID, messages=messages)

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


# Full pipeline

def process_contract(docx_path: str, contract_type: str = None) -> tuple[list[dict], dict, str, str]:
    """
    Upload → Parse → LLM classify → Load ContractSchema (DynamoDB)
    → Generate dynamic prompt → LLM extract → Validate → Return results

    Returns: (validation_results, extracted_data, raw_text, contract_type)
    """
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
    t0 = perf_counter()
    contract_text = read_docx(docx_path)
    t0 = _step(f"[2] LLM classify contract_type...", t0)

    if not contract_type:
        contract_type = detect_contract_type(contract_text)
    t0 = _step(f"[3] Load ContractSchema từ DynamoDB (contract_type={contract_type})...", t0)

    schema = get_extraction_schema(contract_type)
    print(f"    → {len(schema)} fields loaded")
    t0 = _step(f"[4] Generate dynamic prompt + LLM extract...", t0)

    extracted = extract_contract_info(contract_text, schema, contract_type)

    with open(extracted_path, "w", encoding="utf-8") as f:
        json.dump(extracted, f, ensure_ascii=False, indent=2)
    print(f"    → Saved: {extracted_path}")
    t0 = _step(f"[5] Validate against DynamoDB schema...", t0)

    results = validate_contract(extracted, contract_type, validation_path)
    t0 = _step(f"[6] Done", t0)

    errors = sum(1 for r in results if not r["corrected"])
    total = perf_counter() - total_t0
    print(f"    → {len(results)} fields, {errors} errors")
    print(f"    Total: {total:.2f}s")
    print(f"{'='*50}")

    return results, extracted, contract_text, contract_type


if __name__ == "__main__":
    FILES_DIR = "files/hop-dong-mua-ban-don-gian"

    for filename in os.listdir(FILES_DIR):
        if filename.endswith(".docx"):
            process_contract(os.path.join(FILES_DIR, filename))