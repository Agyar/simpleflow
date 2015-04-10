import unittest
import os
import gc

from cdf.core.streams.cache import (
    FileStreamCache,
    BufferedStreamCache
)

# TODO test cbor
class TestStreamCache(unittest.TestCase):
    def setUp(self):
        self.data = [
            [1, 2, 'a'],
            [4, 5, 'b'],
            [7, 8, 'c']
        ]

    def test_harness(self):
        cache = FileStreamCache()
        cache.cache(iter(self.data))

        # first consume
        stream = cache.get_stream()
        self.assertEqual(list(stream), self.data)
        # second consume
        stream = cache.get_stream()
        self.assertEqual(list(stream), self.data)

    def test_empty_stream(self):
        cache = FileStreamCache()
        cache.cache([])
        self.assertEqual([], list(cache.get_stream()))

    def test_file_cleaning(self):
        path = '/tmp/cachefile'

        def test_cache():
            cache = FileStreamCache(path)
            cache.cache(iter(self.data))

        test_cache()
        gc.collect(2)  # force global GC
        self.assertFalse(os.path.exists(path))


class TestBufferedCache(unittest.TestCase):
    def setUp(self):
        self.data = [
            [1, 2, 'a'],
            [4, 5, 'b'],
            [7, 8, 'c']
        ]

    def test_harness(self):
        cache = BufferedStreamCache()
        cache.cache(iter(self.data))

        # first consume
        stream = cache.get_stream()
        self.assertEqual(list(stream), self.data)
        # second consume
        stream = cache.get_stream()
        self.assertEqual(list(stream), self.data)

    def test_buffer_size(self):
        cache = BufferedStreamCache(buffer_size=2)
        cache.cache(iter(self.data))

        # first consume
        stream = cache.get_stream()
        self.assertEqual(list(stream), self.data)
        # second consume
        stream = cache.get_stream()
        self.assertEqual(list(stream), self.data)

    def test_null_buffer_size(self):
        cache = BufferedStreamCache(buffer_size=0)
        cache.cache(iter(self.data))

        # first consume
        stream = cache.get_stream()
        self.assertEqual(list(stream), self.data)
        # second consume
        stream = cache.get_stream()
        self.assertEqual(list(stream), self.data)

    def test_large_buffer_size(self):
        cache = BufferedStreamCache(buffer_size=100)
        cache.cache(iter(self.data))

        # first consume
        stream = cache.get_stream()
        self.assertEqual(list(stream), self.data)
        # second consume
        stream = cache.get_stream()
        self.assertEqual(list(stream), self.data)