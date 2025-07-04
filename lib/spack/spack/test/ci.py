# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
import io
import os
import subprocess
from urllib.error import HTTPError

import pytest

import llnl.util.filesystem as fs

import spack.ci as ci
import spack.concretize
import spack.environment as ev
import spack.error
import spack.paths as spack_paths
import spack.repo as repo
import spack.util.git
from spack.test.conftest import MockHTTPResponse
from spack.version import Version

pytestmark = [pytest.mark.usefixtures("mock_packages")]


@pytest.fixture
def repro_dir(tmp_path):
    result = tmp_path / "repro_dir"
    result.mkdir()
    with fs.working_dir(str(tmp_path)):
        yield result


def test_get_added_versions_new_checksum(mock_git_package_changes):
    repo, filename, commits = mock_git_package_changes

    checksum_versions = {
        "3f6576971397b379d4205ae5451ff5a68edf6c103b2f03c4188ed7075fbb5f04": Version("2.1.5"),
        "a0293475e6a44a3f6c045229fe50f69dc0eebc62a42405a51f19d46a5541e77a": Version("2.1.4"),
        "6c0853bb27738b811f2b4d4af095323c3d5ce36ceed6b50e5f773204fb8f7200": Version("2.0.7"),
        "86993903527d9b12fc543335c19c1d33a93797b3d4d37648b5addae83679ecd8": Version("2.0.0"),
    }

    with fs.working_dir(repo.packages_path):
        added_versions = ci.get_added_versions(
            checksum_versions, filename, from_ref=commits[-1], to_ref=commits[-2]
        )
        assert len(added_versions) == 1
        assert added_versions[0] == Version("2.1.5")


def test_get_added_versions_new_commit(mock_git_package_changes):
    repo, filename, commits = mock_git_package_changes

    checksum_versions = {
        "74253725f884e2424a0dd8ae3f69896d5377f325": Version("2.1.6"),
        "3f6576971397b379d4205ae5451ff5a68edf6c103b2f03c4188ed7075fbb5f04": Version("2.1.5"),
        "a0293475e6a44a3f6c045229fe50f69dc0eebc62a42405a51f19d46a5541e77a": Version("2.1.4"),
        "6c0853bb27738b811f2b4d4af095323c3d5ce36ceed6b50e5f773204fb8f7200": Version("2.0.7"),
        "86993903527d9b12fc543335c19c1d33a93797b3d4d37648b5addae83679ecd8": Version("2.0.0"),
    }

    with fs.working_dir(repo.packages_path):
        added_versions = ci.get_added_versions(
            checksum_versions, filename, from_ref=commits[-2], to_ref=commits[-3]
        )
        assert len(added_versions) == 1
        assert added_versions[0] == Version("2.1.6")


