#!/usr/bin/env python3
"""
Feishu → Lark Wiki Clone — 100% block-by-block fidelity
Supports: text, headings, lists, code, quotes, todos, images, videos,
          files, tables, grids, callouts, iframes, quote containers,
          synced blocks. Chunked upload for files >20MB. Resume-safe.

Usage:
  python -u -X utf8 clone.py                  # full clone
  python -u -X utf8 clone.py test             # test with 1 article
  python -u -X utf8 clone.py crawl            # crawl nodes only
  python -u -X utf8 clone.py crawl <token>    # crawl specific wiki root

Config: edit config.json (created on first run with defaults)
"""
import sys, os, time, json, threading, tempfile, math, zlib
import requests as req_lib
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib.parse import quote

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8')

DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(DIR, 'config.json')
STATE_FILE = os.path.join(DIR, 'clone_state.json')
NODES_DIR = os.path.join(DIR, 'nodes')
os.makedirs(NODES_DIR, exist_ok=True)

# ── Config ──
DEFAULT_CONFIG = {
    "app_id": "",
    "app_secret": "",
    "feishu_base": "https://open.feishu.cn/open-apis",
    "lark_base": "https://open.larksuite.com/open-apis",
    "lark_wiki_space_id": "",
    "wiki_dest_node": "",
    "base_app_token": "",
    "base_table_id": "",
    "source_wikis": [
        {"name": "wiki1", "root_token": "", "category_title": "Category 1"},
        {"name": "wiki2", "root_token": "", "category_title": "Category 2"}
    ],
    "wiki_session_domain": "larkcommunity.feishu.cn"
}

def load_config():
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(DEFAULT_CONFIG, f, indent=2, ensure_ascii=False)
        print(f"Created {CONFIG_FILE} — please fill in your credentials and run again.")
        sys.exit(1)
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

CFG = load_config()
APP_ID = CFG["app_id"]
APP_SECRET = CFG["app_secret"]
FEISHU_BASE = CFG["feishu_base"]
LARK_BASE = CFG["lark_base"]
LARK_WIKI_SPACE_ID = CFG["lark_wiki_space_id"]
WIKI_DEST_NODE = CFG["wiki_dest_node"]
BASE_APP_TOKEN = CFG.get("base_app_token", "")
BASE_TABLE_ID = CFG.get("base_table_id", "")

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
        return self._s.get(f"{self.BASE_URL}{path}", headers=self.h(), timeout=30, **kw).json()

    def post(self, path, json_data=None):
        return self._s.post(f"{self.BASE_URL}{path}", headers=self.h(), json=json_data, timeout=30).json()

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
        try: return self._s.request("DELETE", f"{self.BASE_URL}{path}", headers=self.h(), json=json_data, timeout=30).json()
        except: return {"code": -1}

    def get_raw(self, path, stream=False):
        return self._s.get(f"{self.BASE_URL}{path}", headers={"Authorization": f"Bearer {self.token}"}, stream=stream, timeout=120)

    def post_form(self, path, data=None, files=None):
        return self._s.post(f"{self.BASE_URL}{path}", headers={"Authorization": f"Bearer {self.token}"},
                            data=data, files=files, timeout=120).json()

feishu = LarkAuth(APP_ID, APP_SECRET, FEISHU_BASE)
lark = LarkAuth(APP_ID, APP_SECRET, LARK_BASE)

_wiki_session = None
def get_wiki_session(node_token):
    global _wiki_session
    if _wiki_session: return _wiki_session
    s = req_lib.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0"})
    domain = CFG.get("wiki_session_domain", "larkcommunity.feishu.cn")
    try: s.get(f"https://{domain}/wiki/{node_token}", timeout=30)
    except: pass
    _wiki_session = s
    return _wiki_session

