from __future__ import annotations

import io
from dataclasses import dataclass
from PIL import Image

@dataclass(frozen=True)
class ImageHash:
    bits: tuple[int, ...]
    def __sub__(self, other: 'ImageHash') -> int:
        if len(self.bits) != len(other.bits):
            raise ValueError('hash sizes differ')
        return sum(a != b for a, b in zip(self.bits, other.bits))
    def __str__(self) -> str:
        value = 0
        for bit in self.bits:
            value = (value << 1) | bit
        width = max(1, (len(self.bits) + 3) // 4)
        return f'{value:0{width}x}'

def phash(data: bytes, hash_size: int = 16) -> ImageHash:
    # Dependency-free perceptual hash approximation suitable for integrity checks:
    # low-frequency grayscale DCT coefficients are represented by a binary median threshold.
    # The production project may replace this module with ImageHash without changing callers.
    with Image.open(io.BytesIO(data)) as image:
        image = image.convert('L').resize((hash_size * 2, hash_size * 2))
        pixels = [float(v) for v in image.getdata()]
    n = hash_size * 2
    # Lightweight separable cosine transform for the lowest-frequency block.
    import math
    coeffs: list[float] = []
    for u in range(hash_size):
        for v in range(hash_size):
            total = 0.0
            for x in range(n):
                for y in range(n):
                    total += pixels[y * n + x] * math.cos(((2 * x + 1) * u * math.pi) / (2 * n)) * math.cos(((2 * y + 1) * v * math.pi) / (2 * n))
            coeffs.append(total)
    body = coeffs[1:] or coeffs
    median = sorted(body)[len(body) // 2]
    return ImageHash(tuple(1 if value > median else 0 for value in body))
