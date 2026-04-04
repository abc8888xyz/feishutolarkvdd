# feishutolarkvdd

Feishu wiki to Lark wiki: clone + translate CN to VI.

## Features

### Clone (clone.py)
- **100% block-by-block clone**: text, headings, lists, code, quotes, todos, callouts, grids, tables, quote containers, iframes, dividers
- **Images**: create empty block, download from Feishu, upload to Lark, PATCH replace_image
- **Videos/Files**: chunked upload for files >20MB (prepare/part/finish API)
- **Tables**: create with property, insert rows, populate cells recursively
- **Grids**: create columns, PATCH first default child, clone remaining
- **Callouts**: create, delete default child, clone source children
- **Synced blocks (type 49)**: flatten children to parent level
- **QA verify**: auto-verify blocks/images/files after each clone
- **Resume-safe**: state saved after each page

### Translate (translate_gemini.py)
- **Claude Code CLI**: uses Claude Sonnet for high-quality CN to VI translation
- **Marker strategy**: preserves text formatting with `[[[N]]]` markers
- **Code blocks**: translates Chinese comments only, preserves code syntax
- **Images/files**: copies from cloned Lark docs (no re-download from Feishu)
- **Resume-safe**: tracks translated pages in translate_state.json
- **Base tracker**: updates Bitable with translation status and links

## Block Types

| Type | Name | Clone Method |
|------|------|-------------|
| 2-11 | text, headings | create with elements + style |
| 12-15,17 | bullet, ordered, code, quote, todo | create with elements + style |
| 19 | callout | create, del default child, clone children |
| 22 | divider | create empty |
| 23 | file/video | create, download, upload, PATCH replace_file |
| 24 | grid | create, get column IDs, clone into columns |
| 26 | iframe | create with component URL |
| 27 | image | create empty, download, upload, PATCH replace_image |
| 31 | table | create, insert rows, populate cells |
| 33 | view (file wrapper) | handle child file block |
| 34 | quote container | create, clone children |
| 49 | synced block | flatten children to parent |

## Setup

1. Create a Feishu/Lark app at [open.feishu.cn](https://open.feishu.cn)
2. Grant permissions: `wiki:wiki`, `docx:document`, `drive:drive:readonly`, `drive:file`
3. Edit `config.json` with credentials
4. Install: `pip install -r requirements.txt`
5. For translation: install Claude Code CLI

## Usage

```bash
# Clone
python -u -X utf8 clone.py crawl          # crawl wiki nodes
python -u -X utf8 clone.py test           # test 1 article
python -u -X utf8 clone.py full           # full clone (resume-safe)

# Translate CN to VI
python -u -X utf8 translate_gemini.py              # translate all
python -u -X utf8 translate_gemini.py --stt N      # translate 1 page
python -u -X utf8 translate_gemini.py --start N    # resume from page N

# Sync progress to Bitable
python -u -X utf8 sync_base.py
```

## Config

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

- **Cover images**: Feishu/Lark API does not support setting document cover
- **Chat cards (type 20)**: cross-tenant, cannot clone
- **Sheets (type 30)**: embedded spreadsheets, cannot clone via docx API
- **Boards (type 43)**: whiteboard, cannot clone via API
- **Add-ons (type 40)**: interactive widgets, skip
- **Synced references (type 50)**: cannot create via API
- **.pptx/.pdf files**: not docx format, no blocks to clone
