import os
import re
import base64
import tempfile
import html
import streamlit as st
from main import process_contract
from dynamodb import CONTRACT_REGISTRY, CONTRACT_LABELS, ALLOWED_TYPES, update_field_type
from validator import fetch_schema_from_dynamodb

st.set_page_config(
    page_title="Panasonic Contract Checker",
    page_icon="images/panasonic-logo.jpg",
    layout="wide",
)

# ── Global styles ─────────────────────────────────────────────────────────────
st.markdown("""
<style>
    .block-container { padding-top: 1rem; margin-top: 20px; }
    div[data-testid="metric-container"] {
        background: #f0f5ff;
        border: 1px solid #d0e0ff;
        border-radius: 10px;
        padding: 12px 16px;
    }
    div[data-testid="stVerticalBlockBorderWrapper"] { border-radius: 8px !important; }
    section[data-testid="stSidebar"] { width: 250px !important; min-width: 200px !important; }
    section[data-testid="stSidebar"] > div { padding: 16px 12px !important; }
</style>
""", unsafe_allow_html=True)

# ── Sidebar — chỉ 2 nút ───────────────────────────────────────────────────────
if "page" not in st.session_state:
    st.session_state["page"] = "upload"

with st.sidebar:
    st.image("images/pana-logo-sidebar.png", width=200)
    st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)

    if st.button("📄\nUpload", use_container_width=True,
                 type="primary" if st.session_state["page"] == "upload" else "secondary"):
        st.session_state["page"] = "upload"
        st.rerun()

    st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)

    if st.button("⚙️\nConfig", use_container_width=True,
                 type="primary" if st.session_state["page"] == "config" else "secondary"):
        st.session_state["page"] = "config"
        st.rerun()

# ── Header (chỉ hiện ở trang Upload) ─────────────────────────────────────────
if st.session_state["page"] == "upload":
    hcol1, hcol2 = st.columns([1, 6])
    with hcol1:
        st.image("images/panasonic-logo.png", width=150)
    with hcol2:
        st.markdown("""
            <div style="padding-top:8px;">
                <h1 style="margin:0;font-size:26px;text-transform:uppercase;font-weight:700;color:#0057a8;">
                    Automated Contract Checker
                </h1>
                <p style="margin:4px 0 0;font-size:13px;color:#666;">
                    Upload a contract file (.docx) · Extract information · Highlight errors directly on the document
                </p>
            </div>
        """, unsafe_allow_html=True)
    st.markdown("<hr style='border:none;border-top:2px solid #0057a8;margin:12px 0 24px;'>", unsafe_allow_html=True)

# PAGE: CONFIG
if st.session_state["page"] == "config":
    st.markdown("## ⚙️ Config Rules")
    st.markdown("<div style='margin-bottom:16px;'></div>", unsafe_allow_html=True)

    selected_key = st.selectbox(
        "Contract type",
        options=list(CONTRACT_REGISTRY.keys()),
        format_func=lambda k: CONTRACT_LABELS[k],
    )

    fields = fetch_schema_from_dynamodb(selected_key)

    if not fields:
        st.warning("No schema found for this contract type.")
        st.stop()

    st.markdown(f"{len(fields)} fields for **{CONTRACT_LABELS[selected_key]}**")

    h1, h2, h3 = st.columns([4, 2, 2])
    h1.markdown("**Field name**")
    h2.markdown("**Type**")
    h3.markdown("**Required**")
    st.divider()

    for field in fields:
        col_name, col_type, col_req = st.columns([4, 2, 2])
        with col_name:
            st.markdown(field["field_name"])
        with col_type:
            current_type = field["type"] if field["type"] in ALLOWED_TYPES else ALLOWED_TYPES[0]
            new_type = st.selectbox(
                label="type",
                label_visibility="collapsed",
                options=ALLOWED_TYPES,
                index=ALLOWED_TYPES.index(current_type),
                key=f"type_{selected_key}_{field['field_name']}",
            )
        with col_req:
            st.markdown("✅" if field.get("required") else "—")

        if new_type != field["type"]:
            try:
                update_field_type(selected_key, field["field_name"], new_type)
                st.success(f"Updated **{field['field_name']}** to **`{new_type}`**")
            except Exception as e:
                st.error(f"Failed to update: {e}")

    st.stop()

# PAGE: UPLOAD & CHECK
uploaded = st.file_uploader("Select contract file (.docx)", type=["docx"])
run_btn  = st.button("Analyze & Check", disabled=not uploaded)

