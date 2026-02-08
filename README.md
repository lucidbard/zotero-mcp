# Zotero MCP Server

A [Model Context Protocol](https://modelcontextprotocol.io/) server that gives Claude (and other MCP clients) read-only access to your local [Zotero](https://www.zotero.org/) library. Search papers, retrieve metadata, generate BibTeX, and browse collections — all from your AI assistant.

## What it does

| Tool | Description |
|------|-------------|
| `search_papers` | Fuzzy keyword search across titles, authors, abstracts, tags, DOIs, and citation keys |
| `get_paper_details` | Full metadata for a paper (authors, abstract, tags, collections, PDF path) |
| `get_bibtex` | Generate a BibTeX entry with proper escaping and type mapping |
| `list_collections` | List all Zotero collections |
| `get_collection_items` | Browse papers in a specific collection |
| `get_pdf_path` | Resolve the filesystem path to a paper's PDF attachment |
| `library_stats` | Summary statistics (item count, collections, Better BibTeX status) |

**Note:** This server is **read-only**. It cannot add or import papers into Zotero. Paper import is handled by Zotero itself (browser connector, manual import, drag-and-drop, etc.). Once papers are in your Zotero library, this server makes them accessible to your AI tools.

## Requirements

- Python 3.10+
- [Zotero](https://www.zotero.org/) installed locally with its SQLite database
- [Better BibTeX for Zotero](https://retorque.re/zotero-better-bibtex/) (optional, recommended for citation keys)

## Installation

```bash
git clone https://github.com/lucidbard/zotero-mcp.git
cd zotero-mcp
pip install -r requirements.txt
```

Or install the dependency directly:

```bash
pip install "mcp>=1.0"
```

The only external dependency is the MCP Python SDK. Everything else uses Python's standard library (`sqlite3`, `re`, `pathlib`, etc.).

## Setup

### Claude Desktop

Add to your `claude_desktop_config.json`:

```json
{
    "mcpServers": {
        "zotero": {
            "command": "python",
            "args": ["/absolute/path/to/zotero_mcp.py"]
        }
    }
}
```

### Claude Code

Add to your MCP settings (`.claude/settings.json` or project settings):

```json
{
    "mcpServers": {
        "zotero": {
            "command": "python",
            "args": ["/absolute/path/to/zotero_mcp.py"]
        }
    }
}
```

### Custom Zotero database path

The server auto-detects common Zotero database locations:

- `~/Zotero/zotero.sqlite` (Linux, macOS, Windows)
- `~/snap/zotero-snap/common/Zotero/zotero.sqlite` (Linux Snap)
- `~/Library/Application Support/Zotero/Profiles/*/zotero.sqlite` (macOS)

If your database is elsewhere, set the `ZOTERO_DB_PATH` environment variable:

```json
{
    "mcpServers": {
        "zotero": {
            "command": "python",
            "args": ["/absolute/path/to/zotero_mcp.py"],
            "env": {
                "ZOTERO_DB_PATH": "/path/to/your/Zotero/zotero.sqlite"
            }
        }
    }
}
```

## How it works

The server opens a **read-only** SQLite connection directly to Zotero's local database. No network requests, no API keys, no Zotero account required. It queries the same database that the Zotero desktop app uses.

### Search ranking

Search uses a multi-signal relevance ranking system:

- **Exact match** (field equals query term) scores highest
- **Word boundary match** (term appears as a whole word) scores next
- **Substring match** (term appears within a field) follows
- **Fuzzy match** (trigram similarity for typos/near-misses) catches the rest
- Fields are weighted: title and citation key (3x), DOI and authors (2x), tags (1.5x), abstract (1x)
- Multi-word queries get an AND bonus when all terms match

### Better BibTeX support

If you have [Better BibTeX](https://retorque.re/zotero-better-bibtex/) installed, the server automatically uses its citation keys. It checks two sources:

1. The `extra` field in Zotero items (for `Citation Key:` or `bibtex:` prefixes)
2. The `better-bibtex.sqlite` database (for BBT-managed keys)

### PDF resolution

The `get_pdf_path` tool resolves Zotero's internal `storage:filename` paths to absolute filesystem paths, so your AI assistant can reference or read PDF attachments directly.

## Example usage

Once configured, you can ask Claude things like:

- "Search my Zotero library for papers about neural radiance fields"
- "Get the BibTeX entry for paper KEY123"
- "What collections do I have in Zotero?"
- "Show me all papers in my 'Literature Review' collection"
- "How many papers are in my library?"

## Limitations

- **Read-only**: Cannot create, modify, or delete Zotero items. Use Zotero's browser connector or desktop app to add papers.
- **Local only**: Accesses the local SQLite database directly. Does not sync with Zotero's cloud service.
- **Single user**: Designed for personal use with a single Zotero installation.
- **No PDF content**: Returns PDF file paths but does not extract or search PDF text content. (For PDF text extraction, see the `pdf_processor.py` module in the [Frontier](https://github.com/lucidbard/frontier) project.)

## License

MIT
