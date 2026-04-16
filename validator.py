import re
import json
import boto3
import os
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor, as_completed
from boto3.dynamodb.conditions import Key
from dotenv import load_dotenv

load_dotenv()

SCHEMA_TABLE_NAME = "PanasonicContractSchemaDev"

dynamodb_resource = boto3.resource("dynamodb", region_name='ap-southeast-2')

# Fetch schema
def fetch_schema_from_dynamodb(contract_type: str) -> list[dict]:
    table    = dynamodb_resource.Table(SCHEMA_TABLE_NAME)
    response = table.query(KeyConditionExpression=Key("contract_type").eq(contract_type))
    items    = response.get("Items", [])

    while "LastEvaluatedKey" in response:
        response = table.query(
            KeyConditionExpression=Key("contract_type").eq(contract_type),
            ExclusiveStartKey=response["LastEvaluatedKey"],
        )
        items.extend(response.get("Items", []))

    return sorted(items, key=lambda x: int(x["field_order"]))


# Type checkers 
_NUMERIC_RE = re.compile(r"^\d{1,3}(?:[.,]\d{3})*(?:[.,]\d+)?$|^\d+$")

def is_number(value) -> bool:
    if isinstance(value, (int, float, Decimal)):
        return True
    if isinstance(value, str):
        return bool(_NUMERIC_RE.match(value.strip()))
    return False

def extract_number(value) -> float | None:
    """
    Try to extract a leading number from a string like '500.000 đồng/ngày' → 500000.0
    Returns None if no number can be extracted.
    """
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    if not isinstance(value, str):
        return None
    # Remove thousand separators (dots before 3 digits) then grab leading number
    cleaned = re.sub(r"\.(?=\d{3}(?:[^\d]|$))", "", value.strip())
    cleaned = cleaned.replace(",", ".")
    m = re.match(r"^-?\d+(?:\.\d+)?", cleaned)
    if m:
        try:
            return float(m.group())
        except ValueError:
            return None
    return None

TYPE_CHECKERS = {
    "number": is_number,
    "string": lambda v: isinstance(v, str),
    "object": lambda v: isinstance(v, dict),
    "array":  lambda v: isinstance(v, list),
}


# Object item validation
def _validate_object_items(field_name: str, value: dict, item_schema: dict | None = None) -> list[dict]:
    """
    Validate each item inside value["items"] using item_schema from DynamoDB.
    item_schema: {sub_field: {type, required, min}} — loaded from DynamoDB attribute.
    Falls back to hardcoded defaults if item_schema is None.
    """
    results = []
    items   = value.get("items", [])
    if not isinstance(items, list):
        return results

    # Default fallback schema if not provided
    if not item_schema:
        item_schema = {
            "description":    {"type": "string", "required": True},
            "quantity":       {"type": "number", "required": True, "min": 1},
            "price_per_unit": {"type": "number", "required": True, "min": 0},
            "total_price":    {"type": "number", "required": True, "min": 0},
        }

    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue

        desc   = item.get("description", f"item[{i}]")
        errors = []

        for sub_field, sub_def in item_schema.items():
            sub_type     = sub_def.get("type", "string")
            sub_required = sub_def.get("required", False)
            sub_min      = sub_def.get("min")
            val          = item.get(sub_field)

            if val is None or val == "":
                if sub_required:
                    errors.append(f"{sub_field} missing")
                continue

            checker = TYPE_CHECKERS.get(sub_type)
            if checker and not checker(val):
                errors.append(f"{sub_field} wrong type - expected {sub_type}, got '{val}'")
                continue

            if sub_min is not None and isinstance(val, (int, float)):
                if float(val) < sub_min:
                    errors.append(f"{sub_field} must be >= {sub_min}, got {val}")

        # Cross-check total_price = quantity × unit_price (support both key names)
        qty      = item.get("quantity")
        ppu      = item.get("unit_price") or item.get("price_per_unit")
        tp       = item.get("total_price")
        if all(isinstance(v, (int, float)) for v in [qty, ppu, tp] if v is not None):
            if qty and ppu is not None and tp is not None:
                expected = round(float(qty) * float(ppu), 2)
                actual   = round(float(tp), 2)
                if abs(expected - actual) > 0.01:
                    errors.append(
                        f"total_price mismatch: {qty} × {ppu} = {expected}, but got {actual}"
                    )

        results.append({
            "field":       f"{field_name} - {desc}",
            "value":       item,
            "corrected":   len(errors) == 0,
            "reason":      "; ".join(errors) if errors else None,
            "line_number": 0,
        })

    return results


# Core validation 
def _validate_single_field(field_def: dict, extracted: dict, line_map: dict) -> list[dict]:
    field_name  = field_def["field_name"]
    field_type  = field_def.get("type", "string")
    required    = bool(field_def.get("required", False))
    value       = extracted.get(field_name)
    line_number = line_map.get(field_name, 0)

    if value is None or value == "":
        return [{
            "field":       field_name,
            "value":       value,
            "corrected":   False,
            "reason":      "missing required field" if required else "missing field",
            "line_number": line_number,
        }]

    checker = TYPE_CHECKERS.get(field_type)
    if checker and not checker(value):
        if field_type == "number" and isinstance(value, str):
            parsed = extract_number(value)
            reason = "expected number" if parsed is not None else f"wrong type - expected number, got '{value}'"
            return [{
                "field":       field_name,
                "value":       value,
                "corrected":   False,
                "reason":      reason,
                "line_number": line_number,
            }]
        return [{
            "field":       field_name,
            "value":       value,
            "corrected":   False,
            "reason":      f"wrong type - expected {field_type}",
            "line_number": line_number,
        }]

    if field_type == "object" and isinstance(value, dict):
        raw_item_schema = field_def.get("item_schema")
        item_schema = json.loads(raw_item_schema) if isinstance(raw_item_schema, str) else raw_item_schema
        return [
            {"field": field_name, "value": value, "corrected": True, "reason": None, "line_number": line_number},
            *_validate_object_items(field_name, value, item_schema),
        ]

    return [{"field": field_name, "value": value, "corrected": True, "reason": None, "line_number": line_number}]


def validate(extracted: dict, schema_fields: list[dict], line_map: dict | None = None) -> list[dict]:
    if line_map is None:
        line_map = {}
    with ThreadPoolExecutor(max_workers=min(16, len(schema_fields))) as executor:
        futures = {
            executor.submit(_validate_single_field, field_def, extracted, line_map): i
            for i, field_def in enumerate(schema_fields)
        }
        ordered: list[list[dict]] = [None] * len(schema_fields)
        for future in as_completed(futures):
            idx = futures[future]
            ordered[idx] = future.result()

    return [item for sublist in ordered for item in sublist]


# Entry point
def validate_contract(extracted: dict, contract_type: str, output_path: str) -> list[dict]:
    schema_fields = fetch_schema_from_dynamodb(contract_type)

    if not schema_fields:
        raise ValueError(
            f"Không tìm thấy schema cho contract_type='{contract_type}' trong DynamoDB. "
            "Hãy chạy python dynamodb.py để seed dữ liệu."
        )

    results = validate(extracted, schema_fields)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)

    total     = len(results)
    corrected = sum(1 for r in results if r["corrected"])
    print(f"  → Validation [{contract_type}]: {corrected}/{total} fields correct → {output_path}")

    return results