if run_btn and uploaded is not None:
    try:
        with st.spinner("Analyzing contract..."):
            with tempfile.NamedTemporaryFile(delete=False, suffix=".docx") as tmp:
                tmp.write(uploaded.read())
                tmp_path = tmp.name
            try:
                results, extracted, contract_text, contract_type = process_contract(tmp_path)
            finally:
                os.unlink(tmp_path)

        st.session_state["results"]       = results
        st.session_state["extracted"]     = extracted
        st.session_state["contract_text"] = contract_text
        st.session_state["contract_type"] = contract_type
        st.session_state["filename"]      = uploaded.name
        st.session_state["error_page"]    = 1
        st.rerun()
    except Exception as e:
        st.error(str(e))

if "results" not in st.session_state:
    st.markdown("""
        <div style="text-align:center;padding:40px;color:#aaa;">
            <div style="font-size:48px;">📄</div>
            <div style="font-size:16px;margin-top:8px;">Please upload a contract file to get started</div>
        </div>
    """, unsafe_allow_html=True)
    st.stop()

results: list[dict] = st.session_state["results"]
extracted: dict     = st.session_state["extracted"]
contract_text: str  = st.session_state["contract_text"]
contract_type: str  = st.session_state["contract_type"]
filename: str       = st.session_state["filename"]
typos: list[dict]   = st.session_state.get("typos", [])

errors   = [r for r in results if not r["corrected"]]
ok_count = len(results) - len(errors)

# ── Summary metrics ───────────────────────────────────────────────────────────
st.markdown("<div style='margin-bottom:8px;'></div>", unsafe_allow_html=True)
c1, c2, c3, c4 = st.columns([3, 2, 2, 2])
c1.metric("Contract Type", contract_type.replace("_", " ").title())
c2.metric("Total Fields", len(results))
c3.metric("✅ Valid", ok_count)
c4.metric("❌ Errors", len(errors), delta=f"-{len(errors)}" if errors else None, delta_color="inverse")
st.markdown("<div style='margin-bottom:16px;'></div>", unsafe_allow_html=True)

# ── Highlight helpers ─────────────────────────────────────────────────────────
error_rules: list[tuple]   = []
missing_rules: list[tuple] = []

for r in errors:
    val  = r["value"]
    hint = r.get("line_hint", "").strip()
    if not hint:
        parts = r["field"].split(" - ")
        hint  = re.sub(r"\s*\(.*?\)\s*$", "", parts[-1]).strip()
    if isinstance(val, dict):
        val = val.get("quantity", "")
    if val is not None and str(val).strip():
        error_rules.append((hint, str(val), r["field"], r["reason"] or ""))
    else:
        missing_rules.append((hint, r["field"], r["reason"] or ""))

typo_words: list[dict] = [t for t in typos if t.get("wrong")]


def highlight_line(line, wrong_rules, miss_rules, typo_list):
    escaped     = html.escape(line)
    had_wrong   = False
    had_missing = False
    had_typo    = False

    for hint, val, field, reason in sorted(wrong_rules, key=lambda x: -len(x[1])):
        escaped_val = html.escape(val)
        if escaped_val not in escaped:
            continue
        if hint and hint[:30].lower() not in line.lower():
            continue
        tooltip = html.escape(f"{field}: {reason}", quote=True)
        escaped = escaped.replace(
            escaped_val,
            f'<mark style="background:#ff4d4f;color:#fff;border-radius:3px;'
            f'padding:1px 4px;cursor:help;" title="{tooltip}">{escaped_val}</mark>', 1,
        )
        had_wrong = True

    if not had_wrong:
        for hint, field, reason in miss_rules:
            if not hint:
                continue
            if hint.lower() in line.lower():
                tooltip = html.escape(f"{field}: {reason}", quote=True)
                escaped = (
                    f'<span title="{tooltip}" style="background:#fff1f0;'
                    f'border-left:3px solid #ff4d4f;padding-left:6px;display:block;">'
                    f'{escaped}</span>'
                )
                had_missing = True
                break

    for t in sorted(typo_list, key=lambda x: -len(x.get("wrong", ""))):
        wrong   = t.get("wrong", "")
        correct = t.get("correct", "")
        if not wrong:
            continue
        escaped_wrong = html.escape(wrong)
        if escaped_wrong in escaped:
            tooltip = html.escape(f'Typo: "{wrong}" → "{correct}"', quote=True)
            escaped = escaped.replace(
                escaped_wrong,
                f'<mark style="background:#fadb14;color:#000;border-radius:3px;'
                f'padding:1px 4px;cursor:help;" title="{tooltip}">{escaped_wrong}</mark>', 1,
            )
            had_typo = True

    return escaped, had_wrong, had_missing, had_typo


