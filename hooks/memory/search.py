"""Full-text search over archived turns."""
import re
from . import db as archive_db


def sanitize_fts5_query(raw: str) -> str:
    """
    Natural language -> safe FTS5 query.
    Strips operators/punctuation, quotes each token, implicit AND.
    """
    tokens = re.findall(r'[\w]+', raw, re.UNICODE)
    if not tokens:
        return '""'
    return ' '.join(f'"{t}"' for t in tokens)


def search(session_id: str, query: str, limit: int = 10) -> list:
    """
    FTS5 search with BM25 ranking.
    Returns list of (line_number, role, content, files_touched, relevance).
    """
    conn = archive_db.get_db(session_id)
    safe_query = sanitize_fts5_query(query)

    results = conn.execute("""
        SELECT t.line_number, t.role, t.content, t.files_touched, rank
        FROM turns_fts f
        JOIN turns t ON t.id = f.rowid
        WHERE turns_fts MATCH ? AND t.session_id = ?
        ORDER BY rank
        LIMIT ?
    """, (safe_query, session_id, limit)).fetchall()

    conn.close()
    return results


def format_results(results: list) -> str:
    """Format search results for additionalContext injection."""
    if not results:
        return ""
    lines = ["## Archive Search Results"]
    for line_num, role, content, files, _rank in results:
        # Truncate long content
        preview = content[:300] + "..." if len(content) > 300 else content
        lines.append(f"\n**[L{line_num}] {role}**: {preview}")
        if files:
            lines.append(f"  Files: {files}")
    return "\n".join(lines)
