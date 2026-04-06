#!/usr/bin/env python3
"""
LLMGate GPT-5.4 batch translator: CN→VI
Batch 30 blocks per API call → ~40x faster than Claude CLI.

Usage:
  python -u -X utf8 translate_llmgate.py              # translate all
  python -u -X utf8 translate_llmgate.py --start N     # resume from index N
  python -u -X utf8 translate_llmgate.py --stt N       # single page
  python -u -X utf8 translate_llmgate.py test          # test 1 page
"""
import sys, os, json, re, time, tempfile
import requests as req_lib
from urllib.parse import quote

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8')

DIR = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(DIR, 'config.json'), 'r', encoding='utf-8'))

sys.path.insert(0, DIR)
from clone import (lark, feishu, get_all_blocks, create_block, get_children_ids,
                   del_first_child, upload_media, BLOCK_TYPE_FIELD, LarkAuth)

# ── LLMGate Config ──
LLMGATE_KEY = CFG.get("llmgate_api_key", "sk-llmgate-6siUCpdxpuqbejgmmcSv2bVlLMgsOx7rJUX1q7xDag6ocR3H")
LLMGATE_BASE = "https://llmgate.app/v1"
LLMGATE_MODEL = "gpt-5.4"

LARK_WIKI_SPACE_ID = CFG["lark_wiki_space_id"]
WIKI_DEST_NODE = CFG["wiki_dest_node"]
WIKI_URL = CFG.get("lark_wiki_url_prefix", "https://gg5pahjppze.sg.larksuite.com/wiki")

TRANS_STATE = os.path.join(DIR, "translate_state.json")
CLONE_STATE = os.path.join(DIR, "clone_state.json")

ZH_RE = re.compile(r'[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]')
BLOCK_SEP = "\n<<<B>>>\n"
BATCH_SIZE = 30

SYSTEM_PROMPT = """Dịch giả CN→VI chuyên nghiệp cho tài liệu công nghệ AI.
Quy tắc:
- Chỉ trả về bản dịch, không giải thích
- Giữ nguyên: tên tiếng Anh (OpenClaw, Claude, GPT, Cursor, GitHub...), URL, code
- Tên người Trung Quốc: phiên âm Hán-Việt
- Thuật ngữ kỹ thuật: dịch sang tiếng Việt tự nhiên, dễ hiểu
- Dịch mượt mà cho người Việt đọc
- Không có tiếng Trung → trả nguyên văn
- QUAN TRỌNG: Giữ nguyên markers [[[N]]] và separators <<<B>>>"""


# ── LLMGate API ──
_session = req_lib.Session()

def llm_call(prompt, system=SYSTEM_PROMPT, retries=3):
    for attempt in range(retries):
        try:
            r = _session.post(f"{LLMGATE_BASE}/chat/completions",
                headers={"Authorization": f"Bearer {LLMGATE_KEY}", "Content-Type": "application/json"},
                json={"model": LLMGATE_MODEL,
                      "messages": [{"role": "system", "content": system},
                                   {"role": "user", "content": prompt}],
                      "max_tokens": 8192, "temperature": 0.3},
                timeout=120, verify=False)
            d = r.json()
            if "choices" in d:
                return d["choices"][0]["message"]["content"].strip()
            if r.status_code == 429:
                time.sleep(5 + attempt * 5)
                continue
        except:
            if attempt < retries - 1:
                time.sleep(2 + attempt * 2)
    return None


def translate_title(text):
    if not text or not ZH_RE.search(text):
        return text
    result = llm_call(f"Dịch tiêu đề sau sang tiếng Việt:\n{text}")
    return result or text


def clean_elements(elements):
    cleaned = []
    for e in elements:
        if "text_run" in e:
            tr = e["text_run"]
            ne = {"text_run": {"content": tr.get("content", "")}}
            if "text_element_style" in tr:
                st = {}
                for k in ["bold", "italic", "strikethrough", "underline", "inline_code",
                          "background_color", "text_color"]:
                    if k in tr["text_element_style"] and tr["text_element_style"][k]:
                        st[k] = tr["text_element_style"][k]
                if "link" in tr["text_element_style"]:
                    lk = tr["text_element_style"]["link"]
                    if isinstance(lk, dict) and lk.get("url"):
                        st["link"] = {"url": lk["url"]}
                if st:
                    ne["text_run"]["text_element_style"] = st
            cleaned.append(ne)
        elif "mention_doc" in e:
            md = e["mention_doc"]
            t, u = md.get("title", "[doc]"), md.get("url", "")
            if u:
                cleaned.append({"text_run": {"content": t, "text_element_style": {"link": {"url": quote(u, safe='')}}}})
            else:
                cleaned.append({"text_run": {"content": t}})
        elif "equation" in e:
            cleaned.append(e)
    return cleaned if cleaned else [{"text_run": {"content": ""}}]


