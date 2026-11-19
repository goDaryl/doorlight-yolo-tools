import argparse
import hashlib
import json
import math
import mimetypes
import os
import re
import shutil
import tempfile
import unicodedata
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import uuid4


HOME = Path(__file__).resolve().parent
IMAGE_SUFFIXES = {'.jpeg', '.jpg', '.png', '.bmp', '.webp'}
MAX_REQUEST_BYTES = 4 * 1024 * 1024
MAX_SELECTION = 10000
REVISION = re.compile(r'[0-9a-f]{64}')
DOOR_LABELS = {'door_open', 'door_closed'}
LIGHT_LABELS = {'light_on', 'light_off', 'light_unknown'}


def read_annotation(image):
    entry = {'name': image.name, 'status': 'missing', 'shapes': [], 'width': 0, 'height': 0}
    try:
        path = image.with_suffix('.json')
        if path.resolve().parent != image.parent or path.is_symlink():
            raise ValueError('Unexpected annotation path')
        content = path.read_bytes()
        doc = json.loads(content.decode('utf-8-sig'))
        if not isinstance(doc, dict) or not isinstance(doc.get('shapes'), list):
            raise ValueError('Invalid annotation')
        width, height = int(doc['imageWidth']), int(doc['imageHeight'])
        if min(width, height) <= 0:
            raise ValueError('Invalid image dimensions')
        checked = doc.get('checked')
        status = 'approved' if checked is True else 'rejected' if checked == 'rejected' or type(checked) is int and checked == 2 else 'pending'
        shapes = []
        unusable = 0
        for shape in doc['shapes']:
            try:
                label = shape['label']
                points = shape['points']
                if not isinstance(label, str) or len(points) < 2:
                    raise ValueError('Invalid shape')
                xs, ys = zip(*points)
                if not all(math.isfinite(value) for value in (*xs, *ys)):
                    raise ValueError('Invalid coordinates')
                bounds = [min(xs), min(ys), max(xs), max(ys)]
                if bounds[0] == bounds[2] or bounds[1] == bounds[3]:
                    raise ValueError('Degenerate box')
                if shape.get('shape_type', 'rectangle') != 'rectangle':
                    raise ValueError('Unsupported shape type')
                shapes.append({'label': label, 'box': bounds})
            except (KeyError, TypeError, ValueError, OverflowError):
                unusable += 1
        entry.update(status=status, width=width, height=height, shapes=shapes, unusable=unusable)
        if not unusable:
            entry['revision'] = hashlib.sha256(content).hexdigest()
        return entry, doc, content
    except FileNotFoundError:
        pass
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        entry['status'] = 'invalid'
    return entry, None, None


class Catalog:
    def __init__(self, directory):
        self.directory = directory
        self.entries = {}
        self.invalidated = set()
        self.versions = deque(maxlen=4)
        self.lock = Lock()

    def invalidate(self, names):
        with self.lock:
            self.invalidated.update(names)

    def scan(self):
        images, signatures = [], {}
        with os.scandir(self.directory) as entries:
            for entry in entries:
                _, suffix = os.path.splitext(entry.name)
                suffix = suffix.lower()
                if suffix in IMAGE_SUFFIXES and entry.is_file(follow_symlinks=False):
                    images.append(entry.name)
                elif suffix == '.json':
                    try:
                        stat = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    signatures[os.path.normcase(entry.name)] = (entry.name, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino)
        updated = {}
        changed = len(images) != len(self.entries)
        for name in sorted(images, key=str.casefold):
            signature = signatures.get(os.path.normcase(os.path.splitext(name)[0] + '.json'))
            previous = self.entries.get(name)
            retry = previous and (previous[1]['status'] == 'invalid' or previous[1]['status'] == 'missing' and signature is not None)
            if previous and previous[0] == signature and name not in self.invalidated and not retry:
                updated[name] = previous
                continue
            row = read_annotation(self.directory / name)[0]
            if previous and previous[1] == row:
                row = previous[1]
            else:
                changed = True
            updated[name] = (signature, row)
        self.entries = updated
        self.invalidated.clear()
        if changed or not self.versions:
            self.versions.append((uuid4().hex, {name: entry[1] for name, entry in updated.items()}))

    def response(self, since=None):
        with self.lock:
            self.scan()
            version, current = self.versions[-1]
            payload = {'directory': str(self.directory), 'version': version}
            previous = next((rows for key, rows in self.versions if key == since), None)
            if previous is None:
                return {**payload, 'rows': list(current.values())}
            if since == version:
                return {**payload, 'delta': True, 'rows': [], 'removed': []}
            return {**payload, 'delta': True,
                    'rows': [row for name, row in current.items() if previous.get(name) != row],
                    'removed': [name for name in previous if name not in current]}


