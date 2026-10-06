"""Newly generated synthetic challenge images, never captured from a service."""

import base64
import io


def fixtures(*, duplicate=False, flat=False, seed=41, masked=False, jpeg=False):
    import numpy as np
    from PIL import Image

    rng = np.random.default_rng(seed)
    original = rng.integers(0, 256, (142, 363, 3), dtype=np.uint8)
    x, y = 123, 37
    if flat:
        original[:] = 120
    if duplicate:
        original[y:y+56, 240:296] = original[y:y+56, x:x+56]
    image = Image.fromarray(original)
    piece = image.crop((x, y, x+56, y+56))
    if masked:
        from PIL import ImageDraw
        mask = Image.new("L", (56,56))
        ImageDraw.Draw(mask).ellipse((2,2,54,54), fill=255)
        piece.putalpha(mask)
    permutation = rng.permutation(66)
    atlas = Image.new("RGB", image.size)
    offsets = []
    for dest, source in enumerate(permutation):
        sx, sy = int(source % 33) * 11, int(source // 33) * 71
        dx, dy = dest % 33 * 11, dest // 33 * 71
        atlas.paste(image.crop((dx,dy,dx+11,dy+71)),(sx,sy))
        offsets.append([-sx, -sy])

    def encode(value, compressed=False):
        buffer = io.BytesIO()
        format = "JPEG" if compressed else "PNG"
        value.save(buffer, format=format, quality=95)
        return "data:image/" + format.lower() + ";base64," + base64.b64encode(buffer.getvalue()).decode()

    return {"data":{"point":offsets,"bgImg":encode(atlas, jpeg)}}, encode(piece), (x,y)
