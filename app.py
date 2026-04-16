import os
import re
import base64
import tempfile
import html
import streamlit as st
from main import process_contract
from dynamodb import CONTRACT_REGISTRY, CONTRACT_LABELS, ALLOWED_TYPES, update_field_type
from validator import fetch_schema_from_dynamodb
from prompt_checker import run_prompt_checks

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

# Sidebar
if "page" not in st.session_state:
    st.session_state["page"] = "upload"

with open("images/renova-logo.png", "rb") as _f:
    _renova_b64 = base64.b64encode(_f.read()).decode()

with st.sidebar:
    st.image("images/pana-logo-sidebar.png", width=200)
    st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)

    if st.button("📄\nUpload", use_container_width=True,
                 type="primary" if st.session_state["page"] == "upload" else "secondary"):
        st.session_state["page"] = "upload"
        st.rerun()

    st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)

    if st.button("🤖\nPrompt Check", use_container_width=True,
                 type="primary" if st.session_state["page"] == "prompt" else "secondary"):
        st.session_state["page"] = "prompt"
        st.rerun()

    st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)

    if st.button("⚙️\nConfig", use_container_width=True,
                 type="primary" if st.session_state["page"] == "config" else "secondary"):
        st.session_state["page"] = "config"
        st.rerun()

    # Spacer đẩy footer xuống 
    st.markdown('<div style="flex:1;min-height:60px;"></div>', unsafe_allow_html=True)

    # Footer
    st.markdown(
        f'<div style="padding:20px 16px;border-top:1px solid #21262d;text-align:center; margin-top:20px">'
        f'<img src="data:image/png;base64,{_renova_b64}" style="width:140px;object-fit:contain;opacity:0.9;">'
        f'</div>',
        unsafe_allow_html=True,
    )

