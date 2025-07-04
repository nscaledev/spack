# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import os

import pytest

import spack.binary_distribution as bindist
import spack.cmd.mirror
import spack.concretize
import spack.config
import spack.environment as ev
import spack.error
import spack.mirrors.utils
import spack.package_base
import spack.spec
import spack.util.git
import spack.util.url as url_util
import spack.version
from spack.main import SpackCommand, SpackCommandError

config = SpackCommand("config")
mirror = SpackCommand("mirror")
env = SpackCommand("env")
add = SpackCommand("add")
concretize = SpackCommand("concretize")
install = SpackCommand("install")
buildcache = SpackCommand("buildcache")
uninstall = SpackCommand("uninstall")

pytestmark = pytest.mark.not_on_windows("does not run on windows")


@pytest.mark.disable_clean_stage_check
@pytest.mark.regression("8083")
def test_regression_8083(tmpdir, capfd, mock_packages, mock_fetch, config):
    with capfd.disabled():
        output = mirror("create", "-d", str(tmpdir), "externaltool")
    assert "Skipping" in output
    assert "as it is an external spec" in output


# Unit tests should not be affected by the user's managed environments
@pytest.mark.regression("12345")
def test_mirror_from_env(mutable_mock_env_path, tmp_path, mock_packages, mock_fetch):
    mirror_dir = str(tmp_path / "mirror")
    env_name = "test"

    env("create", env_name)
    with ev.read(env_name):
        add("trivial-install-test-package")
        add("git-test")
        concretize()
        with spack.config.override("config:checksum", False):
            mirror("create", "-d", mirror_dir, "--all")

    e = ev.read(env_name)
    assert set(os.listdir(mirror_dir)) == set([s.name for s in e.user_specs])
    for spec in e.specs_by_hash.values():
        mirror_res = os.listdir(os.path.join(mirror_dir, spec.name))
        expected = ["%s.tar.gz" % spec.format("{name}-{version}")]
        assert mirror_res == expected


# Test for command line-specified spec in concretized environment
def test_mirror_spec_from_env(mutable_mock_env_path, tmp_path, mock_packages, mock_fetch):
    mirror_dir = str(tmp_path / "mirror-B")
    env_name = "test"

    env("create", env_name)
    with ev.read(env_name):
        add("simple-standalone-test@0.9")
        concretize()
        with spack.config.override("config:checksum", False):
            mirror("create", "-d", mirror_dir, "simple-standalone-test")

    e = ev.read(env_name)
    assert set(os.listdir(mirror_dir)) == set([s.name for s in e.user_specs])
    spec = e.concrete_roots()[0]
    mirror_res = os.listdir(os.path.join(mirror_dir, spec.name))
    expected = ["%s.tar.gz" % spec.format("{name}-{version}")]
    assert mirror_res == expected


@pytest.fixture
def source_for_pkg_with_hash(mock_packages, tmpdir):
    s = spack.concretize.concretize_one("trivial-pkg-with-valid-hash")
    local_url_basename = os.path.basename(s.package.url)
    local_path = os.path.join(str(tmpdir), local_url_basename)
    with open(local_path, "w", encoding="utf-8") as f:
        f.write(s.package.hashed_content)
    local_url = url_util.path_to_file_url(local_path)
    s.package.versions[spack.version.Version("1.0")]["url"] = local_url


def test_mirror_skip_unstable(tmpdir_factory, mock_packages, config, source_for_pkg_with_hash):
    mirror_dir = str(tmpdir_factory.mktemp("mirror-dir"))

    specs = [
        spack.concretize.concretize_one(x) for x in ["git-test", "trivial-pkg-with-valid-hash"]
    ]
    spack.mirrors.utils.create(mirror_dir, specs, skip_unstable_versions=True)

    assert set(os.listdir(mirror_dir)) - set(["_source-cache"]) == set(
        ["trivial-pkg-with-valid-hash"]
    )