def local_image(directory, name):
    if (not name or any(c in name for c in '/\\:\x00') or name != name.rstrip(' .')
            or unicodedata.normalize('NFC', name) in {'.', '..'}):
        return None
    image = directory / name
    if (image.suffix.lower() not in IMAGE_SUFFIXES or image.is_reserved()
            or image.resolve().parent != directory or image.is_symlink() or not image.is_file()):
        return None
    return image


def parse_selection(data):
    items = data.get('items') if isinstance(data, dict) else None
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_SELECTION:
        raise ValueError('Invalid selection size')
    selection = {}
    names = set()
    for item in items:
        name, revision = (item.get('name'), item.get('revision')) if isinstance(item, dict) else (None, None)
        if (not isinstance(name, str) or not 1 <= len(name) <= 255 or name.casefold() in names
                or not isinstance(revision, str) or not REVISION.fullmatch(revision)):
            raise ValueError('Invalid selection item')
        names.add(name.casefold())
        selection[name] = revision
    return selection


def relabel_boxes(entry, doc, label, scope):
    is_door = label in DOOR_LABELS
    labels = DOOR_LABELS if is_door else LIGHT_LABELS
    indexes = [i for i, shape in enumerate(entry['shapes']) if shape['label'] in labels]
    if not indexes:
        return 0, '无门框' if is_door else '无灯框'
    if is_door and len(indexes) != 1:
        return 0, '有多个门框，需单独复核'
    if not is_door and scope != 'all':
        if len(indexes) != 2:
            return 0, '左右选择需要恰好两个灯框'
        def center(index):
            left, _, right, _ = entry['shapes'][index]['box']
            return (left + right) / 2
        left, right = sorted(indexes, key=center)
        if center(right) - center(left) <= 1:
            return 0, '两个灯框的左右位置难以区分'
        indexes = [left if scope == 'left' else right]
    changes = 0
    for i in indexes:
        shape = doc['shapes'][i]
        if shape['label'] != label:
            shape['label'] = label
            changes += 1
    return changes, None


def parse_box_edit(data, selection):
    action, index = data.get('action'), data.get('index')
    label = data.get('label')
    bounds = data.get('bounds')
    valid_label = isinstance(label, str) and label in DOOR_LABELS | LIGHT_LABELS
    if (len(selection) != 1 or action not in ('relabel', 'delete', 'resize', 'add')
            or type(index) is not int or index < (-1 if action == 'add' else 0)
            or action not in ('resize', 'add') and bounds is not None
            or action == 'delete' and label is not None
            or action == 'relabel' and not valid_label
            or action == 'resize' and label is not None and not valid_label
            or action == 'add' and not valid_label):
        raise ValueError('Invalid box edit')
    if action in ('resize', 'add'):
        if (not isinstance(bounds, list) or len(bounds) != 4
                or not all(type(value) in (int, float) and 0 <= value < math.inf for value in bounds)
                or bounds[0] + 2 > bounds[2] or bounds[1] + 2 > bounds[3]):
            raise ValueError('Invalid box bounds')
    return action, index, label, bounds


