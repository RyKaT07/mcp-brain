"""Nextcloud integration — read-only file access via WebDAV.

Registers 2 tools: nextcloud_browse and nextcloud_read. Requires
NEXTCLOUD_URL, NEXTCLOUD_USER, and NEXTCLOUD_PASSWORD env vars.
If any is missing, tools are not registered and the server starts
normally without file access.

Recommended setup: create a dedicated Nextcloud user (e.g. mcp-reader)
and share specific folders read-only to that account. mcp-brain sees
only what you share — no code change needed to add/remove access.
"""

from __future__ import annotations

import base64
import io
import logging
from pathlib import PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote
from urllib.request import HTTPBasicAuthHandler, HTTPPasswordMgrWithDefaultRealm, Request, build_opener, urlopen
from xml.etree import ElementTree as ET

from mcp.server.fastmcp import FastMCP

from mcp_brain.auth import PermissionDenied
from mcp_brain.tools._perms import require

logger = logging.getLogger(__name__)

# WebDAV XML namespaces
_DAV_NS = "DAV:"
_DAV = f"{{{_DAV_NS}}}"

# File type routing
_TEXT_EXTENSIONS = frozenset({
    ".md", ".txt", ".csv", ".py", ".go", ".java", ".json", ".yaml", ".yml",
    ".xml", ".sh", ".sql", ".html", ".css", ".js", ".ts", ".toml", ".ini",
    ".cfg", ".log", ".env", ".c", ".h", ".cpp", ".rs", ".rb", ".php",
    ".r", ".m", ".swift", ".kt", ".gradle", ".makefile", ".dockerfile",
    ".proto", ".graphql", ".bat", ".ps1", ".lua", ".pl", ".tex",
})
_PDF_EXTENSIONS = frozenset({".pdf"})
_DOCX_EXTENSIONS = frozenset({".docx"})
_IMAGE_EXTENSIONS = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