class MockMirrorArgs:
    def __init__(
        self,
        specs=None,
        all=False,
        file=None,
        versions_per_spec=None,
        dependencies=False,
        exclude_file=None,
        exclude_specs=None,
        directory=None,
        private=False,
    ):
        self.specs = specs or []
        self.all = all
        self.file = file
        self.versions_per_spec = versions_per_spec
        self.dependencies = dependencies
        self.exclude_file = exclude_file
        self.exclude_specs = exclude_specs
        self.private = private
        self.directory = directory


def test_exclude_specs(mock_packages, config):
    args = MockMirrorArgs(
        specs=["mpich"], versions_per_spec="all", exclude_specs="mpich@3.0.1:3.0.2 mpich@1.0"
    )

    mirror_specs, _ = spack.cmd.mirror._specs_and_action(args)
    expected_include = set(
        spack.concretize.concretize_one(x) for x in ["mpich@3.0.3", "mpich@3.0.4", "mpich@3.0"]
    )
    expected_exclude = set(spack.spec.Spec(x) for x in ["mpich@3.0.1", "mpich@3.0.2", "mpich@1.0"])
    assert expected_include <= set(mirror_specs)
    assert not any(spec.satisfies(y) for spec in mirror_specs for y in expected_exclude)


def test_exclude_specs_public_mirror(mock_packages, config):
    args = MockMirrorArgs(
        specs=["no-redistribute-dependent"],
        versions_per_spec="all",
        dependencies=True,
        private=False,
    )

    mirror_specs, _ = spack.cmd.mirror._specs_and_action(args)
    assert not any(s.name == "no-redistribute" for s in mirror_specs)
    assert any(s.name == "no-redistribute-dependent" for s in mirror_specs)


def test_exclude_file(mock_packages, tmpdir, config):
    exclude_path = os.path.join(str(tmpdir), "test-exclude.txt")
    with open(exclude_path, "w", encoding="utf-8") as exclude_file:
        exclude_file.write(
            """\
mpich@3.0.1:3.0.2
mpich@1.0
"""
        )

    args = MockMirrorArgs(specs=["mpich"], versions_per_spec="all", exclude_file=exclude_path)

    mirror_specs, _ = spack.cmd.mirror._specs_and_action(args)
    expected_include = set(
        spack.concretize.concretize_one(x) for x in ["mpich@3.0.3", "mpich@3.0.4", "mpich@3.0"]
    )
    expected_exclude = set(spack.spec.Spec(x) for x in ["mpich@3.0.1", "mpich@3.0.2", "mpich@1.0"])
    assert expected_include <= set(mirror_specs)
    assert not any(spec.satisfies(y) for spec in mirror_specs for y in expected_exclude)


