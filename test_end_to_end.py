import csv
import json
import os
import unittest
from artemis import VersionDetector, get_logger

WORKING_FOLDER = os.path.join(os.path.expanduser("~"), "Documents", "artemis-wd")
TEST_FILES_BASEDIR = "./test_files"
TEST_FILES_INDEX = os.path.join(TEST_FILES_BASEDIR, 'test_files.csv')
with open(TEST_FILES_INDEX) as csvfile:
    TEST_FILES = list(csv.DictReader(csvfile))

logger = get_logger()

def get_test_file(index):
    """
    :param index: integer indicating the index value of the desired row
    in test_files.csv
    :return: the path to the desired test file.
    """
    return os.path.join(TEST_FILES_BASEDIR, 'files', TEST_FILES[index]['filename'])


def run_test(index, test_case_instance):
    test_file = get_test_file(index)
    vd = VersionDetector(
        test_file,
        dec_ms_title=TEST_FILES[index]['title'],
        dec_version=TEST_FILES[index]['version'],
        working_folder=WORKING_FOLDER,
    )
    result = json.loads(vd.detect())
    logger.info(result)
    test_case_instance.assertEqual(TEST_FILES[index]['approve_deposit'], str(result['approve_deposit']))
    test_case_instance.assertTrue(result['check_results']['sanity_check'])
    test_case_instance.assertTrue(result['test_results']['test_length_of_extracted_text'])
    test_case_instance.assertTrue(result['test_results']['test_title_match_in_extracted_text'])


class TestVersionDetector(unittest.TestCase):
    """
    End to end runs on test files
    """
    def test_docx_am_001(self):
        run_test(0, self)

    def test_docx_am_002(self):
        run_test(2, self)

    def test_docx_am_003(self):
        run_test(7, self)

    def test_docx_am_004(self):
        run_test(12, self)

    def test_pdf_am_001(self):
        run_test(3, self)

    def test_pdf_vor_001(self):
        run_test(1, self)

    def test_pdf_vor_002(self):
        run_test(9, self)
