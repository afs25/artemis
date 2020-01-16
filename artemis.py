#!/usr/bin/env python3

__version__ = '2019.10'
__author__ = 'André Sartori'

import argparse
import chardet
from difflib import SequenceMatcher
import docx2txt
import json
import logging
import logging.config
import math
import os
import regex
import requests
import shelve
import shutil
import statistics
import subprocess
import sys
import textract
import xml.etree.ElementTree as ET
from collections import Counter

from docx import Document
from io import StringIO, BytesIO
from pprint import pprint
from PyPDF2 import PdfFileReader, utils
from PIL import Image
import imagehash
from tempfile import TemporaryDirectory, mkdtemp

from utils.common import get_logger
from utils.constants import SMUR, AM, P, VOR
from utils.patterns import DOI_PATTERN, ALL_CC_LICENCES, RIGHTS_RESERVED_PATTERNS, VERSION_PATTERNS
from utils.logos import PublisherLogo


# # logging.config.fileConfig('logging.conf', defaults={'logfilename': 'artemis.log'})
# logger = logging.getLogger(__name__)
logger = get_logger()


# LOGOS_DB_PATH = os.path.join(os.path.realpath(__file__), "utils", "logos_db.shelve_BKUP") # for some reason, this doesn't
# work with line: with shelve.open("utils/logos_db.shelve_BKUP") as db:
LOGOS_DB_PATH = "utils/logos_db.shelve_BKUP"

NUMBER_OF_CHARACTERS_IN_ONE_PAGE = 2600

PUBLISHER_PDF_METADATA_TAGS = [
    '/CrossMarkDomains#5B1#5D',
    '/CrossMarkDomains#5B2#5D',
    '/CrossmarkDomainExclusive',
    '/CrossmarkMajorVersionDate',
    '/doi',
    '/ElsevierWebPDFSpecifications',
    '/Keywords',
]

DOI_BASE_URL = "https://doi.org/"

NUMBER_PATTERN = regex.compile("\d+")


class ArtemisResult:
    """
    Handles the output of Artemis
    """
    possible_versions = [SMUR, AM, P, VOR]
    smur_prob = 194
    am_prob = 5182
    p_prob = 32
    vor_oa_prob = 3286
    vor_pw_prob = 866
    test_results = {}
    # high-level checks and info
    sanity_check = None  # change to True if paper title is what we expect
    approve_deposit = False  # change to True only if we are confident deposit can go ahead without moderation
    reason = None
    # individual tests
    long_enough = None
    title_match_file_metadata = None
    title_match_extracted_text = None
    extracted_publisher_tags_in_file_metadata = None
    valid_doi_in_extracted_text = None
    cc_match_extracted_text = None
    valid_doi_in_cermine_xml = None
    title_match_cermine_xml = None
    image_on_first_page = None
    detected_logos = None

    def __init__(self, input_filename):
        self.input_filename = input_filename

    def append_test_result(self, test_func, result):
        self.test_results.update({test_func.__name__: result})
        return result

    def json_response(self):
        return json.dumps({
            'input_file': self.input_filename,
            'approve_deposit': self.approve_deposit,
            'reason': self.reason,
            'version_confidence': {
                'SMUR': self.smur_prob / 100,
                'AM': self.am_prob / 100,
                'P': self.p_prob / 100,
                'VOR': (self.vor_oa_prob + self.vor_pw_prob) / 100,
                'VOR_oa': self.vor_oa_prob / 100,
                'VOR_pw': self.vor_pw_prob / 100,
            },
            'check_results': {
              'sanity_check': self.sanity_check,
            },
            'test_results': self.test_results,
        })

    def exclude_versions(self, e_list):
        """
        Excludes versions in e_list from self.possible_versions
        :param e_list: List of versions to exclude
        :return: filtered self.possible_versions
        """
        for v in e_list:
            if v in self.possible_versions:
                self.possible_versions.remove(v)
        return self.possible_versions


