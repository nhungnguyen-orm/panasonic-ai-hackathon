import re
import json
import boto3
import os
from decimal import Decimal
from boto3.dynamodb.conditions import Key
from dotenv import load_dotenv

load_dotenv()

# AWS_PROFILE       = os.getenv("AWS_PROFILE")
# AWS_REGION        = os.getenv("AWS_REGION")
# SCHEMA_TABLE_NAME = "PanasonicContractSchema"
SCHEMA_TABLE_NAME = "PanasonicContractSchemaDev"

# session           = boto3.Session(profile_name=AWS_PROFILE, region_name=AWS_REGION)
dynamodb_resource = boto3.resource("dynamodb", region_name='ap-southeast-2')


# ── Fetch schema for a specific contract type ─────────────────────────────────

def fetch_schema_from_dynamodb(contract_type: str) -> list[dict]:
    """
    Query only the fields belonging to the given contract_type.
    Returns list sorted by field_order.
    """
    table    = dynamodb_resource.Table(SCHEMA_TABLE_NAME)
    response = table.query(
        KeyConditionExpression=Key("contract_type").eq(contract_type)
    )
    items = response.get("Items", [])

    # Handle DynamoDB pagination
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


# ── Core validation ───────────────────────────────────────────────────────────

def validate(extracted: dict, schema_fields: list[dict]) -> list[dict]:
    """
    Compare each extracted value against the expected type from DynamoDB.
    Output: [{ "field", "value", "corrected", "reason" }]
    """
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
            })
            continue

        checker = TYPE_CHECKERS.get(field_type)
        if checker and not checker(value):
            results.append({
                "field":     field_name,
                "value":     value,
                "corrected": False,
                "reason":    f"wrong type - expected {field_type}",
            })
        else:
            results.append({
                "field":     field_name,
                "value":     value,
                "corrected": True,
                "reason":    None,
            })

    return results


# ── Entry point ───────────────────────────────────────────────────────────────

def validate_contract(extracted: dict, contract_type: str, output_path: str) -> list[dict]:
    """
    Fetch schema for contract_type from DynamoDB, validate extracted data,
    save results to output_path, and return results array.
    """
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