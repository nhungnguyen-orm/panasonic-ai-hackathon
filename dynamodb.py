import boto3
import os
import json
from decimal import Decimal
from dotenv import load_dotenv

load_dotenv()

AWS_PROFILE       = os.getenv("AWS_PROFILE")
AWS_REGION        = os.getenv("AWS_REGION")
SCHEMA_TABLE_NAME = "PanasonicContractSchema"

session           = boto3.Session(profile_name=AWS_PROFILE, region_name=AWS_REGION)
dynamodb_client   = session.client("dynamodb")
dynamodb_resource = session.resource("dynamodb")


CONTRACT_REGISTRY = {
    "mua_ban_don_gian":  "schemas/mua-ban-don-gian.json",
    "mua_ban_quoc_te":   "schemas/mua-ban-quoc-te.json",
}


def create_schema_table():
    """
    Table structure:
      PK: contract_type  (S)
      SK: field_name     (S)
      field_order        (N)
      type               (S)
      required           (BOOL)
    """
    try:
        dynamodb_client.create_table(
            TableName=SCHEMA_TABLE_NAME,
            KeySchema=[
                {"AttributeName": "contract_type", "KeyType": "HASH"},
                {"AttributeName": "field_name",    "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "contract_type", "AttributeType": "S"},
                {"AttributeName": "field_name",    "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        print(f"Đang tạo table '{SCHEMA_TABLE_NAME}'...")
        dynamodb_client.get_waiter("table_exists").wait(TableName=SCHEMA_TABLE_NAME)
        print(f"Table '{SCHEMA_TABLE_NAME}' đã sẵn sàng.")
    except dynamodb_client.exceptions.ResourceInUseException:
        print(f"Table '{SCHEMA_TABLE_NAME}' đã tồn tại.")


def recreate_schema_table():
    """
    Delete the existing table (regardless of key schema) and recreate with correct keys.
    Use this when the table was created with an old key schema.
    """
    try:
        print(f"Đang xóa table cũ '{SCHEMA_TABLE_NAME}'...")
        dynamodb_client.delete_table(TableName=SCHEMA_TABLE_NAME)
        dynamodb_client.get_waiter("table_not_exists").wait(TableName=SCHEMA_TABLE_NAME)
        print(f"Đã xóa table '{SCHEMA_TABLE_NAME}'.")
    except dynamodb_client.exceptions.ResourceNotFoundException:
        print(f"Table '{SCHEMA_TABLE_NAME}' chưa tồn tại, bỏ qua bước xóa.")

    create_schema_table()


def seed_schema_table(schema: dict, contract_type: str):
    table = dynamodb_resource.Table(SCHEMA_TABLE_NAME)

    with table.batch_writer() as batch:
        for order, (field_name, meta) in enumerate(schema.items()):
            batch.put_item(Item={
                "contract_type": contract_type,
                "field_name":    field_name,
                "field_order":   Decimal(order),
                "type":          meta.get("type", "string"),
                "required":      meta.get("required", False),
            })

    print(f"  → Seeded {len(schema)} fields for '{contract_type}'.")


def load_schema(schema_path: str) -> dict:
    with open(schema_path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_extraction_schema(contract_type: str) -> dict:
    """
    Load the flat extraction schema {field: ""} directly from DynamoDB.
    This is the single source of truth — no local JSON files needed at runtime.
    """
    from validator import fetch_schema_from_dynamodb
    fields = fetch_schema_from_dynamodb(contract_type)
    if not fields:
        raise ValueError(
            f"Không tìm thấy schema cho '{contract_type}' trong DynamoDB. "
            "Hãy chạy: python dynamodb.py"
        )
    # Return flat {field_name: ""} ordered by field_order
    return {f["field_name"]: "" for f in fields}


if __name__ == "__main__":
    os.makedirs("schemas", exist_ok=True)

    recreate_schema_table()

    for contract_type, schema_path in CONTRACT_REGISTRY.items():
        if os.path.exists(schema_path):
            schema = load_schema(schema_path)
            seed_schema_table(schema, contract_type)
        else:
            print(f"  [SKIP] Schema file not found: {schema_path}")