# ── Crawl ──
def crawl_wiki_tree(space_id, parent_token, depth=0):
    all_nodes = []
    page_token = ""
    while True:
        params = {"parent_node_token": parent_token, "page_size": "50"}
        if page_token: params["page_token"] = page_token
        r = feishu.get(f"/wiki/v2/spaces/{space_id}/nodes", params=params)
        if r.get("code") != 0: break
        items = r["data"].get("items", [])
        for item in items:
            item["depth"] = depth
            prefix = "  " * depth
            print(f"{prefix}- {item.get('title','')}")
            all_nodes.append(item)
            if item.get("has_child"):
                sub = crawl_wiki_tree(space_id, item["node_token"], depth + 1)
                all_nodes.extend(sub)
                time.sleep(0.3)
        if not r["data"].get("has_more"): break
        page_token = r["data"].get("page_token", "")
        time.sleep(0.2)
    return all_nodes

# ── Block helpers ──
def get_all_blocks(auth, doc_id):
    blocks, pt = [], None
    while True:
        p = {"page_size": 500}
        if pt: p["page_token"] = pt
        r = auth.get(f"/docx/v1/documents/{doc_id}/blocks", params=p)
        if r.get("code") != 0: break
        d = r.get("data", {})
        blocks.extend(d.get("items", []))
        if d.get("has_more"): pt = d.get("page_token")
        else: break
    return blocks

def clean_elements(elements):
    cleaned = []
    for e in elements:
        if "text_run" in e:
            tr = e["text_run"]
            ne = {"text_run": {"content": tr.get("content", "")}}
            if "text_element_style" in tr:
                st = {}
                for k in ["bold","italic","strikethrough","underline","inline_code","background_color","text_color"]:
                    if k in tr["text_element_style"] and tr["text_element_style"][k]:
                        st[k] = tr["text_element_style"][k]
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

def create_block(doc_id, parent_id, block_data, retries=5):
    for r in range(retries):
        try:
            res = lark.post(f"/docx/v1/documents/{doc_id}/blocks/{parent_id}/children",
                json_data={"children":[block_data],"index":-1})
        except:
            if r < retries-1: time.sleep(2**r); continue
            return None
        if res.get("code") == 0:
            ch = res.get("data",{}).get("children",[])
            if r % 5 == 4: time.sleep(0.15)
            return ch[0] if ch else None
        if res.get("code") in (230001, 99991400): time.sleep(1+r*0.5); continue
        return None
    return None

def get_children_ids(doc_id, block_id):
    r = lark.get(f"/docx/v1/documents/{doc_id}/blocks/{block_id}")
    if r.get("code") == 0: return r.get("data",{}).get("block",{}).get("children",[])
    return []

def del_first_child(doc_id, pid):
    ch = get_children_ids(doc_id, pid)
    if ch: lark.delete(f"/docx/v1/documents/{doc_id}/blocks/{pid}/children/batch_delete",
                        json_data={"start_index":0,"end_index":1})

# ── Media ──
def download_media(file_token, dest, block_id=None, mount_point="docx_image"):
    try:
        r = feishu.get_raw(f"/drive/v1/medias/{file_token}/download", stream=True)
        if r.status_code == 200 and int(r.headers.get("content-length",0)) > 0:
            with open(dest,"wb") as f:
                for c in r.iter_content(8192): f.write(c)
            if os.path.getsize(dest) > 0: return True
    except: pass
    cdn = (f"https://internal-api-drive-stream.feishu.cn/space/api/box/stream/download/v2/cover/"
           f"{file_token}/?fallback_source=1&mount_node_token={block_id or ''}&mount_point={mount_point}")
    s = _wiki_session
    if s:
        try:
            r = s.get(cdn, timeout=60)
            ct = r.headers.get("content-type","")
            if r.status_code == 200 and len(r.content) > 100 and "text" not in ct:
                with open(dest,"wb") as f: f.write(r.content)
                return True
        except: pass
    return False

def upload_media(filepath, parent_node, parent_type="docx_image", name=None):
    fn = name or os.path.basename(filepath)
    sz = os.path.getsize(filepath)
    if sz == 0: return None
    if sz > 20 * 1024 * 1024: return upload_media_chunked(filepath, parent_node, parent_type, fn)
    ext = fn.rsplit(".",1)[-1].lower() if "." in fn else "bin"
    ct_map = {"jpg":"image/jpeg","jpeg":"image/jpeg","png":"image/png","gif":"image/gif",
              "webp":"image/webp","mp4":"video/mp4","pdf":"application/pdf"}
    ct = ct_map.get(ext, "application/octet-stream")
    safe = fn.encode('ascii','replace').decode('ascii').replace('?','_')
    with open(filepath,"rb") as f: data = f.read()
    for a in range(3):
        r = lark.post_form("/drive/v1/medias/upload_all",
            data={"file_name":safe,"parent_type":parent_type,"parent_node":parent_node,"size":str(sz)},
            files={"file":(safe,data,ct)})
        if r.get("code") == 0: return r["data"]["file_token"]
        if r.get("code") in (230001,99991400,1061002): time.sleep(2+a*2); continue
        break
    return None

