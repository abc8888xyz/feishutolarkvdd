# feishutolarkvdd

Clone Feishu wiki pages to Lark wiki with 100% block-by-block fidelity.

## Features

- **100% block clone**: text, headings, lists, code, quotes, todos, callouts, grids, tables, quote containers, iframes, dividers
- **Images**: download from Feishu → create empty block → upload to Lark → PATCH replace_image
- **Videos/Files**: supports chunked upload for files >20MB (prepare/part/finish API)
- **Tables**: create with property → insert rows → populate cells recursively
- **Grids**: create columns → PATCH first child → clone remaining children
- **Callouts**: create → delete default child → clone source children
- **Synced blocks (type 49)**: flatten children to parent level
- **Resume-safe**: saves state after each page, can resume from where it left off
- **Base tracker**: optional Bitable integration for real-time progress tracking

## Block Types Supported

| Type | Name | Method |
|------|------|--------|
| 2-11 | text, headings | create with elements + style |
| 12-15,17 | bullet, ordered, code, quote, todo | create with elements + style |
| 19 | callout | create → del default child → clone children |
| 22 | divider | create empty |
| 23 | file/video | create → download → upload → PATCH replace_file |
| 24 | grid | create → get column IDs → clone into columns |
| 26 | iframe | create with component URL |
| 27 | image | create empty → download → upload → PATCH replace_image |
| 31 | table | create → insert rows → populate cells |
| 33 | view (file wrapper) | handle child file block |
| 34 | quote container | create → clone children |
| 49 | synced block | flatten children to parent |

## Setup

1. Create a Feishu/Lark app at [open.feishu.cn](https://open.feishu.cn) or [open.larksuite.com](https://open.larksuite.com)
2. Grant permissions: `wiki:wiki`, `docx:document`, `drive:drive:readonly`, `drive:file`
3. Edit `config.json` with your app credentials and wiki space IDs
4. Install dependencies: `pip install -r requirements.txt`

## Usage

```bash
# 1. Crawl source wiki nodes
python -u -X utf8 clone.py crawl

# 2. Test with 1 article
python -u -X utf8 clone.py test

# 3. Full clone
python -u -X utf8 clone.py full

# Resume (automatically skips completed pages)
python -u -X utf8 clone.py full
```

## Config

Edit `config.json`:

```json
{
  "app_id": "your_app_id",
  "app_secret": "your_app_secret",
  "feishu_base": "https://open.feishu.cn/open-apis",
  "lark_base": "https://open.larksuite.com/open-apis",
  "lark_wiki_space_id": "target_wiki_space_id",
  "wiki_dest_node": "target_root_node_token",
  "base_app_token": "",
  "base_table_id": "",
  "source_wikis": [
    {
      "name": "wiki1",
      "root_token": "source_wiki_root_node_token",
      "category_title": "Category Name"
    }
  ]
}
```

## Limitations

- **Cover images**: Feishu/Lark API does not support setting document cover via API
- **Chat cards (type 20)**: cross-tenant, cannot clone
- **Sheets (type 30)**: embedded spreadsheets, cannot clone via docx API
- **Boards (type 43)**: whiteboard blocks, cannot clone via API
- **Add-ons (type 40)**: interactive widgets (reactions etc), skip
- **Synced references (type 50)**: cannot create via API