# ── Batch Translation ──
def batch_translate_blocks(blocks_map):
    """Pre-translate ALL text blocks using batch LLMGate calls. Returns {block_id: translated_elements}."""
    to_translate = []
    code_to_translate = []  # code blocks with Chinese - handled separately
    for block in blocks_map.values():
        bt = block.get('block_type', 0)
        if bt not in BLOCK_TYPE_FIELD:
            continue
        field = BLOCK_TYPE_FIELD[bt]
        if field not in block:
            continue
        elements = clean_elements(block[field].get('elements', []))
        has_zh = any('text_run' in e and ZH_RE.search(e['text_run'].get('content', '')) for e in elements)
        if not has_zh:
            continue
        # Code blocks (type 14): only translate if has Chinese, keep English code as-is
        if bt == 14:
            code_to_translate.append((block['block_id'], elements))
            continue
        run_indices, run_contents = [], []
        for i, e in enumerate(elements):
            if 'text_run' in e:
                run_indices.append(i)
                run_contents.append(e['text_run'].get('content', ''))
        if run_indices:
            to_translate.append((block['block_id'], run_indices, run_contents, elements))

    if not to_translate:
        return {}

    result = {}

    for batch_start in range(0, len(to_translate), BATCH_SIZE):
        batch = to_translate[batch_start:batch_start + BATCH_SIZE]
        batch_marked = []
        for bid, run_indices, run_contents, elements in batch:
            marked = ''
            for idx, content in enumerate(run_contents):
                marked += content
                if idx < len(run_contents) - 1:
                    marked += f'[[[{idx + 1}]]]'
            batch_marked.append(marked)

        combined = BLOCK_SEP.join(batch_marked)
        prompt = f"Dịch TỪNG đoạn (phân cách bởi <<<B>>>), giữ cấu trúc:\n\n{combined}"
        translated = llm_call(prompt)
        if not translated:
            translated = combined

        translated_blocks = re.split(r'<<<B>>>', translated)

        for j, (bid, run_indices, run_contents, elements) in enumerate(batch):
            if j >= len(translated_blocks):
                result[bid] = elements
                continue
            trans_text = translated_blocks[j].strip()
            parts = re.split(r'\[\[\[(\d+)\]\]\]', trans_text)
            translated_parts = [parts[k] for k in range(0, len(parts), 2)]

            if len(translated_parts) != len(run_indices):
                full = re.sub(r'\[\[\[\d+\]\]\]', '', trans_text)
                new_els = list(elements)
                for k, idx in enumerate(run_indices):
                    e = elements[idx]
                    if 'text_run' in e:
                        ne = {'text_run': {'content': full if k == 0 else ''}}
                        if 'text_element_style' in e['text_run']:
                            ne['text_run']['text_element_style'] = e['text_run']['text_element_style']
                        new_els[idx] = ne
                result[bid] = [r for r in new_els if not ('text_run' in r and not r['text_run'].get('content', ''))]
            else:
                new_els = list(elements)
                for k, (idx, tc) in enumerate(zip(run_indices, translated_parts)):
                    e = elements[idx]
                    if 'text_run' in e:
                        ne = {'text_run': {'content': tc}}
                        if 'text_element_style' in e['text_run']:
                            ne['text_run']['text_element_style'] = e['text_run']['text_element_style']
                        new_els[idx] = ne
                non_empty = [r for r in new_els if not ('text_run' in r and not r['text_run'].get('content', ''))]
                result[bid] = non_empty if non_empty else [{'text_run': {'content': ''.join(translated_parts)}}]

        time.sleep(0.5)

    # Translate code blocks with Chinese (separate prompt to preserve code structure)
    if code_to_translate:
        CODE_PROMPT = ("Trong đoạn code sau, CHỈ dịch các phần tiếng Trung sang tiếng Việt. "
                       "GIỮ NGUYÊN toàn bộ code, cú pháp, tên biến, tên hàm, tên lệnh tiếng Anh. "
                       "Chỉ dịch comment và text tiếng Trung. Nếu là tiếng Anh thì giữ nguyên. "
                       "Trả về KẾT QUẢ duy nhất, không giải thích.")
        for bid, elements in code_to_translate:
            full_text = "".join(e.get("text_run", {}).get("content", "") for e in elements if "text_run" in e)
            if not ZH_RE.search(full_text):
                continue  # English code - keep as-is
            translated = llm_call(f"{CODE_PROMPT}\n\n{full_text}")
            if translated and translated != full_text:
                result[bid] = [{"text_run": {"content": translated}}]
            time.sleep(0.3)

    return result


