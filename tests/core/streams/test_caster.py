# -*- coding:utf-8 -*-
import unittest
import logging
from StringIO import StringIO

from cdf.log import logger
from cdf.utils.convert import _raw_to_bool
from cdf.core.streams.utils import split_file
from cdf.core.streams.caster import (
    BasicFieldCaster, MissingValueFieldCaster, Caster,
    MISSING_VALUE, MISSING_OPTION, DEFAULT_OPTION
)

logger.setLevel(logging.DEBUG)


class TestBasicFieldCaster(unittest.TestCase):
    def test_nominal_case(self):
        caster = BasicFieldCaster(int)
        self.assertEqual(10, caster.cast("10"))


class TestMissingValueFieldCaster(unittest.TestCase):
    def test_nominal_case(self):
        caster = MissingValueFieldCaster(int, {MISSING_OPTION: 0})
        self.assertEqual(10, caster.cast("10"))

    def test_missing_value(self):
        caster = MissingValueFieldCaster(int, {MISSING_OPTION: 0})
        self.assertEqual(0, caster.cast(MISSING_VALUE))

    def test_default_value(self):
        caster = MissingValueFieldCaster(int, {DEFAULT_OPTION: 0})
        self.assertEqual(0, caster.cast(MISSING_VALUE))

    def test_missing_priority_over_default(self):
        caster = MissingValueFieldCaster(
            int, {MISSING_OPTION: 0, DEFAULT_OPTION: 10}
        )
        self.assertEqual(0, caster.cast(MISSING_VALUE))

    def test_no_missing_no_default_value(self):
        caster = MissingValueFieldCaster(bool, {DEFAULT_OPTION: 0})
        self.assertEqual(False, caster.cast(MISSING_VALUE))

    def test_empty_value(self):
        caster = MissingValueFieldCaster(int, {DEFAULT_OPTION: 10})
        self.assertEqual(10, caster.cast(''))


class TestCaster(unittest.TestCase):

    def setUp(self):
        pass

    def tearDown(self):
        pass

    def test_caster(self):
        f = StringIO()
        f.write('1\thttp://www.site.com\t1\n')
        f.write('2\thttp://www.site.com/page.html\t0\n')
        f.write('3\thttp://www.site.com/another_page.html\t1\n')
        f.seek(0)

        INFOS_FIELDS = [('id', int),
                        ('url', str),
                        ('gzipped', _raw_to_bool)]
        cast = Caster(INFOS_FIELDS).cast
        urls = cast(split_file(f))
        expected_urls = [
            [1, 'http://www.site.com', True],
            [2, 'http://www.site.com/page.html', False],
            [3, 'http://www.site.com/another_page.html', True]
        ]
        self.assertEquals(list(urls), expected_urls)

    def test_empty_string(self):
        f = StringIO()
        f.write('1\t/some/thing\t\n')
        f.seek(0)

        FIELDS = [('id', int),
                  ('path', str),
                  ('query', str)]
        cast = Caster(FIELDS).cast
        urls = cast(split_file(f))
        expected = [
            [1, '/some/thing', ''],
        ]
        self.assertEquals(list(urls), expected)

    def test_empty_column(self):
        f = StringIO()
        f.write('1\thttp://www.site.com/\t\n')
        f.seek(0)

        FIELDS = [('id', int),
                  ('url', str),
                  ('lang', str, {'default': 'fr'})]
        cast = Caster(FIELDS).cast
        urls = cast(split_file(f))
        expected = [
            [1, 'http://www.site.com/', 'fr'],
        ]
        self.assertEquals(list(urls), expected)

        # Test with no missing value
        f2 = StringIO()
        f2.write('1\thttp://www.site.com/\ten\n')
        f2.seek(0)

        urls = cast(split_file(f2))
        expected = [
            [1, 'http://www.site.com/', 'en'],
        ]

        # Test with no default value
        f = StringIO()
        f.write('1\thttp://www.site.com/\t\n')
        f.seek(0)

        FIELDS = [('id', int),
                  ('url', str),
                  ('lang', str)]
        cast = Caster(FIELDS).cast
        urls = cast(split_file(f))
        expected = [
            [1, 'http://www.site.com/', ''],
        ]
        self.assertEquals(list(urls), expected)

    def test_bad_number_of_columns(self):
        # We plan to append columns over time to existing files
        # For old crawls, we need to add a default value for missing column
        f = StringIO()
        # There is only 2 columns instead of 3 needed :
        f.write('1\thttp://www.site.com/\n')
        f.seek(0)

        FIELDS = [('id', int),
                  ('url', str),
                  ('lang', str, {'missing': 'nolang'})]
        cast = Caster(FIELDS).cast
        urls = cast(split_file(f))
        expected = [
            [1, 'http://www.site.com/', 'nolang'],
        ]
        self.assertEquals(list(urls), expected)

        # Test with no missing value
        f = StringIO()
        f.write('1\thttp://www.site.com/\tfr\n')
        f.seek(0)

        cast = Caster(FIELDS).cast
        urls = cast(split_file(f))
        expected = [
            [1, 'http://www.site.com/', 'fr'],
        ]
        self.assertEquals(list(urls), expected)

    def test_cast_line_generator(self):
        fields = []
        caster = Caster(fields)
        cast_line = caster.cast_line_generator([int, float, _raw_to_bool])
        self.assertEqual([10, 3.14, True], cast_line(["10", "3.14", 1]))