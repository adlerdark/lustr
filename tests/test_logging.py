"""
Logging (logger.py): print() routed into logging once, levels, the quiet access log, recent problems.

    docker exec -w /app lustr python3 -m unittest tests.test_logging -v
"""
import io
import logging
import os
import sys
import tempfile
import unittest

os.environ.setdefault('LUSTR_DATA_DIR', tempfile.mkdtemp(prefix='lustr-test-'))   # before config is imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logger                                          # noqa: E402


class Capture:
    """Collect what reaches the root logger while active."""

    def __enter__(self):
        self.stream = io.StringIO()
        self.handler = logging.StreamHandler(self.stream)
        self.handler.setFormatter(logging.Formatter('%(levelname)s %(name)s: %(message)s'))
        logging.getLogger().addHandler(self.handler)
        return self

    def __exit__(self, *exc):
        logging.getLogger().removeHandler(self.handler)

    @property
    def lines(self):
        return [l for l in self.stream.getvalue().split('\n') if l]


class PrintRouting(unittest.TestCase):
    def test_print_becomes_one_log_line_named_after_the_module(self):
        with Capture() as c:
            print('scan finished:', 12, 'files')
        self.assertEqual(c.lines, ['INFO lustr.test_logging: scan finished: 12 files'])

    def test_levels_from_the_wording(self):
        cases = {'Error saving data: disk full': 'ERROR', 'thumbnail failed for x': 'ERROR',
                 '⚠ ext_db: image too big': 'WARNING', "could not read the saved queue": 'WARNING',
                 'Imported 3 videos': 'INFO', 'run finished with 0 errors': 'INFO'}
        for text, level in cases.items():
            self.assertEqual(logging.getLevelName(logger._level_of(text)), level, text)

    def test_blank_prints_are_dropped(self):
        with Capture() as c:
            print('')
            print('   ')
        self.assertEqual(c.lines, [])

    def test_print_to_a_file_still_writes_the_file(self):
        buf = io.StringIO()
        print('to a file', file=buf)
        self.assertEqual(buf.getvalue(), 'to a file\n')

    def test_debug_is_hidden_at_info(self):
        logger._apply_level('INFO')
        with Capture() as c:
            logging.getLogger('lustr.x').debug('detail')
        self.assertEqual(c.lines, [])


class AccessLog(unittest.TestCase):
    def rec(self, path, status=200):
        return logging.LogRecord('uvicorn.access', logging.INFO, '', 0, '%s - "%s %s HTTP/%s" %d',
                                 ('1.2.3.4:5', 'GET', path, '1.1', status), None)

    def test_routine_polling_is_left_out(self):
        f = logger._AccessFilter()
        for p in ('/api/activity', '/api/health', '/cache/ab/thumb.jpg?v=1', '/api/images/12/thumb?v=3',
                  '/api/movies/5/cover?v=x', '/api/movies/bulk/status', '/api/player/hls/original/3.ts?path=x',
                  '/static/vendor/x.js', '/api/watch/progress'):
            self.assertFalse(f.filter(self.rec(p)), p)

    def test_real_requests_and_errors_are_kept(self):
        f = logger._AccessFilter()
        self.assertTrue(f.filter(self.rec('/api/videos?library=x')))
        self.assertTrue(f.filter(self.rec('/api/health', 500)))
        self.assertTrue(f.filter(self.rec('/cache/x.jpg', 404)))


class Problems(unittest.TestCase):
    def test_warnings_and_errors_are_kept_with_their_id(self):
        logging.getLogger('lustr.t').error('boom', extra={'error_id': 'abc123'})
        logging.getLogger('lustr.t').info('fine')
        last = logger.recent_problems(1)[-1]
        self.assertEqual((last['level'], last['error_id']), ('ERROR', 'abc123'))
        self.assertIn('boom', last['text'])

    def test_level_setting_is_saved(self):
        try:
            self.assertEqual(logger.set_level('debug'), 'DEBUG')
            self.assertEqual(logger.current_level(), 'DEBUG')
            self.assertEqual(logging.getLogger().level, logging.DEBUG)
            with self.assertRaises(ValueError):
                logger.set_level('loud')
        finally:
            logger.set_level('INFO')


if __name__ == '__main__':
    unittest.main()