def test_mirror_crud(mutable_config, capsys):
    with capsys.disabled():
        mirror("add", "mirror", "http://spack.io")

        output = mirror("remove", "mirror")
        assert "Removed mirror" in output

        mirror("add", "mirror", "http://spack.io")

        # no-op
        output = mirror("set-url", "mirror", "http://spack.io")
        assert "No changes made" in output

        output = mirror("set-url", "--push", "mirror", "s3://spack-public")
        assert not output

        # no-op
        output = mirror("set-url", "--push", "mirror", "s3://spack-public")
        assert "No changes made" in output

        output = mirror("remove", "mirror")
        assert "Removed mirror" in output

        # Test S3 connection info token
        mirror("add", "--s3-access-token", "aaaaaazzzzz", "mirror", "s3://spack-public")

        output = mirror("remove", "mirror")
        assert "Removed mirror" in output

        # Test S3 connection info token as variable
        mirror("add", "--s3-access-token-variable", "aaaaaazzzzz", "mirror", "s3://spack-public")

        output = mirror("remove", "mirror")
        assert "Removed mirror" in output

        def do_add_set_seturl_access_pair(
            id_arg, secret_arg, mirror_name="mirror", mirror_url="s3://spack-public"
        ):
            # Test S3 connection info id/key
            output = mirror("add", id_arg, "foo", secret_arg, "bar", mirror_name, mirror_url)
            if "variable" not in secret_arg:
                assert (
                    f"Configuring mirror secrets as plain text with {secret_arg} is deprecated. "
                    in output
                )

            output = config("blame", "mirrors")
            assert all([x in output for x in ("foo", "bar", mirror_name, mirror_url)])
            # Mirror access_pair deprecation warning should not be in blame output
            assert "support for plain text secrets" not in output

            output = mirror("set", id_arg, "foo_set", secret_arg, "bar_set", mirror_name)
            if "variable" not in secret_arg:
                assert "support for plain text secrets" in output
            output = config("blame", "mirrors")
            assert all([x in output for x in ("foo_set", "bar_set", mirror_name, mirror_url)])
            if "variable" not in secret_arg:
                output = mirror(
                    "set", id_arg, "foo_set", secret_arg + "-variable", "bar_set_var", mirror_name
                )
                assert "support for plain text secrets" not in output
                output = config("blame", "mirrors")
                assert all(
                    [x in output for x in ("foo_set", "bar_set_var", mirror_name, mirror_url)]
                )

            output = mirror(
                "set-url",
                id_arg,
                "foo_set_url",
                secret_arg,
                "bar_set_url",
                "--push",
                mirror_name,
                mirror_url + "-push",
            )
            output = config("blame", "mirrors")
            assert all(
                [
                    x in output
                    for x in ("foo_set_url", "bar_set_url", mirror_name, mirror_url + "-push")
                ]
            )

            output = mirror("set", id_arg, "a", mirror_name)
            assert "No changes made to mirror" not in output

            output = mirror("set", secret_arg, "b", mirror_name)
            assert "No changes made to mirror" not in output

            output = mirror("set-url", id_arg, "c", mirror_name, mirror_url)
            assert "No changes made to mirror" not in output

            output = mirror("set-url", secret_arg, "d", mirror_name, mirror_url)
            assert "No changes made to mirror" not in output

            output = mirror("remove", mirror_name)
            assert "Removed mirror" in output

            output = mirror("add", id_arg, "foo", mirror_name, mirror_url)
            assert "Expected both parts of the access pair to be specified. " in output

            output = mirror("set-url", id_arg, "bar", mirror_name, mirror_url)
            assert "Expected both parts of the access pair to be specified. " in output

            output = mirror("set", id_arg, "bar", mirror_name)
            assert "Expected both parts of the access pair to be specified. " in output

            output = mirror("remove", mirror_name)
            assert "Removed mirror" in output

            output = mirror("add", secret_arg, "bar", mirror_name, mirror_url)
            assert "Expected both parts of the access pair to be specified. " in output

            output = mirror("set-url", secret_arg, "bar", mirror_name, mirror_url)
            assert "Expected both parts of the access pair to be specified. " in output

            output = mirror("set", secret_arg, "bar", mirror_name)
            assert "Expected both parts of the access pair to be specified. " in output

            output = mirror("remove", mirror_name)
            assert "Removed mirror" in output

            output = mirror("list")
            assert "No mirrors configured" in output

        do_add_set_seturl_access_pair("--s3-access-key-id", "--s3-access-key-secret")
        do_add_set_seturl_access_pair("--s3-access-key-id", "--s3-access-key-secret-variable")
        do_add_set_seturl_access_pair(
            "--s3-access-key-id-variable", "--s3-access-key-secret-variable"
        )
        with pytest.raises(
            spack.error.SpackError, match="Cannot add mirror with a variable id and text secret"
        ):
            do_add_set_seturl_access_pair("--s3-access-key-id-variable", "--s3-access-key-secret")

        # Test OCI connection info user/password
        do_add_set_seturl_access_pair("--oci-username", "--oci-password")
        do_add_set_seturl_access_pair("--oci-username", "--oci-password-variable")
        do_add_set_seturl_access_pair("--oci-username-variable", "--oci-password-variable")
        with pytest.raises(
            spack.error.SpackError, match="Cannot add mirror with a variable id and text secret"
        ):
            do_add_set_seturl_access_pair("--s3-access-key-id-variable", "--s3-access-key-secret")

        # Test S3 connection info with endpoint URL
        mirror(
            "add",
            "--s3-access-token",
            "aaaaaazzzzz",
            "--s3-endpoint-url",
            "http://localhost/",
            "mirror",
            "s3://spack-public",
        )

        output = mirror("remove", "mirror")
        assert "Removed mirror" in output

        output = mirror("list")
        assert "No mirrors configured" in output

        # Test GCS Mirror
        mirror("add", "mirror", "gs://spack-test")

        output = mirror("remove", "mirror")
        assert "Removed mirror" in output

        output = mirror("list")
        assert "No mirrors configured" in output


