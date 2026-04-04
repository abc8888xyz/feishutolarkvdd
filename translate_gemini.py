#!/usr/bin/env python3
"""
Claude CLI Translation Agent (standalone):
  1. Read cloned pages from clone_state.json + Base
  2. Create new VI wiki node per page
  3. Translate CN -> VI using Claude Code CLI (marker strategy)
  4. Write translated blocks to new doc
  5. Update Base: link, title VI, status

Usage:
  python -u -X utf8 translate_gemini.py              # translate all
  python -u -X utf8 translate_gemini.py --start N    # resume from index N
  python -u -X utf8 translate_gemini.py --stt N      # translate 1 page only
"""
import sys, os, time, json, re, threading, tempfile, subprocess
import requests as req_lib
from urllib.parse import quote
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8')

# ── Config ──
DIR = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(DIR, 'config.json'), 'r', encoding='utf-8'))

APP_ID = CFG["app_id"]
APP_SECRET = CFG["app_secret"]
FEISHU_BASE = CFG["feishu_base"]
LARK_BASE = CFG["lark_base"]
LARK_WIKI_SPACE_ID = CFG["lark_wiki_space_id"]
WIKI_DEST_NODE = CFG["wiki_dest_node"]
BASE_APP_TOKEN = CFG.get("base_app_token", "")
BASE_TABLE_ID = CFG.get("base_table_id", "")

NODES_DIR = os.path.join(DIR, 'nodes')
STATE_FILE = os.path.join(DIR, 'clone_state.json')
TRANS_STATE_FILE = os.path.join(DIR, 'translate_state.json')

# Claude CLI
CLAUDE_CLI = r'C:\Users\vudan\AppData\Local\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude-code\2.1.87\claude.exe'

BLOCK_TYPE_FIELD = {
    2: "text", 3: "heading1", 4: "heading2", 5: "heading3", 6: "heading4",
    7: "heading5", 8: "heading6", 9: "heading7", 10: "heading8", 11: "heading9",
    12: "bullet", 13: "ordered", 14: "code", 15: "quote", 17: "todo"
}

# ── Auth ──
class LarkAuth:
    def __init__(self, app_id, app_secret, base_url):
        self.app_id, self.app_secret, self.BASE_URL = app_id, app_secret, base_url
        self._token, self._expire = None, 0
        self._lock = threading.Lock()
        self._s = req_lib.Session()
        self._s.mount("https://", HTTPAdapter(
            max_retries=Retry(total=3, backoff_factor=1, status_forcelist=[429,500,502,503,504]),
            pool_connections=10, pool_maxsize=10))

    @property
    def token(self):
        with self._lock:
            if self._token and time.time() < self._expire: return self._token
            d = self._s.post(f"{self.BASE_URL}/auth/v3/tenant_access_token/internal",
                json={"app_id": self.app_id, "app_secret": self.app_secret}, timeout=30).json()
            if d.get("code") != 0: raise Exception(f"Auth fail: {d.get('msg')}")
            self._token = d["tenant_access_token"]
            self._expire = time.time() + d.get("expire", 7200) - 60
            return self._token

    def h(self):
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json; charset=utf-8"}

    def get(self, path, **kw):
        try:
            r = self._s.get(f"{self.BASE_URL}{path}", headers=self.h(), timeout=30, **kw)
            return r.json()
        except: return {"code": -1, "msg": "json decode error"}

    def post(self, path, json_data=None):
        try:
            r = self._s.post(f"{self.BASE_URL}{path}", headers=self.h(), json=json_data, timeout=30)
            return r.json()
        except: return {"code": -1, "msg": "empty response"}

    def patch(self, path, json_data=None):
        for r in range(5):
            try:
                resp = self._s.patch(f"{self.BASE_URL}{path}", headers=self.h(), json=json_data, timeout=60).json()
                if resp.get("code") in (230001, 99991400): time.sleep(1+r); continue
                return resp
            except:
                if r < 4: time.sleep(2+r); continue
                return {"code": -1, "msg": "fail"}
        return {"code": -1}

    def delete(self, path, json_data=None):
        try:
            r = self._s.request("DELETE", f"{self.BASE_URL}{path}", headers=self.h(), json=json_data, timeout=30)
            if r.status_code in (200, 204) and not r.text.strip(): return {"code": 0}
            return r.json()
        except: return {"code": -1}

    def get_raw(self, path, stream=False):
        return self._s.get(f"{self.BASE_URL}{path}", headers={"Authorization": f"Bearer {self.token}"}, stream=stream, timeout=120)

    def post_form(self, path, data=None, files=None):
        try:
            r = self._s.post(f"{self.BASE_URL}{path}", headers={"Authorization": f"Bearer {self.token}"},
                             data=data, files=files, timeout=120)
            return r.json()
        except: return {"code": -1, "msg": "upload error"}

