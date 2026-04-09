import re
import json
import boto3
import os
from decimal import Decimal
from boto3.dynamodb.conditions import Key
from dotenv import load_dotenv

load_dotenv()

SCHEMA_TABLE_NAME = "PanasonicContractSchemaDev"

dynamodb_resource = boto3.resource("dynamodb", region_name='ap-southeast-2')


# ── Fetch schema ──────────────────────────────────────────────────────────────

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


# ── Type checkers ─────────────────────────────────────────────────────────────

_NUMERIC_RE = re.compile(r"^\d{1,3}(?:[.,]\d{3})*(?:[.,]\d+)?$|^\d+$")

def is_number(value) -> bool:
    if isinstance(value, (int, float, Decimal)):
        return True
    if isinstance(value, str):
        return bool(_NUMERIC_RE.match(value.strip()))
    return False

TYPE_CHECKERS = {
    "number": is_number,
    "string": lambda v: isinstance(v, str),
    "object": lambda v: isinstance(v, dict),
    "array":  lambda v: isinstance(v, list),
}


# ── Object item validation ────────────────────────────────────────────────────

def _validate_object_items(field_name: str, value: dict) -> list[dict]:
    """
    Validate each item inside value["items"].
    Returns one result entry per item with corrected=True/False.
    """
    results = []
    items   = value.get("items", [])
    if not isinstance(items, list):
        return results

    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        desc   = item.get("description", f"item[{i}]")
        qty    = item.get("quantity")
        errors = []

        if qty is None:
            errors.append("quantity missing")
        elif not isinstance(qty, (int, float)):
            errors.append(f"quantity wrong type - expected number, got '{qty}'")
        else:
            try:
                if float(qty) < 1:
                    errors.append("quantity must be greater than 0")
            except (TypeError, ValueError):
                errors.append(f"quantity wrong type - expected number, got '{qty}'")

        results.append({
            "field":     f"{field_name} - {desc}",
            "value":     item,
            "corrected": len(errors) == 0,
            "reason":    "; ".join(errors) if errors else None,
        })

    return results


# ── Core validation ───────────────────────────────────────────────────────────

def validate(extracted: dict, schema_fields: list[dict]) -> list[dict]:
    results = []

    for field_def in schema_fields:
        field_name = field_def["field_name"]
        field_type = field_def.get("type", "string")
        required   = bool(field_def.get("required", False))
        value      = extracted.get(field_name)

        if value is None or value == "":
            results.append({
                "field":     field_name,
                "value":     value,
                "corrected": False,
                "reason":    "missing required field" if required else "missing field",
                "line_hint": field_def.get("line_hint", ""),
            })
            continue

        checker = TYPE_CHECKERS.get(field_type)
        if checker and not checker(value):
            results.append({
                "field":     field_name,
                "value":     value,
                "corrected": False,
                "reason":    f"wrong type - expected {field_type}",
                "line_hint": field_def.get("line_hint", ""),
            })
        elif field_type == "object" and isinstance(value, dict):
            results.append({
                "field":     field_name,
                "value":     value,
                "corrected": True,
                "reason":    None,
                "line_hint": "",
            })
            results.extend(_validate_object_items(field_name, value))
        else:
            results.append({
                "field":     field_name,
                "value":     value,
                "corrected": True,
                "reason":    None,
                "line_hint": "",
            })

    return results


# ── Entry point ───────────────────────────────────────────────────────────────

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
