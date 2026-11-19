import json
import struct
import zlib
from pathlib import Path


WIDTH, HEIGHT = 640, 400
ROOT = Path(__file__).resolve().parent


def png_chunk(tag, data):
    payload = tag + data
    return struct.pack('!I', len(data)) + payload + struct.pack('!I', zlib.crc32(payload))


def draw_scene(is_open, light, has_target, index):
    pixels = bytearray(WIDTH * HEIGHT * 3)

    def rect(x1, y1, x2, y2, rgb):
        line = bytes(rgb) * (x2 - x1)
        for y in range(y1, y2):
            start = (y * WIDTH + x1) * 3
            pixels[start:start + len(line)] = line

    tint = index * 3
    rect(0, 0, WIDTH, HEIGHT, (216 - tint, 215 - tint, 203 - tint))
    rect(0, 318, WIDTH, HEIGHT, (147, 144, 133))
    rect(0, 385, WIDTH, 387, (168, 161, 145))
    rect(0, 305, WIDTH, 318, (85, 91, 84))
    rect(35, 126, 155, 218, (120, 132, 126))
    rect(41, 132, 149, 212, (177, 184, 174))
    rect(52, 147, 130, 152, (134, 145, 133))
    rect(52, 166, 112, 171, (134, 145, 133))
    shapes = []

    def box(label, bounds):
        x1, y1, x2, y2 = bounds
        shapes.append({'label': label, 'points': [[x1, y1], [x2, y2]], 'group_id': 1,
                       'shape_type': 'rectangle', 'description': '', 'flags': {}})

    if has_target:
        x = 248 + (index % 3) * 9
        rect(x - 6, 63, x + 170, 357, (57, 58, 54))
        rect(x, 70, x + 164, 352, (118, 82, 57))
        rect(x + 5, 74, x + 102 if is_open else x + 158, 349, (153, 110, 76))
        rect(x + 78, 215, x + 98, 220, (226, 208, 179))
        glass = {'light_on': (243, 215, 153), 'light_off': (45, 54, 63),
                 'light_unknown': (119, 127, 129)}[light]
        rect(x + 22, 101, x + 77, 199, (75, 62, 53))
        rect(x + 26, 105, x + 73, 195, glass)
        rect(x + 29, 110, x + 32, 190, tuple(min(255, c + 19) for c in glass))
        box('door_open' if is_open else 'door_closed', (x - 6, 63, x + 170, 357))
        box(light, (x + 26, 105, x + 73, 195))
        if is_open:
            rect(x + 104, 74, x + 162, 349, glass)
            rect(x + 104, 279, x + 162, 349, (101, 92, 83))
            if light == 'light_on':
                rect(x + 110, 94, x + 157, 101, (255, 250, 220))
            box(light, (x + 104, 74, x + 162, 279))

    stride = WIDTH * 3
    scanlines = b''.join(b'\x00' + pixels[i:i + stride] for i in range(0, len(pixels), stride))
    png = b'\x89PNG\r\n\x1a\n'
    png += png_chunk(b'IHDR', struct.pack('!IIBBBBB', WIDTH, HEIGHT, 8, 2, 0, 0, 0))
    png += png_chunk(b'IDAT', zlib.compress(scanlines)) + png_chunk(b'IEND', b'')
    return png, shapes


def generate(directory):
    directory.mkdir(parents=True)
    scenes = [(True, 'light_on', True), (False, 'light_off', True),
              (False, 'light_on', True), (True, 'light_off', True),
              (False, 'light_unknown', True), (False, 'light_unknown', False)]
    for index, scene in enumerate(scenes, start=1):
        name = f'demo-{index:03d}'
        png, shapes = draw_scene(*scene, index)
        image = directory / f'{name}.png'
        image.write_bytes(png)
        annotation = {'version': '2.0.0', 'flags': {}, 'shapes': shapes, 'imagePath': image.name,
                      'imageData': None, 'imageHeight': HEIGHT, 'imageWidth': WIDTH,
                      'checked': index == 5, 'description': ''}
        image.with_suffix('.json').write_text(json.dumps(annotation, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    target = ROOT / 'data' / '演示' / '图像'
    if target.exists():
        print(f'演示目录已经存在，保留其中的修改：{target}')
    else:
        generate(target)
        print(f'已生成 6 张合成演示图及对应标注：{target}')