feishu = LarkAuth(APP_ID, APP_SECRET, FEISHU_BASE)
lark = LarkAuth(APP_ID, APP_SECRET, LARK_BASE)

# ── Block helpers ──
def get_all_blocks(auth, doc_id):
    blocks, pt = [], ""
    while True:
        url = f"/docx/v1/documents/{doc_id}/blocks?page_size=500"
        if pt: url += f"&page_token={pt}"
        r = auth.get(url)
        if r.get("code") != 0: break
        blocks.extend(r["data"].get("items", []))
        if not r["data"].get("has_more"): break
        pt = r["data"].get("page_token", "")
    return blocks

def create_block(auth, doc_id, parent_id, block_data, retries=5):
    for r in range(retries):
        try:
            resp = auth.post(f"/docx/v1/documents/{doc_id}/blocks/{parent_id}/children",
                json_data={"children": [block_data], "index": -1})
            if resp.get("code") == 0:
                items = resp.get("data", {}).get("children", [])
                return items[0] if items else None
            if resp.get("code") in (230001, 99991400, -1):
                time.sleep(3 + r * 3); continue
            return None
        except:
            time.sleep(2 + r * 2)
    return None

# ── Claude CLI Translation ──
ZH_RE = re.compile(r'[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]')
_cache = {}

SYSTEM_PROMPT = "Dich sang tieng Viet thuan, tu nhien, de hieu. CHI tra ve ban dich, KHONG giai thich, KHONG ghi chu. GIU NGUYEN: ten tieng Anh, URL, code, ten AI (GPT-4, Claude, Cursor, Vibe Coding, MCP, SDK...). Ten nguoi/cong ty TQ: phien am Han-Viet (VD: lao jin -> Lao Kim, fei shu -> Feishu). Thuat ngu ky thuat dich tu nhien. Neu khong co tieng Trung tra nguyen van."


