# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import re
import shutil

import pytest

from llnl.util.filesystem import mkdirp, working_dir

import spack.cmd
import spack.cmd.pkg
import spack.main
import spack.paths
import spack.repo
import spack.util.file_cache

#: new fake package template
pkg_template = """\
from spack.package import *

class {name}(Package):
    homepage = "http://www.example.com"
    url      = "http://www.example.com/test-1.0.tar.gz"

    version("1.0", md5="0123456789abcdef0123456789abcdef")

    def install(self, spec, prefix):
        pass
"""

abc = {"mockpkg-a", "mockpkg-b", "mockpkg-c"}
abd = {"mockpkg-a", "mockpkg-b", "mockpkg-d"}


# Force all tests to use a git repository *in* the mock packages repo.
@pytest.fixture(scope="module")
def mock_pkg_git_repo(git, tmp_path_factory):
    """Copy the builtin.mock repo and make a mutable git repo inside it."""
    root_dir = tmp_path_factory.mktemp("mock_pkg_git_repo")
    # create spack_repo subdir
    (root_dir / "spack_repo").mkdir()
    repo_dir = root_dir / "spack_repo" / "builtin_mock"
    shutil.copytree(spack.paths.mock_packages_path, str(repo_dir))

    repo_cache = spack.util.file_cache.FileCache(root_dir / "cache")
    mock_repo = spack.repo.Repo(str(repo_dir), cache=repo_cache)

    with working_dir(mock_repo.packages_path):
        git("init")

        # initial commit with mock packages
        # the -f is necessary in case people ignore build-* in their ignores
        git("add", "-f", ".")
        git("config", "user.email", "testing@spack.io")
        git("config", "user.name", "Spack Testing")
        git("-c", "commit.gpgsign=false", "commit", "-m", "initial mock repo commit")

        # add commit with mockpkg-a, mockpkg-b, mockpkg-c packages
        mkdirp("mockpkg_a", "mockpkg_b", "mockpkg_c")
        with open("mockpkg_a/package.py", "w", encoding="utf-8") as f:
            f.write(pkg_template.format(name="PkgA"))
        with open("mockpkg_b/package.py", "w", encoding="utf-8") as f:
            f.write(pkg_template.format(name="PkgB"))
        with open("mockpkg_c/package.py", "w", encoding="utf-8") as f:
            f.write(pkg_template.format(name="PkgC"))
        git("add", "mockpkg_a", "mockpkg_b", "mockpkg_c")
        git("-c", "commit.gpgsign=false", "commit", "-m", "add mockpkg-a, mockpkg-b, mockpkg-c")

        # remove mockpkg-c, add mockpkg-d
        with open("mockpkg_b/package.py", "a", encoding="utf-8") as f:
            f.write("\n# change mockpkg-b")
        git("add", "mockpkg_b")
        mkdirp("mockpkg_d")
        with open("mockpkg_d/package.py", "w", encoding="utf-8") as f:
            f.write(pkg_template.format(name="PkgD"))
        git("add", "mockpkg_d")
        git("rm", "-rf", "mockpkg_c")
        git(
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-m",
            "change mockpkg-b, remove mockpkg-c, add mockpkg-d",
        )

    with spack.repo.use_repositories(mock_repo):
        yield mock_repo.packages_path


@pytest.fixture(scope="module")
def mock_pkg_names():
    repo = spack.repo.PATH.get_repo("builtin_mock")

    # Be sure to include virtual packages since packages with stand-alone
    # tests may inherit additional tests from the virtuals they provide,
    # such as packages that implement `mpi`.
    return {
        name
        for name in repo.all_package_names(include_virtuals=True)
        if not name.startswith("mockpkg-")
    }


def split(output):
    """Split command line output into an array."""
    output = output.strip()
    return re.split(r"\s+", output) if output else []


pkg = spack.main.SpackCommand("pkg")


def test_pkg_add(git, mock_pkg_git_repo):
    with working_dir(mock_pkg_git_repo):
        mkdirp("mockpkg_e")
        with open("mockpkg_e/package.py", "w", encoding="utf-8") as f:
            f.write(pkg_template.format(name="PkgE"))

    pkg("add", "mockpkg-e")

    with working_dir(mock_pkg_git_repo):
        try:
            assert "A  mockpkg_e/package.py" in git("status", "--short", output=str)
        finally:
            shutil.rmtree("mockpkg_e")
            # Removing a package mid-run disrupts Spack's caching
            if spack.repo.PATH.repos[0]._fast_package_checker:
                spack.repo.PATH.repos[0]._fast_package_checker.invalidate()

    with pytest.raises(spack.main.SpackCommandError):
        pkg("add", "does-not-exist")


