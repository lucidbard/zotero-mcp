# Zotero MCP Server

A [Model Context Protocol](https://modelcontextprotocol.io/) server that gives Claude (and other MCP clients) read-only access to your local [Zotero](https://www.zotero.org/) library. Search papers, retrieve metadata, generate BibTeX, and browse collections — all from your AI assistant.

With the optional **Zotero MCP Bridge** plugin installed in Zotero 7, the server also gains write capabilities: creating collections and organizing items.

## What it does

### Read-only tools (always available)

| Tool | Description |
|------|-------------|
| `search_papers` | Fuzzy keyword search across titles, authors, abstracts, tags, DOIs, and citation keys |
| `get_paper_details` | Full metadata for a paper (authors, abstract, tags, collections, PDF path) |
| `get_bibtex` | Generate a BibTeX entry with proper escaping and type mapping |
| `list_collections` | List all Zotero collections |
| `get_collection_items` | Browse papers in a specific collection |
| `get_pdf_path` | Resolve the filesystem path to a paper's PDF attachment |
| `library_stats` | Summary statistics (item count, collections, plugin status) |

### Write tools (require Zotero MCP Bridge plugin)

These tools only appear when Zotero is running with the MCP Bridge plugin installed:

| Tool | Description |
|------|-------------|
| `create_collection` | Create a new collection (optionally nested under a parent) |
| `add_to_collection` | Add papers to a collection by their item keys |
| `remove_from_collection` | Remove papers from a collection (does not delete them) |

**Note:** Neither the MCP server nor the plugin can create new papers in Zotero. Paper import is handled by Zotero itself (browser connector, manual import, drag-and-drop, etc.). Once papers are in your Zotero library, this server makes them accessible to your AI tools.

## Requirements

- Python 3.10+
- [Zotero](https://www.zotero.org/) installed locally with its SQLite database
- [Better BibTeX for Zotero](https://retorque.re/zotero-better-bibtex/) (optional, recommended for citation keys)
- Zotero MCP Bridge plugin (optional, for write operations)

## Installation

```bash
git clone https://github.com/lucidbard/zotero-mcp.git
cd zotero-mcp
pip install -r requirements.txt
```

Or install the dependencies directly:

```bash
pip install "mcp>=1.0" "httpx>=0.27"
```

## Zotero MCP Bridge plugin

The Zotero MCP Bridge plugin adds HTTP endpoints to Zotero's built-in server (port 23119) that the MCP server calls for write operations. Without the plugin, the MCP server works in read-only mode.

### Building the plugin

```bash
cd zotero-plugin
npm install
npm run build
```

This creates `dist/zotero-mcp-bridge-latest.xpi`.

### Installing the plugin

1. Open Zotero 7
2. Go to **Tools > Add-ons**
3. Click the gear icon > **Install Add-on From File**
4. Select `zotero-plugin/dist/zotero-mcp-bridge-latest.xpi`
5. Restart Zotero when prompted

### Verifying the plugin

With Zotero running and the plugin installed:

```bash
# Health check
curl http://127.0.0.1:23119/zotero-mcp/health

# Or run the test suite
cd zotero-plugin && npm test
```

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

### Read path (SQLite)

The server opens a **read-only** SQLite connection directly to Zotero's local database. No network requests, no API keys, no Zotero account required.

### Write path (plugin HTTP)

When write tools are called, the MCP server sends HTTP requests to the Zotero MCP Bridge plugin at `http://127.0.0.1:23119/zotero-mcp/rpc`. The plugin uses Zotero's internal JavaScript API to perform the operations. This approach is necessary because Zotero's SQLite database is locked by the running Zotero process.

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
- "Create a new collection called 'Deep Learning Survey'" *(requires plugin)*
- "Add papers KEY1 and KEY2 to my 'Related Work' collection" *(requires plugin)*

## Limitations

- **Cannot create papers**: Neither the MCP server nor the plugin can import new papers into Zotero. Use Zotero's browser connector or desktop app to add papers.
- **Local only**: Accesses the local SQLite database directly. Does not sync with Zotero's cloud service.
- **Single user**: Designed for personal use with a single Zotero installation.
- **No PDF content**: Returns PDF file paths but does not extract or search PDF text content.
- **Write tools need Zotero running**: Collection management requires Zotero to be open with the MCP Bridge plugin installed.

## License

MIT