def test_pipeline_dag(config, tmpdir):
    r"""Test creation, pruning, and traversal of PipelineDAG using the
    following package dependency graph:

        a                           a
       /|                          /|
      c b                         c b
        |\        prune 'd'        /|\
        e d        =====>         e | g
        | |\                      | |
        h | g                     h |
         \|                        \|
          f                         f

    """
    builder = repo.MockRepositoryBuilder(tmpdir)
    builder.add_package("pkg-h", dependencies=[("pkg-f", None, None)])
    builder.add_package("pkg-g")
    builder.add_package("pkg-f")
    builder.add_package("pkg-e", dependencies=[("pkg-h", None, None)])
    builder.add_package("pkg-d", dependencies=[("pkg-f", None, None), ("pkg-g", None, None)])
    builder.add_package("pkg-c")
    builder.add_package("pkg-b", dependencies=[("pkg-d", None, None), ("pkg-e", None, None)])
    builder.add_package("pkg-a", dependencies=[("pkg-b", None, None), ("pkg-c", None, None)])

    with repo.use_repositories(builder.root):
        spec_a = spack.concretize.concretize_one("pkg-a")

        key_a = ci.common.PipelineDag.key(spec_a)
        key_b = ci.common.PipelineDag.key(spec_a["pkg-b"])
        key_c = ci.common.PipelineDag.key(spec_a["pkg-c"])
        key_d = ci.common.PipelineDag.key(spec_a["pkg-d"])
        key_e = ci.common.PipelineDag.key(spec_a["pkg-e"])
        key_f = ci.common.PipelineDag.key(spec_a["pkg-f"])
        key_g = ci.common.PipelineDag.key(spec_a["pkg-g"])
        key_h = ci.common.PipelineDag.key(spec_a["pkg-h"])

        pipeline = ci.common.PipelineDag([spec_a])

        expected_bottom_up_traversal = {
            key_a: 4,
            key_b: 3,
            key_c: 0,
            key_d: 1,
            key_e: 2,
            key_f: 0,
            key_g: 0,
            key_h: 1,
        }

        visited = []
        for stage, node in pipeline.traverse_nodes(direction="parents"):
            assert expected_bottom_up_traversal[node.key] == stage
            visited.append(node.key)

        assert len(visited) == len(expected_bottom_up_traversal)
        assert all(k in visited for k in expected_bottom_up_traversal.keys())

        expected_top_down_traversal = {
            key_a: 0,
            key_b: 1,
            key_c: 1,
            key_d: 2,
            key_e: 2,
            key_f: 4,
            key_g: 3,
            key_h: 3,
        }

        visited = []
        for stage, node in pipeline.traverse_nodes(direction="children"):
            assert expected_top_down_traversal[node.key] == stage
            visited.append(node.key)

        assert len(visited) == len(expected_top_down_traversal)
        assert all(k in visited for k in expected_top_down_traversal.keys())

        pipeline.prune(key_d)
        b_children = pipeline.nodes[key_b].children
        assert len(b_children) == 3
        assert all([k in b_children for k in [key_e, key_f, key_g]])

        # check another bottom-up traversal after pruning pkg-d
        expected_bottom_up_traversal = {
            key_a: 4,
            key_b: 3,
            key_c: 0,
            key_e: 2,
            key_f: 0,
            key_g: 0,
            key_h: 1,
        }

        visited = []
        for stage, node in pipeline.traverse_nodes(direction="parents"):
            assert expected_bottom_up_traversal[node.key] == stage
            visited.append(node.key)

        assert len(visited) == len(expected_bottom_up_traversal)
        assert all(k in visited for k in expected_bottom_up_traversal.keys())

        # check top-down traversal after pruning pkg-d
        expected_top_down_traversal = {
            key_a: 0,
            key_b: 1,
            key_c: 1,
            key_e: 2,
            key_f: 4,
            key_g: 2,
            key_h: 3,
        }

        visited = []
        for stage, node in pipeline.traverse_nodes(direction="children"):
            assert expected_top_down_traversal[node.key] == stage
            visited.append(node.key)

        assert len(visited) == len(expected_top_down_traversal)
        assert all(k in visited for k in expected_top_down_traversal.keys())

        a_deps_direct = [n.spec for n in pipeline.get_dependencies(pipeline.nodes[key_a])]
        assert all([s in a_deps_direct for s in [spec_a["pkg-b"], spec_a["pkg-c"]]])


@pytest.mark.not_on_windows("Not supported on Windows (yet)")
def test_import_signing_key(mock_gnupghome):
    signing_key_dir = spack_paths.mock_gpg_keys_path
    signing_key_path = os.path.join(signing_key_dir, "package-signing-key")
    with open(signing_key_path, encoding="utf-8") as fd:
        signing_key = fd.read()

    # Just make sure this does not raise any exceptions
    ci.import_signing_key(signing_key)


def test_download_and_extract_artifacts(tmpdir, monkeypatch):
    monkeypatch.setenv("GITLAB_PRIVATE_TOKEN", "faketoken")

    url = "https://www.nosuchurlexists.itsfake/artifacts.zip"
    working_dir = os.path.join(tmpdir.strpath, "repro")
    test_artifacts_path = os.path.join(
        spack_paths.test_path, "data", "ci", "gitlab", "artifacts.zip"
    )

    def _urlopen_OK(*args, **kwargs):
        with open(test_artifacts_path, "rb") as f:
            return MockHTTPResponse(
                "200", "OK", {"Content-Type": "application/zip"}, io.BytesIO(f.read())
            )

    monkeypatch.setattr(ci, "urlopen", _urlopen_OK)

    ci.download_and_extract_artifacts(url, working_dir)

    found_zip = fs.find(working_dir, "artifacts.zip")
    assert len(found_zip) == 0

    found_install = fs.find(working_dir, "install.sh")
    assert len(found_install) == 1

    def _urlopen_500(*args, **kwargs):
        raise HTTPError(url, 500, "Internal Server Error", {}, None)

    monkeypatch.setattr(ci, "urlopen", _urlopen_500)

    with pytest.raises(spack.error.SpackError):
        ci.download_and_extract_artifacts(url, working_dir)