@pytest.mark.not_on_windows("stdout format conflict")
def test_pkg_list(mock_pkg_git_repo, mock_pkg_names):
    out = split(pkg("list", "HEAD^^"))
    assert sorted(mock_pkg_names) == sorted(out)

    out = split(pkg("list", "HEAD^"))
    assert sorted(mock_pkg_names.union(["mockpkg-a", "mockpkg-b", "mockpkg-c"])) == sorted(out)

    out = split(pkg("list", "HEAD"))
    assert sorted(mock_pkg_names.union(["mockpkg-a", "mockpkg-b", "mockpkg-d"])) == sorted(out)

    # test with three dots to make sure pkg calls `git merge-base`
    out = split(pkg("list", "HEAD^^..."))
    assert sorted(mock_pkg_names) == sorted(out)


@pytest.mark.not_on_windows("stdout format conflict")
def test_pkg_diff(mock_pkg_git_repo, mock_pkg_names):
    out = split(pkg("diff", "HEAD^^", "HEAD^"))
    assert out == ["HEAD^:", "mockpkg-a", "mockpkg-b", "mockpkg-c"]

    out = split(pkg("diff", "HEAD^^", "HEAD"))
    assert out == ["HEAD:", "mockpkg-a", "mockpkg-b", "mockpkg-d"]

    out = split(pkg("diff", "HEAD^", "HEAD"))
    assert out == ["HEAD^:", "mockpkg-c", "HEAD:", "mockpkg-d"]


@pytest.mark.not_on_windows("stdout format conflict")
def test_pkg_added(mock_pkg_git_repo):
    out = split(pkg("added", "HEAD^^", "HEAD^"))
    assert ["mockpkg-a", "mockpkg-b", "mockpkg-c"] == out

    out = split(pkg("added", "HEAD^^", "HEAD"))
    assert ["mockpkg-a", "mockpkg-b", "mockpkg-d"] == out

    out = split(pkg("added", "HEAD^", "HEAD"))
    assert ["mockpkg-d"] == out

    out = split(pkg("added", "HEAD", "HEAD"))
    assert out == []


@pytest.mark.not_on_windows("stdout format conflict")
def test_pkg_removed(mock_pkg_git_repo):
    out = split(pkg("removed", "HEAD^^", "HEAD^"))
    assert out == []

    out = split(pkg("removed", "HEAD^^", "HEAD"))
    assert out == []

    out = split(pkg("removed", "HEAD^", "HEAD"))
    assert out == ["mockpkg-c"]


@pytest.mark.not_on_windows("stdout format conflict")
def test_pkg_changed(mock_pkg_git_repo):
    out = split(pkg("changed", "HEAD^^", "HEAD^"))
    assert out == []

    out = split(pkg("changed", "--type", "c", "HEAD^^", "HEAD^"))
    assert out == []

    out = split(pkg("changed", "--type", "a", "HEAD^^", "HEAD^"))
    assert out == ["mockpkg-a", "mockpkg-b", "mockpkg-c"]

    out = split(pkg("changed", "--type", "r", "HEAD^^", "HEAD^"))
    assert out == []

    out = split(pkg("changed", "--type", "ar", "HEAD^^", "HEAD^"))
    assert out == ["mockpkg-a", "mockpkg-b", "mockpkg-c"]

    out = split(pkg("changed", "--type", "arc", "HEAD^^", "HEAD^"))
    assert out == ["mockpkg-a", "mockpkg-b", "mockpkg-c"]

    out = split(pkg("changed", "HEAD^", "HEAD"))
    assert out == ["mockpkg-b"]

    out = split(pkg("changed", "--type", "c", "HEAD^", "HEAD"))
    assert out == ["mockpkg-b"]

    out = split(pkg("changed", "--type", "a", "HEAD^", "HEAD"))
    assert out == ["mockpkg-d"]

    out = split(pkg("changed", "--type", "r", "HEAD^", "HEAD"))
    assert out == ["mockpkg-c"]

    out = split(pkg("changed", "--type", "ar", "HEAD^", "HEAD"))
    assert out == ["mockpkg-c", "mockpkg-d"]

    out = split(pkg("changed", "--type", "arc", "HEAD^", "HEAD"))
    assert out == ["mockpkg-b", "mockpkg-c", "mockpkg-d"]

    # invalid type argument
    with pytest.raises(spack.main.SpackCommandError):
        pkg("changed", "--type", "foo")


def test_pkg_fails_when_not_git_repo(monkeypatch):
    monkeypatch.setattr(spack.cmd, "spack_is_git_repo", lambda: False)
    with pytest.raises(spack.main.SpackCommandError):
        pkg("added")


def test_pkg_source_requires_one_arg(mock_packages):
    with pytest.raises(spack.main.SpackCommandError):
        pkg("source", "a", "b")

    with pytest.raises(spack.main.SpackCommandError):
        pkg("source", "--canonical", "a", "b")


def test_pkg_source(mock_packages):
    fake_source = pkg("source", "fake")

    fake_file = spack.repo.PATH.filename_for_package_name("fake")
    with open(fake_file, encoding="utf-8") as f:
        contents = f.read()
        assert fake_source == contents