# region parsers
class BaseParser:
    """
    Parser with common methods shared by all inheriting classes
    """
    def __init__(self, file_path, dec_ms_title=None, dec_version=None, dec_authors=None, **kwargs):
        '''

        :param file_path: Path to file this class will evaluate
        :param dec_ms_title: Declared title of manuscript
        :param dec_version: Declared manuscript version of file
        :param dec_authors: Declared authors of manuscript (list)
        :param kwargs: Dictionary of citation details and any other known metadata fields; values may include:
            acceptance_date=None, doi=None, publication_date=None, title=None
        '''
        self.file_path = file_path
        self.file_name = os.path.basename(self.file_path)
        self.file_dirname = os.path.dirname(self.file_path)
        self.file_ext = os.path.splitext(self.file_path)[-1].lower()
        self.file = open(self.file_path)
        self.dec_ms_title = dec_ms_title
        self.dec_version = dec_version
        self.dec_authors = dec_authors
        self.metadata = kwargs

        self.extracted_text = None
        self.doi_in_extracted_text = None
        self.number_of_pages = None
        self.file_metadata = None

    def extract_text(self, method=None):
        '''
        Extracts text from file using textract (https://textract.readthedocs.io/en/stable/python_package.html)
        :return:
        '''

        try:
            if method:
                self.extracted_text = textract.process(self.file_path, method=method)
            else:
                self.extracted_text = textract.process(self.file_path)
        except UnicodeDecodeError:
            logger.error("Textract failed with UnicodeDecodeError")
            return None

        if isinstance(self.extracted_text, str):
            result = chardet.detect(self.extracted_text)
            self.extracted_text = self.extracted_text.decode(result['encoding'])
        else:
            logger.error("extracted_text is a {} instance; only strings are currently "
                         "supported".format(type(self.extracted_text)))
        return self.extracted_text

    def find_match_in_extracted_text(self, query=None, escape_char=True, expected_span=(0, 2600),
                                     allowed_error_ratio=.1):
        """
        Fuzzy search extracted text.
        :param query: Search string; manuscript title by default
        :param escape_char: Escape query characters that have a special regex meaning
        :param expected_span: Tuple indicating start and end characters of sector we expect to find string. For example,
            if we are searching for an article title, we would expect it to appear in the first page of the document.
            An uninterrupted page of text contains about 1300 words, so the first page should span less than
            2600 characters. This is the arbitrarily set default, but we could obtain a median empirically
        :param allowed_error_ratio: By default, a number of errors equal to 20% the length of the search string is
            allowed
        :return:
        """
        if not query:
            query = self.dec_ms_title
        if escape_char:
            query = regex.escape(query)
        if not allowed_error_ratio:
            pattern = query
        else:
            pattern = "{}{{e<{}}}".format(query, int(allowed_error_ratio*len(query)))
        if not self.extracted_text:
            self.extract_text()
        # remove all line breaks from extracted text; otherwise match will often fail
        continuous_text = self.extracted_text.replace('\n', ' ').replace('  ', ' ')
        try:
            logger.debug("pattern: {}".format(pattern))
            m = regex.search(pattern, continuous_text, flags=regex.IGNORECASE)
            if m:
                logger.debug("Match object: {}".format(m))
                match_in_expected_position = False
                if (m.start() >= expected_span[0]) and (m.end() <= expected_span[1]):
                    match_in_expected_position = True
                return {'match': m.group(), 'match in expected position': match_in_expected_position}
        except TypeError:
            logger.error("Attempt to find match in extracted_text failed because it is not a string.")
        return None

    def find_doi_in_extracted_text(self):
        self.doi_in_extracted_text = self.find_match_in_extracted_text(query=DOI_PATTERN, escape_char=False, allowed_error_ratio=0)
        return self.doi_in_extracted_text

    def find_cc_statement_in_extracted_text(self):
        for l in ALL_CC_LICENCES:
            for key, error_ratio in [('url', 0), ('long name', 0.1), ('short name', 0)]:
                m = self.find_match_in_extracted_text(query=l[key], escape_char=False,
                                                      allowed_error_ratio=error_ratio)
                if m:
                    logger.debug("Found Creative Commons statement in extracted text: {}".format(m['match']))
                    return m
        logger.debug("Could not find a Creative Commons statement in extracted text")
        return None

    def convert_to_pdf(self):
        '''
        Converts file to PDF using pandoc (https://pandoc.org/)
        :return:
        '''
        subprocess.run(['pandoc', self.file_path, '--latex-engine=xelatex', '-o',
                        self.file_path.replace(self.file_ext, '.pdf')], check=True)

    def detect_funding(self):
        pass

    def test_title_match_in_file_metadata(self, title_key, min_similarity=0.9):
        """
        Test if there is a match for declared title in file's metadata
        :param title_key: value of key for title field in self.metadata
        :param min_similarity: Minimum similarity for which a match will be accepted
        :return: True for match found; False for no match; None if test could not be performed
        """
        if self.dec_ms_title:
            if title_key in self.file_metadata.keys():
                if SequenceMatcher(None, self.file_metadata[title_key], self.dec_ms_title).ratio() >= min_similarity:
                    logger.debug("Found declared title in file metadata with a similarity of {}".format(min_similarity))
                    return True
                else:
                    logger.debug("Declared title could not be found in file metadata with a"
                                 " similarity of {}".format(min_similarity))
                    return False
            else:
                logger.error("File metadata does not contain title field, so cannot test match")
        else:
            logger.error("No declared title (self.dec_ms_title), so cannot test match")
        return None

    def test_title_match_in_extracted_text(self):
        """
        Test if there is a match for declared title in extracted text
        :return: True for match found; False for no match; None if test could not be performed
        """
        if self.dec_ms_title:
            if self.find_match_in_extracted_text():
                logger.debug("Found declared title (or similar) in extracted text")
                return True
            else:
                logger.debug("Could not find declared title (or similar) in extracted text")
                return False
        else:
            logger.error("No declared title (self.dec_ms_title), so cannot test match")
        return None

    def test_length_of_extracted_text(self, min_length=3*NUMBER_OF_CHARACTERS_IN_ONE_PAGE):
        """
        Test if extracted plain text has at list min_length characters
        :param min_length: minimum number of characters for test to succeed
        :return:
        """
        if self.extracted_text:
            if len(self.extracted_text) >= min_length:
                logger.debug("Extracted text is longer than {} characters".format(min_length))
                return True
            else:
                logger.debug("Extracted text is shorter than {} characters".format(min_length))
                return False
        logger.error("Extracted text unavailable (self.extracted_text), so could not perform test")
        return None

    def test_doi_resolves(self, doi=None):
        """
        Test if DOI resolves; if no DOI given, attempt to use declared DOI in self.metadata
        :param doi: DOI to check
        :return:
        """
        if not doi:
            try:
                doi = self.metadata['doi']
            except KeyError:
                logger.debug("DOI not known; KeyError for self.metadata['doi']")
                return None
        r = requests.get(DOI_BASE_URL + doi, headers={'User-Agent': 'Mozilla/5.0'})
        # r = requests.get("https://www.sciencedirect.com/science/article/pii/S1568786419302216?via%3Dihub", headers={'User-Agent': 'Mozilla/5.0'})
        logger.debug("r.status_code = {}; r.text = {}".format(r.status_code, r.text))
        if r.ok:
            return True
        else:
            return False