# ── Two-column layout ─────────────────────────────────────────────────────────
left, right = st.columns([3, 2], gap="large")

with left:
    st.markdown(
        f'<div style="display:flex;align-items:center;gap:8px;margin-bottom:12px;">'
        f'<span style="font-size:20px;font-weight:600;">📋 Contract Content</span>'
        f'<span style="font-size:13px;color:#888;background:#f0f0f0;padding:2px 10px;'
        f'border-radius:12px;">{html.escape(filename)}</span>'
        f'</div>',
        unsafe_allow_html=True,
    )

    if not errors:
        st.success("No errors detected in the contract.")

    lines      = contract_text.split("\n")
    html_lines = []

    for line in lines:
        if not line.strip():
            html_lines.append("<br>")
            continue
        is_table = line.startswith("[TABLE]:")
        content  = line[8:].strip() if is_table else line
        rendered, had_wrong, had_missing, had_typo = highlight_line(content, error_rules, missing_rules, typo_words)
        had_error = had_wrong or had_missing
        if is_table:
            bg     = "#fff1f0" if had_error else "#fafafa"
            border = "#ff4d4f" if had_error else "#e0e0e0"
            html_lines.append(
                f'<div style="font-family:monospace;font-size:13px;color:#000;'
                f'background:{bg};border:1px solid {border};'
                f'border-radius:4px;padding:6px 10px;margin:3px 0;">{rendered}</div>'
            )
        else:
            html_lines.append(
                f'<p style="margin:4px 0;font-size:14px;line-height:1.8;color:#000;">{rendered}</p>'
            )

    st.markdown(
        '<div style="background:#fff;border:1px solid #e8e8e8;border-radius:10px;'
        'padding:20px 24px;max-height:72vh;overflow-y:auto;box-shadow:0 1px 4px rgba(0,0,0,0.06);">'
        + "".join(html_lines) + "</div>",
        unsafe_allow_html=True,
    )

with right:
    st.markdown('<div style="font-size:20px;font-weight:600;margin-bottom:4px;">Error List</div>',
                unsafe_allow_html=True)

    if not errors:
        st.success("Contract is valid — no errors found.")
    else:
        PAGE_SIZE   = 4
        total_pages = (len(errors) + PAGE_SIZE - 1) // PAGE_SIZE

        if total_pages > 1:
            cols = st.columns(total_pages + 2)
            if cols[0].button("‹", key="prev_page"):
                st.session_state["error_page"] = max(1, st.session_state.get("error_page", 1) - 1)
                st.rerun()
            for i in range(1, total_pages + 1):
                if cols[i].button(str(i), key=f"page_{i}",
                                  type="primary" if i == st.session_state.get("error_page", 1) else "secondary"):
                    st.session_state["error_page"] = i
                    st.rerun()
            if cols[total_pages + 1].button("›", key="next_page"):
                st.session_state["error_page"] = min(total_pages, st.session_state.get("error_page", 1) + 1)
                st.rerun()
            st.caption(f"Page {st.session_state.get('error_page', 1)}/{total_pages} · {len(errors)} errors")
        else:
            st.caption(f"{len(errors)} error(s)")

        page  = st.session_state.get("error_page", 1)
        start = (page - 1) * PAGE_SIZE
        for r in errors[start: start + PAGE_SIZE]:
            val        = r["value"]
            is_missing = val is None or (isinstance(val, str) and val.strip() == "")
            with st.container(border=True):
                st.markdown(f"**{r['field']}**")
                if is_missing:
                    st.markdown(f":red[{r['reason']}]")
                else:
                    display_val = val.get("quantity", str(val)) if isinstance(val, dict) else val
                    st.markdown(f"Value: `{display_val}`  \n:red[{r['reason']}]")

# ── Footer ────────────────────────────────────────────────────────────────────
with open("images/renova-logo.png", "rb") as f:
    renova_b64 = base64.b64encode(f.read()).decode()

st.markdown("<div style='margin-top:40px;'></div>", unsafe_allow_html=True)
fcol1, fcol2, fcol3 = st.columns([2, 2, 2])
with fcol2:
    st.markdown(
        f'<div style="text-align:center;">'
        f'<div style="font-size:12px;color:#aaa;margin-bottom:8px;">© Copyright Renova Cloud. All Rights Reserved.</div>'
        f'<img src="data:image/png;base64,{renova_b64}" style="width:150px;object-fit:contain;">'
        f'</div>',
        unsafe_allow_html=True,
    )
