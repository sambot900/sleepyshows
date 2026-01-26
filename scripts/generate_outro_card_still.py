from __future__ import annotations

import os

try:
    from PIL import Image, ImageDraw, ImageFont  # type: ignore
except Exception as e:  # pragma: no cover
    raise SystemExit(
        "This helper requires Pillow, but it's not a runtime dependency of Sleepy Shows.\n"
        "Install it in your venv and re-run:\n"
        "  pip install pillow\n\n"
        f"Import error: {e}"
    )


def _font(font_path: str, size: int):
    if os.path.exists(font_path):
        return ImageFont.truetype(font_path, size)
    return ImageFont.load_default()


def generate_outro_card_still(
    out_path: str,
    *,
    text: str = "[sleepy shows]",
    width: int = 1920,
    height: int = 1080,
) -> str:
    img = Image.new("RGBA", (int(width), int(height)), (0, 0, 0, 255))
    draw = ImageDraw.Draw(img)

    # Card panel (bottom-ish, like an outro card).
    panel_margin = int(width * 0.08)
    panel_w = width - panel_margin * 2
    panel_h = int(height * 0.26)
    panel_x0 = panel_margin
    panel_y0 = int(height * 0.62)
    panel_x1 = panel_x0 + panel_w
    panel_y1 = panel_y0 + panel_h

    radius = 36
    panel_fill = (15, 15, 15, 235)
    border = (255, 255, 255, 90)

    try:
        draw.rounded_rectangle(
            [panel_x0, panel_y0, panel_x1, panel_y1],
            radius=radius,
            fill=panel_fill,
            outline=border,
            width=3,
        )
    except Exception:
        draw.rectangle(
            [panel_x0, panel_y0, panel_x1, panel_y1],
            fill=panel_fill,
            outline=border,
            width=3,
        )

    font_path = os.path.join(os.path.dirname(__file__), "..", "assets", "HelveticaNeue-CondensedBlack.ttf")
    font_path = os.path.normpath(font_path)
    font = _font(font_path, 110)

    # Center text in the panel.
    s = str(text or "").strip() or "[sleepy shows]"
    bbox = draw.textbbox((0, 0), s, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx = panel_x0 + (panel_w - tw) // 2
    ty = panel_y0 + (panel_h - th) // 2 - 6

    shadow = (0, 0, 0, 220)
    for dx, dy in [(3, 3), (4, 4), (2, 4)]:
        draw.text((tx + dx, ty + dy), s, font=font, fill=shadow)

    # Crude stroke for readability.
    for dx, dy in [(-2, 0), (2, 0), (0, -2), (0, 2), (-2, -2), (2, 2), (-2, 2), (2, -2)]:
        draw.text((tx + dx, ty + dy), s, font=font, fill=(0, 0, 0, 255))

    draw.text((tx, ty), s, font=font, fill=(255, 255, 255, 255))

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    img.convert("RGB").save(out_path, "PNG")
    return out_path


if __name__ == "__main__":
    repo_root = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
    out = os.path.join(repo_root, "docs", "outro_bump_card_still.png")
    p = generate_outro_card_still(out)
    print(p)
