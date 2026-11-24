import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from core import Config, Engine, State, parse_command
from internet_features import _web_json


class WebAnswerTest(unittest.TestCase):
    def test_search_then_single_completion(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send_json(self, data):
                body = json.dumps(data).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                requests.append(('GET', self.path, None))
                if self.path == '/redirect':
                    self.send_response(302)
                    self.send_header('Location', '/private')
                    self.end_headers()
                    return
                self.send_json({'results': [
                    {'url': 'http://10.0.0.5/', 'content': 'Private result'},
                    {'url': 'http://44.31.120.5/', 'content': 'Local AMPR result'},
                    {'url': 'http://tracker.local/', 'content': 'Local hostname'},
                    {'url': 'https://www.spacex.com/launches/', 'content': 'The booster splashed down.'},
                ]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append(('POST', self.path, body))
                self.send_json({'choices': [{'message': {'content': 'The booster splashed down.'}}]})

        with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                url = f'http://127.0.0.1:{server.server_port}'
                engine = Engine(Config(searxng=url, llama=url))
                command = parse_command('infobot: !BOT, how did the last Starship launch go? !!g')
                with patch.dict('os.environ', {'http_proxy': 'http://127.0.0.1:1', 'no_proxy': ''}):
                    self.assertEqual(engine.execute(*command), 'The booster splashed down.')
                self.assertEqual([request[0] for request in requests], ['GET', 'POST'])
                get_url = urlsplit(requests[0][1])
                self.assertEqual(get_url.path, '/search')
                self.assertEqual(parse_qs(get_url.query), {
                    'q': ['how did the last Starship launch go?'],
                    'format': ['json'],
                    'categories': ['general'],
                })
                _, path, body = requests[1]
                self.assertEqual(path, '/v1/chat/completions')
                self.assertNotIn('tools', body)
                model_input = json.loads(body['messages'][1]['content'])
                self.assertEqual(model_input['search_results'], [{'content': 'The booster splashed down.'}])
                with self.assertRaisesRegex(ValueError, 'redirect rejected'):
                    _web_json(url + '/redirect', 1)
                self.assertEqual(len(requests), 3)
            finally:
                server.shutdown()
                thread.join()

    def test_empty_and_unsupported_searches_skip_model(self):
        self.assertIsNone(parse_command('bot where'))
        with patch('internet_features._web_json', return_value={'results': []}) as request:
            engine = Engine(Config())
            self.assertEqual(engine.execute(*parse_command('bot,')), 'Use bot, <question>.')
            request.assert_not_called()
            self.assertEqual(engine.execute(*parse_command('bot, where?')), 'No useful public web results.')
            request.assert_called_once()

    def test_long_unicode_answer_fits_radio_budget(self):
        config = Config(state=':memory:', byte_limit=80, parts=2)
        with patch('core.web_answer', return_value='🚀' * 150):
            reply = Engine(config).execute(*parse_command('bot, where?'))
        state = State(config.state)
        try:
            parts = state.page('tester', reply, config, 1)
            self.assertEqual(len(parts), config.parts)
            self.assertTrue(parts[-1].endswith('…'))
            self.assertTrue(all(len(part.encode()) <= config.byte_limit for part in parts))
            self.assertEqual(state.page('tester', None, config, 2), ['No more saved text.'])
        finally:
            state.db.close()
