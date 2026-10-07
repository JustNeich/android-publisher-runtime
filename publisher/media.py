"""Read ISO BMFF geometry/duration without transforming the approved original."""
import struct
from .config import UserError


def boxes(stream, end):
    while stream.tell() + 8 <= end:
        start = stream.tell()
        size, kind = struct.unpack('>I4s', stream.read(8))
        header = 8
        if size == 1:
            raw = stream.read(8)
            if len(raw) != 8:
                raise UserError('Повреждён MP4.')
            size = struct.unpack('>Q', raw)[0]; header = 16
        if size == 0:
            size = end - start
        if size < header or start + size > end:
            raise UserError('Повреждена структура MP4.')
        yield kind, start + header, start + size
        stream.seek(start + size)


def validate_shorts(path):
    size = path.stat().st_size
    if not 0 < size <= 2 * 1024 ** 3:
        raise UserError('MP4 должен быть не больше 2 ГБ.')
    duration, tracks, brand = None, [], False
    with path.open('rb') as stream:
        for kind, start, end in boxes(stream, size):
            if kind == b'ftyp':
                brand = True
            if kind != b'moov':
                continue
            stream.seek(start)
            for child, a, b in boxes(stream, end):
                stream.seek(a)
                if child == b'mvhd':
                    raw = stream.read(min(b - a, 40))
                    if len(raw) < 20:
                        raise UserError('Нет длительности MP4.')
                    if raw[0] == 1 and len(raw) >= 32:
                        scale, ticks = struct.unpack('>IQ', raw[20:32])
                    elif raw[0] == 0:
                        scale, ticks = struct.unpack('>II', raw[12:20])
                    else:
                        raise UserError('Неизвестная версия времени MP4.')
                    duration = ticks / scale if scale else None
                if child == b'trak':
                    width = height = 0
                    video = False
                    for leaf, x, y in boxes(stream, b):
                        stream.seek(x)
                        if leaf == b'tkhd':
                            if y - x < 84 or y - x > 1024:
                                raise UserError('Некорректная геометрия MP4.')
                            raw = stream.read(y - x)
                            width, height = (v / 65536 for v in struct.unpack('>II', raw[-8:]))
                            matrix = struct.unpack('>9i', raw[-44:-8])
                            if matrix[0] == 0 and matrix[4] == 0 and abs(matrix[1]) == 65536:
                                width, height = height, width
                        if leaf == b'mdia':
                            for atom, u, v in boxes(stream, y):
                                if atom == b'hdlr':
                                    stream.seek(u)
                                    video = stream.read(12)[8:12] == b'vide'
                        stream.seek(y)
                    if video:
                        tracks.append((width, height))
                stream.seek(b)
    if not brand or duration is None or not 0 < duration <= 180 or not tracks or any(w <= 0 or h <= 0 or w > h for w, h in tracks):
        raise UserError('Для Shorts нужен MP4 вертикального или квадратного формата длительностью до 3 минут. Исходник не изменяем.')
    return {'duration': duration, 'width': tracks[0][0], 'height': tracks[0][1], 'bytes': size, 'format': 'shorts', 'transformed': False}