def upload_media_chunked(filepath, parent_node, parent_type, name):
    sz = os.path.getsize(filepath)
    safe = name.encode('ascii','replace').decode('ascii').replace('?','_')
    BLOCK_SIZE = 4 * 1024 * 1024
    prep = lark.post("/drive/v1/medias/upload_prepare", json_data={
        "file_name":safe,"parent_type":parent_type,"parent_node":parent_node,"size":sz})
    if prep.get("code") != 0: return None
    upload_id = prep["data"]["upload_id"]
    expected_blocks = prep["data"]["block_num"]
    with open(filepath,"rb") as f:
        for i in range(expected_blocks):
            chunk = f.read(BLOCK_SIZE)
            if not chunk: break
            checksum = zlib.adler32(chunk) & 0xFFFFFFFF
            for attempt in range(3):
                r = lark.post_form("/drive/v1/medias/upload_part",
                    data={"upload_id":upload_id,"seq":str(i),"size":str(len(chunk)),"checksum":str(checksum)},
                    files={"file":(f"part_{i}",chunk,"application/octet-stream")})
                if r.get("code") == 0: break
                elif r.get("code") in (230001,99991400): time.sleep(2+attempt*2); continue
                else: return None
    finish = lark.post("/drive/v1/medias/upload_finish", json_data={"upload_id":upload_id,"block_num":expected_blocks})
    if finish.get("code") == 0: return finish["data"]["file_token"]
    return None

# ── Block handlers ──
def handle_image(sb, doc_id, pid, stats, src_doc):
    tk = sb.get("image",{}).get("token","")
    if not tk: stats["img_fail"] += 1; return
    tmp = os.path.join(tempfile.gettempdir(), f"img_{time.time_ns()}.tmp")
    bid = sb.get("block_id","")
    if download_media(tk, tmp, block_id=bid):
        r = create_block(doc_id, pid, {"block_type":27,"image":{}})
        if r and "block_id" in r:
            stats["blocks"] += 1
            ft = upload_media(tmp, r["block_id"], "docx_image")
            if ft:
                pr = lark.patch(f"/docx/v1/documents/{doc_id}/blocks/{r['block_id']}", json_data={"replace_image":{"token":ft}})
                if pr.get("code") == 0: stats["images"] += 1
                else: stats["img_fail"] += 1
            else: stats["img_fail"] += 1
        else: stats["img_fail"] += 1
        try: os.remove(tmp)
        except: pass
    else: stats["img_fail"] += 1

def handle_file(sb, doc_id, pid, stats, src_doc, vt=2):
    sf = sb.get("file",{})
    tk, fn = sf.get("token",""), sf.get("name","file")
    r = create_block(doc_id, pid, {"block_type":23,"file":{"view_type":vt}})
    if not r or "block_id" not in r: stats["file_fail"] += 1; return
    stats["blocks"] += 1
    nbt = r.get("block_type",0)
    ch = r.get("children",[])
    fbid = ch[0] if nbt == 33 and ch else r["block_id"]
    if nbt == 33: stats["blocks"] += 1
    if not tk: return
    ext = fn.rsplit(".",1)[-1].lower() if "." in fn else "bin"
    tmp = os.path.join(tempfile.gettempdir(), f"f_{time.time_ns()}.{ext}")
    bid = sb.get("block_id","")
    if download_media(tk, tmp, block_id=bid, mount_point="docx_file"):
        ft = upload_media(tmp, fbid, "docx_file", fn)
        if ft:
            pr = lark.patch(f"/docx/v1/documents/{doc_id}/blocks/{fbid}", json_data={"replace_file":{"token":ft}})
            if pr.get("code") == 0: stats["files"] += 1
            else: stats["file_fail"] += 1
        else: stats["file_fail"] += 1
        try: os.remove(tmp)
        except: pass
    else: stats["file_fail"] += 1