def test_ci_copy_stage_logs_to_artifacts_fail(tmpdir, default_mock_concretization, capfd):
    """The copy will fail because the spec is not concrete so does not have
    a package."""
    log_dir = tmpdir.join("log_dir")
    concrete_spec = default_mock_concretization("printing-package")
    ci.copy_stage_logs_to_artifacts(concrete_spec, log_dir)
    _, err = capfd.readouterr()
    assert "Unable to copy files" in err
    assert "No such file or directory" in err


def test_ci_copy_test_logs_to_artifacts_fail(tmpdir, capfd):
    log_dir = tmpdir.join("log_dir")

    ci.copy_test_logs_to_artifacts("no-such-dir", log_dir)
    _, err = capfd.readouterr()
    assert "Cannot copy test logs" in err

    stage_dir = tmpdir.join("stage_dir").strpath
    os.makedirs(stage_dir)
    ci.copy_test_logs_to_artifacts(stage_dir, log_dir)
    _, err = capfd.readouterr()
    assert "Unable to copy files" in err
    assert "No such file or directory" in err


def test_setup_spack_repro_version(tmpdir, capfd, last_two_git_commits, monkeypatch):
    c1, c2 = last_two_git_commits
    repro_dir = os.path.join(tmpdir.strpath, "repro")
    spack_dir = os.path.join(repro_dir, "spack")
    os.makedirs(spack_dir)

    prefix_save = spack.paths.prefix
    monkeypatch.setattr(spack.paths, "prefix", "/garbage")

    ret = ci.setup_spack_repro_version(repro_dir, c2, c1)
    _, err = capfd.readouterr()

    assert not ret
    assert "Unable to find the path" in err

    monkeypatch.setattr(spack.paths, "prefix", prefix_save)
    monkeypatch.setattr(spack.util.git, "git", lambda: None)

    ret = ci.setup_spack_repro_version(repro_dir, c2, c1)
    out, err = capfd.readouterr()

    assert not ret
    assert "requires git" in err

    class mock_git_cmd:
        def __init__(self, *args, **kwargs):
            self.returncode = 0
            self.check = None

        def __call__(self, *args, **kwargs):
            if self.check:
                self.returncode = self.check(*args, **kwargs)
            else:
                self.returncode = 0

    git_cmd = mock_git_cmd()

    monkeypatch.setattr(spack.util.git, "git", lambda: git_cmd)

    git_cmd.check = lambda *a, **k: 1 if len(a) > 2 and a[2] == c2 else 0
    ret = ci.setup_spack_repro_version(repro_dir, c2, c1)
    _, err = capfd.readouterr()

    assert not ret
    assert "Missing commit: {0}".format(c2) in err

    git_cmd.check = lambda *a, **k: 1 if len(a) > 2 and a[2] == c1 else 0
    ret = ci.setup_spack_repro_version(repro_dir, c2, c1)
    _, err = capfd.readouterr()

    assert not ret
    assert "Missing commit: {0}".format(c1) in err

    git_cmd.check = lambda *a, **k: 1 if a[0] == "clone" else 0
    ret = ci.setup_spack_repro_version(repro_dir, c2, c1)
    _, err = capfd.readouterr()

    assert not ret
    assert "Unable to clone" in err

    git_cmd.check = lambda *a, **k: 1 if a[0] == "checkout" else 0
    ret = ci.setup_spack_repro_version(repro_dir, c2, c1)
    _, err = capfd.readouterr()

    assert not ret
    assert "Unable to checkout" in err

    git_cmd.check = lambda *a, **k: 1 if "merge" in a else 0
    ret = ci.setup_spack_repro_version(repro_dir, c2, c1)
    _, err = capfd.readouterr()

    assert not ret
    assert "Unable to merge {0}".format(c1) in err