# ── Block Processing ──
def process_block_vi(sb, sm, doc_id, pid, stats, pre_trans):
    bt = sb.get('block_type', 0)
    bid = sb.get('block_id', '')
    if bt in (1, 32, 25):
        return

    if bt in BLOCK_TYPE_FIELD:
        field = BLOCK_TYPE_FIELD[bt]
        if field not in sb:
            return
        elements = pre_trans.get(bid, clean_elements(sb[field].get('elements', [])))
        data = {'block_type': bt, field: {'elements': elements}}
        if 'style' in sb[field]:
            cs = {}
            for k in ['align', 'done', 'folded', 'language', 'wrap']:
                if k in sb[field]['style']:
                    cs[k] = sb[field]['style'][k]
            if cs:
                data[field]['style'] = cs
        r = create_block(doc_id, pid, data)
        if r and 'block_id' in r:
            stats['blocks'] += 1
        else:
            stats['failed'] += 1

    elif bt == 27:
        src_token = sb.get('image', {}).get('token', '')
        if src_token:
            r = create_block(doc_id, pid, {'block_type': 27, 'image': {}})
            if r and 'block_id' in r:
                stats['blocks'] += 1
                tmp = os.path.join(tempfile.gettempdir(), f"vi_img_{time.time_ns()}.tmp")
                try:
                    img_r = lark.get_raw(f"/drive/v1/medias/{src_token}/download")
                    if img_r.status_code == 200 and len(img_r.content) > 0:
                        with open(tmp, 'wb') as f:
                            f.write(img_r.content)
                        ft = upload_media(tmp, r['block_id'], 'docx_image')
                        if ft:
                            lark.patch(f"/docx/v1/documents/{doc_id}/blocks/{r['block_id']}",
                                       json_data={"replace_image": {"token": ft}})
                            stats['images'] += 1
                except:
                    stats['img_fail'] += 1
                finally:
                    try: os.remove(tmp)
                    except: pass

    elif bt == 23:
        sf = sb.get('file', {})
        src_token, fn = sf.get('token', ''), sf.get('name', 'file')
        r = create_block(doc_id, pid, {'block_type': 23, 'file': {'view_type': 2}})
        if r and 'block_id' in r:
            stats['blocks'] += 1
            nbt = r.get('block_type', 0)
            ch = r.get('children', [])
            fbid = ch[0] if nbt == 33 and ch else r['block_id']
            if src_token:
                tmp = os.path.join(tempfile.gettempdir(), f"vi_file_{time.time_ns()}.tmp")
                try:
                    fr = lark.get_raw(f"/drive/v1/medias/{src_token}/download")
                    if fr.status_code == 200 and len(fr.content) > 0:
                        with open(tmp, 'wb') as f:
                            f.write(fr.content)
                        ft = upload_media(tmp, fbid, 'docx_file', fn)
                        if ft:
                            lark.patch(f"/docx/v1/documents/{doc_id}/blocks/{fbid}",
                                       json_data={"replace_file": {"token": ft}})
                            stats['files'] += 1
                except:
                    stats['file_fail'] += 1
                finally:
                    try: os.remove(tmp)
                    except: pass

    elif bt == 22:
        r = create_block(doc_id, pid, {'block_type': 22, 'divider': {}})
        if r and 'block_id' in r:
            stats['blocks'] += 1

    elif bt == 19:
        co = sb.get('callout', {})
        cd = {'block_type': 19}
        cc = {k: co[k] for k in ['background_color', 'border_color', 'emoji_id'] if k in co}
        if cc:
            cd['callout'] = cc
        r = create_block(doc_id, pid, cd)
        if r and 'block_id' in r:
            stats['blocks'] += 1
            del_first_child(doc_id, r['block_id'])
            for c in sb.get('children', []):
                if c in sm:
                    process_block_vi(sm[c], sm, doc_id, r['block_id'], stats, pre_trans)

    elif bt == 24:
        src_cols = [c for c in sb.get('children', []) if c in sm and sm[c].get('block_type') == 25]
        if not src_cols:
            return
        cr = [sm[c].get('grid_column', {}).get('width_ratio', 50) for c in src_cols]
        r = create_block(doc_id, pid, {'block_type': 24, 'grid': {'column_size': len(src_cols), 'column_size_ratio': cr}})
        if not r or 'block_id' not in r:
            return
        stats['blocks'] += 1
        dci = get_children_ids(doc_id, r['block_id'])
        stats['blocks'] += len(dci)
        for i, sid in enumerate(src_cols):
            if i >= len(dci):
                break
            col_children = sm[sid].get('children', [])
            col_default = get_children_ids(doc_id, dci[i])
            for gi, g in enumerate(col_children):
                csb = sm.get(g)
                if not csb:
                    continue
                sbt = csb.get('block_type', 0)
                if gi == 0 and col_default and sbt in BLOCK_TYPE_FIELD:
                    fld = BLOCK_TYPE_FIELD.get(sbt)
                    if fld and fld in csb:
                        els = pre_trans.get(csb['block_id'], clean_elements(csb[fld].get('elements', [])))
                        lark.patch(f"/docx/v1/documents/{doc_id}/blocks/{col_default[0]}",
                                   json_data={"update_text_elements": {"elements": els, "style": {}}})
                        continue
                process_block_vi(csb, sm, doc_id, dci[i], stats, pre_trans)

    elif bt == 34:
        r = create_block(doc_id, pid, {'block_type': 34, 'quote_container': {}})
        if r and 'block_id' in r:
            stats['blocks'] += 1
            for c in sb.get('children', []):
                if c in sm:
                    process_block_vi(sm[c], sm, doc_id, r['block_id'], stats, pre_trans)

    elif bt == 49:
        for c in sb.get('children', []):
            if c in sm:
                process_block_vi(sm[c], sm, doc_id, pid, stats, pre_trans)

    elif bt in (26, 29):
        comp = sb.get('iframe', {}).get('component', {})
        url = comp.get('url', '')
        if url:
            r = create_block(doc_id, pid, {'block_type': 26, 'iframe': {
                'component': {'iframe_type': comp.get('iframe_type', 99), 'url': url}}})
            if r and 'block_id' in r:
                stats['blocks'] += 1

    elif bt == 33:
        for c in sb.get('children', []):
            if c in sm:
                process_block_vi(sm[c], sm, doc_id, pid, stats, pre_trans)

    elif bt == 31:
        td = sb.get('table', {})
        prop = td.get('property', {})
        rows, cols = prop.get('row_size', 1), prop.get('column_size', 1)
        cells = td.get('cells', [])
        tp = {'row_size': min(rows, 9), 'column_size': cols}
        cw = prop.get('column_width')
        if cw and len(cw) == cols:
            tp['column_width'] = cw
        r = create_block(doc_id, pid, {'block_type': 31, 'table': {'property': tp}})
        if r and 'block_id' in r:
            stats['blocks'] += 1
            tbid = r['block_id']
            time.sleep(0.3)
            tbl_info = lark.get(f"/docx/v1/documents/{doc_id}/blocks/{tbid}")
            if tbl_info.get('code') == 0:
                dest_cells = tbl_info['data']['block'].get('table', {}).get('cells', [])
                created_rows = tbl_info['data']['block'].get('table', {}).get('property', {}).get('row_size', 1)
                while created_rows < rows:
                    lark.patch(f"/docx/v1/documents/{doc_id}/blocks/{tbid}",
                               json_data={"insert_table_row": {"row_index": created_rows}})
                    created_rows += 1
                    time.sleep(0.2)
                if created_rows > min(rows, 9):
                    tbl_info = lark.get(f"/docx/v1/documents/{doc_id}/blocks/{tbid}")
                    dest_cells = tbl_info['data']['block'].get('table', {}).get('cells', [])
                for ci, src_cid in enumerate(cells):
                    if ci >= len(dest_cells):
                        break
                    src_cell = sm.get(src_cid, {})
                    for child_id in src_cell.get('children', []):
                        if child_id in sm:
                            process_block_vi(sm[child_id], sm, doc_id, dest_cells[ci], stats, pre_trans)
    else:
        stats['skipped'] += 1


