#!/usr/bin/env python3
"""Sync clone progress to Base tracker. Run periodically or once after clone finishes."""
import sys, json, re, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from clone import lark, LARK_BASE, load_state
import requests as req_lib

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8')

DIR = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(DIR, 'config.json'), 'r', encoding='utf-8'))
APP_TOKEN = CFG["base_app_token"]
TABLE_ID = CFG["base_table_id"]

def get_base_records():
    recs = {}
    pt = ''
    while True:
        url = f'{LARK_BASE}/bitable/v1/apps/{APP_TOKEN}/tables/{TABLE_ID}/records?page_size=100'
        if pt: url += f'&page_token={pt}'
        r = req_lib.get(url, headers=lark.h(), timeout=15).json()
        d = r.get('data', {})
        for item in d.get('items', []):
            nt = item['fields'].get('Node gốc', '')
            if nt: recs[nt] = item
        if not d.get('has_more'): break
        pt = d.get('page_token', '')
    return recs

def parse_log():
    """Parse clone output log for QA results."""
    # Find the latest output file
    task_dir = r"C:\Users\vudan\AppData\Local\Temp\claude\D--vudanhdu3"
    qa = {}
    for root, dirs, files in os.walk(task_dir):
        for f in files:
            if f.endswith('.output'):
                fpath = os.path.join(root, f)
                try:
                    with open(fpath, 'r', encoding='utf-8') as fp:
                        lines = fp.readlines()
                    for i, line in enumerate(lines):
                        m = re.match(r'\[(\d+)/281\]\s+(.*)', line.strip())
                        if m:
                            idx = int(m.group(1))
                            sl = lines[i+1].strip() if i+1 < len(lines) else ""
                            ql = lines[i+2].strip() if i+2 < len(lines) else ""
                            info = {}
                            sm = re.search(r'(\d+)blk (\d+)img (\d+)file (\d+)fail (\d+)skip \| (\d+)s', sl)
                            if sm:
                                info.update({"blocks":int(sm.group(1)),"images":int(sm.group(2)),
                                    "files":int(sm.group(3)),"elapsed":int(sm.group(6))})
                            qm = re.search(r'QA \| (PASS|FAIL) blk:(\d+)/(\d+).*img:(\d+)/(\d+).*file:(\d+)/(\d+)', ql)
                            if qm:
                                info.update({"qa":qm.group(1),"dst_blk":int(qm.group(2)),"src_blk":int(qm.group(3)),
                                    "dst_img":int(qm.group(4)),"src_img":int(qm.group(5)),
                                    "dst_file":int(qm.group(6)),"src_file":int(qm.group(7))})
                            if "FAIL |" in sl:
                                fm = re.search(r'FAIL \| (.+)', sl)
                                info["error"] = fm.group(1) if fm else "Unknown"
                                info["qa"] = "N/A"
                            qa[idx] = info
                except: pass
    return qa

def sync():
    state = load_state()
    dm = state.get("dest_map", {})
    completed = set(state.get("completed", []))

    # Load nodes for index mapping
    all_nodes = []
    for fname, source in [("nodes/wiki1_nodes.json", "wiki1"), ("nodes/wiki2_nodes.json", "wiki2")]:
        fpath = os.path.join(DIR, fname)
        if os.path.exists(fpath):
            with open(fpath, 'r', encoding='utf-8') as f:
                all_nodes.extend(json.load(f))

    node_idx = {n["node_token"]: i+1 for i, n in enumerate(all_nodes)}

    qa_data = parse_log()
    base_recs = get_base_records()

    updated = 0
    for nt, rec in base_recs.items():
        rid = rec["record_id"]
        fields = rec["fields"]
        current_status = fields.get("Trạng thái", "")

        if nt in completed and current_status != "Đã clone" and current_status != "Lỗi":
            update = {"Trạng thái": "Đã clone"}
            dst_node = dm.get(nt, "")
            if dst_node:
                dst_link = f"https://gg5pahjppze.sg.larksuite.com/wiki/{dst_node}"
                update["Node clone"] = dst_node
                update["Link clone"] = {"link": dst_link, "text": dst_link}

            idx = node_idx.get(nt, 0)
            if idx in qa_data:
                qd = qa_data[idx]
                if "qa" in qd: update["QA"] = qd["qa"]
                if "src_blk" in qd: update["Blocks gốc"] = qd["src_blk"]; update["Blocks clone"] = qd["dst_blk"]
                if "src_img" in qd: update["Ảnh gốc"] = qd["src_img"]; update["Ảnh clone"] = qd["dst_img"]
                if "src_file" in qd: update["Files gốc"] = qd["src_file"]; update["Files clone"] = qd["dst_file"]
                if "elapsed" in qd: update["Thời gian (s)"] = qd["elapsed"]
                if "error" in qd: update["Ghi chú"] = qd["error"]; update["Trạng thái"] = "Lỗi"

            r = req_lib.put(f'{LARK_BASE}/bitable/v1/apps/{APP_TOKEN}/tables/{TABLE_ID}/records/{rid}',
                headers=lark.h(), json={'fields': update}, timeout=15)
            if r.json().get('code') == 0:
                updated += 1
            time.sleep(0.1)

    print(f"Synced: {len(completed)}/281 completed, {updated} records updated in Base")

if __name__ == '__main__':
    sync()