# Header (chỉ hiện ở Upload)
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
if st.session_state["page"] == "upload":

    uploaded = st.file_uploader("Select contract file (.docx)", type=["docx"])
    run_btn  = st.button("Analyze & Check", disabled=not uploaded)

    if run_btn and uploaded is not None:
        try:
            with st.status("Analyzing contract...", expanded=True) as status:
                step_placeholder = st.empty()
                steps_done = []

                def on_step(msg: str, state: str):
                    if state == "done":
                        steps_done.append(msg)
                    lines_html = "".join(
                        f'<div style="font-size:13px;color:#52c41a;margin:2px 0;">✓ {s}</div>'
                        for s in steps_done
                    )
                    if state == "running":
                        lines_html += f'<div style="font-size:13px;color:#888;margin:2px 0;">⏳ {msg}</div>'
                    step_placeholder.markdown(lines_html, unsafe_allow_html=True)

                with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(uploaded.name)[1]) as tmp:
                    tmp.write(uploaded.read())
                    tmp_path = tmp.name
                try:
                    results, extracted, lines, contract_type = process_contract(tmp_path, on_step=on_step)
                finally:
                    os.unlink(tmp_path)

                status.update(label="✅ Analysis complete!", state="complete", expanded=False)

            st.session_state["rb_results"]      = results
            st.session_state["rb_extracted"]    = extracted
            st.session_state["rb_lines"]        = lines
            st.session_state["rb_contract_type"]= contract_type
            st.session_state["rb_filename"]     = uploaded.name
            st.session_state["error_page"]      = 1
            st.rerun()
        except Exception as e:
            st.error(str(e))

    if "rb_results" not in st.session_state or "rb_lines" not in st.session_state:
        st.markdown("""
            <div style="text-align:center;padding:40px;color:#aaa;">
                <div style="font-size:48px;">📄</div>
                <div style="font-size:16px;margin-top:8px;">Please upload a contract file to get started</div>
            </div>
        """, unsafe_allow_html=True)
        st.stop()

    results: list[dict] = st.session_state["rb_results"]
    extracted: dict     = st.session_state["rb_extracted"]
    lines: list[str]    = st.session_state["rb_lines"]
    contract_type: str  = st.session_state["rb_contract_type"]
    filename: str       = st.session_state["rb_filename"]
    typos: list[dict]   = st.session_state.get("typos", [])

    errors   = [r for r in results if not r["corrected"]]
    ok_count = len(results) - len(errors)

    # Summary metrics
    st.markdown("<div style='margin-bottom:8px;'></div>", unsafe_allow_html=True)
    c1, c2, c3, c4 = st.columns([3, 2, 2, 2])
    c1.metric("Contract Type", contract_type.replace("_", " ").title())
    c2.metric("Total Fields", len(results))
    c3.metric("✅ Valid", ok_count)
    c4.metric("❌ Errors", len(errors), delta=f"-{len(errors)}" if errors else None, delta_color="inverse")
    st.markdown("<div style='margin-bottom:16px;'></div>", unsafe_allow_html=True)

    # Highlight helpers
    # Build: line_number → list of (value_to_highlight, field, reason)
    error_lines: dict[int, list[tuple]] = {}
    # Fallback text-search list cho errors không có line_number
    # Tuple: (search_text, field, reason, is_missing, color)
    error_text_search: list[tuple] = []

    for r in errors:
        ln     = r.get("line_number", 0)
        val    = r.get("value")
        reason = r.get("reason") or ""

        val_str = ""
        if isinstance(val, dict):
            val_str = ""
        elif val is not None and str(val).strip():
            val_str = str(val).strip()

        # Extract giá trị từ reason nếu val_str rỗng (object items: "got 'Fifty'")
        if not val_str and reason:
            m = re.search(r"got ['\"](.+?)['\"]", reason)
            if m:
                val_str = m.group(1)

        if ln and ln > 0:
            error_lines.setdefault(ln, []).append((val_str, r["field"], reason))
        else:
            if val_str:
                error_text_search.append((val_str, r["field"], reason, False, "#ff4d4f"))
            else:
                # Missing field: thử full keyword rồi first word làm fallback
                parts   = r["field"].split(" - ")
                keyword = re.sub(r"\s*\(.*?\)\s*$", "", parts[-1]).strip()
                if keyword:
                    error_text_search.append((keyword, r["field"], reason, True, "#ff4d4f"))
                    first_word = keyword.split()[0]
                    if first_word != keyword and len(first_word) >= 4:
                        error_text_search.append((first_word, r["field"], reason, True, "#ff4d4f"))

    typo_words: list[dict] = [t for t in typos if t.get("wrong")]

    def highlight_line(line_idx: int, line: str, typo_list: list) -> tuple[str, bool]:
        escaped   = html.escape(line)
        had_error = False

        # Schema errors: highlight đúng value theo line_number
        if (line_idx + 1) in error_lines:
            for val_str, field, reason in error_lines[line_idx + 1]:
                tooltip = html.escape(f"{field}: {reason}", quote=True)
                if val_str:
                    escaped_val = html.escape(val_str)
                    # Chỉ match text không nằm trong HTML tag (không có > trước đó chưa đóng)
                    pattern = re.compile(re.escape(escaped_val) + r'(?![^<]*>)')
                    new_escaped = pattern.sub(
                        f'<mark style="background:#ff4d4f;color:#fff;border-radius:3px;'
                        f'padding:1px 4px;cursor:help;" title="{tooltip}">{escaped_val}</mark>',
                        escaped, count=1
                    )
                    if new_escaped != escaped:
                        escaped   = new_escaped
                        had_error = True
                        continue
                # val rỗng (missing) → bôi cả dòng
                escaped = (
                    f'<span title="{tooltip}" style="background:#fff1f0;'
                    f'border-left:3px solid #ff4d4f;padding-left:6px;display:block;">'
                    f'{escaped}</span>'
                )
                had_error = True

        # Fallback text search cho errors không có line_number
        if not had_error:
            for search, field, reason, is_missing, color in sorted(error_text_search, key=lambda x: -len(x[0])):
                escaped_s    = html.escape(search)
                escaped_line = html.escape(line)
                # Case-insensitive search
                if escaped_s.lower() in escaped_line.lower():
                    tooltip = html.escape(f"{field}: {reason}", quote=True)
                    if is_missing:
                        escaped = (
                            f'<span title="{tooltip}" style="background:#fff1f0;'
                            f'border-left:3px solid {color};padding-left:6px;display:block;">'
                            f'{escaped}</span>'
                        )
                    else:
                        # Case-sensitive replace để giữ nguyên text gốc
                        idx = escaped.lower().find(escaped_s.lower())
                        if idx >= 0:
                            original = escaped[idx:idx+len(escaped_s)]
                            # Kiểm tra không nằm trong HTML tag
                            before = escaped[:idx]
                            if before.count('<') == before.count('>'):
                                escaped = escaped[:idx] + \
                                    f'<mark style="background:{color};color:#fff;border-radius:3px;' \
                                    f'padding:1px 4px;cursor:help;" title="{tooltip}">{original}</mark>' + \
                                    escaped[idx+len(escaped_s):]
                    had_error = True

        # Typo highlight
        for t in sorted(typo_list, key=lambda x: -len(x.get("wrong", ""))):
            wrong   = t.get("wrong", "")
            correct = t.get("correct", "")
            if not wrong:
                continue
            escaped_wrong = html.escape(wrong)
            if escaped_wrong in escaped:
                tooltip_t = html.escape(f'Typo: "{wrong}" → "{correct}"', quote=True)
                escaped = escaped.replace(
                    escaped_wrong,
                    f'<mark style="background:#fadb14;color:#000;border-radius:3px;'
                    f'padding:1px 4px;cursor:help;" title="{tooltip_t}">{escaped_wrong}</mark>', 1,
                )

        return escaped, had_error

    # Two-column layout
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

        html_lines = []

        for i, line in enumerate(lines):
            if not line.strip():
                html_lines.append("<div style='height:6px;'></div>")
                continue
            is_table = line.startswith("[TABLE]:")
            content  = line[8:].strip() if is_table else line
            rendered, had_error = highlight_line(i, line, typo_words)

            if is_table:
                bg     = "#fff3f3" if had_error else "#f8f9fa"
                border = "#ff4d4f" if had_error else "#dee2e6"
                html_lines.append(
                    f'<div style="font-family:\'Courier New\',monospace;font-size:12.5px;'
                    f'color:#212529;background:{bg};border:1px solid {border};'
                    f'border-radius:3px;padding:5px 10px;margin:2px 0;'
                    f'overflow-x:auto;">{rendered}</div>'
                )
            else:
                # Detect heading styles
                upper_ratio = sum(1 for c in content if c.isupper()) / max(len(content), 1)
                is_heading  = (upper_ratio > 0.6 and len(content) < 80) or content.startswith("Điều ")
                if is_heading:
                    html_lines.append(
                        f'<p style="margin:14px 0 4px;font-size:13.5px;font-weight:700;'
                        f'color:#1a1a1a;letter-spacing:0.3px;">{rendered}</p>'
                    )
                else:
                    html_lines.append(
                        f'<p style="margin:2px 0;font-size:13.5px;line-height:1.9;'
                        f'color:#212529;text-align:justify;">{rendered}</p>'
                    )

        st.markdown(
            # Outer wrapper — giống trang giấy Word/Google Docs
            '<div style="background:#e8e8e8;padding:24px 32px;border-radius:8px;'
            'max-height:82vh;overflow-y:auto;">'
            # Trang giấy trắng
            '<div style="background:#ffffff;max-width:760px;margin:0 auto;'
            'padding:48px 56px;border-radius:2px;'
            'box-shadow:0 1px 3px rgba(0,0,0,0.12),0 4px 12px rgba(0,0,0,0.08);">'
            + "".join(html_lines) +
            '</div></div>',
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

# PAGE: PROMPT CHECK
elif st.session_state["page"] == "prompt":

    p_results: list[dict] = st.session_state.get("p_results", [])
    p_lines: list[str]    = st.session_state.get("p_lines", [])
    p_filename: str       = st.session_state.get("p_filename", "")

    # CSS: thu gọn file uploader
    st.markdown("""
    <style>
    [data-testid="stFileUploaderDropzone"] {
        border:none !important;background:transparent !important;
        padding:0 !important;min-height:0 !important;
    }
    [data-testid="stFileUploaderDropzoneInstructions"],[data-testid="stFileUploader"]>label,
    [data-testid="stFileUploader"] small,[data-testid="stFileUploaderDropzone"]>div
    { display:none !important; }
    [data-testid="stFileUploaderDropzone"] button {
        display:inline-flex !important;padding:6px 16px !important;
        font-size:13px !important;border-radius:8px !important;height:36px !important;
    }
    </style>
    """, unsafe_allow_html=True)

    # Header
    st.markdown(
        '<div style="margin-bottom:20px;">'
        '<div style="font-size:16px;color:white;">'
        'Upload the contract and describe using natural language'
        '</div></div>',
        unsafe_allow_html=True,
    )

    # Input panel
    with st.container(border=True):
        col_prompt, col_upload = st.columns([4, 1])

        with col_prompt:
            p_prompt_raw = st.text_area(
                label="Điều kiện kiểm tra",
                placeholder=(
                    "Enter a prompt..."
                ),
                height=90,
                label_visibility="collapsed",
                key="p_prompt_input",
            )

        with col_upload:
            st.markdown('<div style="font-size:14px;color:white;margin-bottom:6px;">🔗 File (.docx)</div>', unsafe_allow_html=True)
            p_uploaded = st.file_uploader(
                "upload", type=["docx", "pdf"],
                key=f"prompt_uploader_{st.session_state.get('p_upload_counter', 0)}",
                label_visibility="collapsed",
            )
            if p_uploaded:
                st.markdown(
                    f'<div style="font-size:11px;color:white;margin-top:4px;word-break:break-all;">'
                    f'✅ {html.escape(p_uploaded.name)}</div>',
                    unsafe_allow_html=True,
                )

        st.markdown("<div style='margin-top:8px;'></div>", unsafe_allow_html=True)
        btn_col, hint_col = st.columns([2, 5])
        with btn_col:
            p_run_btn = st.button(
                "Analyze & Check", type="primary", key="p_run_btn",
                disabled=not p_uploaded, use_container_width=True,
            )
        with hint_col:
            if not p_uploaded:
                st.markdown('<div style="font-size:12px;color:#aaa;padding-top:10px;"></div>', unsafe_allow_html=True)
            elif not p_prompt_raw.strip():
                st.markdown('<div style="font-size:12px;color:#f0a500;padding-top:10px;"></div>', unsafe_allow_html=True)

    # Status placeholder — hiện ngay dưới input panel
    p_status_placeholder = st.empty()

    # Empty state
    if not p_results:
        st.markdown(
            '<div style="text-align:center;padding:60px 0;color:#555;">'
            '<div style="font-size:44px;margin-bottom:12px;">📄</div>'
            '<div style="font-size:15px;font-weight:600;margin-bottom:6px;">No results yet</div>'
            '<div style="font-size:13px;color:#888;">Upload your contract and click Analyze & Check to begin.</div>'
            '</div>',
            unsafe_allow_html=True,
        )
    else:
        p_errors   = [r for r in p_results if not r["corrected"]]
        p_ok       = len(p_results) - len(p_errors)
        p_schema_e = [r for r in p_errors if r.get("_source") == "Schema"]
        p_prompt_e = [r for r in p_errors if r.get("_source") == "Prompt"]

        st.markdown("<div style='margin:16px 0 8px;'></div>", unsafe_allow_html=True)
        m1, m2, m3, m4 = st.columns(4)
        m1.markdown(
            f'<div style="font-size:12px;color:#888;margin-bottom:4px;">📄 File</div>'
            f'<div style="font-size:22px;font-weight:600;word-break:break-all;">{html.escape(p_filename)}</div>',
            unsafe_allow_html=True,
        )
        m2.metric("✅ Valid",   p_ok)
        m3.metric("❌ Schema", len(p_schema_e))
        m4.metric("❌ Prompt", len(p_prompt_e))
        st.markdown("<div style='margin-bottom:16px;'></div>", unsafe_allow_html=True)

        # Build highlight data
        p_error_lines: dict[int, list[tuple]] = {}
        p_error_text_search: list[tuple]      = []

        for r in p_errors:
            src    = r.get("_source", "Schema")
            ln     = r.get("line_number", 0)
            val    = r.get("value")
            reason = r.get("reason") or ""

            if src == "Schema":
                val_str = ""
                if isinstance(val, dict):
                    val_str = ""
                elif val is not None and str(val).strip():
                    val_str = str(val).strip()
                if not val_str and reason:
                    m = re.search(r"got ['\"](.+?)['\"]", reason)
                    if m:
                        val_str = m.group(1)
                if ln and ln > 0:
                    p_error_lines.setdefault(ln, []).append((val_str, r["field"], reason, "#ff4d4f"))
                else:
                    if val_str:
                        p_error_text_search.append((val_str, r["field"], reason, False, "#ff4d4f"))
                    else:
                        parts   = r["field"].split(" - ")
                        keyword = re.sub(r"\s*\(.*?\)\s*$", "", parts[-1]).strip()
                        if keyword:
                            p_error_text_search.append((keyword, r["field"], reason, True, "#ff4d4f"))
                            first_word = keyword.split()[0]
                            if first_word != keyword and len(first_word) >= 4:
                                p_error_text_search.append((first_word, r["field"], reason, True, "#ff4d4f"))
            else:
                pos = r.get("value", "")
                if pos and str(pos) != "None":
                    clean_pos = str(pos).strip()
                    if len(clean_pos) > 60:
                        clean_pos = re.split(r"[,\.;]", clean_pos)[0].strip()
                    if clean_pos:
                        p_error_text_search.append((clean_pos, r["field"], reason, False, "#722ed1"))

        res_left, res_right = st.columns([3, 2], gap="large")

        with res_left:
            st.markdown(
                f'<div style="display:flex;align-items:center;gap:8px;margin-bottom:10px;">'
                f'<span style="font-size:15px;font-weight:600;">📋 Contract Content</span>'
                f'<span style="font-size:11px;color:#888;background:#f0f0f0;padding:2px 8px;'
                f'border-radius:10px;">{html.escape(p_filename)}</span>'
                f'</div>',
                unsafe_allow_html=True,
            )

            p_html_lines = []
            for i, line in enumerate(p_lines):
                if not line.strip():
                    p_html_lines.append("<div style='height:6px;'></div>")
                    continue
                is_table = line.startswith("[TABLE]:")
                content  = line[8:].strip() if is_table else line
                escaped  = html.escape(content)
                had_err  = False

                if (i + 1) in p_error_lines:
                    for val_str, field, reason, color in p_error_lines[i + 1]:
                        tooltip = html.escape(f"{field}: {reason}", quote=True)
                        if val_str:
                            escaped_val = html.escape(val_str)
                            pattern = re.compile(re.escape(escaped_val) + r'(?![^<]*>)')
                            new_esc = pattern.sub(
                                f'<mark style="background:{color};color:#fff;border-radius:3px;'
                                f'padding:1px 4px;cursor:help;" title="{tooltip}">{escaped_val}</mark>',
                                escaped, count=1
                            )
                            if new_esc != escaped:
                                escaped = new_esc
                                had_err = True
                                continue
                        escaped = (
                            f'<span title="{tooltip}" style="background:#fff1f0;'
                            f'border-left:3px solid {color};padding-left:6px;display:block;">'
                            f'{escaped}</span>'
                        )
                        had_err = True

                if not had_err:
                    for search, field, reason, is_missing, color in sorted(p_error_text_search, key=lambda x: -len(x[0])):
                        escaped_s = html.escape(search)
                        if escaped_s.lower() in html.escape(line).lower():
                            tooltip = html.escape(f"{field}: {reason}", quote=True)
                            if is_missing:
                                escaped = (
                                    f'<span title="{tooltip}" style="background:#fff1f0;'
                                    f'border-left:3px solid {color};padding-left:6px;display:block;">'
                                    f'{escaped}</span>'
                                )
                            else:
                                idx = escaped.lower().find(escaped_s.lower())
                                if idx >= 0:
                                    before = escaped[:idx]
                                    if before.count('<') == before.count('>'):
                                        original = escaped[idx:idx+len(escaped_s)]
                                        escaped = (
                                            before +
                                            f'<mark style="background:{color};color:#fff;border-radius:3px;'
                                            f'padding:1px 4px;cursor:help;" title="{tooltip}">{original}</mark>' +
                                            escaped[idx+len(escaped_s):]
                                        )
                            had_err = True

                if is_table:
                    bg     = "#fff3f3" if had_err else "#f8f9fa"
                    border = "#ff4d4f" if had_err else "#dee2e6"
                    p_html_lines.append(
                        f'<div style="font-family:\'Courier New\',monospace;font-size:12.5px;'
                        f'color:#212529;background:{bg};border:1px solid {border};'
                        f'border-radius:3px;padding:5px 10px;margin:2px 0;">{escaped}</div>'
                    )
                else:
                    upper_ratio = sum(1 for c in content if c.isupper()) / max(len(content), 1)
                    is_heading  = (upper_ratio > 0.6 and len(content) < 80) or content.startswith("Điều ")
                    if is_heading:
                        p_html_lines.append(
                            f'<p style="margin:14px 0 4px;font-size:13.5px;font-weight:700;'
                            f'color:#1a1a1a;letter-spacing:0.3px;">{escaped}</p>'
                        )
                    else:
                        p_html_lines.append(
                            f'<p style="margin:2px 0;font-size:13.5px;line-height:1.9;'
                            f'color:#212529;text-align:justify;">{escaped}</p>'
                        )

            st.markdown(
                '<div style="background:#e8e8e8;padding:24px 32px;border-radius:8px;'
                'max-height:65vh;overflow-y:auto;">'
                '<div style="background:#ffffff;max-width:760px;margin:0 auto;'
                'padding:48px 56px;border-radius:2px;'
                'box-shadow:0 1px 3px rgba(0,0,0,0.12),0 4px 12px rgba(0,0,0,0.08);">'
                + "".join(p_html_lines) +
                '</div></div>',
                unsafe_allow_html=True,
            )

        with res_right:
            st.markdown(
                '<div style="font-size:24px;font-weight:600;margin-bottom:6px;">Error List</div>',
                unsafe_allow_html=True,
            )
            if not p_errors:
                st.success("✅ No errors were detected.")
            else:
                st.markdown(
                    '<div style="display:flex;gap:8px;margin-bottom:10px;flex-wrap:wrap;">'
                    '<span style="background:#0057a8;color:#fff;font-size:11px;padding:2px 8px;border-radius:10px;">Schema</span>'
                    '<span style="font-size:11px;color:white;">Rule-based</span>&nbsp;'
                    '<span style="background:#722ed1;color:#fff;font-size:11px;padding:2px 8px;border-radius:10px;">Prompt</span>'
                    '<span style="font-size:11px;color:white;">AI check</span>'
                    '</div>',
                    unsafe_allow_html=True,
                )

                P_PAGE_SIZE   = 3
                p_total_pages = (len(p_errors) + P_PAGE_SIZE - 1) // P_PAGE_SIZE

                if p_total_pages > 1:
                    p_cols = st.columns(p_total_pages + 2)
                    if p_cols[0].button("‹", key="p_prev_page"):
                        st.session_state["p_error_page"] = max(1, st.session_state.get("p_error_page", 1) - 1)
                        st.rerun()
                    for i in range(1, p_total_pages + 1):
                        if p_cols[i].button(str(i), key=f"p_page_{i}",
                                            type="primary" if i == st.session_state.get("p_error_page", 1) else "secondary"):
                            st.session_state["p_error_page"] = i
                            st.rerun()
                    if p_cols[p_total_pages + 1].button("›", key="p_next_page"):
                        st.session_state["p_error_page"] = min(p_total_pages, st.session_state.get("p_error_page", 1) + 1)
                        st.rerun()
                    st.caption(f"Page {st.session_state.get('p_error_page', 1)}/{p_total_pages} · {len(p_errors)} errors")
                else:
                    st.caption(f"{len(p_errors)} error(s)")

                p_page  = st.session_state.get("p_error_page", 1)
                p_start = (p_page - 1) * P_PAGE_SIZE
                for r in p_errors[p_start: p_start + P_PAGE_SIZE]:
                    src         = r.get("_source", "Schema")
                    badge_color = "#0057a8" if src == "Schema" else "#722ed1"
                    val         = r["value"]
                    is_missing  = val is None or (isinstance(val, str) and val.strip() == "")
                    with st.container(border=True):
                        st.markdown(
                            f'<div style="display:flex;align-items:center;gap:6px;margin-bottom:4px;">'
                            f'<span style="background:{badge_color};color:#fff;font-size:10px;'
                            f'padding:1px 7px;border-radius:10px;white-space:nowrap;">{src}</span>'
                            f'<span style="font-weight:600;font-size:13px;">{html.escape(r["field"])}</span>'
                            f'</div>',
                            unsafe_allow_html=True,
                        )
                        if is_missing:
                            st.markdown(f":red[{r['reason']}]")
                        else:
                            display_val = val.get("quantity", str(val)) if isinstance(val, dict) else val
                            st.markdown(f"`{display_val}`  \n:red[{r['reason']}]")

    # Run logic
    if p_run_btn and p_uploaded:
        with p_status_placeholder.status("Analyzing contract...", expanded=True) as status:
            step_placeholder = st.empty()
            steps_done = []

            def on_step_p(msg: str, state: str):
                if state == "done":
                    steps_done.append(msg)
                lines_html = "".join(
                    f'<div style="font-size:13px;color:#52c41a;margin:2px 0;">✓ {s}</div>'
                    for s in steps_done
                )
                if state == "running":
                    lines_html += f'<div style="font-size:13px;color:#888;margin:2px 0;">⏳ {msg}</div>'
                step_placeholder.markdown(lines_html, unsafe_allow_html=True)

            with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(p_uploaded.name)[1]) as tmp:
                tmp.write(p_uploaded.read())
                tmp_path = tmp.name
            try:
                rb_results, extracted_p, lines, contract_type_p = process_contract(tmp_path, on_step=on_step_p)
                contract_text_p = "\n".join(lines)
                if p_prompt_raw.strip():
                    on_step_p("🤖 Running prompt checks...", "running")
                    pr_results = run_prompt_checks(contract_text_p, p_prompt_raw)
                    on_step_p(f"🤖 Prompt check: {len(pr_results)} rules checked", "done")
                else:
                    pr_results = []
            finally:
                os.unlink(tmp_path)

            status.update(label="✅ Analysis complete!", state="complete", expanded=False)

        merged = []
        for r in rb_results:
            r["_source"] = "Schema"
            merged.append(r)
        for r in pr_results:
            merged.append({
                "field":     r["rule"],
                "value":     r["position"] if r["position"] != "None" else None,
                "corrected": not r["result"],
                "reason":    r["reasoning"] if r["result"] else None,
                "line_hint": r["position"] if r["position"] != "None" else "",
                "_source":   "Prompt",
            })

        st.session_state["p_results"]    = merged
        st.session_state["p_lines"]      = lines
        st.session_state["p_filename"]   = p_uploaded.name
        st.session_state["p_error_page"] = 1
        st.session_state["p_upload_counter"] = st.session_state.get("p_upload_counter", 0) + 1
        st.rerun()