# ── State ──
def load_trans_state():
    if os.path.exists(TRANS_STATE):
        for _ in range(3):
            try:
                with open(TRANS_STATE, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except:
                time.sleep(0.5)
    return {'translated': [], 'trans_map': {}}

def save_trans_state(ts):
    try:
        current = load_trans_state()
        merged = list(set(current.get('translated', [])) | set(ts.get('translated', [])))
        merged_map = {**current.get('trans_map', {}), **ts.get('trans_map', {})}
        data = {'translated': merged, 'trans_map': merged_map}
        if 'translate_parent' in ts:
            data['translate_parent'] = ts['translate_parent']
        elif 'translate_parent' in current:
            data['translate_parent'] = current['translate_parent']
        tmp = TRANS_STATE + ".tmp"
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, TRANS_STATE)
    except:
        pass


# ── Translate One Page ──
def translate_one(clone_node_token, dest_parent, original_title):
    t0 = time.time()
    res = {'success': False, 'url': '', 'dest_node': '', 'blocks': 0, 'images': 0,
           'img_fail': 0, 'files': 0, 'file_fail': 0, 'failed': 0, 'skipped': 0,
           'elapsed': 0, 'error': '', 'vi_title': ''}
    try:
        ni = lark.get(f"/wiki/v2/spaces/get_node?token={clone_node_token}")
        if ni.get('code') != 0:
            res['error'] = 'clone node not found'
            return res
        clone_doc = ni['data']['node']['obj_token']
        blocks = get_all_blocks(lark, clone_doc)
        if not blocks:
            res['error'] = 'No blocks'
            return res
        sm = {b['block_id']: b for b in blocks}

        vi_title = translate_title(original_title)
        res['vi_title'] = vi_title

        # Batch translate all text
        pre_trans = batch_translate_blocks(sm)

        # Create VI page
        cr = None
        for a in range(5):
            cr = lark.post(f"/wiki/v2/spaces/{LARK_WIKI_SPACE_ID}/nodes", json_data={
                "obj_type": "docx", "parent_node_token": dest_parent,
                "node_type": "origin", "title": vi_title})
            if cr.get('code') == 0:
                break
            if cr.get('code') in (230001, 99991400):
                time.sleep(5 + a * 5)
                continue
            break
        if not cr or cr.get('code') != 0:
            res['error'] = (cr or {}).get('msg', 'create fail')
            return res

        dn = cr['data']['node']
        vi_doc = dn['obj_token']
        res['dest_node'] = dn['node_token']
        res['url'] = f"{WIKI_URL}/{dn['node_token']}"

        stats = {'blocks': 0, 'images': 0, 'files': 0, 'failed': 0, 'skipped': 0,
                 'img_fail': 0, 'file_fail': 0}
        root_children = blocks[0].get('children', [])
        for bid in root_children:
            if bid in sm:
                process_block_vi(sm[bid], sm, vi_doc, vi_doc, stats, pre_trans)

        res['success'] = True
        for k in stats:
            res[k] = stats[k]
    except Exception as e:
        res['error'] = str(e)[:200]
    res['elapsed'] = round(time.time() - t0)
    return res