def resize_points(points, old_bounds, bounds):
    left, top, right, bottom = old_bounds
    corners = {(left, top), (right, top), (right, bottom), (left, bottom)}
    # 保留 X-AnyLabeling 的两点或四点格式和每个角点的顺序。
    if len(points) == 4 and set(map(tuple, points)) == corners or len(points) == 2:
        x_values = {left: bounds[0], right: bounds[2]}
        y_values = {top: bounds[1], bottom: bounds[3]}
        return [[x_values[x], y_values[y]] for x, y in points]
    raise ValueError('Unsupported rectangle points')


def edit_box(entry, doc, box_edit):
    action, index, label, bounds = box_edit
    if action == 'add':
        if index not in (-1, len(doc['shapes'])):
            return 0, '图片标注已变更，请重读标注后再添加'
        if bounds[2] > entry['width'] or bounds[3] > entry['height']:
            return 0, '框坐标超出图片边界，请重新绘制'
        doc['shapes'].append({
            'label': label,
            'score': None,
            'points': [[bounds[0], bounds[1]], [bounds[2], bounds[3]]],
            'group_id': None,
            'description': '',
            'difficult': False,
            'shape_type': 'rectangle',
            'flags': {},
            'attributes': {},
            'kie_linking': [],
        })
        return 1, None
    # 有版本号的标注中，每个展示框都与原 shapes 中的同一位置对应。
    if index >= len(entry['shapes']):
        return 0, '选中的框不存在，请重新选择'
    if action == 'delete':
        del doc['shapes'][index]
        return 1, None
    shape = doc['shapes'][index]
    changed = False
    if action == 'resize':
        if bounds[2] > entry['width'] or bounds[3] > entry['height']:
            return 0, '框坐标超出图片边界，请重新调整'
        old_bounds = entry['shapes'][index]['box']
        if bounds != old_bounds:
            try:
                shape['points'] = resize_points(shape['points'], old_bounds, bounds)
            except ValueError:
                return 0, '该矩形的点格式无法调整，请在标注软件中修改'
            changed = True
    if label is not None and shape['label'] != label:
        shape['label'] = label
        changed = True
    if not changed:
        return 0, None
    return 1, None


def save_annotations(directory, selection, label=None, scope='all', box_edit=None):
    updated_names, skipped = [], []
    box_count = unchanged = 0
    backup = None
    approving = label is None and box_edit is None
    for name, revision in selection.items():
        try:
            image = local_image(directory, name)
            if image is None:
                skipped.append({'name': name, 'reason': '图片不在当前目录'})
                continue
            entry, doc, original = read_annotation(image)
            if entry.get('revision') != revision:
                skipped.append({'name': name, 'reason': '标注已变更或不可编辑，请重读标注'})
                continue
            if approving:
                if entry['status'] != 'pending':
                    skipped.append({'name': name, 'reason': '只对未验收图片标记合格'})
                    continue
                doc['checked'] = True
                box_changes = 0
            else:
                box_changes, reason = edit_box(entry, doc, box_edit) if box_edit else relabel_boxes(entry, doc, label, scope)
                if reason:
                    skipped.append({'name': name, 'reason': reason})
                    continue
                if not box_changes:
                    unchanged += 1
                    continue
            updated = (json.dumps(doc, ensure_ascii=False, allow_nan=False, indent=2) + '\n').encode('utf-8')
            if backup is None:
                action = '验收' if approving else '单框修订' if box_edit else '分类修订'
                prefix = datetime.now().strftime(f'网页{action}备份_%Y%m%d_%H%M%S_')
                backup = Path(tempfile.mkdtemp(prefix=prefix, dir=directory.parent))
            path = image.with_suffix('.json')
            with (backup / path.name).open('xb') as stream:
                stream.write(original)
            fd, name_tmp = tempfile.mkstemp(suffix='.tmp', dir=directory)
            temporary = Path(name_tmp)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(updated)
                # 尽量避免覆盖两次读取之间由标注软件保存的内容。
                if path.read_bytes() != original:
                    skipped.append({'name': name, 'reason': '保存前标注已变更'})
                    continue
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
            updated_names.append(name)
            box_count += box_changes
        except (OSError, RuntimeError, ValueError):
            skipped.append({'name': name, 'reason': '无法备份或写入标注'})
    return {'approved' if approving else 'updated': updated_names, 'boxes': box_count,
            'unchanged': unchanged, 'skipped': skipped, 'backup': backup.name if backup else None}


