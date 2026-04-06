#!/usr/bin/env python3
"""
Fix code blocks: find all translated pages with Chinese code blocks,
translate Chinese parts to Vietnamese, update blocks in-place via PATCH.
No need to re-create pages - just patch the code blocks directly.

Usage: python -u -X utf8 fix_code_blocks.py
"""
import sys, os, json, re, time, warnings
warnings.filterwarnings("ignore")
import requests as req_lib

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8')

DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, DIR)
from clone import lark, get_all_blocks, LARK_BASE

CFG = json.load(open(os.path.join(DIR, 'config.json'), 'r', encoding='utf-8'))
LLMGATE_KEY = CFG.get("llmgate_api_key", "")
LLMGATE_BASE = "https://llmgate.app/v1"
LLMGATE_MODEL = "gpt-5.4"

ZH_RE = re.compile(r'[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]')

CODE_PROMPT = ("Trong đoạn code/config sau, CHỈ dịch các phần tiếng Trung sang tiếng Việt. "
               "GIỮ NGUYÊN toàn bộ code, cú pháp, tên biến, tên hàm, tên lệnh tiếng Anh, URL, JSON keys. "
               "Chỉ dịch comment (#, //) và text/value tiếng Trung. "
               "Trả về KẾT QUẢ duy nhất, không giải thích, không thêm markdown.")

_session = req_lib.Session()

def llm_translate_code(text):
    for attempt in range(3):
        try:
            r = _session.post(f"{LLMGATE_BASE}/chat/completions",
                headers={"Authorization": f"Bearer {LLMGATE_KEY}", "Content-Type": "application/json"},
                json={"model": LLMGATE_MODEL,
                      "messages": [{"role": "system", "content": CODE_PROMPT},
                                   {"role": "user", "content": text}],
                      "max_tokens": 8192, "temperature": 0.2},
                timeout=120, verify=False)
            d = r.json()
            if "choices" in d:
                result = d["choices"][0]["message"]["content"].strip()
                # Remove markdown code fences if GPT added them
                if result.startswith("```"):
                    lines = result.split("\n")
                    if lines[-1].strip() == "```":
                        result = "\n".join(lines[1:-1])
                    else:
                        result = "\n".join(lines[1:])
                return result
            if r.status_code == 429:
                time.sleep(5 + attempt * 5)
        except:
            time.sleep(2 + attempt * 2)
    return None


def main():
    print("=" * 60)
    print("  FIX CODE BLOCKS — Translate Chinese → Vietnamese")
    print("=" * 60)

    ts = json.load(open(os.path.join(DIR, 'translate_state.json')))
    trans_map = ts.get('trans_map', {})

    all_nodes = []
    for fname in ["nodes/wiki1_nodes.json", "nodes/wiki2_nodes.json"]:
        fpath = os.path.join(DIR, fname)
        if os.path.exists(fpath):
            with open(fpath, 'r', encoding='utf-8') as f:
                all_nodes.extend(json.load(f))

    print(f"  Pages with VI translation: {len(trans_map)}")

    # Test LLMGate
    test = llm_translate_code("# 这是测试\nprint('hello')")
    print(f"  LLMGate test: {test}")

    total_fixed = 0
    total_skipped = 0
    total_pages = 0
    errors = 0

    for idx, node in enumerate(all_nodes):
        nt = node['node_token']
        if nt not in trans_map:
            continue

        vi_node = trans_map[nt]
        title = node.get('title', '')[:45]

        try:
            ni = lark.get(f"/wiki/v2/spaces/get_node?token={vi_node}")
            if ni.get('code') != 0:
                continue
            vi_doc = ni['data']['node']['obj_token']
            blocks = get_all_blocks(lark, vi_doc)

            # Find code blocks with Chinese
            code_blocks_to_fix = []
            for b in blocks:
                if b.get('block_type') != 14:
                    continue
                if 'code' not in b:
                    continue
                code_text = ""
                for e in b['code'].get('elements', []):
                    if 'text_run' in e:
                        code_text += e['text_run'].get('content', '')
                if code_text and ZH_RE.search(code_text):
                    code_blocks_to_fix.append((b['block_id'], code_text))

            if not code_blocks_to_fix:
                continue

            total_pages += 1
            page_fixed = 0

            for block_id, original_text in code_blocks_to_fix:
                translated = llm_translate_code(original_text)
                if translated and translated != original_text:
                    # PATCH update the code block in-place
                    patch_r = lark.patch(
                        f"/docx/v1/documents/{vi_doc}/blocks/{block_id}",
                        json_data={"update_text_elements": {
                            "elements": [{"text_run": {"content": translated}}],
                            "style": {}
                        }}
                    )
                    if patch_r.get('code') == 0:
                        page_fixed += 1
                        total_fixed += 1
                    else:
                        errors += 1
                else:
                    total_skipped += 1
                time.sleep(0.3)

            if page_fixed > 0:
                print(f"  [{total_pages}] {title} | fixed {page_fixed}/{len(code_blocks_to_fix)} code blocks")

        except Exception as e:
            errors += 1

        if total_pages % 20 == 0 and total_pages > 0:
            print(f"  --- {total_pages} pages processed, {total_fixed} blocks fixed ---")

    print(f"\n{'=' * 60}")
    print(f"  DONE")
    print(f"  Pages with code blocks: {total_pages}")
    print(f"  Code blocks fixed: {total_fixed}")
    print(f"  Skipped (no change): {total_skipped}")
    print(f"  Errors: {errors}")
    print(f"{'=' * 60}")


if __name__ == '__main__':
    main()