def test_mirror_nonexisting(mutable_config):
    with pytest.raises(SpackCommandError):
        mirror("remove", "not-a-mirror")

    with pytest.raises(SpackCommandError):
        mirror("set-url", "not-a-mirror", "http://spack.io")


def test_mirror_name_collision(mutable_config):
    mirror("add", "first", "1")

    with pytest.raises(SpackCommandError):
        mirror("add", "first", "1")


# Unit tests should not be affected by the user's managed environments
def test_mirror_destroy(
    mutable_mock_env_path,
    install_mockery,
    mock_packages,
    mock_fetch,
    mock_archive,
    mutable_config,
    monkeypatch,
    tmpdir,
):
    # Create a temp mirror directory for buildcache usage
    mirror_dir = tmpdir.join("mirror_dir")
    mirror_url = "file://{0}".format(mirror_dir.strpath)
    mirror("add", "atest", mirror_url)

    spec_name = "libdwarf"

    # Put a binary package in a buildcache
    install("--fake", "--no-cache", spec_name)
    buildcache("push", "-u", "-f", mirror_dir.strpath, spec_name)

    blobs_path = bindist.buildcache_relative_blobs_path()

    contents = os.listdir(mirror_dir.strpath)
    assert blobs_path in contents

    # Destroy mirror by name
    mirror("destroy", "-m", "atest")

    assert not os.path.exists(mirror_dir.strpath)

    buildcache("push", "-u", "-f", mirror_dir.strpath, spec_name)

    contents = os.listdir(mirror_dir.strpath)
    assert blobs_path in contents

    # Destroy mirror by url
    mirror("destroy", "--mirror-url", mirror_url)

    assert not os.path.exists(mirror_dir.strpath)

    uninstall("-y", spec_name)
    mirror("remove", "atest")


