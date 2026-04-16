"""
prompt_checker.py — Luồng Prompt-based Contract Check
Hoàn toàn độc lập với validator.py và rule-based flow.

Flow: contract_text + user_prompt (free-form chat) -> LLM tự hiểu rules -> list[{rule, position, result, reasoning}]
"""

import os
import re
import json
import boto3
from dotenv import load_dotenv

load_dotenv()

MODEL_ID_EXTRACT = os.getenv("MODEL_ID_EXTRACT")

_client = boto3.client("bedrock-runtime", region_name="ap-southeast-2")

SYSTEM_TEMPLATE = """# ROLE
You are a highly precise Contract Audit Middleware. The user will describe what they want to check in natural language (Vietnamese or English, casual or formal). Your job is to:
1. Understand the user's intent and extract ALL distinct rules/conditions they want verified.
2. Check each rule against the contract content.
3. Return findings in machine-readable format.

# RULES:
- DO NOT provide any introductory text
- ONLY output a valid JSON array inside <json></json>
- Each rule the user mentions must become a separate finding in the array

# OUTPUT FORMAT
Return a JSON **array** where each element represents one rule check:
<json>
[
  {{
    "rule": "Short clear description of the rule being checked",
    "position": "A SHORT phrase (5-10 words max) copied VERBATIM from ONE single line of the contract where the violation occurs. Must be findable via exact string search on one line. Use 'None' if no violation.",
    "result": true,
    "reasoning": "Explanation under 100 words."
  }}
]
</json>
- "result": true = violation found, false = passes
- "position": verbatim short snippet from a SINGLE contract line, NOT a full paragraph, NOT multi-line
- Extract ALL rules from the user's message, even if written casually or combined with 'và', 'and', ','

# USER'S CHECK REQUEST
{user_prompt}"""


def _parse_json_block(raw: str) -> list:
    match = re.search(r"<json>\s*(.*?)\s*</json>", raw, re.DOTALL)
    if not match:
        # fallback: tìm array trực tiếp
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        if not match:
            raise ValueError("No JSON array found in response")
    parsed = json.loads(match.group(1) if "<json>" in raw else match.group())
    if isinstance(parsed, dict):
        return [parsed]
    return parsed


def run_prompt_checks(contract_text: str, user_prompt: str) -> list[dict]:
    """
    Send the user's free-form prompt + contract text to LLM in a single call.
    LLM extracts all rules from the prompt and checks each one against the contract.

    Returns list of:
        {
            "rule":      str,   # rule description
            "position":  str,   # verbatim snippet or "None"
            "result":    bool,  # True = violation
            "reasoning": str,
        }
    """
    if not user_prompt or not user_prompt.strip():
        return []

    prompt = SYSTEM_TEMPLATE.format(user_prompt=user_prompt.strip())
    full_message = f"{prompt}\n\n# CONTRACT CONTENT\n{contract_text}"

    response = _client.converse(
        modelId=MODEL_ID_EXTRACT,
        messages=[{"role": "user", "content": [{"text": full_message}]}],
        inferenceConfig={"temperature": 0, "maxTokens": 1024},
    )
    raw = response["output"]["message"]["content"][0]["text"].strip()

    try:
        results = _parse_json_block(raw)
    except Exception as e:
        print(f"  [WARN] prompt_checker parse error: {e} | raw: {raw[:200]}")
        return []

    return [
        {
            "rule":      r.get("rule", user_prompt[:80]),
            "position":  r.get("position", "None"),
            "result":    bool(r.get("result", False)),
            "reasoning": r.get("reasoning", ""),
        }
        for r in results
        if isinstance(r, dict)
    ]
