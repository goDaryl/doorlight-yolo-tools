import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

from demo import generate
from web.server import make_handler


class ReviewIntegrationTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / '图像'
        generate(self.directory)
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.directory))
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.origin = f'http://127.0.0.1:{server.server_port}'

    def api(self, route, payload=None):
        headers = {'Origin': self.origin, 'Content-Type': 'application/json'}
        body = json.dumps(payload).encode() if payload is not None else None
        with urlopen(Request(self.origin + route, body, headers), timeout=5) as response:
            return json.load(response)

    def item(self, index=0):
        row = self.api('/api/catalog')['rows'][index]
        return {key: row[key] for key in ('name', 'revision')}

    def test_box_write_backup_and_stale_revision(self):
        item = self.item()
        path = (self.directory / item['name']).with_suffix('.json')
        original = path.read_bytes()
        data = {'items': [item], 'action': 'resize', 'index': 1,
                'bounds': [285, 105, 330, 150], 'label': 'light_off'}
        result = self.api('/api/box-edit', data)
        self.assertEqual(result['updated'], [item['name']])
        self.assertEqual((self.directory.parent / result['backup'] / path.name).read_bytes(), original)
        doc = json.loads(path.read_bytes())
        self.assertEqual((doc['shapes'][1]['label'], doc['shapes'][1]['points']),
                         ('light_off', [[285, 105], [330, 150]]))
        self.assertEqual(doc['checked'], False)
        self.assertEqual(self.api('/api/box-edit', data)['updated'], [])

        data = {'items': [self.item()], 'action': 'add', 'index': -1,
                'bounds': [284, 152, 330, 195], 'label': 'light_unknown'}
        self.assertEqual(self.api('/api/box-edit', data)['boxes'], 1)
        self.assertEqual(json.loads(path.read_bytes())['shapes'][-1]['label'], 'light_unknown')
        data = {'items': [self.item()], 'action': 'delete', 'index': 3}
        self.assertEqual(self.api('/api/box-edit', data)['boxes'], 1)
        self.assertEqual(len(json.loads(path.read_bytes())['shapes']), 3)

    def test_batch_scope_approval_and_external_refresh(self):
        initial = self.api('/api/catalog')
        first, second = self.item(), self.item(1)
        result = self.api('/api/relabel', {'items': [first, second], 'label': 'light_off', 'scope': 'right'})
        self.assertEqual(result['updated'], [first['name']])
        self.assertEqual([row['name'] for row in result['skipped']], [second['name']])
        first_path = (self.directory / first['name']).with_suffix('.json')
        first_doc = json.loads(first_path.read_bytes())
        self.assertEqual([s['label'] for s in first_doc['shapes']], ['door_open', 'light_on', 'light_off'])

        updated_first = self.item()
        approved = self.api('/api/approve', {'items': [updated_first, second]})
        self.assertEqual(approved['approved'], [first['name'], second['name']])
        first_doc['shapes'][1]['label'] = 'light_unknown'
        first_doc['checked'] = True
        first_path.write_text(json.dumps(first_doc), encoding='utf-8')
        delta = self.api('/api/catalog?since=' + initial['version'])
        self.assertTrue(delta['delta'])
        self.assertEqual([r['name'] for r in delta['rows']], [first['name'], second['name']])
        self.assertEqual(delta['rows'][0]['shapes'][1]['label'], 'light_unknown')


if __name__ == '__main__':
    unittest.main()
