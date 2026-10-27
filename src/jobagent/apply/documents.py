"""Cover-letter files: plain text when the form accepts .txt, otherwise a minimal one-font PDF (no extra deps)."""

from __future__ import annotations

import re
import textwrap
from pathlib import Path

_REPLACE = {"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-",
            "…": "...", " ": " ", "•": "-"}


def _latin1(text: str) -> str:
    for k, v in _REPLACE.items():
        text = text.replace(k, v)
    return text.encode("latin-1", "replace").decode("latin-1")


def write_pdf(text: str, path: Path, title: str = "Cover Letter") -> Path:
    """Letter-size PDF, Helvetica 11pt, wrapped at ~92 characters, paginated."""
    lines: list[str] = []
    for para in _latin1(text).split("\n"):
        lines.extend(textwrap.wrap(para, 92) or [""])
    per_page = 52
    pages = [lines[i:i + per_page] for i in range(0, max(len(lines), 1), per_page)]
    objs: list[bytes] = []

    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    font_id = 3
    page_ids, content_ids = [], []
    next_id = 4
    for _ in pages:
        page_ids.append(next_id)
        content_ids.append(next_id + 1)
        next_id += 2
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{p} 0 R" for p in page_ids)
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    for page_lines, pid, cid in zip(pages, page_ids, content_ids):
        stream = "BT /F1 11 Tf 14 TL 72 720 Td " + " ".join(f"({esc(l)}) Tj T*" for l in page_lines) + " ET"
        data = stream.encode("latin-1")
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 {font_id} 0 R >> >> "
                    f"/Contents {cid} 0 R >>".encode())
        objs.append(b"<< /Length " + str(len(data)).encode() + b" >>\nstream\n" + data + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R /Info << /Title ({esc(title)}) >> >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))
    return path


def write_cover_letter(text: str, folder: Path, stem: str, accept: str) -> Path:
    """Pick a format the upload field accepts (.pdf preferred, .txt otherwise)."""
    accept = (accept or "").lower()
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", stem)[:80]
    if not accept or "pdf" in accept:
        return write_pdf(text, folder / f"{safe}.pdf")
    if ".txt" in accept or "text/plain" in accept:
        p = folder / f"{safe}.txt"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p
    return write_pdf(text, folder / f"{safe}.pdf")