class DocxParser(BaseParser):
    """
    Parser for .docx files
    """
    def extract_file_metadata(self):
        '''
        Extracts the metadata of a .docx file
        :return:
        '''
        docx = Document(docx=self.file_path)
        self.file_metadata = {
            'author': docx.core_properties.author,
            'created': docx.core_properties.created,
            'last_modified_by': docx.core_properties.last_modified_by,
            'last_printed': docx.core_properties.last_printed,
            'modified': docx.core_properties.modified,
            'revision': docx.core_properties.revision,
            'title': docx.core_properties.title,
            'category': docx.core_properties.category,
            'comments': docx.core_properties.comments,
            'identifier': docx.core_properties.identifier,
            'keywords': docx.core_properties.keywords,
            'language': docx.core_properties.language,
            'subject': docx.core_properties.subject,
            'version': docx.core_properties.version,
            'keywords': docx.core_properties.keywords,
            'content_status': docx.core_properties.content_status,
        }

    def extract_text(self, method=None):
        """
        Overwrites extract_text function of BaseParser to use docx2txt instead
        """
        self.extracted_text = docx2txt.process(self.file_path)
        return self.extracted_text

    def parse(self):
        """
        Workflow for DOCX files
        :return: Tuple where: first element is string "success" or "fail" to indicate outcome; second element is
            string containing details
        """
        # plausible_versions = ['submitted version', 'accepted version', SMUR, AM]  # use ArtemisResult.possible_versions instead
        approve_deposit = False
        reason = ""
        r = ArtemisResult(self.file_name)
        r.exclude_versions([P, VOR])
        r.smur_prob = 5000
        r.am_prob = 5000
        r.p_prob = 0
        r.vor_oa_prob = 0
        r.vor_pw_prob = 0

        self.extract_file_metadata()
        r.title_match_file_metadata = r.append_test_result(
            self.test_title_match_in_file_metadata,
            self.test_title_match_in_file_metadata('title'),
        )

        self.extract_text()
        r.long_enough = r.append_test_result(
            self.test_length_of_extracted_text,
            self.test_length_of_extracted_text(),
        )

        r.title_match_extracted_text = r.append_test_result(
            self.test_title_match_in_extracted_text,
            self.test_title_match_in_extracted_text(),
        )

        if r.long_enough and (r.title_match_file_metadata or r.title_match_extracted_text):
            r.sanity_check = True
            if self.dec_version.lower() in r.possible_versions:
                r.approve_deposit = True
                r.reason = "Declared version is plausible"
            else:
                r.reason = "This is either a submitted or accepted version, " \
                       "but declared version is {}".format(self.dec_version)
        else:
            if r.long_enough:
                r.reason = "Could not find declared title ({}) in file {}".format(self.dec_ms_title, self.file_name)
            else:
                r.reason = "File {} is quite short for a journal article. Please check.".format(self.file_name)

        return r.json_response()


