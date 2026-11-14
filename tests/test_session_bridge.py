"""Offline queue admission and deduplication checks."""
import json
import sqlite3
import unittest

from bridge.config import Config
from session_bridge import receive


class SessionQueueTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(':memory:')
        self.addCleanup(self.connection.close)
        self.connection.execute('CREATE TABLE control (id INTEGER PRIMARY KEY, owner TEXT, enabled INTEGER)')
        self.connection.execute("INSERT INTO control VALUES (1, 'test-owner', 1)")
        self.connection.execute('CREATE TABLE messages (message_id TEXT PRIMARY KEY, chat_id TEXT, text TEXT, state TEXT, created REAL)')
        self.connection.commit()
        self.config = Config(app_id='test', app_secret='test', allow_open_ids=['user'], allow_chat_ids=['chat'])
        self.message = {
            'message_id': 'message', 'chat_id': 'chat', 'open_id': 'user',
            'chat_type': 'group', 'message_type': 'text',
            'content': json.dumps({'text': '@_bot hello'}),
            'mentions': [{'key': '@_bot', 'open_id': 'bot'}],
        }

    def test_authorized_message_is_queued_once(self):
        self.assertTrue(receive(self.connection, self.message, self.config, 'bot'))
        self.assertFalse(receive(self.connection, self.message, self.config, 'bot'))
        self.assertEqual(self.connection.execute('SELECT text, state FROM messages').fetchall(), [('hello', 'queued')])

    def test_both_allowlists_are_required(self):
        for attribute in ['allow_open_ids', 'allow_chat_ids']:
            with self.subTest(attribute=attribute):
                original = getattr(self.config, attribute)
                setattr(self.config, attribute, [])
                self.assertFalse(receive(self.connection, self.message, self.config, 'bot'))
                setattr(self.config, attribute, original)

    def test_missing_mention_or_bot_identity_is_rejected(self):
        self.assertFalse(receive(self.connection, self.message, self.config, ''))
        self.message['mentions'] = []
        self.assertFalse(receive(self.connection, self.message, self.config, 'bot'))

    def test_disabled_bridge_rejects_messages(self):
        self.connection.execute('UPDATE control SET enabled=0')
        self.connection.commit()
        self.assertFalse(receive(self.connection, self.message, self.config, 'bot'))

    def test_bot_cannot_submit_its_own_message(self):
        self.config.allow_open_ids = ['bot']
        self.message['open_id'] = 'bot'
        self.assertFalse(receive(self.connection, self.message, self.config, 'bot'))

    def test_empty_text_and_missing_message_id_are_rejected(self):
        self.assertFalse(receive(self.connection, dict(self.message, content='{}'), self.config, 'bot'))
        self.assertFalse(receive(self.connection, dict(self.message, message_id=''), self.config, 'bot'))


if __name__ == '__main__':
    unittest.main()
