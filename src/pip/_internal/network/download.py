"""Download files with progress indicators.
"""
import cgi
import logging
import mimetypes
import os
from zipfile import ZipFile

from pip._vendor import requests
from pip._vendor.requests.models import CONTENT_CHUNK_SIZE

from pip._internal.cli.progress_bars import DownloadProgressProvider
from pip._internal.models.index import PyPI
from pip._internal.network.cache import is_from_cache
from pip._internal.network.utils import response_chunks
from pip._internal.utils.misc import (
    format_size,
    redact_auth_from_url,
    splitext,
)
from pip._internal.utils.typing import MYPY_CHECK_RUNNING

if MYPY_CHECK_RUNNING:
    from typing import Dict, Iterable, Optional, Tuple

    from pip._vendor.requests.models import Response

    from pip._internal.models.link import Link
    from pip._internal.network.session import PipSession
    from pip._internal.utils.hashes import Hashes

logger = logging.getLogger(__name__)

# We use Accept-Encoding: identity here because requests defaults to
# accepting compressed responses. This breaks in a variety of ways
# depending on how the server is configured:
# - Some servers will notice that the file isn't a compressible file
#   and will leave the file alone and with an empty Content-Encoding
# - Some servers will notice that the file is already compressed and
#   leave the file alone, adding a Content-Encoding: gzip header
# - Some servers won't notice anything at all and will take a file
#   that's already been compressed and compress it again and set
#   the Content-Encoding: gzip header
# By setting this to request only the identity encoding we're hoping to
# eliminate the third case. Hopefully there does not exist a server
# which when given a file will notice it is already compressed and that
# you're not asking for a compressed file and will then decompress it
# before sending because if that's the case I don't think it'll ever be
# possible to make this work.
HEADERS = {'Accept-Encoding': 'identity'}


def _get_range_headers(size):
    # type: (int) -> Dict[str, str]
    return {'Accept-Encoding': 'identity', 'Range': 'bytes=-{}'.format(size)}


def _get_http_response_size(resp):
    # type: (Response) -> Optional[int]
    try:
        return int(resp.headers['content-length'])
    except (ValueError, KeyError, TypeError):
        return None


def _prepare_download(
    resp,  # type: Response
    link,  # type: Link
    progress_bar  # type: str
):
    # type: (...) -> Iterable[bytes]
    total_length = _get_http_response_size(resp)

    if link.netloc == PyPI.file_storage_domain:
        url = link.show_url
    else:
        url = link.url_without_fragment

    logged_url = redact_auth_from_url(url)

    if total_length:
        logged_url = '{} ({})'.format(logged_url, format_size(total_length))

    if is_from_cache(resp):
        logger.info("Using cached %s", logged_url)
    else:
        logger.info("Downloading %s", logged_url)

    if logger.getEffectiveLevel() > logging.INFO:
        show_progress = False
    elif is_from_cache(resp):
        show_progress = False
    elif not total_length:
        show_progress = True
    elif total_length > (40 * 1000):
        show_progress = True
    else:
        show_progress = False

    chunks = response_chunks(resp, CONTENT_CHUNK_SIZE)

    if not show_progress:
        return chunks

    return DownloadProgressProvider(
        progress_bar, max=total_length
    )(chunks)


def sanitize_content_filename(filename):
    # type: (str) -> str
    """
    Sanitize the "filename" value from a Content-Disposition header.
    """
    return os.path.basename(filename)


def parse_content_disposition(content_disposition, default_filename):
    # type: (str, str) -> str
    """
    Parse the "filename" value from a Content-Disposition header, and
    return the default filename if the result is empty.
    """
    _type, params = cgi.parse_header(content_disposition)
    filename = params.get('filename')
    if filename:
        # We need to sanitize the filename to prevent directory traversal
        # in case the filename contains ".." path parts.
        filename = sanitize_content_filename(filename)
    return filename or default_filename


def _get_http_response_filename(resp, link):
    # type: (Response, Link) -> str
    """Get an ideal filename from the given HTTP response, falling back to
    the link filename if not provided.
    """
    filename = link.filename  # fallback
    # Have a look at the Content-Disposition header for a better guess
    content_disposition = resp.headers.get('content-disposition')
    if content_disposition:
        filename = parse_content_disposition(content_disposition, filename)
    ext = splitext(filename)[1]  # type: Optional[str]
    if not ext:
        ext = mimetypes.guess_extension(
            resp.headers.get('content-type', '')
        )
        if ext:
            filename += ext
    if not ext and link.url != resp.url:
        ext = os.path.splitext(resp.url)[1]
        if ext:
            filename += ext
    return filename


class Downloader(object):
    def __init__(self, session, progress_bar):
        # type: (PipSession, str) -> None
        self._session = session
        self._progress_bar = progress_bar

    def _download(self, link, headers):
        # type: (Link, Dict[str, str]) -> Tuple[Response, str, Iterable[bytes]]
        url = link.url.split('#', 1)[0]
        resp = self._session.get(url, headers=headers, stream=True)
        try:
            resp.raise_for_status()
        except requests.HTTPError as e:
            logger.critical(
                "HTTP error %s while getting %s", e.response.status_code, link)
            raise
        return (resp, _get_http_response_filename(resp, link),
                _prepare_download(resp, link, self._progress_bar))

    def _download_partial(self, link, tmpdir, size=8000):
        # type: (Link, str, int) -> Tuple[str, str]
        response, filename, chunks = self._download(
            link, _get_range_headers(size))
        file_path = os.path.join(tmpdir, filename)
        with open(file_path, 'wb') as content_file:
            for chunk in chunks:
                content_file.write(chunk)
        with ZipFile(file_path) as wheel:
            if any(s.endswith('/METADATA') for s in wheel.namelist()):
                return file_path, response.headers.get('content-type', '')
        return self._download_partial(link, tmpdir, size*2)

    def _download_all(self, link, tmpdir, hashes):
        # type: (Link, str, Optional[Hashes]) -> Tuple[str, str]
        response, filename, chunks = self._download(link, HEADERS)
        file_path = os.path.join(tmpdir, filename)
        with open(file_path, 'wb') as content_file:
            for chunk in chunks:
                content_file.write(chunk)
        if hashes:
            hashes.check_against_path(file_path)
        return file_path, response.headers.get('content-type', '')

    def download_file(self, link, tmpdir, hashes):
        # type: (Link, str, Optional[Hashes]) -> Tuple[str, str]
        """Download link url into temp_dir using provided session."""
        if link.is_wheel:  # and hashes is None:
            return self._download_partial(link, tmpdir)
        return self._download_all(link, tmpdir, hashes)