def test_get_spec_filter_list(mutable_mock_env_path, mutable_mock_repo):
    """Tests that, given an active environment and list of touched pkgs, we get the right
    list of possibly-changed env specs.

    The test concretizes the following environment:

    [    ]  hypre@=0.2.15+shared build_system=generic
    [bl  ]      ^openblas-with-lapack@=0.2.15 build_system=generic
    [    ]  mpileaks@=2.3~debug~opt+shared+static build_system=generic
    [bl  ]      ^callpath@=1.0 build_system=generic
    [bl  ]          ^dyninst@=8.2 build_system=generic
    [bl  ]              ^libdwarf@=20130729 build_system=generic
    [bl  ]              ^libelf@=0.8.13 build_system=generic
    [b   ]      ^gcc@=10.2.1 build_system=generic languages='c,c++,fortran'
    [ l  ]      ^gcc-runtime@=10.2.1 build_system=generic
    [bl  ]      ^mpich@=3.0.4~debug build_system=generic

    and simulates a change in libdwarf.
    """
    e1 = ev.create("test")
    e1.add("mpileaks")
    e1.add("hypre")
    e1.concretize()

    touched = ["libdwarf"]

    # Make sure we return the correct set of possibly affected specs,
    # given a dependent traversal depth and the fact that the touched
    # package is libdwarf.  Passing traversal depth of None or something
    # equal to or larger than the greatest depth in the graph are
    # equivalent and result in traversal of all specs from the touched
    # package to the root.  Passing negative traversal depth results in
    # no spec traversals.  Passing any other number yields differing
    # numbers of possibly affected specs.

    full_set = {
        "mpileaks",
        "mpich",
        "callpath",
        "dyninst",
        "libdwarf",
        "libelf",
        "gcc",
        "gcc-runtime",
        "compiler-wrapper",
    }
    depth_2_set = {
        "mpich",
        "callpath",
        "dyninst",
        "libdwarf",
        "libelf",
        "gcc",
        "gcc-runtime",
        "compiler-wrapper",
    }
    depth_1_set = {"dyninst", "libdwarf", "libelf", "gcc", "gcc-runtime", "compiler-wrapper"}
    depth_0_set = {"libdwarf", "libelf", "gcc", "gcc-runtime", "compiler-wrapper"}

    expectations = {
        None: full_set,
        3: full_set,
        100: full_set,
        -1: set(),
        0: depth_0_set,
        1: depth_1_set,
        2: depth_2_set,
    }

    for key, val in expectations.items():
        affected_specs = ci.get_spec_filter_list(e1, touched, dependent_traverse_depth=key)
        affected_pkg_names = {s.name for s in affected_specs}
        assert affected_pkg_names == val


@pytest.mark.regression("29947")
def test_affected_specs_on_first_concretization(mutable_mock_env_path):
    e = ev.create("first_concretization")
    e.add("mpileaks~shared")
    e.add("mpileaks+shared")
    e.concretize()

    affected_specs = spack.ci.get_spec_filter_list(e, ["callpath"])
    mpileaks_specs = [s for s in affected_specs if s.name == "mpileaks"]
    assert len(mpileaks_specs) == 2, e.all_specs()


@pytest.mark.not_on_windows("Reliance on bash script not supported on Windows")
def test_ci_process_command(repro_dir):
    result = ci.process_command("help", commands=[], repro_dir=str(repro_dir))
    help_sh = repro_dir / "help.sh"
    assert help_sh.exists() and not result


@pytest.mark.not_on_windows("Reliance on bash script not supported on Windows")
def test_ci_process_command_fail(repro_dir, monkeypatch):
    msg = "subprocess wait exception"

    def _fail(self, args):
        raise RuntimeError(msg)

    monkeypatch.setattr(subprocess.Popen, "__init__", _fail)
    with pytest.raises(RuntimeError, match=msg):
        ci.process_command("help", [], str(repro_dir))