class PdfParser(BaseParser):
    #TODO: If pdf metadata field '/Creator' == publisher name, PDF is proof/published version
    #TODO: Investigate identifying watermark http://blog.uorz.me/2018/06/19/removeing-watermark-with-PyPDF2.html
    """
    Parser for .pdf files
    """
    def __init__(self, file_path, dec_ms_title=None, dec_version=None, dec_authors=None, **kwargs):
        self.cerm_ran_and_parsed = False
        self.cerm_doi = None
        self.cerm_title = None
        self.cerm_journal_title = None
        super(PdfParser, self).__init__(file_path, dec_ms_title=dec_ms_title,
                                        dec_version=dec_version, dec_authors=dec_authors, **kwargs)

    def extract_file_metadata(self):
        '''
        Extracts the metadata of a PDF file. For more information on PDF metadata tags, see
        https://www.sno.phy.queensu.ca/~phil/exiftool/TagNames/PDF.html
        :return:
        '''
        with open(self.file_path, 'rb') as f:
            pdf = PdfFileReader(f, strict=False)
            info = pdf.getDocumentInfo()
            # pprint(pdf.getPage(0).extractText())
            logger.debug("output of pdf.getDocumentInfo(): {}".format(info))
            self.number_of_pages = pdf.getNumPages()
            self.file_metadata = info

    def extract_text(self):
        try:
            super(PdfParser, self).extract_text()
        except TypeError:
            logger.warning("Text extraction of PDF using textract default method failed; "
                           "trying again with pdfminer method")
            super(PdfParser, self).extract_text(method='pdfminer')
        if not isinstance(self.extracted_text, str):
            logger.warning("Text extraction of PDF using textract failed; "
                           "using cermine instead")
            self.cermine_file()
            cermtxt_path = self.file_path.replace(self.file_ext, ".cermtxt")
            if os.path.exists(cermtxt_path):
                with open(cermtxt_path) as f:
                    self.extracted_text = f.read()
            else:
                logger.error("Cermine failed to extract text; using pdftotext instead")
                subprocess.run(["pdftotext", self.file_path], check=True)
                txt_path = self.file_path.replace(self.file_ext, ".txt")
                with open(txt_path) as f:
                    self.extracted_text = f.read()

    def cermine_file(self):
        '''
        Runs CERMINE (https://github.com/CeON/CERMINE) on pdf file. Useful presentation:
        https://www.slideshare.net/dtkaczyk/tkaczyk-grotoap2slides
        :return:
        '''
        try:
            subprocess.run(["java", "-cp", "cermine-impl-1.13-jar-with-dependencies.jar",
                        "pl.edu.icm.cermine.ContentExtractor", "-path", self.file_dirname, "-outputs",
                        # '"jats,text"'
                        # '"trueviz"'
                        '"jats,text,zones,trueviz,images"'
                        ],
                       check=True)
        except subprocess.CalledProcessError as e:
            logger.error("return code: {}; output: {}".format(e.returncode, e.output))

    def parse_cermxml(self):
        cermxml_path = self.file_path.replace(self.file_ext, ".cermxml")
        if os.path.exists(cermxml_path):
            with open(cermxml_path) as f:
                tree = ET.parse(f)
            root = tree.getroot()
            # extract DOI
            for c_id in root.iter('article-id'):
                if c_id.get('pub-id-type') == 'doi':
                    if self.cerm_doi is not None:
                        logger.warning("Previously detected DOI {} will be overwritten by value {}".format(self.cerm_doi,
                                                                                                    c_id.text))
                    self.cerm_doi = c_id.text

            # extract title
            for c_title in root.find('front').iter('article-title'):
                if self.cerm_title is not None:
                    logger.warning("Previously detected title '{}' will be overwritten by value '{}'".format(self.cerm_title,
                                                                                                       c_title.text))
                self.cerm_title = c_title.text

            # extract journal title
            for c_journal in root.iter('journal-title'):
                if self.cerm_journal_title is not None:
                    logger.warning("Previously detected journal title '{}' will be overwritten by"
                                   " value '{}'".format(self.cerm_journal_title, c_journal.text))
                self.cerm_journal_title = c_journal.text

            self.cerm_ran_and_parsed = True
            return self.cerm_doi, self.cerm_title, self.cerm_journal_title
        return None

    def detect_publisher_logos(self, max_hash_difference=5, stop_at_first_match=False):
        """
        Detects publisher logos in file
        :param max_hash_difference: max_hash_difference to be passed to PublisherLogo.test_hash_match
        :param stop_at_first_match: if True, stop trying additional matches if one is found
        :return: list of detected logos (as PublisherLogo instances)
        """
        detected_logos = []
        images_folder = self.file_path.replace(self.file_ext, ".images")
        if not os.path.exists(images_folder):
            self.cermine_file()
        for i in os.listdir(images_folder):
            i_path = os.path.join(images_folder, i)
            pl = PublisherLogo(i, path=i_path)
            with shelve.open(LOGOS_DB_PATH) as db:
                for key in db:
                    logo = db[key]
                    logo.test_hash_match(pl, max_hash_difference=max_hash_difference, method="perception")
                    if logo.test_hash_match(pl, max_hash_difference=max_hash_difference):
                        logger.debug("Extracted image {} matched logo {}".format(i_path, logo.name))
                        detected_logos.append(logo)
                        if stop_at_first_match:
                            break
            if detected_logos and stop_at_first_match:
                break
        return detected_logos

    def test_file_has_image_on_first_page(self):
        images_folder = self.file_path.replace(self.file_ext, ".images")
        if not os.path.exists(images_folder):
            self.cermine_file()
        for i in os.listdir(images_folder):
            if "img_1_" in i:
                return True
        return False

    def extract_publisher_tags_from_file_metadata(self):
        detected_publisher_tags = list()
        for tag in PUBLISHER_PDF_METADATA_TAGS:
            if tag in self.file_metadata.keys():
                logger.debug("Found publisher tag {} in file metadata".format(tag))
                if self.file_metadata[tag]:
                    detected_publisher_tags.append(tag)
                else:
                    logger.debug("However tag {} in file metadata has no value".format(tag))
        if not detected_publisher_tags:
            logger.debug("Could not find any publisher tags in file metadata")
        return detected_publisher_tags

    def test_title_match_cermxml(self, min_similarity=0.9):
        """
        Test if there is a match for declared title in title element of xml file produced by cermine
        :return: True, False or None
        """
        if not self.cerm_ran_and_parsed:
            self.parse_cermxml()
        if self.dec_ms_title and self.cerm_title:
            if SequenceMatcher(None, self.cerm_title, self.dec_ms_title).ratio() >= min_similarity:
                logger.debug("Declared title matches title identified by CERMINE")
                return True
            else:
                logger.debug("Declared title does not match title identified by CERMINE")
                return False
        logger.debug("Could not test title match with cermxml; "
                     "self.dec_ms_title: {}; self.cerm_title: {}".format(self.dec_ms_title, self.cerm_title))
        return None

    def test_doi_match(self):
        result = self.find_doi_in_extracted_text()
        if result:
            logger.debug("Found DOI in extracted text")
            return True
        logger.debug("Could not find DOI in extracted text")
        return False

    def test_valid_doi_in_extracted_text(self, *args, **kwargs):
        """
        Alias for self.test_doi_resolves. Used only to differentiate from test_valid_doi_in_cermine_xml
        in response object
        """
        return self.test_doi_resolves(*args, **kwargs)

    def test_valid_doi_in_cermine_xml(self, *args, **kwargs):
        """
        Alias for self.test_doi_resolves. Used only to differentiate from test_valid_doi_in_extracted_text
        in response object
        """
        return self.test_doi_resolves(*args, **kwargs)

    def parse(self):
        '''
        Workflow for PDF files
        :return: Tuple where: first element is string "success" or "fail" to indicate outcome; second element is
            string containing details
        '''

        # plausible_versions = [
        #     'submitted version', SMUR,
        #     'accepted version', AM,
        #     'proof', P,
        #     'published version', 'version of record', VOR,
        # ]  # use ArtemisResult.possible_versions instead

        approve_deposit = False
        reason = ""

        r = ArtemisResult(self.file_name)
        r.smur_prob = 2500
        r.am_prob = 2500
        r.p_prob = 2500
        r.vor_oa_prob = 1250
        r.vor_pw_prob = 1250

        # self.test_doi_resolves()

        # region file metadata tests
        self.extract_file_metadata()

        r.title_match_file_metadata = r.append_test_result(
            self.test_title_match_in_file_metadata,
            self.test_title_match_in_file_metadata('/Title'),
        )

        r.extracted_publisher_tags_in_file_metadata = r.append_test_result(
            self.extract_publisher_tags_from_file_metadata,
            self.extract_publisher_tags_from_file_metadata(),
        )

        if r.extracted_publisher_tags_in_file_metadata:
            r.exclude_versions(['submitted version', SMUR])
            r.smur_prob = 0

            r.exclude_versions(['accepted version', AM])
            r.am_prob = 0
        # endregion

        # region extracted text tests
        self.extract_text()
        r.long_enough = r.append_test_result(
            self.test_length_of_extracted_text,
            self.test_length_of_extracted_text(),
        )

        r.title_match_extracted_text = r.append_test_result(
            self.test_title_match_in_extracted_text,
            self.test_title_match_in_extracted_text(),
        )

        self.find_doi_in_extracted_text()
        if self.doi_in_extracted_text:
            doi_match = self.doi_in_extracted_text['match']
            r.valid_doi_in_extracted_text = r.append_test_result(
                self.test_valid_doi_in_extracted_text,
                self.test_valid_doi_in_extracted_text(doi=doi_match),
            )

        r.cc_match_extracted_text = r.append_test_result(
            self.find_cc_statement_in_extracted_text,
            self.find_cc_statement_in_extracted_text(),
        )
        # endregion

        # region cermine tests
        self.cermine_file()
        self.parse_cermxml()
        if self.cerm_doi:
            r.valid_doi_in_cermine_xml = r.append_test_result(
                self.test_valid_doi_in_cermine_xml,
                self.test_valid_doi_in_cermine_xml(doi=self.cerm_doi),
            )

        r.title_match_cermine_xml = r.append_test_result(
            self.test_title_match_cermxml,
            self.test_title_match_cermxml(),
        )
        # endregion

        # region logo tests
        r.image_on_first_page = r.append_test_result(
            self.test_file_has_image_on_first_page,
            self.test_file_has_image_on_first_page(),
        )

        r.detected_logos = r.append_test_result(
            self.detect_publisher_logos,
            self.detect_publisher_logos(),
        )

        # exclude r.possible_versions that are not corroborated by detected logos
        if r.detected_logos:
            logger.debug("r.possible_versions before considering logos: {}".format(r.possible_versions))
            suggested_versions = []
            for dl in r.detected_logos:
                for version in dl.metadata["indicate_ms_versions"]:
                    if version not in suggested_versions:
                        suggested_versions.append(version)
            logger.debug("Versions suggested by logos: {}".format(suggested_versions))
            for v in reversed(r.possible_versions): # https://stackoverflow.com/a/14283447
                if v not in suggested_versions:
                    r.exclude_versions([v])
            logger.debug("r.possible_versions after considering logos: {}".format(r.possible_versions))
        # endregion

        # region decision
        if r.long_enough and (r.title_match_file_metadata or r.title_match_extracted_text):
            r.sanity_check = True

            if r.extracted_publisher_tags_in_file_metadata or r.cc_match_extracted_text:
                # file is publisher-generated
                # TODO: Add more tests here
                r.exclude_versions([SMUR, AM])
                if self.dec_version.lower() in ['submitted version', 'accepted version', SMUR, AM]:
                    r.reason = 'PDF metadata contains publisher tags, but declared version is author-generated'
                if r.cc_match_extracted_text:
                    r.reason = 'Create Commons licence detected in extracted text'
                    r.approve_deposit = True  # could be proof, so additional checking is desirable
                else:
                    r.reason = 'Publisher-generated version; no evidence of CC licence'
            else:
                if self.dec_version.lower() in ['submitted version', 'accepted version', SMUR, AM]:
                    r.approve_deposit = True
                    r.reason = 'Could not find any evidence that this PDF is publisher-generated'
                else:
                    r.reason = "This is either a submitted or accepted version, " \
                             "but declared version is {}".format(self.dec_version)
        else:
            if r.long_enough:
                r.reason = "Could not find declared title ({}) in file {}".format(self.dec_ms_title, self.file_name)
            else:
                r.reason = "File {} is quite short for a journal article. Please check.".format(self.file_name)
        # endregion

        return r.json_response()
