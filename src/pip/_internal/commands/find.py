import collections
import logging

from pip._vendor.packaging.requirements import InvalidRequirement, Requirement

from pip._internal.cli import cmdoptions
from pip._internal.cli.req_command import RequirementCommand
from pip._internal.cli.status_codes import ERROR, SUCCESS
from pip._internal.exceptions import InvalidWheelFilename
from pip._internal.models.wheel import Wheel
from pip._internal.utils.misc import splitext, write_output
from pip._internal.utils.typing import MYPY_CHECK_RUNNING

from .list import tabulate

if MYPY_CHECK_RUNNING:
    from typing import Dict, Iterator, List, Tuple

    from pip._vendor.packaging.version import _BaseVersion

    from pip._internal.index.package_finder import BestCandidateResult
    from pip._internal.models.link import Link

    Row = Tuple[str, str, str]


logger = logging.getLogger(__name__)


class _Info(object):
    def __init__(self):
        # type: () -> None
        self.sdists = []  # type: List[str]
        self.wheels = []  # type: List[Wheel]

    def add(self, link):
        # type: (Link) -> bool
        if link.is_yanked:
            return False
        if link.is_vcs:
            return False
        if not link.is_wheel:
            self.sdists.append(link.filename)
            return True
        try:
            wheel = Wheel(link.filename)
        except InvalidWheelFilename:
            logger.warning("Skipping invalid wheel: %s", link)
            return False
        self.wheels.append(wheel)
        return True


def _collect_candidate_info(best):
    # type: (BestCandidateResult) -> Dict[_BaseVersion, _Info]
    info = collections.OrderedDict()  # type: Dict[_BaseVersion, _Info]
    for candidate in best.iter_applicable():
        if candidate.version not in info:
            info[candidate.version] = _Info()
        info[candidate.version].add(candidate.link)
    return info


def _iter_rows(info_mapping):
    # type: (Dict[_BaseVersion, _Info]) -> Iterator[Row]
    yield ("version", "sdist", "wheel")
    for version, info in reversed(info_mapping.items()):
        row = (
            str(version),
            " ".join(splitext(n)[-1][1:] for n in info.sdists),
            " ".join(str(t) for w in info.wheels for t in w.file_tags),
        )
        yield row


def _write_table(rows):
    # type: (List[Row]) -> None
    result, sizes = tabulate(rows)
    result.insert(1, " ".join("-" * size for size in sizes))
    for row in result:
        write_output(row)


class FindCommand(RequirementCommand):
    """Find package versions with a specifier.
    """

    usage = """
      %prog [options] <requirement specifier> [package-index-options] ..."""
    ignore_require_venv = True

    def __init__(self, *args, **kwargs):
        super(FindCommand, self).__init__(*args, **kwargs)
        cmd_opts = self.cmd_opts

        cmd_opts.add_option(cmdoptions.no_binary())
        cmd_opts.add_option(cmdoptions.only_binary())
        cmd_opts.add_option(cmdoptions.prefer_binary())
        cmd_opts.add_option(cmdoptions.pre())

        cmdoptions.add_target_python_options(cmd_opts)

        index_opts = cmdoptions.make_option_group(
            cmdoptions.index_group,
            self.parser,
        )

        self.parser.insert_option_group(0, index_opts)
        self.parser.insert_option_group(0, cmd_opts)

    def run(self, options, args):
        if len(args) != 1:
            logger.error("Specify exactly one package.")
            return ERROR

        try:
            req = Requirement(args[0])
        except InvalidRequirement as e:
            logger.error("Invalid requirement: %s", e)
            return ERROR
        if req.url:
            logger.error("URL-based lookup not supported: %s", req)
            return ERROR
        if req.extras:
            logger.info("Ignoring extras: %s", req)
        if req.marker:
            logger.info("Ignoring marker: %s", req)

        session = self.get_default_session(options)
        target_python = cmdoptions.make_target_python(options)
        finder = self._build_package_finder(
            options=options,
            session=session,
            target_python=target_python,
        )

        best = finder.find_best_candidate(req.name, req.specifier)
        info = _collect_candidate_info(best)
        rows = list(_iter_rows(info))
        _write_table(rows)

        return SUCCESS