def test_ci_create_buildcache(tmpdir, working_env, config, monkeypatch):
    """Test that create_buildcache returns a list of objects with the correct
    keys and types."""
    monkeypatch.setattr(ci, "push_to_build_cache", lambda a, b, c: True)

    results = ci.create_buildcache(
        None, destination_mirror_urls=["file:///fake-url-one", "file:///fake-url-two"]
    )

    assert len(results) == 2
    result1, result2 = results
    assert result1.success
    assert result1.url == "file:///fake-url-one"
    assert result2.success
    assert result2.url == "file:///fake-url-two"

    results = ci.create_buildcache(None, destination_mirror_urls=["file:///fake-url-one"])

    assert len(results) == 1
    assert results[0].success
    assert results[0].url == "file:///fake-url-one"


def test_ci_run_standalone_tests_missing_requirements(
    tmpdir, working_env, default_mock_concretization, capfd
):
    """This test case checks for failing prerequisite checks."""
    ci.run_standalone_tests()
    err = capfd.readouterr()[1]
    assert "Job spec is required" in err

    args = {"job_spec": default_mock_concretization("printing-package")}
    ci.run_standalone_tests(**args)
    err = capfd.readouterr()[1]
    assert "Reproduction directory is required" in err


@pytest.mark.not_on_windows("Reliance on bash script not supported on Windows")
def test_ci_run_standalone_tests_not_installed_junit(
    tmp_path, repro_dir, working_env, mock_test_stage, capfd
):
    log_file = tmp_path / "junit.xml"
    args = {
        "log_file": str(log_file),
        "job_spec": spack.concretize.concretize_one("printing-package"),
        "repro_dir": str(repro_dir),
        "fail_fast": True,
    }

    ci.run_standalone_tests(**args)
    err = capfd.readouterr()[1]
    assert "No installed packages" in err
    assert os.path.getsize(log_file) > 0


@pytest.mark.not_on_windows("Reliance on bash script not supported on Windows")
def test_ci_run_standalone_tests_not_installed_cdash(
    tmp_path, repro_dir, working_env, mock_test_stage, capfd
):
    """Test run_standalone_tests with cdash and related options."""
    log_file = tmp_path / "junit.xml"
    args = {
        "log_file": str(log_file),
        "job_spec": spack.concretize.concretize_one("printing-package"),
        "repro_dir": str(repro_dir),
    }

    # Cover when CDash handler provided (with the log file as well)
    ci_cdash = {
        "url": "file://fake",
        "build-group": "fake-group",
        "project": "ci-unit-testing",
        "site": "fake-site",
    }
    os.environ["SPACK_CDASH_BUILD_NAME"] = "ci-test-build"
    os.environ["SPACK_CDASH_BUILD_STAMP"] = "ci-test-build-stamp"
    os.environ["CI_RUNNER_DESCRIPTION"] = "test-runner"
    handler = ci.CDashHandler(ci_cdash)
    args["cdash"] = handler
    ci.run_standalone_tests(**args)
    out = capfd.readouterr()[0]
    # CDash *and* log file output means log file ignored
    assert "xml option is ignored with CDash" in out

    # copy test results (though none)
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    handler.copy_test_results(str(tmp_path), str(artifacts_dir))
    err = capfd.readouterr()[1]
    assert "Unable to copy files" in err
    assert "No such file or directory" in err


def test_ci_skipped_report(tmpdir, config):
    """Test explicit skipping of report as well as CI's 'package' arg."""
    pkg = "trivial-smoke-test"
    spec = spack.concretize.concretize_one(pkg)
    ci_cdash = {
        "url": "file://fake",
        "build-group": "fake-group",
        "project": "ci-unit-testing",
        "site": "fake-site",
    }
    os.environ["SPACK_CDASH_BUILD_NAME"] = "fake-test-build"
    os.environ["SPACK_CDASH_BUILD_STAMP"] = "ci-test-build-stamp"
    os.environ["CI_RUNNER_DESCRIPTION"] = "test-runner"
    handler = ci.CDashHandler(ci_cdash)
    reason = "Testing skip"
    handler.report_skipped(spec, tmpdir.strpath, reason=reason)

    reports = [name for name in tmpdir.listdir() if str(name).endswith("Testing.xml")]
    assert len(reports) == 1
    expected = f"Skipped {pkg} package"
    with open(reports[0], "r", encoding="utf-8") as f:
        have = [0, 0]
        for line in f:
            if expected in line:
                have[0] += 1
            elif reason in line:
                have[1] += 1
        assert all(count == 1 for count in have)