def register_nextcloud_tools(
    mcp: FastMCP,
    base_url: str,
    username: str,
    password: str,
) -> None:
    """Register Nextcloud tools on the MCP server.

    Args:
        mcp: FastMCP instance to register tools on.
        base_url: Nextcloud server URL (e.g. http://10.0.0.42 or https://cloud.example.com).
        username: Nextcloud username (e.g. mcp-reader).
        password: App password for the Nextcloud account.
    """

    webdav_base = f"{base_url.rstrip('/')}/remote.php/dav/files/{username}"

    # -- HTTP helpers --------------------------------------------------------

    def _build_opener():
        auth_handler = HTTPBasicAuthHandler(HTTPPasswordMgrWithDefaultRealm())
        auth_handler.add_password(None, base_url, username, password)
        return build_opener(auth_handler)

    _opener = _build_opener()

    def _webdav_url(path: str) -> str:
        """Build full WebDAV URL with proper encoding for non-ASCII paths."""
        clean = path.strip("/")
        if not clean:
            return webdav_base + "/"
        # Encode each path segment separately to preserve slashes
        parts = clean.split("/")
        encoded = "/".join(quote(p, safe="") for p in parts)
        return f"{webdav_base}/{encoded}"

    def _propfind(path: str) -> bytes:
        """PROPFIND request — list directory contents."""
        url = _webdav_url(path)
        body = b'<?xml version="1.0" encoding="utf-8"?><d:propfind xmlns:d="DAV:"><d:allprop/></d:propfind>'
        req = Request(url, data=body, method="PROPFIND")
        req.add_header("Depth", "1")
        req.add_header("Content-Type", "application/xml; charset=utf-8")
        resp = _opener.open(req, timeout=15)
        return resp.read()

    def _get_binary(path: str) -> bytes:
        """GET request — download file as bytes."""
        url = _webdav_url(path)
        req = Request(url, method="GET")
        resp = _opener.open(req, timeout=30)
        return resp.read()

    def _api_error(e: HTTPError) -> str:
        if e.code == 404:
            return "Path not found on Nextcloud."
        try:
            body = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            body = ""
        return f"Nextcloud error ({e.code}): {body}" if body else f"Nextcloud error ({e.code})"

    def _format_size(size: int) -> str:
        if size < 1024:
            return f"{size} B"
        if size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        return f"{size / (1024 * 1024):.1f} MB"

    def _parse_propfind(xml_bytes: bytes, request_path: str) -> list[dict]:
        """Parse PROPFIND XML response into list of entries."""
        root = ET.fromstring(xml_bytes)
        entries = []
        # Normalize request path for comparison (skip self-entry)
        req_parts = PurePosixPath(unquote(request_path.strip("/")))

        for response in root.findall(f"{_DAV}response"):
            href_el = response.find(f"{_DAV}href")
            if href_el is None or href_el.text is None:
                continue

            href = unquote(href_el.text)
            # Extract relative path from href
            # href is like /remote.php/dav/files/username/path/to/file
            idx = href.find(f"/files/{username}/")
            if idx == -1:
                rel = href.rstrip("/")
            else:
                rel = href[idx + len(f"/files/{username}/"):].strip("/")

            # Skip self (the directory itself)
            if PurePosixPath(rel) == req_parts:
                continue

            propstat = response.find(f"{_DAV}propstat")
            if propstat is None:
                continue
            prop = propstat.find(f"{_DAV}prop")
            if prop is None:
                continue

            is_collection = prop.find(f"{_DAV}resourcetype/{_DAV}collection") is not None

            size_el = prop.find(f"{_DAV}getcontentlength")
            size = int(size_el.text) if size_el is not None and size_el.text else 0

            modified_el = prop.find(f"{_DAV}getlastmodified")
            modified = modified_el.text if modified_el is not None else ""

            name = PurePosixPath(rel).name

            entries.append({
                "name": name,
                "path": rel,
                "is_folder": is_collection,
                "size": size,
                "modified": modified,
            })

        # Sort: folders first, then files, alphabetically
        entries.sort(key=lambda e: (not e["is_folder"], e["name"].lower()))
        return entries

    # -- File type handlers --------------------------------------------------

    def _read_text(raw: bytes) -> str:
        return raw.decode("utf-8", errors="replace")

    def _read_pdf(raw: bytes) -> str:
        try:
            import pdfplumber
        except ImportError:
            return "Error: pdfplumber is not installed. PDF reading is unavailable."
        pages = []
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            for i, page in enumerate(pdf.pages, 1):
                text = page.extract_text()
                if text:
                    pages.append(f"--- Page {i} ---\n{text}")
        if not pages:
            return "PDF has no extractable text (possibly scanned images only)."
        return "\n\n".join(pages)

    def _read_docx(raw: bytes) -> str:
        try:
            from docx import Document
        except ImportError:
            return "Error: python-docx is not installed. DOCX reading is unavailable."
        doc = Document(io.BytesIO(raw))
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        if not paragraphs:
            return "Document has no text content."
        return "\n\n".join(paragraphs)

    def _read_image(raw: bytes, ext: str) -> list[dict]:
        mime = _IMAGE_EXTENSIONS.get(ext, "image/jpeg")
        b64 = base64.b64encode(raw).decode("ascii")
        return [
            {"type": "image", "data": b64, "mimeType": mime},
        ]

    # -- Tools ---------------------------------------------------------------

    @mcp.tool()
    def nextcloud_browse(path: str = "") -> str:
        """Browse files and folders on Nextcloud.

        Returns a listing of files and folders at the given path.
        Use this to navigate the Nextcloud file tree before reading
        specific files.

        Args:
            path: Folder path to list. Empty string for root.
                  Example: "27. Studia/24. Semestr 6"
        """
        try:
            require("nextcloud:read")
        except PermissionDenied as e:
            return str(e)
        try:
            xml_bytes = _propfind(path)
            entries = _parse_propfind(xml_bytes, path)

            if not entries:
                return f"Empty folder: {path or '(root)'}"

            lines = []
            for e in entries:
                if e["is_folder"]:
                    lines.append(f"\U0001f4c1 {e['name']}/")
                else:
                    size = _format_size(e["size"]) if e["size"] else ""
                    lines.append(f"\U0001f4c4 {e['name']} ({size})")

            header = f"## {path or '(root)'}\n"
            return header + "\n".join(lines)
        except HTTPError as e:
            return _api_error(e)
        except URLError as e:
            return f"Nextcloud connection error: {e}"

    @mcp.tool()
    def nextcloud_read(path: str) -> str | list[dict]:
        """Read a file from Nextcloud. Auto-detects file type by extension.

        Supported types:
        - Text files (md, txt, csv, py, go, json, yaml, etc.) → returns text
        - PDF → extracts text from all pages
        - Word (.docx) → extracts text from paragraphs
        - Images (jpg, png, gif, webp, bmp) → returns image for visual analysis

        Args:
            path: Full path to the file.
                  Example: "27. Studia/24. Semestr 6/Mikroprocesory/Wyklad_05.pdf"
        """
        try:
            require("nextcloud:read")
        except PermissionDenied as e:
            return str(e)
        try:
            ext = PurePosixPath(path).suffix.lower()

            raw = _get_binary(path)

            # Route by extension
            if ext in _IMAGE_EXTENSIONS:
                return _read_image(raw, ext)
            if ext in _PDF_EXTENSIONS:
                return _read_pdf(raw)
            if ext in _DOCX_EXTENSIONS:
                return _read_docx(raw)
            if ext in _TEXT_EXTENSIONS:
                return _read_text(raw)

            # Unknown — try text, fallback to error
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                return (
                    f"Unsupported file type: {ext}. "
                    f"Supported: text ({', '.join(sorted(_TEXT_EXTENSIONS)[:10])}...), "
                    f"PDF, DOCX, images (jpg, png, gif, webp, bmp)."
                )
        except HTTPError as e:
            return _api_error(e)
        except URLError as e:
            return f"Nextcloud connection error: {e}"
