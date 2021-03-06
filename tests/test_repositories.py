import random

import pkg_resources
import pytest

from req_compile.repos.repository import WheelVersionTags, Candidate, sort_candidates


@pytest.mark.parametrize(
    "sys_py_version, py_requires",
    [
        ("3.5.0", None),
        ("2.6.10", None),
        ("3.6.3", ("py3",)),
        ("3.6.3", ("py2", "py3")),
        ("3.6.3", ()),
    ],
)
def test_version_compatible(mock_py_version, sys_py_version, py_requires):
    mock_py_version(sys_py_version)
    assert WheelVersionTags(py_requires).check_compatibility()


@pytest.mark.parametrize(
    "sys_py_version, py_requires", [("3.6.3", ("py2",)), ("2.7.16", ("py3",)),]
)
def test_version_incompatible(mock_py_version, sys_py_version, py_requires):
    mock_py_version(sys_py_version)
    assert not WheelVersionTags(py_requires).check_compatibility()


@pytest.mark.parametrize(
    "py_requires, expected",
    [
        (("py2",), "py2"),
        (("py2", "py3"), "py2.py3"),
        (("py3", "py2"), "py2.py3"),
        ((), "any"),
        (None, "any"),
    ],
)
def test_version_str(py_requires, expected):
    assert str(WheelVersionTags(py_requires)) == expected


def test_sort_non_semver():
    # This is the order that pip chooses
    candidate_vers = (
        "2019.3",
        "2017.2",
        "2015.6",
        "2013.6",
        "2013b0",
        "2012rc0",
        "2012b0",
        "2009r",
        "2013d",
        "2011k",
    )
    candidates = []
    for ver in candidate_vers:
        candidates.append(
            Candidate(
                "pytz", None, pkg_resources.parse_version(ver), None, None, "any", None
            )
        )

    reference = list(candidates)
    random.shuffle(candidates)

    candidates = sort_candidates(candidates)
    assert reference == candidates
