"""
Standalone Zotero MCP Server

A Model Context Protocol server that provides direct read-only access to a
local Zotero SQLite database. No external services required — just a Zotero
installation with its SQLite database.

Features:
- Search papers with fuzzy/keyword matching and relevance ranking
- Get full paper details (metadata, creators, tags, collections)
- Generate BibTeX entries
- List and browse collections
- Resolve PDF attachment paths
- Better BibTeX citation key support (via extra field or BBT database)

Usage:
    # With default Zotero path auto-detection
    python zotero_mcp.py

    # With explicit database path
    ZOTERO_DB_PATH=/path/to/zotero.sqlite python zotero_mcp.py

Configure in Claude Desktop / claude_desktop_config.json:
    {
        "mcpServers": {
            "zotero": {
                "command": "python",
                "args": ["/path/to/zotero_mcp.py"],
                "env": {
                    "ZOTERO_DB_PATH": "/path/to/Zotero/zotero.sqlite"
                }
            }
        }
    }
"""

import asyncio
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Optional, Dict, List

import httpx

from mcp.server import Server
from mcp.types import (
    Tool,
    TextContent,
)


# ---------------------------------------------------------------------------
# Zotero MCP Bridge plugin integration
# ---------------------------------------------------------------------------

PLUGIN_BASE_URL = "http://127.0.0.1:23119"
PLUGIN_RPC_URL = f"{PLUGIN_BASE_URL}/zotero-mcp/rpc"
PLUGIN_HEALTH_URL = f"{PLUGIN_BASE_URL}/zotero-mcp/health"


async def _plugin_available() -> bool:
    """Check if the Zotero MCP Bridge plugin is running."""
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(PLUGIN_HEALTH_URL, timeout=2.0)
            return r.status_code == 200
    except Exception:
        return False


async def _plugin_rpc(method: str, params: dict = None) -> dict:
    """Call the Zotero MCP Bridge plugin's RPC endpoint."""
    async with httpx.AsyncClient() as client:
        r = await client.post(
            PLUGIN_RPC_URL,
            json={"method": method, "params": params or {}},
            timeout=10.0,
        )
        r.raise_for_status()
        return r.json()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def find_zotero_db() -> Optional[Path]:
    """Auto-detect the Zotero SQLite database path.

    Checks common locations across platforms:
    - Linux: ~/Zotero/zotero.sqlite
    - macOS: ~/Zotero/zotero.sqlite
    - Windows: ~/Zotero/zotero.sqlite (also AppData)
    - Environment variable override: ZOTERO_DB_PATH
    """
    # Environment variable takes priority
    env_path = os.environ.get("ZOTERO_DB_PATH")
    if env_path:
        p = Path(env_path)
        if p.exists():
            return p
        print(f"Warning: ZOTERO_DB_PATH={env_path} does not exist", file=sys.stderr)

    # Common paths
    home = Path.home()
    candidates = [
        home / "Zotero" / "zotero.sqlite",
        home / "snap" / "zotero-snap" / "common" / "Zotero" / "zotero.sqlite",
        home / ".zotero" / "zotero" / "zotero.sqlite",
    ]

    # macOS profile paths
    profiles_dir = home / "Library" / "Application Support" / "Zotero" / "Profiles"
    if profiles_dir.exists():
        for profile in profiles_dir.iterdir():
            if profile.is_dir():
                candidate = profile / "zotero.sqlite"
                if candidate.exists():
                    candidates.insert(0, candidate)

    for candidate in candidates:
        if candidate.exists():
            return candidate

    return None


# ---------------------------------------------------------------------------
# Database access layer
# ---------------------------------------------------------------------------

# Compiled regex for extracting years from date strings
YEAR_REGEX = re.compile(r"\b(19|20)\d{2}\b")

DEFAULT_SEARCH_LIMIT = 20