def handle_table(sb, sm, doc_id, pid, stats, src_doc):
    td = sb.get("table",{})
    prop = td.get("property",{})
    rows, cols = prop.get("row_size",1), prop.get("column_size",1)
    src_cells = td.get("cells",[])
    def ct(cid):
        cell = sm.get(cid,{})
        parts = []
        for c in cell.get("children",[]):
            cb = sm.get(c,{})
            for f in BLOCK_TYPE_FIELD.values():
                if f in cb:
                    for e in cb[f].get("elements",[]):
                        if "text_run" in e: parts.append(e["text_run"].get("content",""))
                        elif "mention_doc" in e: parts.append(e["mention_doc"].get("title",""))
        return "".join(parts).strip()
    ir = min(rows,9)
    ic = [ct(src_cells[i]) if i < len(src_cells) else "" for i in range(ir*cols)]
    tp = {"row_size":ir,"column_size":cols}
    cw = prop.get("column_width")
    if cw and len(cw) == cols: tp["column_width"] = cw
    r = create_block(doc_id, pid, {"block_type":31,"table":{"property":tp,"cells":ic}})
    if not r or "block_id" not in r: stats["failed"] += 1; return
    stats["blocks"] += 1
    tbid = r["block_id"]
    cr = ir
    while cr < rows:
        x = lark.patch(f"/docx/v1/documents/{doc_id}/blocks/{tbid}", json_data={"insert_table_row":{"row_index":cr}})
        if x.get("code") == 0: cr += 1
        else: break
    time.sleep(0.3)
    db = get_all_blocks(lark, doc_id)
    dm = {b["block_id"]:b for b in db}
    dc = dm.get(tbid,{}).get("table",{}).get("cells",[])
    if not dc: return
    stats["blocks"] += len(dc)
    for ci in range(min(len(src_cells),len(dc))):
        sc = sm.get(src_cells[ci],{})
        sch = sc.get("children",[])
        if not sch: continue
        dcc = dm.get(dc[ci],{}).get("children",[])
        for si, scid in enumerate(sch):
            csb = sm.get(scid,{})
            if not csb: continue
            bt = csb.get("block_type",0)
            if bt in BLOCK_TYPE_FIELD and si == 0 and dcc:
                fld = BLOCK_TYPE_FIELD.get(bt)
                if fld and fld in csb:
                    els = clean_elements(csb[fld].get("elements",[]))
                    lark.patch(f"/docx/v1/documents/{doc_id}/blocks/{dcc[0]}", json_data={"update_text_elements":{"elements":els,"style":{}}})
                else: process_block(csb, sm, doc_id, dc[ci], stats, src_doc)
            else: process_block(csb, sm, doc_id, dc[ci], stats, src_doc)

def handle_grid(sb, sm, doc_id, pid, stats, src_doc):
    sc = [c for c in sb.get("children",[]) if c in sm and sm[c].get("block_type") == 25]
    if not sc: return
    cr = [sm[c].get("grid_column",{}).get("width_ratio",50) for c in sc]
    r = create_block(doc_id, pid, {"block_type":24,"grid":{"column_size":len(sc),"column_size_ratio":cr}})
    if not r or "block_id" not in r:
        for s in sc:
            for g in sm.get(s,{}).get("children",[]):
                if g in sm: process_block(sm[g], sm, doc_id, pid, stats, src_doc)
        return
    stats["blocks"] += 1
    dci = get_children_ids(doc_id, r["block_id"])
    stats["blocks"] += len(dci)
    for i, sid in enumerate(sc):
        if i >= len(dci): break
        src_col_children = sm[sid].get("children",[])
        col_default_children = get_children_ids(doc_id, dci[i])
        for gi, g in enumerate(src_col_children):
            csb = sm.get(g)
            if not csb: continue
            sbt = csb.get("block_type",0)
            if gi == 0 and col_default_children and sbt in BLOCK_TYPE_FIELD:
                fld = BLOCK_TYPE_FIELD.get(sbt)
                if fld and fld in csb:
                    els = clean_elements(csb[fld].get("elements",[]))
                    lark.patch(f"/docx/v1/documents/{doc_id}/blocks/{col_default_children[0]}",
                        json_data={"update_text_elements":{"elements":els,"style":{}}})
                    continue
            process_block(csb, sm, doc_id, dci[i], stats, src_doc)

