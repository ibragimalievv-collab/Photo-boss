"""Lossless, bounded receipt attachment set; one-file history stays unchanged.

The internal envelope is storage/transport, never a third-party archive format.
Canonical ordering makes a two-file request independent of selection order.
Every document is decoded and validated separately before storage or AI I/O.
"""
import hashlib
import json
import struct

MAGIC = b'PBRCPT1\n'
MAX_FILE = 8 * 1024 * 1024
MAX_SET = 2 * MAX_FILE + 2048


def pack(documents):
    if not isinstance(documents, (list, tuple)) or not 1 <= len(documents) <= 2:
        raise ValueError('К одному платежу можно прикрепить один или два файла.')
    if any(not isinstance(item, bytes) or not 100 <= len(item) <= MAX_FILE
           or item.startswith(MAGIC) for item in documents):
        raise ValueError('Каждый файл чека должен быть непустым и не больше 8 МБ.')
    unique = {hashlib.sha256(item).hexdigest(): item for item in documents}
    ordered = sorted(unique.items())
    if len(ordered) == 1:
        return ordered[0][1]
    header = json.dumps([{'size': len(raw), 'sha256': digest} for digest, raw in ordered],
                        separators=(',', ':'), sort_keys=True).encode()
    return MAGIC + struct.pack('!I', len(header)) + header + b''.join(raw for _, raw in ordered)


def unpack(raw):
    if not isinstance(raw, bytes) or len(raw) > MAX_SET:
        raise ValueError('Превышен размер вложений чека: до двух файлов по 8 МБ.')
    if not raw.startswith(MAGIC):
        if not 100 <= len(raw) <= MAX_FILE:
            raise ValueError('Каждый файл чека должен быть не больше 8 МБ.')
        return [raw]
    try:
        cursor = len(MAGIC)
        size = struct.unpack('!I', raw[cursor:cursor + 4])[0]
        cursor += 4
        if not 2 <= size <= 1024:
            raise ValueError
        entries = json.loads(raw[cursor:cursor + size])
        cursor += size
        if not isinstance(entries, list) or not 1 <= len(entries) <= 2:
            raise ValueError
        documents = []
        for entry in entries:
            if (not isinstance(entry, dict) or set(entry) != {'size', 'sha256'}
                    or type(entry['size']) is not int or not 100 <= entry['size'] <= MAX_FILE):
                raise ValueError
            document = raw[cursor:cursor + entry['size']]
            cursor += entry['size']
            if (len(document) != entry['size'] or hashlib.sha256(document).hexdigest() != entry['sha256']
                    or document.startswith(MAGIC)):
                raise ValueError
            documents.append(document)
        if cursor != len(raw):
            raise ValueError
        return documents
    except (ValueError, KeyError, TypeError, struct.error, UnicodeDecodeError):
        raise ValueError('Вложения чека повреждены. Выберите файлы заново.') from None


def canonical(raw):
    return pack(unpack(raw))


def manifest(raw):
    from .receipt_ai import document_format
    result = []
    for content in unpack(canonical(raw)):
        _, mime = document_format(content)
        result.append({'sha256': hashlib.sha256(content).hexdigest(), 'size': len(content), 'mime': mime})
    return result