class ZoteroDb:
    """Read-only access to a local Zotero SQLite database.

    Mirrors the query patterns from Frontier's src/zotero.rs, adapted for
    Python's sqlite3 module.
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.data_dir = db_path.parent
        self.conn = self._open_connection()

    def _open_connection(self) -> sqlite3.Connection:
        """Open a read-only SQLite connection with performance pragmas."""
        # uri=True enables the ?mode=ro parameter for true read-only
        uri = f"file:{self.db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.execute("PRAGMA mmap_size = 268435456")
        return conn

    def refresh(self) -> None:
        """Re-open the connection to pick up external changes."""
        self.conn.close()
        self.conn = self._open_connection()

    def is_available(self) -> bool:
        """Check if the database is accessible."""
        try:
            self.conn.execute("SELECT 1 FROM items LIMIT 1")
            return True
        except Exception:
            return False

    def item_count(self) -> int:
        """Count items excluding attachments, notes, and deleted items."""
        row = self.conn.execute(
            """
            SELECT COUNT(*)
            FROM items i
            JOIN itemTypes it ON i.itemTypeID = it.itemTypeID
            WHERE it.typeName NOT IN ('attachment', 'note', 'annotation')
              AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
            """
        ).fetchone()
        return row[0] if row else 0

    def search_items(self, query: str, limit: int = DEFAULT_SEARCH_LIMIT) -> List[Dict[str, Any]]:
        """Search for items matching the query with fuzzy/keyword matching.

        Tokenizes query into keywords and searches across title, abstract,
        creators, tags, DOI, and citation key. Results are ranked by
        relevance: exact > substring > fuzzy, with items matching all
        tokens ranked higher.
        """
        tokens = _tokenize_query(query)
        if not tokens:
            return []

        # Build SQL with one LIKE condition per token (OR across tokens)
        conditions = []
        params = []

        for token in tokens:
            pattern = f"%{_escape_like(token)}%"
            conditions.append(
                """(
                    (f.fieldName IN ('title', 'shortTitle', 'publicationTitle')
                        AND idv.value LIKE ? ESCAPE '\\')
                    OR (f.fieldName = 'abstractNote' AND idv.value LIKE ? ESCAPE '\\')
                    OR (f.fieldName = 'DOI' AND idv.value LIKE ? ESCAPE '\\')
                    OR c.lastName LIKE ? ESCAPE '\\'
                    OR c.firstName LIKE ? ESCAPE '\\'
                    OR t.name LIKE ? ESCAPE '\\'
                )"""
            )
            # Each condition references the pattern 6 times
            params.extend([pattern] * 6)

        where_clause = " OR ".join(conditions)
        candidate_limit = limit * 5

        sql = f"""
            SELECT DISTINCT i.itemID, i.key, it.typeName
            FROM items i
            JOIN itemTypes it ON i.itemTypeID = it.itemTypeID
            LEFT JOIN itemData id ON i.itemID = id.itemID
            LEFT JOIN itemDataValues idv ON id.valueID = idv.valueID
            LEFT JOIN fields f ON id.fieldID = f.fieldID
            LEFT JOIN itemCreators ic ON i.itemID = ic.itemID
            LEFT JOIN creators c ON ic.creatorID = c.creatorID
            LEFT JOIN itemTags itag ON i.itemID = itag.itemID
            LEFT JOIN tags t ON itag.tagID = t.tagID
            WHERE it.typeName NOT IN ('attachment', 'note', 'annotation')
              AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
              AND ({where_clause})
            ORDER BY i.dateModified DESC
            LIMIT ?
        """
        params.append(candidate_limit)

        rows = self.conn.execute(sql, params).fetchall()
        if not rows:
            return []

        item_ids = [row[0] for row in rows]
        item_rows = [(row[0], row[1], row[2]) for row in rows]

        items = self._build_items_batch(item_rows, item_ids)

        # Score and rank
        scored = [(self._score_item(item, tokens), item) for item in items]
        scored = [(s, item) for s, item in scored if s > 0.0]
        scored.sort(key=lambda x: x[0], reverse=True)

        return [item for _, item in scored[:limit]]

    def get_item(self, key: str) -> Optional[Dict[str, Any]]:
        """Get a single item by its Zotero key."""
        row = self.conn.execute(
            """
            SELECT i.itemID, i.key, it.typeName
            FROM items i
            JOIN itemTypes it ON i.itemTypeID = it.itemTypeID
            WHERE i.key = ?
              AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
            """,
            (key,),
        ).fetchone()

        if not row:
            return None

        items = self._build_items_batch(
            [(row[0], row[1], row[2])], [row[0]]
        )
        return items[0] if items else None

    def get_bibtex(self, key: str) -> Optional[str]:
        """Generate a BibTeX entry for an item."""
        item = self.get_item(key)
        if not item:
            return None

        citation_key = item.get("citation_key") or item["key"]
        entry_type = _map_item_type_to_bibtex(item["item_type"])

        lines = [f"@{entry_type}{{{citation_key},"]

        if item["title"]:
            lines.append(f"  title = {{{_escape_bibtex(item['title'])}}},")

        # Authors
        authors = [
            c for c in item["creators"] if c["creator_type"] == "author"
        ]
        if authors:
            author_strs = []
            for a in authors:
                if a["first_name"]:
                    author_strs.append(f"{a['last_name']}, {a['first_name']}")
                else:
                    author_strs.append(a["last_name"])
            lines.append(f"  author = {{{' and '.join(author_strs)}}},")

        # Editors
        editors = [
            c for c in item["creators"] if c["creator_type"] == "editor"
        ]
        if editors:
            editor_strs = []
            for e in editors:
                if e["first_name"]:
                    editor_strs.append(f"{e['last_name']}, {e['first_name']}")
                else:
                    editor_strs.append(e["last_name"])
            lines.append(f"  editor = {{{' and '.join(editor_strs)}}},")

        # Date/Year
        if item.get("date"):
            year = _extract_year(item["date"])
            if year:
                lines.append(f"  year = {{{year}}},")

        # Journal
        if item.get("journal") and item["item_type"] == "journalArticle":
            lines.append(f"  journal = {{{_escape_bibtex(item['journal'])}}},")

        # Booktitle
        if item.get("booktitle"):
            lines.append(f"  booktitle = {{{_escape_bibtex(item['booktitle'])}}},")

        # Publisher
        if item.get("publisher"):
            lines.append(f"  publisher = {{{_escape_bibtex(item['publisher'])}}},")

        # Volume
        if item.get("volume"):
            lines.append(f"  volume = {{{item['volume']}}},")

        # Number
        if item.get("number"):
            lines.append(f"  number = {{{item['number']}}},")

        # Pages
        if item.get("pages"):
            lines.append(f"  pages = {{{item['pages']}}},")

        # DOI
        if item.get("doi"):
            lines.append(f"  doi = {{{item['doi']}}},")

        # URL
        if item.get("url"):
            lines.append(f"  url = {{{item['url']}}},")

        # Abstract
        if item.get("abstract_text"):
            lines.append(f"  abstract = {{{_escape_bibtex(item['abstract_text'])}}},")

        lines.append("}")
        return "\n".join(lines) + "\n"

    def list_collections(self) -> List[Dict[str, Any]]:
        """List all collections."""
        rows = self.conn.execute(
            """
            SELECT c.key, c.collectionName, pc.key as parentKey
            FROM collections c
            LEFT JOIN collections pc ON c.parentCollectionID = pc.collectionID
            ORDER BY c.collectionName
            """
        ).fetchall()

        return [
            {
                "key": row[0],
                "name": row[1],
                "parent_key": row[2],
            }
            for row in rows
        ]

    def get_collection_items(
        self, collection_key: str, limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Get all items in a collection."""
        rows = self.conn.execute(
            """
            SELECT i.itemID, i.key, it.typeName
            FROM items i
            JOIN itemTypes it ON i.itemTypeID = it.itemTypeID
            JOIN collectionItems ci ON i.itemID = ci.itemID
            JOIN collections c ON ci.collectionID = c.collectionID
            WHERE c.key = ?
              AND it.typeName NOT IN ('attachment', 'note', 'annotation')
              AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
            ORDER BY i.dateModified DESC
            LIMIT ?
            """,
            (collection_key, limit),
        ).fetchall()

        item_ids = [row[0] for row in rows]
        item_rows = [(row[0], row[1], row[2]) for row in rows]
        return self._build_items_batch(item_rows, item_ids)

    def get_pdf_path(self, key: str) -> Optional[str]:
        """Get the PDF attachment path for an item."""
        row = self.conn.execute(
            """
            SELECT i.itemID
            FROM items i
            WHERE i.key = ?
              AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
            """,
            (key,),
        ).fetchone()

        if not row:
            return None

        item_id = row[0]
        pdf_map = self._batch_get_pdf_paths([item_id])
        path = pdf_map.get(item_id)
        return str(path) if path else None

    # -------------------------------------------------------------------
    # Batch query helpers (avoid N+1 queries)
    # -------------------------------------------------------------------

    def _build_items_batch(
        self,
        item_rows: List[tuple],
        item_ids: List[int],
    ) -> List[Dict[str, Any]]:
        """Build item dicts from batch queries."""
        if not item_ids:
            return []

        fields_map = self._batch_get_fields(item_ids)
        creators_map = self._batch_get_creators(item_ids)
        tags_map = self._batch_get_tags(item_ids)
        collections_map = self._batch_get_collections(item_ids)
        pdf_map = self._batch_get_pdf_paths(item_ids)
        citation_keys_map = self._batch_get_citation_keys(item_ids)

        items = []
        for item_id, key, item_type in item_rows:
            fields = fields_map.get(item_id, {})
            creators = creators_map.get(item_id, [])
            tags = tags_map.get(item_id, [])
            collections = collections_map.get(item_id, [])
            pdf_path = pdf_map.get(item_id)
            citation_key = citation_keys_map.get(item_id)

            # Resolve booktitle for conference papers
            booktitle = fields.get("bookTitle")
            if not booktitle and item_type == "conferencePaper":
                booktitle = fields.get("proceedingsTitle")

            items.append({
                "key": key,
                "item_type": item_type,
                "title": fields.get("title", ""),
                "creators": creators,
                "date": fields.get("date"),
                "abstract_text": fields.get("abstractNote"),
                "doi": fields.get("DOI"),
                "url": fields.get("url"),
                "tags": tags,
                "collections": collections,
                "pdf_path": str(pdf_path) if pdf_path else None,
                "citation_key": citation_key,
                "journal": fields.get("publicationTitle"),
                "booktitle": booktitle,
                "publisher": fields.get("publisher"),
                "pages": fields.get("pages"),
                "volume": fields.get("volume"),
                "number": fields.get("issue"),
            })

        return items

    def _placeholders(self, ids: List[int]) -> str:
        """Build SQL placeholder string for IN clause."""
        return ",".join("?" for _ in ids)

    def _batch_get_fields(self, item_ids: List[int]) -> Dict[int, Dict[str, str]]:
        ph = self._placeholders(item_ids)
        rows = self.conn.execute(
            f"""
            SELECT id.itemID, f.fieldName, idv.value
            FROM itemData id
            JOIN fields f ON id.fieldID = f.fieldID
            JOIN itemDataValues idv ON id.valueID = idv.valueID
            WHERE id.itemID IN ({ph})
            """,
            item_ids,
        ).fetchall()

        result: Dict[int, Dict[str, str]] = {}
        for row in rows:
            result.setdefault(row[0], {})[row[1]] = row[2]
        return result

    def _batch_get_creators(self, item_ids: List[int]) -> Dict[int, List[Dict[str, str]]]:
        ph = self._placeholders(item_ids)
        rows = self.conn.execute(
            f"""
            SELECT ic.itemID, c.firstName, c.lastName, ct.creatorType
            FROM itemCreators ic
            JOIN creators c ON ic.creatorID = c.creatorID
            JOIN creatorTypes ct ON ic.creatorTypeID = ct.creatorTypeID
            WHERE ic.itemID IN ({ph})
            ORDER BY ic.itemID, ic.orderIndex
            """,
            item_ids,
        ).fetchall()

        result: Dict[int, List[Dict[str, str]]] = {}
        for row in rows:
            result.setdefault(row[0], []).append({
                "first_name": row[1] or "",
                "last_name": row[2] or "",
                "creator_type": row[3],
            })
        return result

    def _batch_get_tags(self, item_ids: List[int]) -> Dict[int, List[str]]:
        ph = self._placeholders(item_ids)
        rows = self.conn.execute(
            f"""
            SELECT it.itemID, t.name
            FROM itemTags it
            JOIN tags t ON it.tagID = t.tagID
            WHERE it.itemID IN ({ph})
            """,
            item_ids,
        ).fetchall()

        result: Dict[int, List[str]] = {}
        for row in rows:
            result.setdefault(row[0], []).append(row[1])
        return result

    def _batch_get_collections(self, item_ids: List[int]) -> Dict[int, List[str]]:
        ph = self._placeholders(item_ids)
        rows = self.conn.execute(
            f"""
            SELECT ci.itemID, c.collectionName
            FROM collectionItems ci
            JOIN collections c ON ci.collectionID = c.collectionID
            WHERE ci.itemID IN ({ph})
            """,
            item_ids,
        ).fetchall()

        result: Dict[int, List[str]] = {}
        for row in rows:
            result.setdefault(row[0], []).append(row[1])
        return result

    def _batch_get_pdf_paths(self, item_ids: List[int]) -> Dict[int, Optional[Path]]:
        ph = self._placeholders(item_ids)
        rows = self.conn.execute(
            f"""
            SELECT ia.parentItemID, ia.path, i.key
            FROM itemAttachments ia
            JOIN items i ON ia.itemID = i.itemID
            WHERE ia.parentItemID IN ({ph})
              AND ia.contentType = 'application/pdf'
              AND ia.itemID NOT IN (SELECT itemID FROM deletedItems)
            """,
            item_ids,
        ).fetchall()

        result: Dict[int, Optional[Path]] = {}
        for row in rows:
            parent_id, path_str, attachment_key = row[0], row[1], row[2]
            if parent_id in result:
                continue  # Take first PDF only
            if path_str:
                resolved = self._resolve_pdf_path(path_str, attachment_key)
                result[parent_id] = resolved
        return result

    def _resolve_pdf_path(self, path: str, attachment_key: str) -> Optional[Path]:
        """Resolve Zotero storage:filename paths to absolute paths."""
        if path.startswith("storage:"):
            filename = path[len("storage:"):]
            resolved = self.data_dir / "storage" / attachment_key / filename
        else:
            resolved = Path(path)

        return resolved if resolved.exists() else None

    def _batch_get_citation_keys(self, item_ids: List[int]) -> Dict[int, Optional[str]]:
        """Get citation keys from extra field and Better BibTeX database."""
        ph = self._placeholders(item_ids)

        # Try the extra field first (Citation Key: or bibtex: prefix)
        rows = self.conn.execute(
            f"""
            SELECT id.itemID, idv.value
            FROM itemData id
            JOIN fields f ON id.fieldID = f.fieldID
            JOIN itemDataValues idv ON id.valueID = idv.valueID
            WHERE id.itemID IN ({ph}) AND f.fieldName = 'extra'
            """,
            item_ids,
        ).fetchall()

        result: Dict[int, Optional[str]] = {}
        for row in rows:
            key = _extract_citation_key_from_extra(row[1])
            if key:
                result[row[0]] = key

        # Try Better BibTeX database for items missing citation keys
        bbt_db_path = self.data_dir / "better-bibtex.sqlite"
        if bbt_db_path.exists():
            missing_ids = [id for id in item_ids if id not in result]
            if missing_ids:
                try:
                    bbt_conn = sqlite3.connect(
                        f"file:{bbt_db_path}?mode=ro", uri=True
                    )
                    ph2 = ",".join("?" for _ in missing_ids)
                    bbt_rows = bbt_conn.execute(
                        f"SELECT itemID, citationKey FROM citationkey WHERE itemID IN ({ph2})",
                        missing_ids,
                    ).fetchall()
                    for row in bbt_rows:
                        result[row[0]] = row[1]
                    bbt_conn.close()
                except Exception:
                    pass  # BBT database may not have the expected schema

        return result

    # -------------------------------------------------------------------
    # Scoring and ranking (mirrors src/zotero.rs scoring logic)
    # -------------------------------------------------------------------

    def _score_item(self, item: Dict[str, Any], tokens: List[str]) -> float:
        """Score an item against search tokens for relevance ranking."""
        if not tokens:
            return 0.0

        total_score = 0.0
        tokens_matched = 0

        for token in tokens:
            best = 0.0

            # Title (weight 3x)
            best = max(best, _score_field(token, item["title"]) * 3.0)

            # Citation key (weight 3x)
            if item.get("citation_key"):
                best = max(best, _score_field(token, item["citation_key"]) * 3.0)

            # DOI (weight 2x)
            if item.get("doi"):
                best = max(best, _score_field(token, item["doi"]) * 2.0)

            # Authors (weight 2x)
            for c in item.get("creators", []):
                best = max(best, _score_field(token, c["last_name"]) * 2.0)
                best = max(best, _score_field(token, c["first_name"]) * 2.0)

            # Tags (weight 1.5x)
            for tag in item.get("tags", []):
                best = max(best, _score_field(token, tag) * 1.5)

            # Abstract (weight 1x)
            if item.get("abstract_text"):
                best = max(best, _score_field(token, item["abstract_text"]))

            if best > 0.0:
                tokens_matched += 1
            total_score += best

        # AND bonus: all tokens matched
        n = len(tokens)
        if n > 1:
            ratio = tokens_matched / n
            if tokens_matched == n:
                total_score *= 2.0
            else:
                total_score *= 0.5 + ratio * 0.5

        return total_score