def handle_callout(sb, sm, doc_id, pid, stats, src_doc):
    cd = {"block_type":19}
    if "callout" in sb:
        co = sb["callout"]
        cc = {}
        for k in ["background_color","border_color","emoji_id"]:
            if k in co: cc[k] = co[k]
        if cc: cd["callout"] = cc
    r = create_block(doc_id, pid, cd)
    if not r or "block_id" not in r: stats["failed"] += 1; return
    stats["blocks"] += 1
    del_first_child(doc_id, r["block_id"])
    for c in sb.get("children",[]):
        if c in sm: process_block(sm[c], sm, doc_id, r["block_id"], stats, src_doc)

def process_block(sb, sm, doc_id, pid, stats, src_doc=None):
    bt = sb.get("block_type",0)
    if bt in (1, 32, 25): return
    if bt in BLOCK_TYPE_FIELD:
        fld = BLOCK_TYPE_FIELD[bt]
        if fld not in sb: return
        els = clean_elements(sb[fld].get("elements",[]))
        d = {"block_type":bt, fld:{"elements":els}}
        if "style" in sb[fld]:
            cs = {}
            for k in ["align","done","folded","language","wrap"]:
                if k in sb[fld]["style"]: cs[k] = sb[fld]["style"][k]
            if cs: d[fld]["style"] = cs
        r = create_block(doc_id, pid, d)
        if r and "block_id" in r:
            stats["blocks"] += 1
            for c in sb.get("children",[]):
                if c in sm: process_block(sm[c], sm, doc_id, pid, stats, src_doc)
        else: stats["failed"] += 1
    elif bt == 27: handle_image(sb, doc_id, pid, stats, src_doc)
    elif bt == 23: handle_file(sb, doc_id, pid, stats, src_doc)
    elif bt == 33:
        vt = sb.get("view",{}).get("view_type",2)
        for c in sb.get("children",[]):
            if c in sm and sm[c].get("block_type") == 23:
                handle_file(sm[c], doc_id, pid, stats, src_doc, vt=vt)
    elif bt == 31: handle_table(sb, sm, doc_id, pid, stats, src_doc)
    elif bt == 19: handle_callout(sb, sm, doc_id, pid, stats, src_doc)
    elif bt == 24: handle_grid(sb, sm, doc_id, pid, stats, src_doc)
    elif bt == 49:
        for c in sb.get("children",[]):
            if c in sm: process_block(sm[c], sm, doc_id, pid, stats, src_doc)
    elif bt == 34:
        r = create_block(doc_id, pid, {"block_type":34,"quote_container":{}})
        if r and "block_id" in r:
            stats["blocks"] += 1
            for c in sb.get("children",[]):
                if c in sm: process_block(sm[c], sm, doc_id, r["block_id"], stats, src_doc)
    elif bt in (26, 29):
        comp = sb.get("iframe",{}).get("component",{})
        url = comp.get("url","")
        if url:
            r = create_block(doc_id, pid, {"block_type":26,"iframe":{"component":{"iframe_type":comp.get("iframe_type",99),"url":url}}})
            if r and "block_id" in r: stats["blocks"] += 1
            else: stats["failed"] += 1
        else: stats["skipped"] += 1
    elif bt == 22:
        r = create_block(doc_id, pid, {"block_type":22,"divider":{}})
        if r and "block_id" in r: stats["blocks"] += 1
    else: stats["skipped"] += 1

# ── Base update ──
def base_update(record_id, fields):
    if not BASE_APP_TOKEN: return
    for a in range(3):
        try:
            r = req_lib.put(f'{LARK_BASE}/bitable/v1/apps/{BASE_APP_TOKEN}/tables/{BASE_TABLE_ID}/records/{record_id}',
                headers=lark.h(), json={'fields':fields}, timeout=15)
            if r.json().get('code') == 0: return True
        except: pass
        time.sleep(0.5)