@pytest.mark.usefixtures("mock_packages")
class TestMirrorCreate:
    @pytest.mark.regression("31736", "31985")
    def test_all_specs_with_all_versions_dont_concretize(self):
        args = MockMirrorArgs(all=True, exclude_file=None, exclude_specs=None)
        mirror_specs, _ = spack.cmd.mirror._specs_and_action(args)
        assert all(not s.concrete for s in mirror_specs)

    @pytest.mark.parametrize(
        "cli_args,error_str",
        [
            # Passed more than one among -f --all
            (
                {"specs": None, "file": "input.txt", "all": True},
                "cannot specify specs with a file if",
            ),
            (
                {"specs": "hdf5", "file": "input.txt", "all": False},
                "cannot specify specs with a file AND",
            ),
            ({"specs": None, "file": None, "all": False}, "no packages were specified"),
            # Passed -n along with --all
            (
                {"specs": None, "file": None, "all": True, "versions_per_spec": 2},
                "cannot specify '--versions_per-spec'",
            ),
        ],
    )
    def test_error_conditions(self, cli_args, error_str):
        args = MockMirrorArgs(**cli_args)
        with pytest.raises(spack.error.SpackError, match=error_str):
            spack.cmd.mirror.mirror_create(args)

    @pytest.mark.parametrize(
        "cli_args,not_expected",
        [
            (
                {
                    "specs": "boost bowtie callpath",
                    "exclude_specs": "bowtie",
                    "dependencies": False,
                },
                ["bowtie"],
            ),
            (
                {
                    "specs": "boost bowtie callpath",
                    "exclude_specs": "bowtie callpath",
                    "dependencies": False,
                },
                ["bowtie", "callpath"],
            ),
            (
                {
                    "specs": "boost bowtie callpath",
                    "exclude_specs": "bowtie",
                    "dependencies": True,
                },
                ["bowtie"],
            ),
        ],
    )
    def test_exclude_specs_from_user(self, cli_args, not_expected, config):
        mirror_specs, _ = spack.cmd.mirror._specs_and_action(MockMirrorArgs(**cli_args))
        assert not any(s.satisfies(y) for s in mirror_specs for y in not_expected)

    @pytest.mark.parametrize("abstract_specs", [("bowtie", "callpath")])
    def test_specs_from_cli_are_the_same_as_from_file(self, abstract_specs, config, tmpdir):
        args = MockMirrorArgs(specs=" ".join(abstract_specs))
        specs_from_cli = spack.cmd.mirror.concrete_specs_from_user(args)

        input_file = tmpdir.join("input.txt")
        input_file.write("\n".join(abstract_specs))
        args = MockMirrorArgs(file=str(input_file))
        specs_from_file = spack.cmd.mirror.concrete_specs_from_user(args)

        assert specs_from_cli == specs_from_file

    @pytest.mark.parametrize(
        "input_specs,nversions",
        [("callpath", 1), ("mpich", 4), ("callpath mpich", 3), ("callpath mpich", "all")],
    )
    def test_versions_per_spec_produces_concrete_specs(self, input_specs, nversions, config):
        args = MockMirrorArgs(specs=input_specs, versions_per_spec=nversions)
        specs = spack.cmd.mirror.concrete_specs_from_user(args)
        assert all(s.concrete for s in specs)


def test_mirror_type(mutable_config):
    """Test the mirror set command"""
    mirror("add", "example", "--type", "binary", "http://example.com")
    assert spack.config.get("mirrors:example") == {
        "url": "http://example.com",
        "source": False,
        "binary": True,
    }

    mirror("set", "example", "--type", "source")
    assert spack.config.get("mirrors:example") == {
        "url": "http://example.com",
        "source": True,
        "binary": False,
    }

    mirror("set", "example", "--type", "binary")
    assert spack.config.get("mirrors:example") == {
        "url": "http://example.com",
        "source": False,
        "binary": True,
    }
    mirror("set", "example", "--type", "binary", "--type", "source")
    assert spack.config.get("mirrors:example") == {
        "url": "http://example.com",
        "source": True,
        "binary": True,
    }


def test_mirror_set_2(mutable_config):
    """Test the mirror set command"""
    mirror("add", "example", "http://example.com")
    mirror(
        "set",
        "example",
        "--push",
        "--url",
        "http://example2.com",
        "--s3-access-key-id",
        "username",
        "--s3-access-key-secret",
        "password",
    )

    assert spack.config.get("mirrors:example") == {
        "url": "http://example.com",
        "push": {"url": "http://example2.com", "access_pair": ["username", "password"]},
    }


def test_mirror_add_set_signed(mutable_config):
    mirror("add", "--signed", "example", "http://example.com")
    assert spack.config.get("mirrors:example") == {"url": "http://example.com", "signed": True}
    mirror("set", "--unsigned", "example")
    assert spack.config.get("mirrors:example") == {"url": "http://example.com", "signed": False}
    mirror("set", "--signed", "example")
    assert spack.config.get("mirrors:example") == {"url": "http://example.com", "signed": True}