# ---------------------------------------------------------------------------
# Scoring helpers (pure functions)
# ---------------------------------------------------------------------------

def _tokenize_query(query: str) -> List[str]:
    """Split query into lowercase keywords."""
    return [t.lower() for t in query.split() if t.strip()]


def _escape_like(s: str) -> str:
    """Escape special chars in SQL LIKE patterns."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _score_field(token: str, field: str) -> float:
    """Score how well a token matches a field value.

    Returns:
        10.0 for exact match
        8.0 for word-boundary match
        5.0 for substring match
        0.0-3.0 for fuzzy match (trigram similarity, threshold 0.3)
    """
    if not field:
        return 0.0

    fl = field.lower()
    tl = token.lower()

    if fl == tl:
        return 10.0

    if tl in fl:
        # Word boundary check
        words = re.split(r"[^a-zA-Z0-9]+", fl)
        if tl in words:
            return 8.0
        return 5.0

    # Fuzzy: trigram similarity against each word
    words = [w for w in re.split(r"[^a-zA-Z0-9]+", fl) if w]
    best_sim = max((_trigram_similarity(tl, w) for w in words), default=0.0)
    if best_sim >= 0.3:
        return best_sim * 3.0

    return 0.0


def _trigram_similarity(a: str, b: str) -> float:
    """Dice coefficient on character trigrams."""
    a_tri = _trigrams(a)
    b_tri = _trigrams(b)
    if not a_tri and not b_tri:
        return 1.0
    if not a_tri or not b_tri:
        return 0.0
    intersection = len(set(a_tri) & set(b_tri))
    return (2.0 * intersection) / (len(a_tri) + len(b_tri))


def _trigrams(s: str) -> List[str]:
    """Generate character trigrams from a string."""
    padded = f"  {s} "
    if len(padded) < 3:
        return []
    return [padded[i : i + 3] for i in range(len(padded) - 2)]


def _extract_citation_key_from_extra(extra: str) -> Optional[str]:
    """Extract citation key from Zotero's extra field."""
    for line in extra.splitlines():
        line = line.strip()
        if line.startswith("Citation Key:"):
            return line[len("Citation Key:") :].strip()
        if line.startswith("bibtex:"):
            return line[len("bibtex:") :].strip()
    return None


