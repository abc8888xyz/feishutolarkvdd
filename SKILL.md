---
name: feishu-to-lark-clone
description: >
  Clone Feishu wiki pages to Lark wiki with 100% block-by-block fidelity, including images, videos,
  files, tables, grids, callouts, iframes. Supports batch-cloning entire wiki trees with Base tracking
  and translating CN→VI using Claude Code CLI. Use this skill whenever the user wants to:
  copy/clone/migrate a Feishu wiki to Lark, transfer content between Feishu and Lark,
  crawl a wiki tree, translate wiki content from Chinese to Vietnamese,
  or mentions "clone wiki", "copy feishu", "cào wiki", "clone feishu sang lark", "sao chép wiki",
  "dịch wiki", "dịch sang tiếng việt", "crawl wiki", "clone feishu", "migrate wiki".
  Also trigger when user provides a feishu.cn/wiki/ URL and wants it cloned to Lark.
---

# Feishu → Lark Wiki Clone + Translate

Pipeline: **Crawl → Clone → Translate → Replace backlinks**

## Quick Start

### 1. Setup config

Copy `config.example.json` → `config.json` and fill in credentials:

```json
{
  "app_id": "<FEISHU_APP_ID>",
  "app_secret": "<FEISHU_APP_SECRET>",
  "feishu_base": "https://open.feishu.cn/open-apis",
  "lark_base": "https://open.larksuite.com/open-apis",
  "lark_wiki_space_id": "<TARGET_WIKI_SPACE_ID>",
  "wiki_dest_node": "<TARGET_ROOT_NODE_TOKEN>",
  "lark_wiki_url_prefix": "https://your-tenant.sg.larksuite.com/wiki",
  "source_wikis": [
    {
      "name": "my_wiki",
      "root_token": "<SOURCE_ROOT_NODE_TOKEN>",
      "category_title": "My Wiki Category"
    }
  ]
}
```

**Get tokens from URLs:**
- `https://waytoagi.feishu.cn/wiki/ABC123` → root_token = `ABC123`
- Lark wiki space ID: from wiki settings or API

**Required Feishu/Lark app permissions:**
- `wiki:wiki` (read + write wiki)
- `docx:document` (read + create documents)
- `drive:drive:readonly` (download media)
- `drive:file` (upload media)

### 2. Crawl wiki nodes

```bash
python -u -X utf8 clone.py crawl
```

Output: `nodes/<name>_nodes.json` with full wiki tree structure.

### 3. Clone all pages

```bash
# Test with 1 article first
python -u -X utf8 clone.py test

# Full clone (resume-safe)
python -u -X utf8 clone.py full
```

Each page is cloned block-by-block with QA verification. State saved in `clone_state.json`.

For long runs (>10 min timeout), use the auto-restart wrapper:
```bash
bash run_clone.sh
```

### 4. Translate CN → VI (optional)

```bash
# Test 1 page
python -u -X utf8 translate_gemini.py --stt 1

# Translate all
python -u -X utf8 translate_gemini.py

# Resume from page N
python -u -X utf8 translate_gemini.py --start N
```

Uses Claude Code CLI with marker strategy to preserve formatting.

---

## Block Types Supported

| Type | Name | Method |
|------|------|--------|
| 2-11 | text, headings | create with elements + style |
| 12-15,17 | bullet, ordered, code, quote, todo | create with elements + style |
| 19 | callout | create → del default child → clone children |
| 22 | divider | create empty |
| 23 | file/video | create → download → upload → PATCH replace_file |
| 24 | grid | create → get column IDs → PATCH first child → clone rest |
| 26 | iframe | create with component URL |
| 27 | image | create empty → download → upload → PATCH replace_image |
| 31 | table | create → insert rows → populate cells recursively |
| 33 | view (file wrapper) | handle child file block |
| 34 | quote container | create → clone children |
| 49 | synced block | flatten children to parent |

## Key Technical Details

### Image clone (3-step):
1. `POST create_block` with `{"block_type": 27, "image": {}}` (empty)
2. Download from Feishu → Upload to Lark with `parent_node = image_block_id`
3. `PATCH replace_image` with uploaded file_token

### Chunked upload (files > 20MB):
1. `POST /drive/v1/medias/upload_prepare` → get upload_id
2. `POST /drive/v1/medias/upload_part` × N (4MB chunks with adler32 checksum)
3. `POST /drive/v1/medias/upload_finish`

### Grid column handling:
- Lark auto-creates 1 empty text child per column (cannot delete)
- Strategy: PATCH first default child with source content, then create remaining

### Translation marker strategy:
- Join text runs with `[[[1]]] [[[2]]]` markers
- Translate entire string preserving markers
- Split result by markers back into individual runs with original styles

## Limitations

- **Cover images**: Feishu/Lark API does not support setting document cover
- **Chat cards (type 20)**: cross-tenant, cannot clone
- **Sheets (type 30)**: embedded spreadsheets, cannot clone
- **Boards (type 43)**: whiteboard blocks, cannot clone
- **.pptx/.pdf**: not docx format, no blocks to clone

## IMPORTANT

- Always use `-u -X utf8` flags on Windows
- Run clone commands in background with timeout 600000ms for large wikis
- State files (`clone_state.json`, `translate_state.json`) enable resume — do not delete
- If a page fails with "No blocks", it's likely a non-docx file (form, pptx, pdf) — auto-skipped on resume