def test_pkg_canonical_source(mock_packages):
    source = pkg("source", "multimethod")
    assert '@when("@2.0")' in source
    assert "Check that multimethods work with boolean values" in source

    canonical_1 = pkg("source", "--canonical", "multimethod@1.0")
    assert "@when" not in canonical_1
    assert "should_not_be_reached by diamond inheritance test" not in canonical_1
    assert "return 'base@1.0'" in canonical_1
    assert "return 'base@2.0'" not in canonical_1
    assert "return 'first_parent'" not in canonical_1
    assert "'should_not_be_reached by diamond inheritance test'" not in canonical_1

    canonical_2 = pkg("source", "--canonical", "multimethod@2.0")
    assert "@when" not in canonical_2
    assert "return 'base@1.0'" not in canonical_2
    assert "return 'base@2.0'" in canonical_2
    assert "return 'first_parent'" in canonical_2
    assert "'should_not_be_reached by diamond inheritance test'" not in canonical_2

    canonical_3 = pkg("source", "--canonical", "multimethod@3.0")
    assert "@when" not in canonical_3
    assert "return 'base@1.0'" not in canonical_3
    assert "return 'base@2.0'" not in canonical_3
    assert "return 'first_parent'" not in canonical_3
    assert "'should_not_be_reached by diamond inheritance test'" not in canonical_3

    canonical_4 = pkg("source", "--canonical", "multimethod@4.0")
    assert "@when" not in canonical_4
    assert "return 'base@1.0'" not in canonical_4
    assert "return 'base@2.0'" not in canonical_4
    assert "return 'first_parent'" not in canonical_4
    assert "'should_not_be_reached by diamond inheritance test'" in canonical_4


def test_pkg_hash(mock_packages):
    output = pkg("hash", "pkg-a", "pkg-b").strip().split()
    assert len(output) == 2 and all(len(elt) == 32 for elt in output)

    output = pkg("hash", "multimethod").strip().split()
    assert len(output) == 1 and all(len(elt) == 32 for elt in output)


group_args = [
    "/path/one.py",  # 12
    "/path/two.py",  # 12
    "/path/three.py",  # 14
    "/path/four.py",  # 13
    "/path/five.py",  # 13
    "/path/six.py",  # 12
    "/path/seven.py",  # 14
    "/path/eight.py",  # 14
    "/path/nine.py",  # 13
    "/path/ten.py",  # 12
]


@pytest.mark.parametrize(
    ["args", "max_group_size", "prefix_length", "max_group_length", "lengths", "error"],
    [
        (group_args, 3, 0, 1, None, ValueError),  # element too long
        (group_args, 3, 0, 13, None, ValueError),  # element too long
        (group_args, 3, 12, 25, None, ValueError),  # prefix and words too long
        (group_args, 3, 0, 25, [2, 1, 1, 1, 1, 1, 1, 1, 1], None),
        (group_args, 3, 0, 26, [2, 1, 1, 2, 1, 1, 2], None),
        (group_args, 3, 0, 40, [3, 3, 2, 2], None),
        (group_args, 3, 0, 43, [3, 3, 3, 1], None),
        (group_args, 4, 0, 54, [4, 3, 3], None),
        (group_args, 4, 0, 56, [4, 4, 2], None),
        ([], 500, 0, None, [], None),
    ],
)
def test_group_arguments(
    mock_packages, args, max_group_size, prefix_length, max_group_length, lengths, error
):
    generator = spack.cmd.group_arguments(
        args,
        max_group_size=max_group_size,
        prefix_length=prefix_length,
        max_group_length=max_group_length,
    )

    # just check that error cases raise
    if error:
        with pytest.raises(ValueError):
            list(generator)
        return

    groups = list(generator)
    assert sum(groups, []) == args
    assert [len(group) for group in groups] == lengths
    assert all(
        sum(len(elt) for elt in group) + (len(group) - 1) <= max_group_length for group in groups
    )


@pytest.mark.skipif(not spack.cmd.pkg.get_grep(), reason="grep is not installed")
def test_pkg_grep(mock_packages, capfd):
    # only splice-* mock packages have the string "splice" in them
    pkg("grep", "-l", "splice")
    output, _ = capfd.readouterr()
    assert output.strip() == "\n".join(
        spack.repo.PATH.get_pkg_class(name).module.__file__
        for name in [
            "depends-on-manyvariants",
            "manyvariants",
            "splice-a",
            "splice-depends-on-t",
            "splice-h",
            "splice-t",
            "splice-vh",
            "splice-vt",
            "splice-z",
            "virtual-abi-1",
            "virtual-abi-2",
            "virtual-abi-multi",
        ]
    )

    # ensure that this string isn't found
    with pytest.raises(spack.main.SpackCommandError):
        pkg("grep", "abcdefghijklmnopqrstuvwxyz")
    assert pkg.returncode == 1
    output, _ = capfd.readouterr()
    assert output.strip() == ""

    # ensure that we return > 1 for an error
    with pytest.raises(spack.main.SpackCommandError):
        pkg("grep", "--foobarbaz-not-an-option")
    assert pkg.returncode == 2