def _extract_year(date: str) -> Optional[str]:
    """Extract a 4-digit year from various date formats."""
    m = YEAR_REGEX.search(date)
    return m.group(0) if m else None


def _map_item_type_to_bibtex(item_type: str) -> str:
    """Map Zotero item types to BibTeX entry types."""
    mapping = {
        "journalArticle": "article",
        "book": "book",
        "bookSection": "incollection",
        "conferencePaper": "inproceedings",
        "thesis": "phdthesis",
        "report": "techreport",
        "webpage": "misc",
        "presentation": "misc",
        "manuscript": "unpublished",
        "patent": "patent",
    }
    return mapping.get(item_type, "misc")


def _escape_bibtex(s: str) -> str:
    """Escape special characters for BibTeX."""
    return (
        s.replace("&", r"\&")
        .replace("%", r"\%")
        .replace("$", r"\$")
        .replace("#", r"\#")
        .replace("_", r"\_")
        .replace("{", r"\{")
        .replace("}", r"\}")
        .replace("~", r"\textasciitilde{}")
        .replace("^", r"\textasciicircum{}")
    )


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _format_item_summary(item: Dict[str, Any], index: Optional[int] = None) -> str:
    """Format an item as a readable summary string."""
    prefix = f"{index}. " if index is not None else ""
    title = item.get("title") or "Untitled"
    authors = ", ".join(
        f"{c['first_name']} {c['last_name']}".strip()
        for c in item.get("creators", [])
        if c.get("creator_type") == "author"
    )
    date = item.get("date") or "n.d."
    key = item.get("key", "")
    citation_key = item.get("citation_key")
    item_type = item.get("item_type", "")

    lines = [f"{prefix}**{title}**"]
    if authors:
        lines.append(f"   Authors: {authors}")
    lines.append(f"   Date: {date}")
    lines.append(f"   Type: {item_type}")
    lines.append(f"   Key: `{key}`")
    if citation_key:
        lines.append(f"   Citation: {citation_key}")
    if item.get("doi"):
        lines.append(f"   DOI: {item['doi']}")

    return "\n".join(lines)


