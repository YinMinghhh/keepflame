"""Local, confidence-gated image matching; no files, service calls or telemetry."""

from __future__ import annotations

import base64
from contextlib import ExitStack
from dataclasses import dataclass
import io
import re


class MatchError(ValueError):
    pass


@dataclass(frozen=True)
class Match:
    x: int
    y: int
    confidence: float
    margin: float


def _image(uri: str):
    from PIL import Image

    if not isinstance(uri, str) or len(uri) > 1_400_000:
        raise MatchError("image_size")
    header, separator, encoded = uri.partition(",")
    if not separator or header not in ("data:image/png;base64", "data:image/jpeg;base64"):
        raise MatchError("image_format")
    try:
        raw = base64.b64decode(encoded, validate=True)
        with Image.open(io.BytesIO(raw)) as source:
            if source.format not in ("PNG", "JPEG") or not (0 < source.width <= 1024 and 0 < source.height <= 512):
                raise MatchError("image_dimensions")
            return source.convert("RGBA")
    except MatchError:
        raise
    except Exception as exc:
        raise MatchError("image_decode") from exc


def solve_challenge(payload: dict, piece_uri: str) -> Match:
    """Support the observed 33x2 tile layout; reject unknown/ambiguous images."""
    with ExitStack() as images:
        return _solve_challenge(payload, piece_uri, images)


def _solve_challenge(payload, piece_uri, images):
    import numpy as np
    from PIL import Image

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or not isinstance(data.get("point"), list):
        raise MatchError("challenge_shape")
    offsets = data["point"]
    if len(offsets) != 66:
        raise MatchError("unsupported_layout")
    atlas = _image(data.get("bgImg"))
    images.callback(atlas.close)
    if atlas.size != (363, 142):
        raise MatchError("unsupported_layout")
    canvas = Image.new("RGBA", atlas.size)
    images.callback(canvas.close)
    seen = set()
    for index, item in enumerate(offsets):
        if not isinstance(item, list) or len(item) != 2:
            raise MatchError("tile_offset")
        if any(type(v) is not int and not (isinstance(v, str) and re.fullmatch(r"-?[0-9]{1,3}", v)) for v in item):
            raise MatchError("tile_offset")
        x, y = (abs(int(v)) for v in item)
        if x % 11 or y % 71 or x + 11 > atlas.width or y + 71 > atlas.height or (x, y) in seen:
            raise MatchError("tile_offset")
        seen.add((x, y))
        with atlas.crop((x, y, x + 11, y + 71)) as tile:
            canvas.paste(tile, (index % 33 * 11, index // 33 * 71))
    piece = _image(piece_uri)
    images.callback(piece.close)
    if not (28 <= piece.width <= 224 and 28 <= piece.height <= 224):
        raise MatchError("piece_dimensions")
    piece = piece.resize((56, 56), Image.Resampling.LANCZOS)
    images.callback(piece.close)
    with canvas.convert("L") as grey:
        background = np.array(grey, dtype=np.float32)
    with piece.convert("L") as grey:
        patch = np.array(grey, dtype=np.float32)
    with piece.getchannel("A") as alpha:
        mask = np.array(alpha) >= 240
    # Sample a fixed lattice: bounded work even for untrusted image input.
    mask[1::2, :] = False
    mask[:, 1::2] = False
    ys, xs = np.nonzero(mask)
    if len(xs) < 128:
        raise MatchError("insufficient_pixels")
    template = patch[ys, xs]
    template = template - template.mean()
    energy = float(np.linalg.norm(template))
    if energy < 20:
        raise MatchError("flat_piece")
    scores = np.full((87, 308), -1.0, dtype=np.float32)
    positions = np.arange(scores.shape[1])[:, None]
    for y in range(scores.shape[0]):
        samples = background[y + ys[None, :], positions + xs[None, :]]
        samples -= samples.mean(axis=1, keepdims=True)
        denominator = np.linalg.norm(samples, axis=1) * energy
        scores[y] = np.divide(samples @ template, denominator, out=np.full(308, -1.0), where=denominator > 1)
    y, x = np.unravel_index(int(np.argmax(scores)), scores.shape)
    best = float(scores[y, x])
    alternatives = scores.copy()
    alternatives[max(0, y - 8):y + 9, max(0, x - 8):x + 9] = -1
    margin = best - float(alternatives.max())
    if not np.isfinite(best) or best < 0.90 or margin < 0.04:
        raise MatchError("ambiguous_match")
    return Match(int(x), int(y), round(best, 5), round(margin, 5))
