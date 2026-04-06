#!/usr/bin/env python3
"""
QA translated pages: check blocks, images, compare with clone source.
Updates Base with QA results. Re-translates failed pages.

Usage:
  python -u -X utf8 qa_translate.py              # QA all translated pages
  python -u -X utf8 qa_translate.py --fix         # QA + re-translate failed ones
"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8')

DIR = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(DIR, 'config.json'), 'r', encoding='utf-8'))

from clone import lark, get_all_blocks
import requests as req_lib

LARK_BASE = CFG["lark_base"]
BASE_APP = "LaUZbNulxaLytEswEpTlLpS5g4c"
BASE_TABLE = "tblvHleRHAQbPLEI"
WIKI_URL = CFG.get("lark_wiki_url_prefix", "https://gg5pahjppze.sg.larksuite.com/wiki")

FIX_MODE = "--fix" in sys.argv


def get_base_records():
    recs = {}
    pt = ''
    while True:
        url = f'{LARK_BASE}/bitable/v1/apps/{BASE_APP}/tables/{BASE_TABLE}/records?page_size=100'
        if pt: url += f'&page_token={pt}'
        r = req_lib.get(url, headers=lark.h(), timeout=15).json()
        d = r.get('data', {})
        for item in d.get('items', []):
            stt = item['fields'].get('STT')
            if stt is not None:
                recs[int(stt)] = item
        if not d.get('has_more'): break
        pt = d.get('page_token', '')
    return recs


def base_update(rec_id, fields):
    for a in range(3):
        try:
            r = req_lib.put(
                f'{LARK_BASE}/bitable/v1/apps/{BASE_APP}/tables/{BASE_TABLE}/records/{rec_id}',
                headers=lark.h(), json={'fields': fields}, timeout=15)
            if r.json().get('code') == 0: return True
        except: pass
        time.sleep(0.5)


def qa_one_page(clone_node, vi_node):
    """Compare clone vs translated page. Returns (status, detail)."""
    try:
        # Get clone doc blocks
        ni = lark.get(f"/wiki/v2/spaces/get_node?token={clone_node}")
        if ni.get('code') != 0:
            return "FAIL", "clone node not found"
        clone_doc = ni['data']['node']['obj_token']
        clone_blocks = get_all_blocks(lark, clone_doc)

        # Get VI doc blocks
        ni2 = lark.get(f"/wiki/v2/spaces/get_node?token={vi_node}")
        if ni2.get('code') != 0:
            return "FAIL", "VI node not found"
        vi_doc = ni2['data']['node']['obj_token']
        vi_blocks = get_all_blocks(lark, vi_doc)

        if not vi_blocks or len(vi_blocks) <= 1:
            return "FAIL", "VI page empty"

        # Count images
        clone_imgs = sum(1 for b in clone_blocks if b["block_type"] == 27 and b.get("image", {}).get("token", ""))
        vi_imgs = sum(1 for b in vi_blocks if b["block_type"] == 27 and b.get("image", {}).get("token", ""))

        # Count text blocks
        clone_text = sum(1 for b in clone_blocks if b["block_type"] in (2, 3, 4, 5, 6, 7, 12, 13, 14, 15, 17))
        vi_text = sum(1 for b in vi_blocks if b["block_type"] in (2, 3, 4, 5, 6, 7, 12, 13, 14, 15, 17))

        # Block coverage
        pct = round(len(vi_blocks) / len(clone_blocks) * 100) if clone_blocks else 100

        detail = f"blk:{len(vi_blocks)}/{len(clone_blocks)}({pct}%) img:{vi_imgs}/{clone_imgs} text:{vi_text}/{clone_text}"

        # Pass criteria: >=50% blocks, >=80% images
        if pct >= 50 and (vi_imgs >= clone_imgs * 0.8 or clone_imgs == 0):
            return "PASS", detail
        else:
            return "FAIL", detail

    except Exception as e:
        return "FAIL", str(e)[:100]


def main():
    print("=" * 60)
    print("  QA TRANSLATED PAGES")
    print("=" * 60)

    # Load states
    clone_state = json.load(open(os.path.join(DIR, 'clone_state.json')))
    dest_map = clone_state.get('dest_map', {})

    trans_state = json.load(open(os.path.join(DIR, 'translate_state.json')))
    trans_map = trans_state.get('trans_map', {})
    translated = set(trans_state.get('translated', []))

    # Load nodes
    all_nodes = []
    for wiki in CFG.get("source_wikis", []):
        fpath = os.path.join(DIR, 'nodes', f"{wiki['name']}_nodes.json")
        if os.path.exists(fpath):
            with open(fpath, 'r', encoding='utf-8') as f:
                all_nodes.extend(json.load(f))

    # Load Base records
    base_recs = get_base_records()
    print(f"  Base records: {len(base_recs)}")

    # QA each translated page
    to_qa = []
    for i, n in enumerate(all_nodes):
        nt = n['node_token']
        if nt not in translated or nt not in trans_map:
            continue
        clone_node = dest_map.get(nt, '')
        vi_node = trans_map[nt]
        if not clone_node:
            continue
        to_qa.append((i + 1, n, clone_node, vi_node))

    print(f"  Pages to QA: {len(to_qa)}")

    pass_count, fail_count = 0, 0
    failed_pages = []

    for rank, (stt, node, clone_node, vi_node) in enumerate(to_qa):
        title = node.get('title', '')[:50]
        status, detail = qa_one_page(clone_node, vi_node)

        if status == "PASS":
            pass_count += 1
            print(f"  [{rank+1}/{len(to_qa)}] PASS | {title} | {detail}")
        else:
            fail_count += 1
            print(f"  [{rank+1}/{len(to_qa)}] FAIL | {title} | {detail}")
            failed_pages.append((stt, node, clone_node, vi_node, detail))

        # Update Base
        rec = base_recs.get(stt)
        if rec:
            base_update(rec['record_id'], {
                'QA Dich': status,
                'Ghi chu': detail
            })

        time.sleep(0.3)

    print(f"\n{'=' * 60}")
    print(f"  QA RESULTS")
    print(f"  PASS: {pass_count}")
    print(f"  FAIL: {fail_count}")
    print(f"{'=' * 60}")

    if failed_pages and FIX_MODE:
        print(f"\n  Re-translating {len(failed_pages)} failed pages...")
        from translate_gemini import translate_one, load_trans_state, save_trans_state

        ts = load_trans_state()
        translate_parent = ts.get('translate_parent', '')

        for stt, node, clone_node, vi_node, detail in failed_pages:
            title = node.get('title', '')[:50]
            print(f"\n  Re-translate: {title}")

            # Remove old VI node from translated set
            nt = node['node_token']
            trans_set = set(ts.get('translated', []))
            trans_set.discard(nt)
            ts['translated'] = list(trans_set)
            if nt in ts.get('trans_map', {}):
                del ts['trans_map'][nt]
            save_trans_state(ts)

            # Re-translate
            try:
                info = translate_one(node, clone_node, ts, translate_parent)
                if info.get('success'):
                    print(f"    OK | {info.get('blocks', 0)}blk {info.get('images', 0)}img")
                    trans_set.add(nt)
                    ts['translated'] = list(trans_set)
                    ts['trans_map'][nt] = info['new_node']
                    save_trans_state(ts)

                    rec = base_recs.get(stt)
                    if rec:
                        vi_link = f"{WIKI_URL}/{info['new_node']}"
                        base_update(rec['record_id'], {
                            'QA Dich': 'Chua QA',
                            'Dich': 'Da dich',
                            'Link dich': {'link': vi_link, 'text': vi_link},
                            'Tieu de (VI)': info.get('vi_title', ''),
                            'Ghi chu': f"Re-translated: {info.get('blocks',0)}blk"
                        })
                else:
                    print(f"    FAIL | {info.get('error', '')}")
            except Exception as e:
                print(f"    ERROR | {e}")


if __name__ == '__main__':
    main()