def test_mirror_add_set_autopush(mutable_config):
    # Add mirror without autopush
    mirror("add", "example", "http://example.com")
    assert spack.config.get("mirrors:example") == "http://example.com"
    mirror("set", "--no-autopush", "example")
    assert spack.config.get("mirrors:example") == {"url": "http://example.com", "autopush": False}
    mirror("set", "--autopush", "example")
    assert spack.config.get("mirrors:example") == {"url": "http://example.com", "autopush": True}
    mirror("set", "--no-autopush", "example")
    assert spack.config.get("mirrors:example") == {"url": "http://example.com", "autopush": False}
    mirror("remove", "example")

    # Add mirror with autopush
    mirror("add", "--autopush", "example", "http://example.com")
    assert spack.config.get("mirrors:example") == {"url": "http://example.com", "autopush": True}
    mirror("set", "--autopush", "example")
    assert spack.config.get("mirrors:example") == {"url": "http://example.com", "autopush": True}
    mirror("set", "--no-autopush", "example")
    assert spack.config.get("mirrors:example") == {"url": "http://example.com", "autopush": False}
    mirror("set", "--autopush", "example")
    assert spack.config.get("mirrors:example") == {"url": "http://example.com", "autopush": True}
    mirror("remove", "example")


@pytest.mark.require_provenance
@pytest.mark.disable_clean_stage_check
@pytest.mark.parametrize("mirror_knows_commit", (True, False))
def test_binary_provenance_url_fails_mirror_resolves_commit(
    git,
    mock_git_repository,
    mock_packages,
    monkeypatch,
    tmpdir,
    mutable_config,
    mirror_knows_commit,
):
    """Extract git commit from a source mirror since other methods failed"""
    repo_path = mock_git_repository.path
    monkeypatch.setattr(
        spack.package_base.PackageBase, "git", f"file://{repo_path}", raising=False
    )
    monkeypatch.setattr(spack.util.git, "get_commit_sha", lambda x, y: None, raising=False)

    gold_commit = git("-C", repo_path, "rev-parse", "main", output=str).strip()
    # create a fake mirror
    mirror_path = str(tmpdir.join("test-mirror"))
    if mirror_knows_commit:
        mirror("create", "-d", mirror_path, f"git-test-commit@main commit={gold_commit}")
    else:
        mirror("create", "-d", mirror_path, "git-test-commit@main")
    mirror("add", "--type", "source", "test-mirror", mirror_path)

    spec = spack.concretize.concretize_one("git-test-commit@main")
    assert spec.package.stage.archive_file
    assert "commit" in spec.variants
    assert spec.variants["commit"].value == gold_commit


@pytest.mark.require_provenance
@pytest.mark.disable_clean_stage_check
def test_binary_provenance_relative_to_mirror(
    git, mock_git_version_info, mock_packages, monkeypatch, tmpdir, mutable_config
):
    """Integration test to evaluate how commit resolution should behave with a mirror

    We want to confirm that the mirror doesn't break users ability to get a more recent commit
    Use `mock_git_version_info` repo because it has function scope and we can mess with the git
    history.
    """
    repo_path, _, _ = mock_git_version_info
    monkeypatch.setattr(
        spack.package_base.PackageBase, "git", f"file://{repo_path}", raising=False
    )

    # create a fake mirror
    mirror_path = str(tmpdir.join("test-mirror"))
    mirror("create", "-d", mirror_path, "git-test-commit@main")
    mirror("add", "--type", "source", "test-mirror", mirror_path)
    mirror_commit = git("-C", repo_path, "rev-parse", "main", output=str).strip()

    # push the commit past mirror
    git("-C", repo_path, "checkout", "main", output=str)
    git("-C", repo_path, "commit", "--allow-empty", "-m", "bump sha")
    head_commit = git("-C", repo_path, "rev-parse", "main", output=str).strip()

    spec_mirror = spack.concretize.concretize_one("git-test-commit@main")
    assert spec_mirror.variants["commit"].value == mirror_commit

    spec_head = spack.concretize.concretize_one(f"git-test-commit@main commit={head_commit}")
    assert spec_head.variants["commit"].value == head_commit