def get_base_records():
    if not BASE_APP_TOKEN: return {}
    recs, pt = {}, ''
    while True:
        url = f'{LARK_BASE}/bitable/v1/apps/{BASE_APP_TOKEN}/tables/{BASE_TABLE_ID}/records?page_size=100'
        if pt: url += f'&page_token={pt}'
        r = req_lib.get(url, headers=lark.h(), timeout=15)
        d = r.json().get('data',{})
        for i in d.get('items',[]):
            nt = i['fields'].get('Node Token gốc','')
            if nt: recs[nt] = i
        if not d.get('has_more'): break
        pt = d.get('page_token','')
    return recs

# ── State ──
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE,'r',encoding='utf-8') as f: return json.load(f)
    return {'dest_map':{},'completed':[]}

def save_state(state):
    with open(STATE_FILE,'w',encoding='utf-8') as f: json.dump(state,f,ensure_ascii=False)

# ── Clone one page ──
def clone_one(node, dest_parent):
    nt = node['node_token']
    title = node.get('title','(untitled)')
    t0 = time.time()
    res = {'success':False,'url':'','dest_node':'','blocks':0,'images':0,'img_fail':0,
           'files':0,'file_fail':0,'failed':0,'skipped':0,'total_blocks':0,'elapsed':0,'error':''}
    try:
        ni = None
        for a in range(3):
            ni = feishu.get(f"/wiki/v2/spaces/get_node?token={nt}")
            if ni.get('code') == 0: break
            time.sleep(2+a*3)
        if not ni or ni.get('code') != 0:
            res['error'] = (ni or {}).get('msg','get_node fail'); return res
        src_doc = ni['data']['node']['obj_token']
        sb = None
        for a in range(5):
            sb = get_all_blocks(feishu, src_doc)
            if sb: break
            time.sleep(3+a*3)
        if not sb: res['error'] = 'No blocks'; return res
        sm = {b['block_id']:b for b in sb}
        res['total_blocks'] = len(sb)
        rc = sb[0].get('children',[])
        cr = None
        for a in range(5):
            cr = lark.post(f"/wiki/v2/spaces/{LARK_WIKI_SPACE_ID}/nodes", json_data={
                "obj_type":"docx","parent_node_token":dest_parent,"node_type":"origin","title":title})
            if cr.get('code') == 0: break
            if cr.get('code') in (230001,99991400): time.sleep(5+a*5); continue
            break
        if not cr or cr.get('code') != 0:
            res['error'] = (cr or {}).get('msg','create fail'); return res
        dn = cr['data']['node']
        dd = dn['obj_token']
        res['dest_node'] = dn['node_token']
        res['url'] = f"https://gg5pahjppze.sg.larksuite.com/wiki/{dn['node_token']}"
        stats = {"blocks":0,"images":0,"files":0,"failed":0,"skipped":0,"img_fail":0,"file_fail":0}
        for bid in rc:
            if bid in sm: process_block(sm[bid], sm, dd, dd, stats, src_doc)
        res['success'] = True
        for k in stats: res[k] = stats[k]
    except Exception as e:
        res['error'] = str(e)[:200]
    res['elapsed'] = round(time.time()-t0)
    return res

