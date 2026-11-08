"""Decodable image fixtures: receipts are verified with Pillow before storage."""
import hashlib
import io

from PIL import Image


def real_jpeg(seed=b'receipt', size=(64, 48)):
    """A real JPEG whose bytes depend on ``seed`` (distinct seeds → distinct files)."""
    digest = hashlib.sha256(seed if isinstance(seed, bytes) else str(seed).encode()).digest()
    image = Image.new('RGB', size, (digest[0], digest[1], digest[2]))
    for i in range(24):
        image.putpixel((i % size[0], (i * 7) % size[1]), (digest[i % 32], digest[(i + 5) % 32], digest[(i + 11) % 32]))
    output = io.BytesIO()
    image.save(output, format='JPEG', quality=90)
    return output.getvalue()
