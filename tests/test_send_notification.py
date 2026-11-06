import contextlib
import io
import json
import os
import unittest
import urllib.error
from unittest import mock

from scripts import send_notification


class SendNotificationTests(unittest.TestCase):
    def invoke(self, body, status=200):
        response = mock.Mock(status=status, headers={"Content-Type": "application/json"})
        response.geturl.return_value = "https://ntfy.sh/test-topic"
        response.read.return_value = body.encode()
        response.__enter__ = mock.Mock(return_value=response)
        response.__exit__ = mock.Mock(return_value=False)
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(send_notification.urllib.request, 'urlopen', return_value=response) as opener, \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = send_notification.main(['test-topic', 'Test message 😀'])
        return status, stdout.getvalue(), stderr.getvalue(), opener.call_args.args[0]

    def test_success_shows_server_reply_and_sends_utf8_with_optional_token(self):
        body = json.dumps(dict(id='test-id', event='message', topic='test-topic', message='Test message 😀'))
        with mock.patch.dict(os.environ, {'NTFY_TOKEN': 'private-token'}, clear=True):
            status, stdout, stderr, request = self.invoke(body)
        self.assertEqual(status, 0)
        self.assertIn('HTTP status: 200', stdout)
        self.assertIn('test-id', stdout)
        self.assertEqual(stderr, '')
        self.assertEqual(request.data, 'Test message 😀'.encode())
        self.assertEqual(request.get_header('Authorization'), 'Bearer private-token')
        self.assertNotIn('private-token', stdout)

    def test_rejects_html_empty_json_and_wrong_acknowledgments(self):
        good = dict(id='test-id', event='message', topic='test-topic', message='Test message 😀')
        bodies = ['<html>Proxy login</html>', '[]', '{}',
                  json.dumps(dict(good, topic='wrong-topic')),
                  json.dumps(dict(good, event='open')),
                  json.dumps(dict(good, id='')),
                  json.dumps(dict(good, message='')),
                  json.dumps(dict(good, message='unexpected text'))]
        for body in bodies:
            with self.subTest(body=body):
                status, stdout, stderr, _ = self.invoke(body)
                self.assertEqual(status, 1)
                self.assertIn(body, stdout)
                self.assertIn('Publish failed:', stderr)

    def test_http_error_shows_error_response_and_exits_unsuccessfully(self):
        error = urllib.error.HTTPError('https://ntfy.sh/test-topic', 403, 'Forbidden',
                                      {'Content-Type': 'application/json'}, io.BytesIO(b'{"error":"denied"}'))
        with mock.patch.object(send_notification.urllib.request, 'urlopen', side_effect=error), \
                contextlib.redirect_stdout(io.StringIO()) as stdout, \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            status = send_notification.main(['test-topic', 'hello'])
        self.assertEqual(status, 1)
        self.assertIn('403', stdout.getvalue())
        self.assertIn('denied', stdout.getvalue())
        self.assertIn('Publish failed:', stderr.getvalue())

    def test_custom_server_and_full_topic_url(self):
        with mock.patch.dict(os.environ, {'NTFY_SERVER_URL': 'https://notify.example'}, clear=True):
            self.assertEqual(send_notification.destination('topic'), ('https://notify.example/topic', 'topic'))
            self.assertEqual(send_notification.destination('https://other.example/my-topic'),
                             ('https://other.example/my-topic', 'my-topic'))