# endregion


class VersionDetector:
    def __init__(self, file_path, keep_temp_files=False,
                 dec_ms_title=None, dec_version=None, dec_authors=None, working_folder=None, **kwargs):
        '''

        :param file_path: Path to file this class will evaluate
        :param keep_temp_files: If true, temp directory containing files extracted by CERMINE is not deleted
        :param dec_ms_title: Declared title of manuscript
        :param dec_version: Declared manuscript version of file
        :param dec_authors: Declared authors of manuscript (list)
        :param **kwargs: Dictionary of citation details and any other known metadata fields; values may include:
            acceptance_date=None, doi=None, publication_date=None, title=None
        '''
        self.file_path = file_path
        self.file_name = os.path.basename(self.file_path)
        self.file_ext = os.path.splitext(self.file_path)[-1].lower()
        self.keep_temp_files = keep_temp_files
        self.dec_ms_title = dec_ms_title
        self.dec_version = dec_version
        self.dec_authors = dec_authors
        self.working_folder = working_folder
        self.metadata = kwargs
        logger.info("----- Working on file {}".format(file_path))

    def check_extension(self):
        logger.debug("file_ext: {}; file_path: {}".format(self.file_ext, self.file_path))
        if self.file_ext == ".pdf":
            return "pdf"
        elif self.file_ext == ".docx":
            return "docx"
        elif self.file_ext in [".doc", ".html", ".htm", ".odt", ".ppt", ".pptx", ".rtf", ".tex", ".txt"]:
            return "editable_document"
        else:
            logger.error("Unrecognised file extension {} detected for {}".format(self.file_ext, self.file_path))
            return self.file_ext

    def detect(self):
        """
        Detect version of file using appropriate parser
        :return:
        """
        ext = self.check_extension()
        if ext == "docx":
            p = DocxParser(self.file_path, self.dec_ms_title, self.dec_version, self.dec_authors, **self.metadata)
            result = p.parse()
        elif ext == "pdf":
            def pdf_routine(detector_instance, target_file):
                shutil.copy2(detector_instance.file_path, target_file)
                pdfparser = PdfParser(target_file, detector_instance.dec_ms_title,
                                      detector_instance.dec_version, detector_instance.dec_authors,
                                      **detector_instance.metadata)
                return pdfparser.parse()

            if self.working_folder:
                target = os.path.join(self.working_folder, self.file_name)
                result = pdf_routine(self, target)
            elif not self.keep_temp_files:
                with TemporaryDirectory(prefix="artemis-") as tmpdir:
                    target = os.path.join(tmpdir, self.file_name)
                    result = pdf_routine(self, target)
            else:
                tmpdir = mkdtemp(prefix="artemis-")
                target = os.path.join(tmpdir, self.file_name)
                result = pdf_routine(self, target)

        else:
            error_msg = "{} is not a supported file extension".format(ext)
            logger.error(error_msg)
            return "fail", error_msg
            # sys.exit(error_msg)
        return result