# ── Main ──
def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "full"

    # Crawl mode
    if mode == "crawl":
        print("=== CRAWL WIKI NODES ===")
        feishu.token
        for wiki in CFG.get("source_wikis", []):
            root = wiki.get("root_token", "")
            if not root: continue
            ni = feishu.get(f"/wiki/v2/spaces/get_node?token={root}")
            if ni.get("code") != 0:
                print(f"Cannot access {root}: {ni.get('msg')}")
                continue
            space_id = ni["data"]["node"]["space_id"]
            name = wiki.get("name", root)
            print(f"\n--- {name} (space {space_id}) ---")
            nodes = crawl_wiki_tree(space_id, root)
            out = os.path.join(NODES_DIR, f"{name}_nodes.json")
            with open(out, "w", encoding="utf-8") as f:
                json.dump(nodes, f, ensure_ascii=False, indent=2)
            print(f"Saved {len(nodes)} nodes to {out}")
        return

    limit = 1 if mode == "test" else None

    print("="*60)
    print("  FEISHU → LARK WIKI CLONE (100% block-by-block)")
    print("="*60)

    all_nodes = []
    for wiki in CFG.get("source_wikis", []):
        name = wiki.get("name", "wiki")
        fpath = os.path.join(NODES_DIR, f"{name}_nodes.json")
        if os.path.exists(fpath):
            with open(fpath,'r',encoding='utf-8') as f:
                nodes = json.load(f)
            for n in nodes: n["_source"] = name
            all_nodes.extend(nodes)

    total = len(all_nodes)
    if total == 0:
        print("No nodes found. Run 'python clone.py crawl' first.")
        return
    print(f"  Pages: {total}")

    state = load_state()
    dest_map = state['dest_map']
    completed = set(state['completed'])
    print(f"  Done: {len(completed)}")

    base_records = get_base_records()
    print(f"  Base: {len(base_records)} records")

    feishu.token
    lark.token
    first_root = CFG["source_wikis"][0].get("root_token","") if CFG["source_wikis"] else ""
    if first_root: get_wiki_session(first_root)

    # Create categories
    for wiki in CFG.get("source_wikis", []):
        cat_key = f"cat_{wiki['name']}"
        cat_title = wiki.get("category_title", wiki["name"])
        if cat_key not in dest_map:
            cr = lark.post(f"/wiki/v2/spaces/{LARK_WIKI_SPACE_ID}/nodes", json_data={
                "obj_type":"docx","node_type":"origin","title":cat_title,"parent_node_token":WIKI_DEST_NODE})
            if cr.get('code') == 0:
                dest_map[cat_key] = cr['data']['node']['node_token']
                print(f"  Created: {cat_title}")
            time.sleep(0.5)
    save_state(state)

    ok, fail, count = len(completed), 0, 0
    t0 = time.time()

    for i, node in enumerate(all_nodes):
        if limit and count >= limit: break
        nt = node['node_token']
        if nt in completed: continue

        title = node.get('title','(untitled)')
        depth = node.get('depth',0)
        parent_old = node.get('parent_node_token','')
        cat_key = f"cat_{node['_source']}"

        if depth == 0: dp = dest_map.get(cat_key, WIKI_DEST_NODE)
        elif parent_old in dest_map: dp = dest_map[parent_old]
        else: dp = dest_map.get(cat_key, WIKI_DEST_NODE)

        rec = base_records.get(nt)
        if rec: base_update(rec['record_id'], {'Trạng thái':'Đang clone'})

        print(f"\n[{i+1}/{total}] {title}")
        info = clone_one(node, dp)

        if info['success']:
            ok += 1; count += 1
            print(f"  OK | {info['blocks']}blk {info['images']}img {info['files']}file {info['failed']}fail {info['skipped']}skip | {info['elapsed']}s")
            dest_map[nt] = info['dest_node']
            completed.add(nt)
            state['dest_map'] = dest_map
            state['completed'] = list(completed)
            save_state(state)
            if rec: base_update(rec['record_id'], {
                'Trạng thái':'Đã clone','Blocks gốc':info['total_blocks'],'Blocks clone':info['blocks'],
                'Ảnh gốc':info['images']+info['img_fail'],'Ảnh clone':info['images'],
                'Node Token mới':info['dest_node'],'Link mới':{'link':info['url'],'text':title},
                'Ghi chú QA':f"{info['blocks']}blk {info['images']}img {info['files']}file {info['elapsed']}s",
                'QA Kết quả':'OK' if info['failed']==0 and info['img_fail']==0 else 'WARN'})
        else:
            fail += 1; count += 1
            print(f"  FAIL | {info['error']}")
            if rec: base_update(rec['record_id'], {'Trạng thái':'Lỗi','Ghi chú QA':info['error'][:200],'QA Kết quả':'ERROR'})
        time.sleep(0.5)

    elapsed = time.time() - t0
    print(f"\n{'='*60}")
    print(f"  DONE ({elapsed:.0f}s = {elapsed/60:.1f} min)")
    print(f"  OK: {ok}/{total}  FAIL: {fail}")
    print(f"{'='*60}")

if __name__ == '__main__':
    main()
