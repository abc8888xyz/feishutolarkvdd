#!/usr/bin/env python3
"""
Fix backlinks: replace old Feishu wiki links with new Lark VI wiki links.
Scans all translated pages, finds feishu.cn/wiki/ links, replaces with
corresponding VI page links via PATCH update_text_elements.

Usage: python -u -X utf8 fix_backlinks.py
"""
import sys, os, json, re, time, warnings
warnings.filterwarnings("ignore")
from urllib.parse import unquote, quote

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8')

DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, DIR)
from clone import lark, get_all_blocks

CFG = json.load(open(os.path.join(DIR, 'config.json'), 'r', encoding='utf-8'))
WIKI_URL = CFG.get("lark_wiki_url_prefix", "https://gg5pahjppze.sg.larksuite.com/wiki")

FEISHU_LINK_RE = re.compile(r'https?://[a-z]+\.feishu\.cn/wiki/([A-Za-z0-9]+)')


def build_link_map():
    """Build mapping: old feishu node_token -> new VI lark URL."""
    ts = json.load(open(os.path.join(DIR, 'translate_state.json')))
    trans_map = ts.get('trans_map', {})
    cs = json.load(open(os.path.join(DIR, 'clone_state.json')))
    dest_map = cs.get('dest_map', {})

    link_map = {}  # old_node_token -> new_url

    # Also map wiki root tokens
    for wiki in CFG.get("source_wikis", []):
        root = wiki.get("root_token", "")
        if root:
            # Map root to wiki dest
            link_map[root] = f"{WIKI_URL}/{CFG['wiki_dest_node']}"

    # Map each article: old token -> VI link (preferred) or clone link (fallback)
    all_nodes = []
    for fname in ["nodes/wiki1_nodes.json", "nodes/wiki2_nodes.json"]:
        fpath = os.path.join(DIR, fname)
        if os.path.exists(fpath):
            with open(fpath, 'r', encoding='utf-8') as f:
                all_nodes.extend(json.load(f))

    for n in all_nodes:
        nt = n['node_token']
        if nt in trans_map:
            link_map[nt] = f"{WIKI_URL}/{trans_map[nt]}"
        elif nt in dest_map:
            link_map[nt] = f"{WIKI_URL}/{dest_map[nt]}"

    return link_map, trans_map, all_nodes


def replace_url(url, link_map):
    """Replace feishu wiki URL with new lark URL if mapped."""
    decoded = unquote(url)
    m = FEISHU_LINK_RE.search(decoded)
    if not m:
        return None
    old_token = m.group(1)
    if old_token in link_map:
        new_url = link_map[old_token]
        return quote(new_url, safe=':/')
    return None


def fix_page_backlinks(vi_doc, link_map):
    """Scan and fix all backlinks in a single translated page. Returns (fixed, total)."""
    blocks = get_all_blocks(lark, vi_doc)
    fixed = 0
    total_links = 0

    for b in blocks:
        bt = b.get('block_type', 0)
        block_id = b.get('block_id', '')

        # Find the content field
        field = None
        for key in b:
            if isinstance(b[key], dict) and 'elements' in b[key]:
                field = key
                break
        if not field:
            continue

        elements = b[field].get('elements', [])
        has_changes = False
        new_elements = []

        for e in elements:
            if 'text_run' not in e:
                new_elements.append(e)
                continue

            tr = e['text_run']
            style = tr.get('text_element_style', {})
            if 'link' not in style:
                new_elements.append(e)
                continue

            url = style['link'].get('url', '')
            total_links += 1
            new_url = replace_url(url, link_map)

            if new_url:
                # Build new element with replaced URL
                ne = {'text_run': {'content': tr.get('content', '')}}
                new_style = dict(style)
                new_style['link'] = {'url': new_url}
                ne['text_run']['text_element_style'] = new_style
                new_elements.append(ne)
                has_changes = True
            else:
                new_elements.append(e)

        if has_changes:
            # PATCH update the block
            patch_data = {"update_text_elements": {"elements": new_elements, "style": {}}}
            r = lark.patch(f"/docx/v1/documents/{vi_doc}/blocks/{block_id}", json_data=patch_data)
            if r.get('code') == 0:
                fixed += 1
            time.sleep(0.2)

    return fixed, total_links


def main():
    print("=" * 60)
    print("  FIX BACKLINKS — Replace Feishu → Lark VI links")
    print("=" * 60)

    link_map, trans_map, all_nodes = build_link_map()
    print(f"  Link map: {len(link_map)} mappings")
    print(f"  Translated pages: {len(trans_map)}")

    total_fixed = 0
    total_links = 0
    pages_with_fixes = 0

    for idx, n in enumerate(all_nodes):
        nt = n['node_token']
        if nt not in trans_map:
            continue

        vi_node = trans_map[nt]
        title = n.get('title', '')[:45]

        try:
            ni = lark.get(f"/wiki/v2/spaces/get_node?token={vi_node}")
            if ni.get('code') != 0:
                continue
            vi_doc = ni['data']['node']['obj_token']

            fixed, links = fix_page_backlinks(vi_doc, link_map)
            total_links += links
            total_fixed += fixed

            if fixed > 0:
                pages_with_fixes += 1
                print(f"  [{pages_with_fixes}] {title} | fixed {fixed} links")

        except Exception as e:
            pass

        if (idx + 1) % 50 == 0:
            print(f"  --- scanned {idx+1} pages, {total_fixed} links fixed ---")

    print(f"\n{'=' * 60}")
    print(f"  DONE")
    print(f"  Pages with fixes: {pages_with_fixes}")
    print(f"  Total links fixed: {total_fixed}")
    print(f"  Total links scanned: {total_links}")
    print(f"{'=' * 60}")


if __name__ == '__main__':
    main()