if __name__ == "__main__":
    sign_off = '''-------------
Artemis {}
Author: {}
Copyright (c) 2019

Artemis code and documentation is available at https://github.com/afs25/artemis

You are free to distribute this software under the terms of the MIT License.  
The complete text of the MIT License can be found at 
https://opensource.org/licenses/MIT

        '''.format(__version__, __author__)
    description_text = 'Detects the manuscript version of an academic journal article'

    parser = argparse.ArgumentParser(description=description_text, epilog=sign_off, prog='Artemis',
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('path', type=str, metavar='<path>',
                        help='Path to input file (journal article file to be analysed)')
    parser.add_argument('-k', '--keep', dest='keep', action="store_true",
                        help='Keep temporary files')
    parser.add_argument('-t', '--title', dest='title', type=str, metavar='"Expected title of journal article"',
                        help='Expected/declared title of journal article')
    parser.add_argument('-v', '--version', dest='version', type=str,
                        metavar='"{}", "{}", "{}" or "{}"'.format(SMUR, AM, P, VOR),
                        help='Expected/declared version of journal article')
    parser.add_argument('-w', '--working-folder', dest='working-folder', type=str,
                        metavar='<path>',
                        help='Path to working folder to be used (instead of temp folder)')
    arguments = parser.parse_args()

    detector = VersionDetector(
        arguments.path,
        keep_temp_files=arguments.keep,
        dec_ms_title=arguments.title,
        dec_version=arguments.version,
        working_folder=arguments.working-folder,
    )
    print(detector.detect())

    # TODO: This project has some useful functions: https://github.com/Phyks/libbmc/blob/master/libbmc/doi.py

