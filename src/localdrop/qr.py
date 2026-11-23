"""QR codes without Pillow: segno is pure Python and has a built-in PNG writer."""

from __future__ import annotations

import io


def make_qr_png(text: str, scale: int = 8, border: int = 2) -> bytes | None:
    try:
        import segno
    except Exception:  # library missing - UI falls back to plain text
        return None
    buf = io.BytesIO()
    segno.make(text, error="m").save(buf, kind="png", scale=scale, border=border,
                                     dark="#0b0f1a", light="#ffffff")
    return buf.getvalue()
