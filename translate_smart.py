#!/usr/bin/env python3
"""
Smart translator: sort articles small→large, skip too-big articles for later.
Usage: python -u -X utf8 translate_smart.py [--max-blocks N]
"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8')

DIR = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(DIR, 'config.json'), 'r', encoding='utf-8'))

from clone import lark, feishu, get_all_blocks, LarkAuth

MAX_BLOCKS = 300  # default: skip articles > 300 blocks
if '--max-blocks' in sys.argv:
    idx = sys.argv.index('--max-blocks')
    MAX_BLOCKS = int(sys.argv[idx + 1])

def main():
    # Load state
    clone_state = json.load(open(os.path.join(DIR, 'clone_state.json')))
    dest_map = clone_state.get('dest_map', {})

    trans_file = os.path.join(DIR, 'translate_state.json')
    if os.path.exists(trans_file):
        trans_state = json.load(open(trans_file))
    else:
        trans_state = {'translated': [], 'trans_map': {}}
    translated = set(trans_state.get('translated', []))

    # Load all nodes
    all_nodes = []
    for wiki in CFG.get("source_wikis", []):
        fpath = os.path.join(DIR, 'nodes', f"{wiki['name']}_nodes.json")
        if os.path.exists(fpath):
            with open(fpath, 'r', encoding='utf-8') as f:
                nodes = json.load(f)
            for n in nodes:
                n["_source"] = wiki["name"]
            all_nodes.extend(nodes)

    # Gather remaining articles with block counts
    print("Scanning article sizes...")
    remaining = []
    for i, n in enumerate(all_nodes):
        nt = n['node_token']
        if nt not in dest_map or nt in translated:
            continue
        dest_node = dest_map[nt]
        try:
            ni = lark.get(f"/wiki/v2/spaces/get_node?token={dest_node}")
            if ni.get('code') != 0:
                continue
            doc_id = ni['data']['node']['obj_token']
            blocks = get_all_blocks(lark, doc_id)
            remaining.append((i, n, dest_node, doc_id, len(blocks)))
        except:
            remaining.append((i, n, dest_node, '', 0))
        time.sleep(0.2)

    # Sort small → large
    remaining.sort(key=lambda x: x[4])

    small = [r for r in remaining if r[4] <= MAX_BLOCKS]
    big = [r for r in remaining if r[4] > MAX_BLOCKS]

    print(f"\nTotal remaining: {len(remaining)}")
    print(f"Small (≤{MAX_BLOCKS} blocks): {len(small)} — will translate now")
    print(f"Big (>{MAX_BLOCKS} blocks): {len(big)} — deferred")
    if big:
        for idx, n, _, _, blk in big[:10]:
            print(f"  [{idx}] {blk} blk | {n['title'][:50]}")

    # Now translate small articles using translate_gemini.py's translate_one
    from translate_gemini import translate_one, load_trans_state, save_trans_state, translate_text

    trans_state = load_trans_state()
    translated = set(trans_state.get('translated', []))

    # Get/create VI parent folder
    LARK_WIKI_SPACE_ID = CFG["lark_wiki_space_id"]
    WIKI_DEST_NODE = CFG["wiki_dest_node"]
    translate_parent = trans_state.get('translate_parent', '')
    if not translate_parent:
        cr = lark.post(f"/wiki/v2/spaces/{LARK_WIKI_SPACE_ID}/nodes", json_data={
            "obj_type": "docx", "node_type": "origin",
            "title": "Ban dich Tieng Viet",
            "parent_node_token": WIKI_DEST_NODE})
        if cr.get('code') == 0:
            translate_parent = cr['data']['node']['node_token']
            trans_state['translate_parent'] = translate_parent
            save_trans_state(trans_state)
            print(f"Created VI folder: {translate_parent}")

    ok, fail = 0, 0
    t0 = time.time()

    for rank, (idx, node, dest_node, doc_id, blk_count) in enumerate(small):
        nt = node['node_token']
        if nt in translated:
            continue
        title = node.get('title', '(untitled)')
        print(f"\n[{rank+1}/{len(small)}] {blk_count} blk | {title}")

        try:
            info = translate_one(node, dest_node, trans_state, translate_parent)
        except Exception as e:
            info = {'success': False, 'error': str(e)[:200]}

        if info.get('success'):
            ok += 1
            print(f"  OK | {info.get('vi_title','')[:50]} | {info.get('blocks',0)}blk {info.get('elapsed',0)}s")
            translated.add(nt)
            trans_state['translated'] = list(translated)
            save_trans_state(trans_state)
        else:
            fail += 1
            print(f"  FAIL | {info.get('error','')}")
            translated.add(nt)  # skip on resume
            trans_state['translated'] = list(translated)
            save_trans_state(trans_state)

        time.sleep(0.5)

    elapsed = time.time() - t0
    print(f"\n{'='*60}")
    print(f"  DONE ({elapsed:.0f}s = {elapsed/60:.1f} min)")
    print(f"  OK: {ok} | FAIL: {fail} | Big deferred: {len(big)}")
    print(f"{'='*60}")

if __name__ == '__main__':
    main()