# ── Main ──
def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "full"
    start_from = 0
    end_at = 281
    only_stt = None

    if '--start' in sys.argv:
        start_from = int(sys.argv[sys.argv.index('--start') + 1])
    if '--end' in sys.argv:
        end_at = int(sys.argv[sys.argv.index('--end') + 1])
    if '--stt' in sys.argv:
        only_stt = int(sys.argv[sys.argv.index('--stt') + 1])
    if mode == "test":
        end_at = start_from + 1

    print("=" * 60)
    print(f"  TRANSLATE CN→VI (LLMGate GPT-5.4 batch)")
    print("=" * 60)

    # Load state
    clone_state = json.load(open(CLONE_STATE))
    dest_map = clone_state.get('dest_map', {})

    all_nodes = []
    for wiki in CFG.get("source_wikis", []):
        fpath = os.path.join(DIR, 'nodes', f"{wiki['name']}_nodes.json")
        if os.path.exists(fpath):
            with open(fpath, 'r', encoding='utf-8') as f:
                nodes = json.load(f)
            for n in nodes:
                n["_source"] = wiki["name"]
            all_nodes.extend(nodes)

    total = len(all_nodes)
    trans_state = load_trans_state()
    translated = set(trans_state.get('translated', []))
    print(f"  Pages: {total} | Translated: {len(translated)}")

    # Test LLMGate
    test = llm_call("Dịch: 你好")
    print(f"  LLMGate test: {test}")

    # VI parent folder
    vi_parent = trans_state.get('translate_parent', '')
    if not vi_parent:
        cr = lark.post(f"/wiki/v2/spaces/{LARK_WIKI_SPACE_ID}/nodes", json_data={
            "obj_type": "docx", "node_type": "origin",
            "title": "Ban dich Tieng Viet (GPT-5.4)", "parent_node_token": WIKI_DEST_NODE})
        if cr.get('code') == 0:
            vi_parent = cr['data']['node']['node_token']
            trans_state['translate_parent'] = vi_parent
            save_trans_state(trans_state)
    print(f"  VI folder: {vi_parent}")

    # Load Base record map for realtime updates
    base_map_file = os.path.join(DIR, 'base_record_map.json')
    base_map = {}
    if os.path.exists(base_map_file):
        with open(base_map_file, 'r') as f:
            bm = json.load(f)
        base_map = bm.get('records', {})
        BASE_APP = bm.get('app_token', '')
        BASE_TBL = bm.get('table_id', '')
        print(f"  Base tracker: {len(base_map)} records")
    else:
        BASE_APP = BASE_TBL = ''

    def base_update(nt, fields):
        if not BASE_APP or nt not in base_map:
            return
        try:
            req_lib.put(f"{LARK_BASE}/bitable/v1/apps/{BASE_APP}/tables/{BASE_TBL}/records/{base_map[nt]}",
                headers=lark.h(), json={'fields': fields}, timeout=15, verify=False)
        except:
            pass

    ok, fail, count = 0, 0, 0
    t0 = time.time()

    for i, node in enumerate(all_nodes):
        if only_stt is not None and i != only_stt:
            continue
        if i < start_from or i >= end_at:
            continue
        nt = node['node_token']
        if nt in translated:
            continue
        if nt not in dest_map:
            continue

        title = node.get('title', '(untitled)')
        clone_token = dest_map[nt]

        print(f"\n[{i + 1}/{total}] {title}")
        base_update(nt, {'Dich': 'Dang dich...'})

        info = translate_one(clone_token, vi_parent, title)

        if info['success']:
            ok += 1
            count += 1
            print(f"  OK | {info['vi_title'][:50]}")
            print(f"  {info['blocks']}blk {info['images']}img {info['files']}file | {info['elapsed']}s")
            translated.add(nt)
            trans_state['translated'] = list(translated)
            trans_state['trans_map'][nt] = info['dest_node']
            save_trans_state(trans_state)
            base_update(nt, {
                'Dich': 'Da dich',
                'Tieu de VI': info['vi_title'],
                'Link dich': {'link': info['url'], 'text': info['url']},
                'Blocks': f"{info['blocks']}blk {info['images']}img {info['elapsed']}s",
                'QA Dich': 'OK' if info['failed'] == 0 else 'WARN'
            })
        else:
            fail += 1
            count += 1
            print(f"  FAIL | {info['error']}")
            translated.add(nt)
            trans_state['translated'] = list(translated)
            save_trans_state(trans_state)
            base_update(nt, {'Dich': 'Loi', 'Ghi chu': info['error'][:200]})

        time.sleep(0.3)

    elapsed = time.time() - t0
    print(f"\n{'=' * 60}")
    print(f"  DONE ({elapsed:.0f}s = {elapsed / 60:.1f} min)")
    print(f"  OK: {ok} | FAIL: {fail}")
    print(f"{'=' * 60}")


if __name__ == '__main__':
    main()