def make_handler(directory):
    directory = directory.resolve(strict=True)
    write_lock = Lock()
    catalog = Catalog(directory)

    class ReviewHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send_data(self, content_type, content, status=200):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(content)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(content)

        def send_json(self, data, status=200):
            content = json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
            self.send_data('application/json; charset=utf-8', content, status)

        def valid_host(self):
            return self.headers.get('Host') in {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}

        def serve_image(self, name):
            image = local_image(directory, name)
            if image is None:
                return self.send_error(404)
            mime = mimetypes.guess_type(image.name)[0] or 'application/octet-stream'
            with image.open('rb') as stream:
                self.send_response(200)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(image.stat().st_size))
                self.send_header('Cache-Control', 'private, max-age=60')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.end_headers()
                shutil.copyfileobj(stream, self.wfile)

        def do_GET(self):
            if not self.valid_host():
                return self.send_error(403)
            url = urlsplit(self.path)
            route = url.path
            try:
                if route == '/':
                    self.send_data('text/html; charset=utf-8', (HOME / '复核.html').read_bytes())
                elif route == '/api/catalog':
                    since = parse_qs(url.query).get('since', [None])[0]
                    self.send_json(catalog.response(since))
                elif route.startswith('/image/'):
                    self.serve_image(unquote(route[len('/image/'):]))
                else:
                    self.send_error(404)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def do_POST(self):
            try:
                if not self.valid_host() or self.headers.get('Origin') != f'http://{self.headers.get("Host")}':
                    return self.send_json({'error': '只能从本机复核页面修改标注。'}, 403)
                route = urlsplit(self.path).path
                if route not in {'/api/approve', '/api/relabel', '/api/box-edit'}:
                    return self.send_error(404)
                if self.headers.get_content_type() != 'application/json':
                    return self.send_json({'error': '请求格式不正确。'}, 415)
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 1 <= length <= MAX_REQUEST_BYTES:
                        return self.send_json({'error': '选择为空或超出一次可提交的数量。'}, 413)
                    self.connection.settimeout(15)
                    data = json.loads(self.rfile.read(length))
                    selection = parse_selection(data)
                    label, scope, box_edit = None, 'all', None
                    if route == '/api/relabel':
                        label, scope = data.get('label'), data.get('scope')
                        if (not isinstance(label, str) or label not in DOOR_LABELS | LIGHT_LABELS
                                or scope not in ('all', 'left', 'right')
                                or label in DOOR_LABELS and scope != 'all'):
                            return self.send_json({'error': '请选择有效的门灯状态和修改范围。'}, 400)
                    elif route == '/api/box-edit':
                        box_edit = parse_box_edit(data, selection)
                except (ValueError, TimeoutError, RecursionError):
                    return self.send_json({'error': '选中的图片未能读取，请刷新页面再试。'}, 400)
                with write_lock:
                    result = save_annotations(directory, selection, label, scope, box_edit)
                    catalog.invalidate(result['approved' if route == '/api/approve' else 'updated'])
                self.send_json(result)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    return ReviewHandler


def main():
    parser = argparse.ArgumentParser(description='展示并批量验收 X-AnyLabeling 标注')
    parser.add_argument('directory', nargs='?', type=Path, default=HOME.parent / 'data' / '图像')
    parser.add_argument('--port', type=int, default=8768)
    args = parser.parse_args()
    directory = args.directory.resolve()
    if not directory.is_dir():
        parser.error(f'Image directory does not exist: {directory}. Supply a directory or run demo.py first.')
    with ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(directory)) as server:
        print(f'http://127.0.0.1:{server.server_port}/  {directory}', flush=True)
        server.serve_forever()


if __name__ == '__main__':
    main()