def _format_item_detail(item: Dict[str, Any]) -> str:
    """Format full item details."""
    title = item.get("title") or "Untitled"
    lines = [f"**{title}**\n"]

    # Authors
    authors = [
        f"{c['first_name']} {c['last_name']}".strip()
        for c in item.get("creators", [])
    ]
    if authors:
        lines.append(f"**Authors:** {', '.join(authors)}")

    lines.append(f"**Date:** {item.get('date') or 'n.d.'}")
    lines.append(f"**Type:** {item.get('item_type', 'unknown')}")
    lines.append(f"**Key:** `{item['key']}`")

    if item.get("citation_key"):
        lines.append(f"**Citation key:** {item['citation_key']}")
    if item.get("doi"):
        lines.append(f"**DOI:** {item['doi']}")
    if item.get("url"):
        lines.append(f"**URL:** {item['url']}")
    if item.get("journal"):
        lines.append(f"**Journal:** {item['journal']}")
    if item.get("booktitle"):
        lines.append(f"**Book/Proceedings:** {item['booktitle']}")
    if item.get("publisher"):
        lines.append(f"**Publisher:** {item['publisher']}")
    if item.get("volume"):
        vol = item["volume"]
        if item.get("number"):
            vol += f"({item['number']})"
        lines.append(f"**Volume:** {vol}")
    if item.get("pages"):
        lines.append(f"**Pages:** {item['pages']}")

    # Tags
    tags = item.get("tags", [])
    if tags:
        lines.append(f"\n**Tags:** {', '.join(tags)}")

    # Collections
    collections = item.get("collections", [])
    if collections:
        lines.append(f"**Collections:** {', '.join(collections)}")

    # PDF
    if item.get("pdf_path"):
        lines.append(f"\n**PDF:** {item['pdf_path']}")

    # Abstract
    if item.get("abstract_text"):
        lines.append(f"\n**Abstract:**\n{item['abstract_text']}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

app = Server("zotero")

# Global database reference (initialized in main)
db: Optional[ZoteroDb] = None


@app.list_tools()
async def list_tools() -> list[Tool]:
    # Check if the Zotero plugin is available for write operations
    plugin_active = await _plugin_available()

    tools = [
        Tool(
            name="search_papers",
            description=(
                "Search your Zotero library for papers matching a query. "
                "Searches across titles, authors, abstracts, tags, DOIs, "
                "and citation keys with fuzzy matching and relevance ranking."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (keywords, author names, DOIs, etc.)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum results to return (default 20)",
                        "default": 20,
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="get_paper_details",
            description=(
                "Get full metadata for a paper by its Zotero key. "
                "Returns title, authors, abstract, tags, collections, "
                "PDF path, citation key, and all bibliographic fields."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Zotero item key (e.g., 'ABC12345')",
                    },
                },
                "required": ["key"],
            },
        ),
        Tool(
            name="get_bibtex",
            description=(
                "Generate a BibTeX entry for a paper. Uses Better BibTeX "
                "citation keys when available."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Zotero item key",
                    },
                },
                "required": ["key"],
            },
        ),
        Tool(
            name="list_collections",
            description="List all collections in the Zotero library.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="get_collection_items",
            description="Get all papers in a specific Zotero collection.",
            inputSchema={
                "type": "object",
                "properties": {
                    "collection_key": {
                        "type": "string",
                        "description": "Collection key",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum items to return (default 50)",
                        "default": 50,
                    },
                },
                "required": ["collection_key"],
            },
        ),
        Tool(
            name="get_pdf_path",
            description=(
                "Get the filesystem path to a paper's PDF attachment. "
                "Returns the resolved path if the PDF exists on disk."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Zotero item key",
                    },
                },
                "required": ["key"],
            },
        ),
        Tool(
            name="library_stats",
            description="Get summary statistics about the Zotero library.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
    ]

    # Add write tools only when the Zotero plugin is available
    if plugin_active:
        tools.extend([
            Tool(
                name="create_collection",
                description=(
                    "Create a new collection in Zotero. "
                    "Requires the Zotero MCP Bridge plugin to be running in Zotero."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Name for the new collection",
                        },
                        "parent_key": {
                            "type": "string",
                            "description": "Key of parent collection (optional, for nested collections)",
                        },
                    },
                    "required": ["name"],
                },
            ),
            Tool(
                name="add_to_collection",
                description=(
                    "Add papers to a Zotero collection. "
                    "Requires the Zotero MCP Bridge plugin to be running in Zotero."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "collection_key": {
                            "type": "string",
                            "description": "Key of the target collection",
                        },
                        "item_keys": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Array of item keys to add to the collection",
                        },
                    },
                    "required": ["collection_key", "item_keys"],
                },
            ),
            Tool(
                name="remove_from_collection",
                description=(
                    "Remove papers from a Zotero collection. "
                    "Does not delete the papers, just removes them from the collection. "
                    "Requires the Zotero MCP Bridge plugin to be running in Zotero."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "collection_key": {
                            "type": "string",
                            "description": "Key of the collection",
                        },
                        "item_keys": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Array of item keys to remove from the collection",
                        },
                    },
                    "required": ["collection_key", "item_keys"],
                },
            ),
        ])

    return tools


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    assert db is not None, "Database not initialized"

    try:
        if name == "search_papers":
            query = arguments["query"]
            limit = arguments.get("limit", DEFAULT_SEARCH_LIMIT)
            items = db.search_items(query, limit)

            if not items:
                return [TextContent(type="text", text=f"No results for '{query}'.")]

            lines = [f"Found {len(items)} result(s) for '{query}':\n"]
            for i, item in enumerate(items, 1):
                lines.append(_format_item_summary(item, i))
                lines.append("")
            return [TextContent(type="text", text="\n".join(lines))]

        elif name == "get_paper_details":
            key = arguments["key"]
            item = db.get_item(key)
            if not item:
                return [TextContent(type="text", text=f"No item found with key '{key}'.")]
            return [TextContent(type="text", text=_format_item_detail(item))]

        elif name == "get_bibtex":
            key = arguments["key"]
            bibtex = db.get_bibtex(key)
            if not bibtex:
                return [TextContent(type="text", text=f"No item found with key '{key}'.")]
            return [TextContent(type="text", text=f"```bibtex\n{bibtex}```")]

        elif name == "list_collections":
            collections = db.list_collections()
            if not collections:
                return [TextContent(type="text", text="No collections found.")]

            lines = [f"Found {len(collections)} collection(s):\n"]
            for c in collections:
                indent = "  " if c["parent_key"] else ""
                lines.append(f"{indent}- **{c['name']}** (key: `{c['key']}`)")
            return [TextContent(type="text", text="\n".join(lines))]

        elif name == "get_collection_items":
            collection_key = arguments["collection_key"]
            limit = arguments.get("limit", 50)
            items = db.get_collection_items(collection_key, limit)

            if not items:
                return [TextContent(
                    type="text",
                    text=f"No items found in collection '{collection_key}'.",
                )]

            lines = [f"Collection contains {len(items)} item(s):\n"]
            for i, item in enumerate(items, 1):
                lines.append(_format_item_summary(item, i))
                lines.append("")
            return [TextContent(type="text", text="\n".join(lines))]

        elif name == "get_pdf_path":
            key = arguments["key"]
            path = db.get_pdf_path(key)
            if path:
                return [TextContent(type="text", text=f"PDF path: {path}")]
            return [TextContent(
                type="text",
                text=f"No PDF attachment found for item '{key}'.",
            )]

        elif name == "library_stats":
            count = db.item_count()
            collections = db.list_collections()
            plugin_active = await _plugin_available()
            text = (
                f"**Zotero Library Statistics**\n\n"
                f"- Total items: {count}\n"
                f"- Collections: {len(collections)}\n"
                f"- Database: {db.db_path}\n"
                f"- Data directory: {db.data_dir}\n"
            )

            # Check for Better BibTeX
            bbt_path = db.data_dir / "better-bibtex.sqlite"
            if bbt_path.exists():
                text += "- Better BibTeX: installed\n"
            else:
                text += "- Better BibTeX: not detected\n"

            # Check plugin status
            if plugin_active:
                text += "- Zotero MCP Bridge plugin: active (write tools available)\n"
            else:
                text += "- Zotero MCP Bridge plugin: not detected (read-only mode)\n"

            return [TextContent(type="text", text=text)]

        elif name == "create_collection":
            result = await _plugin_rpc("createCollection", {
                "name": arguments["name"],
                "parentKey": arguments.get("parent_key"),
            })
            text = (
                f"Created collection **{result['name']}**\n"
                f"- Key: `{result['key']}`\n"
            )
            if result.get("parentKey"):
                text += f"- Parent: `{result['parentKey']}`\n"
            return [TextContent(type="text", text=text)]

        elif name == "add_to_collection":
            result = await _plugin_rpc("addToCollection", {
                "collectionKey": arguments["collection_key"],
                "itemKeys": arguments["item_keys"],
            })
            added = result.get("added", [])
            errors = result.get("errors", [])
            text = f"Added {len(added)} item(s) to collection `{arguments['collection_key']}`.\n"
            if errors:
                text += "\nErrors:\n"
                for err in errors:
                    text += f"- `{err['key']}`: {err['error']}\n"
            return [TextContent(type="text", text=text)]

        elif name == "remove_from_collection":
            result = await _plugin_rpc("removeFromCollection", {
                "collectionKey": arguments["collection_key"],
                "itemKeys": arguments["item_keys"],
            })
            removed = result.get("removed", [])
            errors = result.get("errors", [])
            text = f"Removed {len(removed)} item(s) from collection `{arguments['collection_key']}`.\n"
            if errors:
                text += "\nErrors:\n"
                for err in errors:
                    text += f"- `{err['key']}`: {err['error']}\n"
            return [TextContent(type="text", text=text)]

        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]

    except Exception as e:
        return [TextContent(type="text", text=f"Error: {e}")]


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def main():
    global db

    db_path = find_zotero_db()
    if not db_path:
        print(
            "Error: Could not find Zotero database.\n"
            "Set ZOTERO_DB_PATH environment variable to the path of your zotero.sqlite file.\n"
            "Common locations:\n"
            "  Linux/macOS: ~/Zotero/zotero.sqlite\n"
            "  Windows: C:\\Users\\<name>\\Zotero\\zotero.sqlite",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Opening Zotero database: {db_path}", file=sys.stderr)
    db = ZoteroDb(db_path)

    count = db.item_count()
    print(f"Library contains {count} items", file=sys.stderr)

    from mcp.server.stdio import stdio_server

    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