def claude_translate(text):
    """Call Claude CLI to translate text."""
    try:
        result = subprocess.run(
            [CLAUDE_CLI, '-p', f'{SYSTEM_PROMPT}\n\nDich sang tieng Viet:\n{text}',
             '--output-format', 'text', '--model', 'sonnet'],
            capture_output=True, text=True, timeout=60, encoding='utf-8'
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception as e:
        print(f"    Claude CLI error: {str(e)[:80]}")
    return text


def translate_text(text):
    if not text or not text.strip(): return text
    if not ZH_RE.search(text): return text
    if text in _cache: return _cache[text]

    for attempt in range(3):
        translated = claude_translate(text)
        if translated != text:
            _cache[text] = translated
            return translated
        if attempt < 2: time.sleep(1)
    return text


def translate_elements(elements):
    """Translate text elements preserving formatting via marker strategy."""
    has_zh = any(
        "text_run" in e and ZH_RE.search(e["text_run"].get("content", ""))
        for e in elements
    )
    if not has_zh: return elements

    run_indices, run_contents = [], []
    for i, e in enumerate(elements):
        if "text_run" in e:
            run_indices.append(i)
            run_contents.append(e["text_run"].get("content", ""))
        elif "mention_doc" in e:
            run_indices.append(i)
            run_contents.append(e["mention_doc"].get("title", ""))

    if not run_indices: return elements

    # Join with markers
    marked_text = ""
    for idx, content in enumerate(run_contents):
        marked_text += content
        if idx < len(run_contents) - 1:
            marked_text += f"[[[{idx+1}]]]"

    # Translate with marker instruction
    marker_prompt = (
        f"{SYSTEM_PROMPT}\n\n"
        "QUAN TRONG: Giu nguyen cac marker dang [[[N]]] trong ban dich, "
        "dat chung o vi tri phu hop nhat ve mat ngu nghia.\n\n"
        f"Dich sang tieng Viet:\n{marked_text}"
    )

    translated_marked = marked_text
    for attempt in range(3):
        try:
            result = subprocess.run(
                [CLAUDE_CLI, '-p', marker_prompt, '--output-format', 'text', '--model', 'sonnet'],
                capture_output=True, text=True, timeout=60, encoding='utf-8'
            )
            if result.returncode == 0 and result.stdout.strip():
                translated_marked = result.stdout.strip()
                break
        except: pass
        if attempt < 2: time.sleep(1)

    # Split by markers
    parts = re.split(r'\[\[\[(\d+)\]\]\]', translated_marked)
    translated_parts = [parts[i] for i in range(0, len(parts), 2)]

    # Fallback if marker count mismatch
    if len(translated_parts) != len(run_contents):
        full_translated = translate_text("".join(run_contents))
        result_els = list(elements)
        for k, i in enumerate(run_indices):
            e = elements[i]
            if "text_run" in e:
                ne = {"text_run": {"content": full_translated if k == 0 else ""}}
                if "text_element_style" in e["text_run"]:
                    ne["text_run"]["text_element_style"] = e["text_run"]["text_element_style"]
                result_els[i] = ne
            elif "mention_doc" in e:
                result_els[i] = {"text_run": {"content": full_translated if k == 0 else ""}}
        return [r for r in result_els if not ("text_run" in r and r["text_run"].get("content", "") == "")]

    # Assign translated text back with original styles
    result_els = list(elements)
    for k, (i, tc) in enumerate(zip(run_indices, translated_parts)):
        e = elements[i]
        if "text_run" in e:
            ne = {"text_run": {"content": tc}}
            if "text_element_style" in e["text_run"]:
                ne["text_run"]["text_element_style"] = e["text_run"]["text_element_style"]
            result_els[i] = ne
        elif "mention_doc" in e:
            result_els[i] = {"text_run": {"content": tc}}

    non_empty = [r for r in result_els if not ("text_run" in r and r["text_run"].get("content", "") == "")]
    return non_empty if non_empty else [{"text_run": {"content": translate_text("".join(run_contents))}}]


def translate_code_elements(elements):
    """Translate code block: only translate Chinese text, keep English/code as-is."""
    has_zh = any(
        "text_run" in e and ZH_RE.search(e["text_run"].get("content", ""))
        for e in elements
    )
    if not has_zh: return elements

    # For code blocks, translate the full content as one piece
    full_text = "".join(
        e.get("text_run", {}).get("content", "") for e in elements if "text_run" in e
    )
    if not ZH_RE.search(full_text): return elements

    translated = claude_translate_code(full_text)
    if translated and translated != full_text:
        return [{"text_run": {"content": translated}}]
    return elements


def claude_translate_code(text):
    """Translate Chinese parts in code block, keep code structure intact."""
    try:
        prompt = (
            "Trong doan code/pseudo-code sau, CHI dich cac phan tieng Trung sang tieng Viet. "
            "GIU NGUYEN toan bo code, cu phap, ten bien, ten ham, ten lenh tieng Anh. "
            "Chi dich comment va text tieng Trung. Tra ve KET QUA duy nhat, khong giai thich.\n\n"
            f"{text}"
        )
        result = subprocess.run(
            [CLAUDE_CLI, '-p', prompt, '--output-format', 'text', '--model', 'sonnet'],
            capture_output=True, text=True, timeout=60, encoding='utf-8'
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception as e:
        print(f"    Code translate error: {str(e)[:80]}")
    return text


def clean_elements(elements):
    cleaned = []
    for e in elements:
        if "text_run" in e:
            tr = e["text_run"]
            ne = {"text_run": {"content": tr.get("content", "")}}
            if "text_element_style" in tr:
                st = {}
                for k in ["bold","italic","strikethrough","underline","inline_code","background_color","text_color"]:
                    if k in tr["text_element_style"]: st[k] = tr["text_element_style"][k]
                if "link" in tr["text_element_style"]:
                    lk = tr["text_element_style"]["link"]
                    if isinstance(lk, dict) and lk.get("url"): st["link"] = {"url": lk["url"]}
                if st: ne["text_run"]["text_element_style"] = st
            cleaned.append(ne)
        elif "mention_doc" in e:
            md = e["mention_doc"]
            t, u = md.get("title","[doc]"), md.get("url","")
            if u: cleaned.append({"text_run":{"content":t,"text_element_style":{"link":{"url":quote(u,safe='')}}}})
            else: cleaned.append({"text_run":{"content":t}})
        elif "equation" in e:
            cleaned.append(e)
    return cleaned if cleaned else [{"text_run":{"content":""}}]


# ── Block Processing with Translation ──
def process_block_translate(src_block, src_map, dst_doc, parent_id, stats):
    bt = src_block.get("block_type", 0)

    # Skip certain types
    if bt in (1, 25, 32): return

    # Text-like blocks
    if bt in BLOCK_TYPE_FIELD:
        field = BLOCK_TYPE_FIELD[bt]
        if field not in src_block: return
        elements = clean_elements(src_block[field].get("elements", []))
        if bt == 14:
            # Code block: only translate if contains Chinese (comments, pseudo-code)
            elements = translate_code_elements(elements)
        else:
            elements = translate_elements(elements)
        data = {"block_type": bt, field: {"elements": elements}}
        if "style" in src_block[field]:
            cs = {}
            for k in ["align","done","folded","language","wrap"]:
                if k in src_block[field]["style"]: cs[k] = src_block[field]["style"][k]
            if cs: data[field]["style"] = cs
        r = create_block(lark, dst_doc, parent_id, data)
        if r and "block_id" in r: stats["blocks"] += 1
        else: stats["failed"] += 1

    # Divider
    elif bt == 22:
        r = create_block(lark, dst_doc, parent_id, {"block_type": 22, "divider": {}})
        if r and "block_id" in r: stats["blocks"] += 1

    # Image
    elif bt == 27:
        src_token = src_block.get("image", {}).get("token", "")
        if not src_token: return
        r = create_block(lark, dst_doc, parent_id, {"block_type": 27, "image": {}})
        if r and "block_id" in r:
            new_bid = r["block_id"]
            tmp = os.path.join(tempfile.gettempdir(), f"trans_img_{stats.get('images',0)}.tmp")
            try:
                img_r = lark.get_raw(f"/drive/v1/medias/{src_token}/download")
                if img_r.status_code == 200:
                    with open(tmp, 'wb') as f: f.write(img_r.content)
                    with open(tmp, 'rb') as fh:
                        fname = f"img_{new_bid}.png"
                        up = lark.post_form("/drive/v1/medias/upload_all",
                            data={"file_name": fname, "parent_type": "docx_image",
                                  "parent_node": new_bid, "size": str(os.path.getsize(tmp))},
                            files={"file": (fname, fh, "image/png")})
                    if up.get("code") == 0:
                        ft = up["data"]["file_token"]
                        lark.patch(f"/docx/v1/documents/{dst_doc}/blocks/{new_bid}",
                            json_data={"replace_image": {"token": ft}})
                        stats["images"] = stats.get("images", 0) + 1
                    else:
                        stats["img_fail"] = stats.get("img_fail", 0) + 1
                else:
                    stats["img_fail"] = stats.get("img_fail", 0) + 1
            except:
                stats["img_fail"] = stats.get("img_fail", 0) + 1
            finally:
                try: os.remove(tmp)
                except: pass
            stats["blocks"] += 1

    # File/Video
    elif bt == 23:
        sf = src_block.get("file", {})
        src_token = sf.get("token", "")
        if not src_token: return
        r = create_block(lark, dst_doc, parent_id, {"block_type": 23, "file": {}})
        if r and "block_id" in r:
            new_bid = r["block_id"]
            tmp = os.path.join(tempfile.gettempdir(), f"trans_file_{stats.get('files',0)}.tmp")
            try:
                file_r = lark.get_raw(f"/drive/v1/medias/{src_token}/download")
                if file_r.status_code == 200:
                    with open(tmp, 'wb') as f: f.write(file_r.content)
                    fname = sf.get("name", f"file_{new_bid}")
                    with open(tmp, 'rb') as fh:
                        up = lark.post_form("/drive/v1/medias/upload_all",
                            data={"file_name": fname, "parent_type": "docx_file",
                                  "parent_node": new_bid, "size": str(os.path.getsize(tmp))},
                            files={"file": (fname, fh, "application/octet-stream")})
                    if up.get("code") == 0:
                        ft = up["data"]["file_token"]
                        lark.patch(f"/docx/v1/documents/{dst_doc}/blocks/{new_bid}",
                            json_data={"replace_file": {"token": ft}})
                        stats["files"] = stats.get("files", 0) + 1
            except:
                stats["file_fail"] = stats.get("file_fail", 0) + 1
            finally:
                try: os.remove(tmp)
                except: pass
            stats["blocks"] += 1

    # Table
    elif bt == 31:
        td = src_block.get("table", {})
        rows = td.get("property", {}).get("row_size", 1)
        cols = td.get("property", {}).get("column_size", 1)
        cells = td.get("cells", [])
        prop = {"row_size": rows, "column_size": cols}
        cw = td.get("property", {}).get("column_width")
        if cw: prop["column_width"] = cw

        r = create_block(lark, dst_doc, parent_id, {"block_type": 31, "table": {"property": prop}})
        if not r or "block_id" not in r:
            stats["failed"] += 1; return
        stats["blocks"] += 1
        tbl_bid = r["block_id"]
        time.sleep(0.5)

        # Get table block to find its cell children (with retry)
        dest_cell_ids = []
        for _retry in range(5):
            tbl_info = lark.get(f"/docx/v1/documents/{dst_doc}/blocks/{tbl_bid}")
            if tbl_info.get("code") == 0:
                dest_table = tbl_info.get("data", {}).get("block", {}).get("table", {})
                dest_cell_ids = dest_table.get("cells", [])
                if dest_cell_ids:
                    break
            time.sleep(1 + _retry)

        if not dest_cell_ids:
            print(f"    TABLE WARN: no cells for {tbl_bid}")
            return

        # If API created fewer rows, insert more
        created_rows = dest_table.get("property", {}).get("row_size", 1)
        if created_rows < rows:
            for ri in range(rows - created_rows):
                lark.patch(f"/docx/v1/documents/{dst_doc}/blocks/{tbl_bid}",
                    json_data={"insert_table_row": {"row_index": created_rows + ri}})
                time.sleep(0.2)
            # Re-fetch cells after inserting rows
            for _retry in range(3):
                tbl_info = lark.get(f"/docx/v1/documents/{dst_doc}/blocks/{tbl_bid}")
                if tbl_info.get("code") == 0:
                    dest_cell_ids = tbl_info.get("data", {}).get("block", {}).get("table", {}).get("cells", [])
                    if dest_cell_ids: break
                time.sleep(1)

        for ci, src_cid in enumerate(cells):
            if ci >= len(dest_cell_ids): break
            src_cell = src_map.get(src_cid, {})
            for child_id in src_cell.get("children", []):
                if child_id in src_map:
                    process_block_translate(src_map[child_id], src_map, dst_doc, dest_cell_ids[ci], stats)

    # Callout
    elif bt == 19:
        co = src_block.get("callout", {})
        data = {"block_type": 19}
        clean_co = {k: co[k] for k in ["background_color","border_color","emoji_id"] if k in co}
        if clean_co: data["callout"] = clean_co
        r = create_block(lark, dst_doc, parent_id, data)
        if r and "block_id" in r:
            stats["blocks"] += 1
            # Delete default child
            try:
                cb = lark.get(f"/docx/v1/documents/{dst_doc}/blocks/{r['block_id']}")
                children = cb.get("data", {}).get("block", {}).get("children", [])
                if children:
                    lark.delete(f"/docx/v1/documents/{dst_doc}/blocks/{r['block_id']}/children/batch_delete",
                        json_data={"start_index": 0, "end_index": len(children)})
            except: pass
            for cid in src_block.get("children", []):
                if cid in src_map:
                    process_block_translate(src_map[cid], src_map, dst_doc, r["block_id"], stats)
        else: stats["failed"] += 1

    # Grid
    elif bt == 24:
        src_children = src_block.get("children", [])
        src_cols = [cid for cid in src_children if cid in src_map and src_map[cid].get("block_type") == 25]
        col_count = len(src_cols)
        if col_count == 0: return

        # Get column widths
        col_widths = []
        for cid in src_cols:
            w = src_map[cid].get("grid_column", {}).get("width_ratio")
            if w: col_widths.append(w)

        gd = {"column_size": col_count}
        if len(col_widths) == col_count: gd["column_width"] = col_widths

        r = create_block(lark, dst_doc, parent_id, {"block_type": 24, "grid": gd})
        if not r or "block_id" not in r: return
        stats["blocks"] += 1
        grid_bid = r["block_id"]

        dest_cols_r = lark.get(f"/docx/v1/documents/{dst_doc}/blocks/{grid_bid}")
        dest_col_ids = dest_cols_r.get("data", {}).get("block", {}).get("children", []) if dest_cols_r.get("code") == 0 else []

        for i, src_col_id in enumerate(src_cols):
            if i >= len(dest_col_ids): break
            src_col = src_map[src_col_id]
            for gcid in src_col.get("children", []):
                if gcid in src_map:
                    process_block_translate(src_map[gcid], src_map, dst_doc, dest_col_ids[i], stats)

    # Quote container
    elif bt == 34:
        r = create_block(lark, dst_doc, parent_id, {"block_type": 34, "quote_container": {}})
        if r and "block_id" in r:
            stats["blocks"] += 1
            for cid in src_block.get("children", []):
                if cid in src_map:
                    process_block_translate(src_map[cid], src_map, dst_doc, r["block_id"], stats)

    # iFrame
    elif bt in (26, 29):
        comp = src_block.get("iframe", {}).get("component", {})
        if comp:
            data = {"block_type": bt, "iframe": {"component": comp}}
            r = create_block(lark, dst_doc, parent_id, data)
            if r and "block_id" in r: stats["blocks"] += 1

    # Synced block - flatten
    elif bt == 49:
        for cid in src_block.get("children", []):
            if cid in src_map:
                process_block_translate(src_map[cid], src_map, dst_doc, parent_id, stats)

    # View (file wrapper)
    elif bt == 33:
        for cid in src_block.get("children", []):
            if cid in src_map:
                process_block_translate(src_map[cid], src_map, dst_doc, parent_id, stats)

    else:
        stats["skipped"] = stats.get("skipped", 0) + 1


# ── State management ──
def load_trans_state():
    if os.path.exists(TRANS_STATE_FILE):
        with open(TRANS_STATE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {'translated': [], 'trans_map': {}}

def save_trans_state(ts):
    with open(TRANS_STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(ts, f, ensure_ascii=False)

# ── Base helpers ──
def base_update(rec_id, fields):
    if not BASE_APP_TOKEN: return
    for attempt in range(3):
        try:
            r = req_lib.put(
                f'{LARK_BASE}/bitable/v1/apps/{BASE_APP_TOKEN}/tables/{BASE_TABLE_ID}/records/{rec_id}',
                headers=lark.h(), json={'fields': fields}, timeout=15)
            if r.json().get('code') == 0: return True
            time.sleep(1 + attempt)
        except: time.sleep(1)
    return False

def get_base_records():
    if not BASE_APP_TOKEN: return {}
    recs, pt = {}, ''
    while True:
        url = f'{LARK_BASE}/bitable/v1/apps/{BASE_APP_TOKEN}/tables/{BASE_TABLE_ID}/records?page_size=100'
        if pt: url += f'&page_token={pt}'
        d = req_lib.get(url, headers=lark.h(), timeout=15).json().get('data', {})
        for i in d.get('items', []):
            nt = i['fields'].get('Node Token gốc', '')
            if nt: recs[nt] = i
        if not d.get('has_more'): break
        pt = d.get('page_token', '')
    return recs


# ── Translate one page ──
def translate_one(node, dest_node, trans_state, translate_parent):
    node_token = node['node_token']
    title = node.get('title', '(untitled)') or '(untitled)'
    t0 = time.time()

    # Read blocks from cloned Lark page
    r = lark.get(f'/wiki/v2/spaces/get_node?token={dest_node}')
    if r.get('code') != 0:
        return {'success': False, 'error': f'get dest node: {r.get("msg")}'}

    src_doc = r['data']['node']['obj_token']
    sb = None
    for a in range(3):
        sb = get_all_blocks(lark, src_doc)
        if sb: break
        time.sleep(3 + a * 3)

    if not sb:
        # Fallback: read from Feishu source
        ni = feishu.get(f'/wiki/v2/spaces/get_node?token={node_token}')
        if ni.get('code') == 0:
            feishu_doc = ni['data']['node']['obj_token']
            sb = get_all_blocks(feishu, feishu_doc)

    if not sb:
        return {'success': False, 'error': 'No blocks to translate'}

    sm = {b['block_id']: b for b in sb}
    total_blocks = len(sb)
    root_children = sb[0].get('children', [])
    print(f"  src={src_doc}, blocks={total_blocks}, children={len(root_children)}")

    # Translate title
    vi_title = translate_text(title)
    print(f"  Title: {title[:50]} -> {vi_title[:50]}")

    # Determine parent node for translated page
    trans_map = trans_state.get('trans_map', {})
    parent_tok = node.get('parent_node_token', '')
    trans_parent = trans_map.get(parent_tok, translate_parent)

    # Create new wiki node
    cr = None
    for a in range(5):
        cr = lark.post(f"/wiki/v2/spaces/{LARK_WIKI_SPACE_ID}/nodes", json_data={
            "obj_type": "docx", "parent_node_token": trans_parent,
            "node_type": "origin", "title": vi_title})
        if cr.get('code') == 0: break
        if cr.get('code') in (230001, 99991400):
            time.sleep(5 + a * 5); continue
        break

    if not cr or cr.get('code') != 0:
        return {'success': False, 'error': f'create node: {(cr or {}).get("msg")}'}

    dn = cr['data']['node']
    dst_doc = dn['obj_token']
    new_node_token = dn['node_token']
    url = f"https://congdongagi.sg.larksuite.com/wiki/{new_node_token}"
    print(f"  New: {url}")

    # Register for children
    trans_map[node_token] = new_node_token
    trans_state['trans_map'] = trans_map

    # Process all blocks with translation
    stats = {'blocks': 0, 'images': 0, 'img_fail': 0, 'files': 0, 'file_fail': 0, 'failed': 0, 'skipped': 0}
    for j, bid in enumerate(root_children):
        if bid not in sm: continue
        process_block_translate(sm[bid], sm, dst_doc, dst_doc, stats)
        if (j + 1) % 20 == 0:
            elapsed = round(time.time() - t0)
            print(f"  [{j+1}/{len(root_children)}] blk={stats['blocks']} img={stats['images']} fail={stats['failed']} | {elapsed}s")

    elapsed = round(time.time() - t0)
    return {
        'success': True, 'url': url, 'new_node': new_node_token,
        'vi_title': vi_title, 'blocks': stats['blocks'],
        'images': stats['images'], 'files': stats.get('files', 0),
        'failed': stats['failed'], 'total_blocks': total_blocks,
        'elapsed': elapsed
    }


# ── Main ──
def main():
    start_from = 0
    end_at = None
    only_stt = None

    if '--start' in sys.argv:
        idx = sys.argv.index('--start')
        if idx + 1 < len(sys.argv): start_from = int(sys.argv[idx + 1])
    if '--end' in sys.argv:
        idx = sys.argv.index('--end')
        if idx + 1 < len(sys.argv): end_at = int(sys.argv[idx + 1])
    if '--stt' in sys.argv:
        idx = sys.argv.index('--stt')
        if idx + 1 < len(sys.argv):
            only_stt = int(sys.argv[idx + 1])
            start_from = only_stt

    print("=" * 60)
    print("  CLAUDE CLI TRANSLATION AGENT")
    print(f"  CN -> VI | Claude Code CLI")
    print("=" * 60)

    # Init Claude CLI
    print("\n  Checking Claude CLI...")
    if not os.path.exists(CLAUDE_CLI):
        print(f"  Claude CLI not found: {CLAUDE_CLI}")
        return
    test = claude_translate("你好")
    print(f"  Claude CLI ready: '你好' -> '{test}'")
    if test == "你好":
        print("  WARNING: Claude CLI may not be working properly")
        return

    # Load data
    all_nodes = []
    for wiki in CFG.get("source_wikis", []):
        name = wiki.get("name", "wiki")
        fpath = os.path.join(NODES_DIR, f"{name}_nodes.json")
        if os.path.exists(fpath):
            with open(fpath, 'r', encoding='utf-8') as f:
                nodes = json.load(f)
            for n in nodes: n["_source"] = name
            all_nodes.extend(nodes)

    with open(STATE_FILE, 'r', encoding='utf-8') as f:
        state = json.load(f)
    dest_map = state['dest_map']

    trans_state = load_trans_state()
    translated = set(trans_state.get('translated', []))

    print("\n  Loading Base records...")
    base_records = get_base_records()

    # Warm auth
    print("  Warming auth...")
    feishu.token; lark.token

    # Create or get VI category folder
    translate_parent = trans_state.get('translate_parent', '')
    if not translate_parent:
        cr = lark.post(f"/wiki/v2/spaces/{LARK_WIKI_SPACE_ID}/nodes", json_data={
            "obj_type": "docx", "node_type": "origin",
            "title": "Ban dich Tieng Viet (WaytoAGI)",
            "parent_node_token": WIKI_DEST_NODE})
        if cr.get('code') == 0:
            translate_parent = cr['data']['node']['node_token']
            trans_state['translate_parent'] = translate_parent
            save_trans_state(trans_state)
            print(f"  Created VI folder: {translate_parent}")
        else:
            print(f"  Failed to create VI folder: {cr.get('msg')}")
            return
    else:
        print(f"  VI folder: {translate_parent}")

    # Find pages to translate
    to_translate = []
    for i, n in enumerate(all_nodes):
        if i < start_from: continue
        if end_at is not None and i >= end_at: continue
        if only_stt is not None and i != only_stt: continue
        tok = n['node_token']
        dest_node = dest_map.get(tok)
        if not dest_node: continue
        if tok in translated: continue
        to_translate.append((i, n, dest_node))

    total = len(to_translate)
    print(f"\n  Pages to translate: {total}")
    if not total:
        print("  Nothing to translate!")
        return

    ok_count, fail_count = 0, 0
    batch_start = time.time()

    for idx, (i, node, dest_node) in enumerate(to_translate):
        title = node.get('title', '(untitled)') or '(untitled)'
        print(f"\n[{idx+1}/{total}] #{i} | {title[:60]}")

        nt = node['node_token']
        rec = base_records.get(nt)
        if rec: base_update(rec['record_id'], {'Trang thai dich': 'Dang dich'})

        try:
            info = translate_one(node, dest_node, trans_state, translate_parent)
        except Exception as e:
            info = {'success': False, 'error': f'Unhandled: {str(e)[:150]}'}

        if info['success']:
            ok_count += 1
            print(f"  OK | {info['blocks']}blk {info['images']}img {info['files']}file {info['elapsed']}s")

            translated.add(nt)
            trans_state['translated'] = list(translated)
            save_trans_state(trans_state)

            if rec: base_update(rec['record_id'], {
                'Trang thai dich': 'Da dich',
                'Link dich VI': {'link': info['url'], 'text': info['vi_title']},
                'Tieu de (VI)': info['vi_title'],
                'Ghi chu': f"{info['blocks']}blk {info['images']}img {info['files']}file {info['elapsed']}s (Gemini)"
            })
        else:
            fail_count += 1
            print(f"  FAIL | {info['error']}")
            if rec: base_update(rec['record_id'], {
                'Trang thai dich': 'Loi dich',
                'Ghi chu': info['error'][:200]
            })

        # Refresh auth every 15 pages
        if (idx + 1) % 15 == 0:
            try: lark.token; feishu.token
            except: pass

    elapsed = time.time() - batch_start
    print(f"\n{'='*60}")
    print(f"  TRANSLATE COMPLETE ({elapsed:.0f}s = {elapsed/60:.1f} min)")
    print(f"  Success: {ok_count}/{total}")
    print(f"  Failed:  {fail_count}")
    print(f"  Cache:   {len(_cache)} texts")
    print(f"{'='*60}")


if __name__ == '__main__':
    main()
