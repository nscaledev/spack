# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
import json
import os
import pathlib
import shutil
from typing import NamedTuple

import _vendoring.jsonschema
import pytest

from llnl.util.filesystem import mkdirp, working_dir

import spack
import spack.binary_distribution
import spack.ci as ci
import spack.cmd
import spack.cmd.ci
import spack.concretize
import spack.environment as ev
import spack.hash_types as ht
import spack.main
import spack.paths as spack_paths
import spack.repo
import spack.spec
import spack.stage
import spack.util.spack_yaml as syaml
import spack.version
from spack.ci import gitlab as gitlab_generator
from spack.ci.common import PipelineDag, PipelineOptions, SpackCIConfig
from spack.ci.generator_registry import generator
from spack.cmd.ci import FAILED_CREATE_BUILDCACHE_CODE
from spack.error import SpackError
from spack.schema.database_index import schema as db_idx_schema
from spack.test.conftest import MockHTTPResponse

config_cmd = spack.main.SpackCommand("config")
ci_cmd = spack.main.SpackCommand("ci")
env_cmd = spack.main.SpackCommand("env")
mirror_cmd = spack.main.SpackCommand("mirror")
gpg_cmd = spack.main.SpackCommand("gpg")
install_cmd = spack.main.SpackCommand("install")
uninstall_cmd = spack.main.SpackCommand("uninstall")
buildcache_cmd = spack.main.SpackCommand("buildcache")

pytestmark = [
    pytest.mark.usefixtures("mock_packages"),
    pytest.mark.not_on_windows("does not run on windows"),
    pytest.mark.maybeslow,
]


@pytest.fixture()
def ci_base_environment(working_env, tmpdir):
    os.environ["CI_PROJECT_DIR"] = tmpdir.strpath
    os.environ["CI_PIPELINE_ID"] = "7192"
    os.environ["CI_JOB_NAME"] = "mock"


@pytest.fixture(scope="function")
def mock_git_repo(git, tmpdir):
    """Create a mock git repo with two commits, the last one creating
    a .gitlab-ci.yml"""

    repo_path = tmpdir.join("mockspackrepo").strpath
    mkdirp(repo_path)

    with working_dir(repo_path):
        git("init")

        with open("README.md", "w", encoding="utf-8") as f:
            f.write("# Introduction")

        with open(".gitlab-ci.yml", "w", encoding="utf-8") as f:
            f.write(
                """
testjob:
    script:
        - echo "success"
            """
            )

        git("config", "--local", "user.email", "testing@spack.io")
        git("config", "--local", "user.name", "Spack Testing")

        # initial commit with README
        git("add", "README.md")
        git("-c", "commit.gpgsign=false", "commit", "-m", "initial commit")

        # second commit, adding a .gitlab-ci.yml
        git("add", ".gitlab-ci.yml")
        git("-c", "commit.gpgsign=false", "commit", "-m", "add a .gitlab-ci.yml")

        yield repo_path


@pytest.fixture()
def ci_generate_test(tmp_path, mutable_mock_env_path, install_mockery, ci_base_environment):
    """Returns a function that creates a new test environment, and runs 'spack generate'
    on it, given the content of the spack.yaml file.

    Additional positional arguments will be added to the 'spack generate' call.
    """

    def _func(spack_yaml_content, *args, fail_on_error=True):
        spack_yaml = tmp_path / "spack.yaml"
        spack_yaml.write_text(spack_yaml_content)

        env_cmd("create", "test", str(spack_yaml))
        outputfile = tmp_path / ".gitlab-ci.yml"
        with ev.read("test"):
            output = ci_cmd(
                "generate",
                "--output-file",
                str(outputfile),
                *args,
                output=str,
                fail_on_error=fail_on_error,
            )

        return spack_yaml, outputfile, output

    return _func


def test_ci_generate_with_env(ci_generate_test, tmp_path, mock_binary_index):
    """Make sure we can get a .gitlab-ci.yml from an environment file
    which has the gitlab-ci, cdash, and mirrors sections.
    """
    mirror_url = tmp_path / "ci-mirror"
    spack_yaml, outputfile, _ = ci_generate_test(
        f"""\
spack:
  definitions:
    - old-gcc-pkgs:
      - archive-files
      - callpath
      # specify ^openblas-with-lapack to ensure that builtin.mock repo flake8
      # package (which can also provide lapack) is not chosen, as it violates
      # a package-level check which requires exactly one fetch strategy (this
      # is apparently not an issue for other tests that use it).
      - hypre@0.2.15 ^openblas-with-lapack
  specs:
    - matrix:
      - [$old-gcc-pkgs]
  mirrors:
    buildcache-destination: {mirror_url}
  ci:
    pipeline-gen:
    - submapping:
      - match:
          - arch=test-debian6-core2
        build-job:
          tags:
            - donotcare
          image: donotcare
      - match:
          - arch=test-debian6-m1
        build-job:
          tags:
            - donotcare
          image: donotcare
    - cleanup-job:
        image: donotcare
        tags: [donotcare]
    - reindex-job:
        script:: [hello, world]
        custom_attribute: custom!
  cdash:
    build-group: Not important
    url: https://my.fake.cdash
    project: Not used
    site: Nothing
""",
        "--artifacts-root",
        str(tmp_path / "my_artifacts_root"),
    )
    yaml_contents = syaml.load(outputfile.read_text())

    assert "workflow" in yaml_contents
    assert "rules" in yaml_contents["workflow"]
    assert yaml_contents["workflow"]["rules"] == [{"when": "always"}]

    assert "stages" in yaml_contents
    assert len(yaml_contents["stages"]) == 6
    assert yaml_contents["stages"][0] == "stage-0"
    assert yaml_contents["stages"][5] == "stage-rebuild-index"

    assert "rebuild-index" in yaml_contents
    rebuild_job = yaml_contents["rebuild-index"]
    assert (
        rebuild_job["script"][0] == f"spack buildcache update-index --keys {mirror_url.as_uri()}"
    )
    assert rebuild_job["custom_attribute"] == "custom!"

    assert "variables" in yaml_contents
    assert "SPACK_ARTIFACTS_ROOT" in yaml_contents["variables"]
    assert yaml_contents["variables"]["SPACK_ARTIFACTS_ROOT"] == "my_artifacts_root"


def test_ci_generate_with_env_missing_section(ci_generate_test, tmp_path, mock_binary_index):
    """Make sure we get a reasonable message if we omit gitlab-ci section"""
    env_yaml = f"""\
spack:
  specs:
    - archive-files
  mirrors:
    buildcache-destination: {tmp_path / 'ci-mirror'}
"""
    expect = "Environment does not have a `ci` configuration"
    with pytest.raises(ci.SpackCIError, match=expect):
        ci_generate_test(env_yaml)


def test_ci_generate_with_cdash_token(ci_generate_test, tmp_path, mock_binary_index, monkeypatch):
    """Make sure we it doesn't break if we configure cdash"""
    monkeypatch.setenv("SPACK_CDASH_AUTH_TOKEN", "notreallyatokenbutshouldnotmatter")
    spack_yaml_content = f"""\
spack:
  specs:
    - archive-files
  mirrors:
    buildcache-destination: {tmp_path / "ci-mirror"}
  ci:
    pipeline-gen:
    - submapping:
      - match:
          - archive-files
        build-job:
          tags:
            - donotcare
          image: donotcare
  cdash:
    build-group: Not important
    url: {(tmp_path / "cdash").as_uri()}
    project: Not used
    site: Nothing
"""

    def _urlopen(*args, **kwargs):
        return MockHTTPResponse.with_json(200, "OK", headers={}, body={})

    monkeypatch.setattr(ci.common, "_urlopen", _urlopen)

    spack_yaml, original_file, output = ci_generate_test(spack_yaml_content)
    yaml_contents = syaml.load(original_file.read_text())

    # That fake token should have resulted in being unable to
    # register build group with cdash, but the workload should
    # still have been generated.
    assert "Failed to create or retrieve buildgroup" in output
    expected_keys = ["rebuild-index", "stages", "variables", "workflow"]
    assert all([key in yaml_contents.keys() for key in expected_keys])


def test_ci_generate_with_custom_settings(
    ci_generate_test, tmp_path, mock_binary_index, monkeypatch
):
    """Test use of user-provided scripts and attributes"""
    monkeypatch.setattr(spack, "get_version", lambda: "0.15.3")
    monkeypatch.setattr(spack, "get_spack_commit", lambda: "big ol commit sha")
    spack_yaml, outputfile, _ = ci_generate_test(
        f"""\
spack:
  specs:
    - archive-files
  mirrors:
    buildcache-destination: {tmp_path / "ci-mirror"}
  ci:
    pipeline-gen:
    - submapping:
      - match:
          - archive-files
        build-job:
          tags:
            - donotcare
          variables:
            ONE: plain-string-value
            TWO: ${{INTERP_ON_BUILD}}
          before_script:
            - mkdir /some/path
            - pushd /some/path
            - git clone ${{SPACK_REPO}}
            - cd spack
            - git checkout ${{SPACK_REF}}
            - popd
          script:
            - spack -d ci rebuild
          after_script:
            - rm -rf /some/path/spack
          custom_attribute: custom!
          artifacts:
            paths:
            - some/custom/artifact
"""
    )
    yaml_contents = syaml.load(outputfile.read_text())

    assert yaml_contents["variables"]["SPACK_VERSION"] == "0.15.3"
    assert yaml_contents["variables"]["SPACK_CHECKOUT_VERSION"] == "big ol commit sha"

    assert any("archive-files" in key for key in yaml_contents)
    for ci_key, ci_obj in yaml_contents.items():
        if "archive-files" not in ci_key:
            continue

        # Ensure we have variables, possibly interpolated
        assert ci_obj["variables"]["ONE"] == "plain-string-value"
        assert ci_obj["variables"]["TWO"] == "${INTERP_ON_BUILD}"

        # Ensure we have scripts verbatim
        assert ci_obj["before_script"] == [
            "mkdir /some/path",
            "pushd /some/path",
            "git clone ${SPACK_REPO}",
            "cd spack",
            "git checkout ${SPACK_REF}",
            "popd",
        ]
        assert ci_obj["script"][1].startswith("cd ")
        ci_obj["script"][1] = "cd ENV"
        assert ci_obj["script"] == [
            "spack -d ci rebuild",
            "cd ENV",
            "spack env activate --without-view .",
            "spack ci rebuild",
        ]
        assert ci_obj["after_script"] == ["rm -rf /some/path/spack"]

        # Ensure we have the custom attributes
        assert "some/custom/artifact" in ci_obj["artifacts"]["paths"]
        assert ci_obj["custom_attribute"] == "custom!"


def test_ci_generate_pkg_with_deps(ci_generate_test, tmp_path, ci_base_environment):
    """Test pipeline generation for a package w/ dependencies"""
    spack_yaml, outputfile, _ = ci_generate_test(
        f"""\
spack:
  specs:
    - dependent-install
  mirrors:
    buildcache-destination: {tmp_path / 'ci-mirror'}
  ci:
    pipeline-gen:
    - submapping:
      - match:
          - dependent-install
        build-job:
          tags:
            - donotcare
      - match:
          - dependency-install
        build-job:
          tags:
            - donotcare
"""
    )
    yaml_contents = syaml.load(outputfile.read_text())

    found = []
    for ci_key, ci_obj in yaml_contents.items():
        if "dependency-install" in ci_key:
            assert "stage" in ci_obj
            assert ci_obj["stage"] == "stage-0"
            found.append("dependency-install")
        if "dependent-install" in ci_key:
            assert "stage" in ci_obj
            assert ci_obj["stage"] == "stage-1"
            found.append("dependent-install")

    assert "dependent-install" in found
    assert "dependency-install" in found


def test_ci_generate_for_pr_pipeline(ci_generate_test, tmp_path, monkeypatch):
    """Test generation of a PR pipeline with disabled rebuild-index"""
    monkeypatch.setenv("SPACK_PIPELINE_TYPE", "spack_pull_request")

    spack_yaml, outputfile, _ = ci_generate_test(
        f"""\
spack:
  specs:
    - dependent-install
  mirrors:
    buildcache-destination: {tmp_path / 'ci-mirror'}
  ci:
    pipeline-gen:
    - submapping:
      - match:
          - dependent-install
        build-job:
          tags:
            - donotcare
      - match:
          - dependency-install
        build-job:
          tags:
            - donotcare
    - cleanup-job:
        image: donotcare
        tags: [donotcare]
    rebuild-index: False
"""
    )
    yaml_contents = syaml.load(outputfile.read_text())

    assert "rebuild-index" not in yaml_contents
    assert "variables" in yaml_contents
    assert "SPACK_PIPELINE_TYPE" in yaml_contents["variables"]
    assert (
        ci.common.PipelineType[yaml_contents["variables"]["SPACK_PIPELINE_TYPE"]]
        == ci.common.PipelineType.PULL_REQUEST
    )


def test_ci_generate_with_external_pkg(ci_generate_test, tmp_path, monkeypatch):
    """Make sure we do not generate jobs for external pkgs"""
    spack_yaml, outputfile, _ = ci_generate_test(
        f"""\
spack:
  specs:
    - archive-files
    - externaltest
  mirrors:
    buildcache-destination: {tmp_path / "ci-mirror"}
  ci:
    pipeline-gen:
    - submapping:
      - match:
          - archive-files
          - externaltest
        build-job:
          tags:
            - donotcare
          image: donotcare
"""
    )
    yaml_contents = syaml.load(outputfile.read_text())
    # Check that the "externaltool" package was not erroneously staged
    assert all("externaltool" not in key for key in yaml_contents)


def test_ci_rebuild_missing_config(tmp_path, working_env, mutable_mock_env_path):
    spack_yaml = tmp_path / "spack.yaml"
    spack_yaml.write_text(
        """
    spack:
      specs:
        - archive-files
    """
    )

    env_cmd("create", "test", str(spack_yaml))
    env_cmd("activate", "--without-view", "--sh", "test")
    out = ci_cmd("rebuild", fail_on_error=False)
    assert "env containing ci" in out
    env_cmd("deactivate")


def _signing_key():
    signing_key_path = pathlib.Path(spack_paths.mock_gpg_keys_path) / "package-signing-key"
    return signing_key_path.read_text()


class RebuildEnv(NamedTuple):
    broken_spec_file: pathlib.Path
    ci_job_url: str
    ci_pipeline_url: str
    env_dir: pathlib.Path
    log_dir: pathlib.Path
    mirror_dir: pathlib.Path
    mirror_url: str
    repro_dir: pathlib.Path
    root_spec_dag_hash: str
    test_dir: pathlib.Path
    working_dir: pathlib.Path


def create_rebuild_env(
    tmp_path: pathlib.Path, pkg_name: str, broken_tests: bool = False
) -> RebuildEnv:
    scratch = tmp_path / "working_dir"
    log_dir = scratch / "logs"
    repro_dir = scratch / "repro"
    test_dir = scratch / "test"
    env_dir = scratch / "concrete_env"
    mirror_dir = scratch / "mirror"
    broken_specs_path = scratch / "naughty-list"

    mirror_url = mirror_dir.as_uri()

    ci_job_url = "https://some.domain/group/project/-/jobs/42"
    ci_pipeline_url = "https://some.domain/group/project/-/pipelines/7"

    env_dir.mkdir(parents=True)
    with open(env_dir / "spack.yaml", "w", encoding="utf-8") as f:
        f.write(
            f"""
spack:
  definitions:
    - packages: [{pkg_name}]
  specs:
    - $packages
  mirrors:
    buildcache-destination: {mirror_dir}
  ci:
    broken-specs-url: {broken_specs_path.as_uri()}
    broken-tests-packages: {json.dumps([pkg_name] if broken_tests else [])}
    pipeline-gen:
    - submapping:
      - match:
          - {pkg_name}
        build-job:
          tags:
            - donotcare
          image: donotcare
  cdash:
    build-group: Not important
    url: https://my.fake.cdash
    project: Not used
    site: Nothing
"""
        )

    with ev.Environment(env_dir) as env:
        env.concretize()
        env.write()

    shutil.copy(env_dir / "spack.yaml", tmp_path / "spack.yaml")

    root_spec_dag_hash = env.concrete_roots()[0].dag_hash()

    return RebuildEnv(
        broken_spec_file=broken_specs_path / root_spec_dag_hash,
        ci_job_url=ci_job_url,
        ci_pipeline_url=ci_pipeline_url,
        env_dir=env_dir,
        log_dir=log_dir,
        mirror_dir=mirror_dir,
        mirror_url=mirror_url,
        repro_dir=repro_dir,
        root_spec_dag_hash=root_spec_dag_hash,
        test_dir=test_dir,
        working_dir=scratch,
    )


def activate_rebuild_env(tmp_path: pathlib.Path, pkg_name: str, rebuild_env: RebuildEnv):
    env_cmd("activate", "--without-view", "--sh", "-d", ".")

    # Create environment variables as gitlab would do it
    os.environ.update(
        {
            "SPACK_ARTIFACTS_ROOT": str(rebuild_env.working_dir),
            "SPACK_JOB_LOG_DIR": str(rebuild_env.log_dir),
            "SPACK_JOB_REPRO_DIR": str(rebuild_env.repro_dir),
            "SPACK_JOB_TEST_DIR": str(rebuild_env.test_dir),
            "SPACK_LOCAL_MIRROR_DIR": str(rebuild_env.mirror_dir),
            "SPACK_CONCRETE_ENV_DIR": str(rebuild_env.env_dir),
            "CI_PIPELINE_ID": "7192",
            "SPACK_SIGNING_KEY": _signing_key(),
            "SPACK_JOB_SPEC_DAG_HASH": rebuild_env.root_spec_dag_hash,
            "SPACK_JOB_SPEC_PKG_NAME": pkg_name,
            "SPACK_COMPILER_ACTION": "NONE",
            "SPACK_CDASH_BUILD_NAME": pkg_name,
            "SPACK_REMOTE_MIRROR_URL": rebuild_env.mirror_url,
            "SPACK_PIPELINE_TYPE": "spack_protected_branch",
            "CI_JOB_URL": rebuild_env.ci_job_url,
            "CI_PIPELINE_URL": rebuild_env.ci_pipeline_url,
            "CI_PROJECT_DIR": str(tmp_path / "ci-project"),
        }
    )


@pytest.mark.parametrize("broken_tests", [True, False])
def test_ci_rebuild_mock_success(
    tmp_path: pathlib.Path,
    working_env,
    mutable_mock_env_path,
    install_mockery,
    mock_gnupghome,
    mock_fetch,
    mock_binary_index,
    monkeypatch,
    broken_tests,
):
    pkg_name = "archive-files"
    rebuild_env = create_rebuild_env(tmp_path, pkg_name, broken_tests)

    monkeypatch.setattr(spack.cmd.ci, "SPACK_COMMAND", "echo")

    with working_dir(rebuild_env.env_dir):
        activate_rebuild_env(tmp_path, pkg_name, rebuild_env)

        out = ci_cmd("rebuild", "--tests", fail_on_error=False)

        # We didn"t really run the build so build output file(s) are missing
        assert "Unable to copy files" in out
        assert "No such file or directory" in out

        if broken_tests:
            # We generate a skipped tests report in this case
            assert "Unable to run stand-alone tests" in out
        else:
            # No installation means no package to test and no test log to copy
            assert "Cannot copy test logs" in out


def test_ci_rebuild_mock_failure_to_push(
    tmp_path: pathlib.Path,
    working_env,
    mutable_mock_env_path,
    install_mockery,
    mock_gnupghome,
    mock_fetch,
    mock_binary_index,
    ci_base_environment,
    monkeypatch,
):
    pkg_name = "trivial-install-test-package"
    rebuild_env = create_rebuild_env(tmp_path, pkg_name)

    # Mock the install script succuess
    def mock_success(*args, **kwargs):
        return 0

    monkeypatch.setattr(ci, "process_command", mock_success)

    # Mock failure to push to the build cache
    def mock_push_or_raise(*args, **kwargs):
        raise spack.binary_distribution.PushToBuildCacheError(
            "Encountered problem pushing binary <url>: <expection>"
        )

    monkeypatch.setattr(spack.binary_distribution.Uploader, "push_or_raise", mock_push_or_raise)

    with working_dir(rebuild_env.env_dir):
        activate_rebuild_env(tmp_path, pkg_name, rebuild_env)

        expect = f"Command exited with code {FAILED_CREATE_BUILDCACHE_CODE}"
        with pytest.raises(spack.main.SpackCommandError, match=expect):
            ci_cmd("rebuild", fail_on_error=True)


def test_ci_require_signing(
    tmp_path: pathlib.Path,
    working_env,
    mutable_mock_env_path,
    mock_gnupghome,
    ci_base_environment,
    monkeypatch,
):
    spack_yaml = tmp_path / "spack.yaml"
    spack_yaml.write_text(
        f"""
spack:
 specs:
   - archive-files
 mirrors:
   buildcache-destination: {tmp_path / "ci-mirror"}
 ci:
   pipeline-gen:
   - submapping:
     - match:
         - archive-files
       build-job:
         tags:
           - donotcare
         image: donotcare
"""
    )
    env_cmd("activate", "--without-view", "--sh", "-d", str(spack_yaml.parent))

    # Run without the variable to make sure we don't accidentally require signing
    output = ci_cmd("rebuild", output=str, fail_on_error=False)
    assert "spack must have exactly one signing key" not in output

    # Now run with the variable to make sure it works
    monkeypatch.setenv("SPACK_REQUIRE_SIGNING", "True")
    output = ci_cmd("rebuild", output=str, fail_on_error=False)
    assert "spack must have exactly one signing key" in output
    env_cmd("deactivate")


def test_ci_nothing_to_rebuild(
    tmp_path: pathlib.Path,
    working_env,
    mutable_mock_env_path,
    install_mockery,
    monkeypatch,
    mock_fetch,
    ci_base_environment,
    mock_binary_index,
):
    scratch = tmp_path / "working_dir"
    mirror_dir = scratch / "mirror"
    mirror_url = mirror_dir.as_uri()

    with open(tmp_path / "spack.yaml", "w", encoding="utf-8") as f:
        f.write(
            f"""
spack:
 definitions:
   - packages: [archive-files]
 specs:
   - $packages
 mirrors:
   buildcache-destination: {mirror_url}
 ci:
   pipeline-gen:
   - submapping:
     - match:
         - archive-files
       build-job:
         tags:
           - donotcare
         image: donotcare
"""
        )

    install_cmd("archive-files")
    buildcache_cmd("push", "-f", "-u", "--update-index", mirror_url, "archive-files")

    with working_dir(tmp_path):
        env_cmd("create", "test", "./spack.yaml")
        with ev.read("test") as env:
            env.concretize()

            # Create environment variables as gitlab would do it
            os.environ.update(
                {
                    "SPACK_ARTIFACTS_ROOT": str(scratch),
                    "SPACK_JOB_LOG_DIR": "log_dir",
                    "SPACK_JOB_REPRO_DIR": "repro_dir",
                    "SPACK_JOB_TEST_DIR": "test_dir",
                    "SPACK_CONCRETE_ENV_DIR": str(tmp_path),
                    "SPACK_JOB_SPEC_DAG_HASH": env.concrete_roots()[0].dag_hash(),
                    "SPACK_JOB_SPEC_PKG_NAME": "archive-files",
                    "SPACK_COMPILER_ACTION": "NONE",
                }
            )

            ci_out = ci_cmd("rebuild", output=str)

            assert "No need to rebuild archive-files" in ci_out

            env_cmd("deactivate")


@pytest.mark.disable_clean_stage_check
def test_push_to_build_cache(
    tmp_path: pathlib.Path,
    mutable_mock_env_path,
    install_mockery,
    mock_fetch,
    mock_gnupghome,
    ci_base_environment,
    mock_binary_index,
):
    scratch = tmp_path / "working_dir"
    mirror_dir = scratch / "mirror"
    mirror_url = mirror_dir.as_uri()

    ci.import_signing_key(_signing_key())

    with working_dir(tmp_path):
        with open("spack.yaml", "w", encoding="utf-8") as f:
            f.write(
                f"""\
spack:
 definitions:
   - packages: [patchelf]
 specs:
   - $packages
 mirrors:
   buildcache-destination: {mirror_url}
 ci:
   pipeline-gen:
   - submapping:
     - match:
         - patchelf
       build-job:
         tags:
           - donotcare
         image: donotcare
   - cleanup-job:
       tags:
         - nonbuildtag
       image: basicimage
   - any-job:
       tags:
         - nonbuildtag
       image: basicimage
       custom_attribute: custom!
"""
            )
        env_cmd("create", "test", "./spack.yaml")
        with ev.read("test") as current_env:
            current_env.concretize()
            install_cmd("--keep-stage")

            concrete_spec = list(current_env.roots())[0]
            spec_json = concrete_spec.to_json(hash=ht.dag_hash)
            json_path = str(tmp_path / "spec.json")
            with open(json_path, "w", encoding="utf-8") as ypfd:
                ypfd.write(spec_json)

            for s in concrete_spec.traverse():
                ci.push_to_build_cache(s, mirror_url, True)

            # Now test the --prune-dag (default) option of spack ci generate
            mirror_cmd("add", "test-ci", mirror_url)

            outputfile_pruned = str(tmp_path / "pruned_pipeline.yml")
            ci_cmd("generate", "--output-file", outputfile_pruned)

            with open(outputfile_pruned, encoding="utf-8") as f:
                contents = f.read()
                yaml_contents = syaml.load(contents)
                # Make sure there are no other spec jobs or rebuild-index
                assert set(yaml_contents.keys()) == {"no-specs-to-rebuild", "workflow"}

                the_elt = yaml_contents["no-specs-to-rebuild"]
                assert "tags" in the_elt
                assert "nonbuildtag" in the_elt["tags"]
                assert "image" in the_elt
                assert the_elt["image"] == "basicimage"
                assert the_elt["custom_attribute"] == "custom!"

                assert "rules" in yaml_contents["workflow"]
                assert yaml_contents["workflow"]["rules"] == [{"when": "always"}]

            outputfile_not_pruned = str(tmp_path / "unpruned_pipeline.yml")
            ci_cmd("generate", "--no-prune-dag", "--output-file", outputfile_not_pruned)

            # Test the --no-prune-dag option of spack ci generate
            with open(outputfile_not_pruned, encoding="utf-8") as f:
                contents = f.read()
                yaml_contents = syaml.load(contents)

                found_spec_job = False

                for ci_key in yaml_contents.keys():
                    if "patchelf" in ci_key:
                        the_elt = yaml_contents[ci_key]
                        assert "variables" in the_elt
                        job_vars = the_elt["variables"]
                        assert "SPACK_SPEC_NEEDS_REBUILD" in job_vars
                        assert job_vars["SPACK_SPEC_NEEDS_REBUILD"] == "False"
                        assert the_elt["custom_attribute"] == "custom!"
                        found_spec_job = True

                assert found_spec_job

            mirror_cmd("rm", "test-ci")

            # Test generating buildcache index while we have bin mirror
            buildcache_cmd("update-index", mirror_url)

            # Validate resulting buildcache (database) index
            layout_version = spack.binary_distribution.CURRENT_BUILD_CACHE_LAYOUT_VERSION
            url_and_version = spack.binary_distribution.MirrorURLAndVersion(
                mirror_url, layout_version
            )
            index_fetcher = spack.binary_distribution.DefaultIndexFetcher(url_and_version, None)
            result = index_fetcher.conditional_fetch()
            _vendoring.jsonschema.validate(json.loads(result.data), db_idx_schema)

            # Now that index is regenerated, validate "buildcache list" output
            assert "patchelf" in buildcache_cmd("list", output=str)

            logs_dir = scratch / "logs_dir"
            logs_dir.mkdir()
            ci.copy_stage_logs_to_artifacts(concrete_spec, str(logs_dir))
            assert "spack-build-out.txt.gz" in os.listdir(logs_dir)


def test_push_to_build_cache_exceptions(monkeypatch, tmp_path, capsys):
    def push_or_raise(*args, **kwargs):
        raise spack.binary_distribution.PushToBuildCacheError("Error: Access Denied")

    monkeypatch.setattr(spack.binary_distribution.Uploader, "push_or_raise", push_or_raise)

    # Input doesn't matter, as we are faking exceptional output
    url = tmp_path.as_uri()
    ci.push_to_build_cache(None, url, None)
    assert f"Problem writing to {url}: Error: Access Denied" in capsys.readouterr().err


@pytest.mark.parametrize("match_behavior", ["first", "merge"])
@pytest.mark.parametrize("git_version", ["big ol commit sha", None])
def test_ci_generate_override_runner_attrs(
    ci_generate_test, tmp_path, monkeypatch, match_behavior, git_version
):
    """Test that we get the behavior we want with respect to the provision
    of runner attributes like tags, variables, and scripts, both when we
    inherit them from the top level, as well as when we override one or
    more at the runner level"""
    monkeypatch.setattr(spack, "spack_version", "0.20.0.test0")
    monkeypatch.setattr(spack, "get_version", lambda: "0.20.0.test0 (blah)")
    monkeypatch.setattr(spack, "get_spack_commit", lambda: git_version)
    spack_yaml, outputfile, _ = ci_generate_test(
        f"""\
spack:
  specs:
    - dependent-install
    - pkg-a
  mirrors:
    buildcache-destination: {tmp_path / "ci-mirror"}
  ci:
    pipeline-gen:
    - match_behavior: {match_behavior}
      submapping:
        - match:
            - dependent-install
          build-job:
            tags:
              - specific-one
            variables:
              THREE: specificvarthree
        - match:
            - dependency-install
        - match:
            - pkg-a
          build-job:
            tags:
              - specific-a-2
        - match:
            - pkg-a
          build-job-remove:
            tags:
              - toplevel2
          build-job:
            tags:
              - specific-a
            variables:
              ONE: specificvarone
              TWO: specificvartwo
            before_script::
              - - custom pre step one
            script::
              - - custom main step
            after_script::
              - custom post step one
    - build-job:
        tags:
          - toplevel
          - toplevel2
        variables:
          ONE: toplevelvarone
          TWO: toplevelvartwo
        before_script:
          - - pre step one
            - pre step two
        script::
          - - main step
        after_script:
          - - post step one
    - cleanup-job:
        image: donotcare
        tags: [donotcare]
"""
    )

    yaml_contents = syaml.load(outputfile.read_text())

    assert "variables" in yaml_contents
    global_vars = yaml_contents["variables"]
    assert "SPACK_VERSION" in global_vars
    assert global_vars["SPACK_VERSION"] == "0.20.0.test0 (blah)"
    assert "SPACK_CHECKOUT_VERSION" in global_vars
    assert global_vars["SPACK_CHECKOUT_VERSION"] == git_version or "v0.20.0.test0"

    for ci_key in yaml_contents.keys():
        if ci_key.startswith("pkg-a"):
            # Make sure pkg-a's attributes override variables, and all the
            # scripts.  Also, make sure the 'toplevel' tag doesn't
            # appear twice, but that a's specific extra tag does appear
            the_elt = yaml_contents[ci_key]
            assert the_elt["variables"]["ONE"] == "specificvarone"
            assert the_elt["variables"]["TWO"] == "specificvartwo"
            assert "THREE" not in the_elt["variables"]
            assert len(the_elt["tags"]) == (2 if match_behavior == "first" else 3)
            assert "specific-a" in the_elt["tags"]
            if match_behavior == "merge":
                assert "specific-a-2" in the_elt["tags"]
            assert "toplevel" in the_elt["tags"]
            assert "toplevel2" not in the_elt["tags"]
            assert len(the_elt["before_script"]) == 1
            assert the_elt["before_script"][0] == "custom pre step one"
            assert len(the_elt["script"]) == 1
            assert the_elt["script"][0] == "custom main step"
            assert len(the_elt["after_script"]) == 1
            assert the_elt["after_script"][0] == "custom post step one"
        if "dependency-install" in ci_key:
            # Since the dependency-install match omits any
            # runner-attributes, make sure it inherited all the
            # top-level attributes.
            the_elt = yaml_contents[ci_key]
            assert the_elt["variables"]["ONE"] == "toplevelvarone"
            assert the_elt["variables"]["TWO"] == "toplevelvartwo"
            assert "THREE" not in the_elt["variables"]
            assert len(the_elt["tags"]) == 2
            assert "toplevel" in the_elt["tags"]
            assert "toplevel2" in the_elt["tags"]
            assert len(the_elt["before_script"]) == 2
            assert the_elt["before_script"][0] == "pre step one"
            assert the_elt["before_script"][1] == "pre step two"
            assert len(the_elt["script"]) == 1
            assert the_elt["script"][0] == "main step"
            assert len(the_elt["after_script"]) == 1
            assert the_elt["after_script"][0] == "post step one"
        if "dependent-install" in ci_key:
            # The dependent-install match specifies that we keep the two
            # top level variables, but add a third specifc one.  It
            # also adds a custom tag which should be combined with
            # the top-level tag.
            the_elt = yaml_contents[ci_key]
            assert the_elt["variables"]["ONE"] == "toplevelvarone"
            assert the_elt["variables"]["TWO"] == "toplevelvartwo"
            assert the_elt["variables"]["THREE"] == "specificvarthree"
            assert len(the_elt["tags"]) == 3
            assert "specific-one" in the_elt["tags"]
            assert "toplevel" in the_elt["tags"]
            assert "toplevel2" in the_elt["tags"]
            assert len(the_elt["before_script"]) == 2
            assert the_elt["before_script"][0] == "pre step one"
            assert the_elt["before_script"][1] == "pre step two"
            assert len(the_elt["script"]) == 1
            assert the_elt["script"][0] == "main step"
            assert len(the_elt["after_script"]) == 1
            assert the_elt["after_script"][0] == "post step one"


def test_ci_rebuild_index(
    tmp_path: pathlib.Path, working_env, mutable_mock_env_path, install_mockery, mock_fetch, capsys
):
    scratch = tmp_path / "working_dir"
    mirror_dir = scratch / "mirror"
    mirror_url = mirror_dir.as_uri()

    with open(tmp_path / "spack.yaml", "w", encoding="utf-8") as f:
        f.write(
            f"""
spack:
  specs:
  - callpath
  mirrors:
    buildcache-destination: {mirror_url}
  ci:
    pipeline-gen:
    - submapping:
      - match:
        - patchelf
        build-job:
          tags:
          - donotcare
          image: donotcare
"""
        )

    with working_dir(tmp_path):
        env_cmd("create", "test", "./spack.yaml")
        with ev.read("test"):
            concrete_spec = spack.concretize.concretize_one("callpath")
            with open(tmp_path / "spec.json", "w", encoding="utf-8") as f:
                f.write(concrete_spec.to_json(hash=ht.dag_hash))

            install_cmd("--fake", str(tmp_path / "spec.json"))
            buildcache_cmd("push", "-u", "-f", mirror_url, "callpath")
            ci_cmd("rebuild-index")

            with capsys.disabled():
                output = buildcache_cmd("list", "-L", "--allarch")
                assert concrete_spec.dag_hash() + " callpath" in output


def test_ci_get_stack_changed(mock_git_repo, monkeypatch):
    """Test that we can detect the change to .gitlab-ci.yml in a
    mock spack git repo."""
    monkeypatch.setattr(spack.paths, "prefix", mock_git_repo)
    assert ci.get_stack_changed("/no/such/env/path") is True


def test_ci_generate_prune_untouched(ci_generate_test, tmp_path, monkeypatch):
    """Test pipeline generation with pruning works to eliminate
    specs that were not affected by a change"""
    monkeypatch.setenv("SPACK_PRUNE_UNTOUCHED", "TRUE")  # enables pruning of untouched specs

    def fake_compute_affected(r1=None, r2=None):
        return ["libdwarf"]

    def fake_stack_changed(env_path, rev1="HEAD^", rev2="HEAD"):
        return False

    monkeypatch.setattr(ci, "compute_affected_packages", fake_compute_affected)
    monkeypatch.setattr(ci, "get_stack_changed", fake_stack_changed)

    spack_yaml, outputfile, _ = ci_generate_test(
        f"""\
spack:
  specs:
    - archive-files
    - callpath
  mirrors:
    buildcache-destination: {tmp_path / 'ci-mirror'}
  ci:
    pipeline-gen:
    - build-job:
        tags:
          - donotcare
        image: donotcare
"""
    )

    # Dependency graph rooted at callpath
    # callpath -> dyninst -> libelf
    #                     -> libdwarf -> libelf
    #          -> mpich
    env_hashes = {}
    with ev.read("test") as active_env:
        active_env.concretize()
        for s in active_env.all_specs():
            env_hashes[s.name] = s.dag_hash()

    yaml_contents = syaml.load(outputfile.read_text())

    generated_hashes = []
    for ci_key in yaml_contents.keys():
        if "variables" in yaml_contents[ci_key]:
            generated_hashes.append(yaml_contents[ci_key]["variables"]["SPACK_JOB_SPEC_DAG_HASH"])

    assert env_hashes["archive-files"] not in generated_hashes
    for spec_name in ["callpath", "dyninst", "mpich", "libdwarf", "libelf"]:
        assert env_hashes[spec_name] in generated_hashes


def test_ci_subcommands_without_mirror(
    tmp_path: pathlib.Path,
    mutable_mock_env_path,
    install_mockery,
    ci_base_environment,
    mock_binary_index,
):
    """Make sure we catch if there is not a mirror and report an error"""
    with open(tmp_path / "spack.yaml", "w", encoding="utf-8") as f:
        f.write(
            """\
spack:
  specs:
    - archive-files
  ci:
    pipeline-gen:
    - submapping:
      - match:
          - archive-files
        build-job:
          tags:
            - donotcare
          image: donotcare
"""
        )

    with working_dir(tmp_path):
        env_cmd("create", "test", "./spack.yaml")

        with ev.read("test"):
            # Check the 'generate' subcommand
            expect = "spack ci generate requires a mirror named 'buildcache-destination'"
            with pytest.raises(ci.SpackCIError, match=expect):
                ci_cmd("generate", "--output-file", str(tmp_path / ".gitlab-ci.yml"))

            # Also check the 'rebuild-index' subcommand
            output = ci_cmd("rebuild-index", output=str, fail_on_error=False)
            assert "spack ci rebuild-index requires an env containing a mirror" in output


def test_ci_generate_read_broken_specs_url(
    tmp_path: pathlib.Path,
    mutable_mock_env_path,
    install_mockery,
    mock_packages,
    monkeypatch,
    ci_base_environment,
):
    """Verify that `broken-specs-url` works as intended"""
    spec_a = spack.concretize.concretize_one("pkg-a")
    a_dag_hash = spec_a.dag_hash()

    spec_flattendeps = spack.concretize.concretize_one("dependent-install")
    flattendeps_dag_hash = spec_flattendeps.dag_hash()

    broken_specs_url = tmp_path.as_uri()

    # Mark 'a' as broken (but not 'dependent-install')
    broken_spec_a_url = "{0}/{1}".format(broken_specs_url, a_dag_hash)
    job_stack = "job_stack"
    a_job_url = "a_job_url"
    ci.write_broken_spec(
        broken_spec_a_url, spec_a.name, job_stack, a_job_url, "pipeline_url", spec_a.to_dict()
    )

    # Test that `spack ci generate` notices this broken spec and fails.
    with open(tmp_path / "spack.yaml", "w", encoding="utf-8") as f:
        f.write(
            f"""\
spack:
  specs:
    - dependent-install
    - pkg-a
  mirrors:
    buildcache-destination: {(tmp_path / "ci-mirror").as_uri()}
  ci:
    broken-specs-url: "{broken_specs_url}"
    pipeline-gen:
    - submapping:
      - match:
          - pkg-a
          - dependent-install
          - pkg-b
          - dependency-install
        build-job:
          tags:
            - donotcare
          image: donotcare
"""
        )

    with working_dir(tmp_path):
        env_cmd("create", "test", "./spack.yaml")
        with ev.read("test"):
            # Check output of the 'generate' subcommand
            output = ci_cmd("generate", output=str, fail_on_error=False)
            assert "known to be broken" in output

            expected = (
                f"{spec_a.name}/{a_dag_hash[:7]} (in stack {job_stack}) was "
                f"reported broken here: {a_job_url}"
            )
            assert expected in output

            not_expected = f"dependent-install/{flattendeps_dag_hash[:7]} (in stack"
            assert not_expected not in output


def test_ci_generate_external_signing_job(ci_generate_test, tmp_path, monkeypatch):
    """Verify that in external signing mode: 1) each rebuild jobs includes
    the location where the binary hash information is written and 2) we
    properly generate a final signing job in the pipeline."""
    monkeypatch.setenv("SPACK_PIPELINE_TYPE", "spack_protected_branch")
    _, outputfile, _ = ci_generate_test(
        f"""\
spack:
  specs:
    - archive-files
  mirrors:
    buildcache-destination: {(tmp_path / "ci-mirror").as_uri()}
  ci:
    pipeline-gen:
    - submapping:
      - match:
          - archive-files
        build-job:
          tags:
            - donotcare
          image: donotcare
    - signing-job:
        tags:
          - nonbuildtag
          - secretrunner
        image:
          name: customdockerimage
          entrypoint: []
        variables:
          IMPORTANT_INFO: avalue
        script::
          - echo hello
        custom_attribute: custom!
"""
    )
    yaml_contents = syaml.load(outputfile.read_text())

    assert "sign-pkgs" in yaml_contents
    signing_job = yaml_contents["sign-pkgs"]
    assert "tags" in signing_job
    signing_job_tags = signing_job["tags"]
    for expected_tag in ["notary", "protected", "aws"]:
        assert expected_tag in signing_job_tags
    assert signing_job["custom_attribute"] == "custom!"


def test_ci_reproduce(
    tmp_path: pathlib.Path,
    mutable_mock_env_path,
    install_mockery,
    monkeypatch,
    last_two_git_commits,
    ci_base_environment,
    mock_binary_index,
):
    repro_dir = tmp_path / "repro_dir"
    image_name = "org/image:tag"

    with open(tmp_path / "spack.yaml", "w", encoding="utf-8") as f:
        f.write(
            f"""
spack:
 definitions:
   - packages: [archive-files]
 specs:
   - $packages
 mirrors:
   buildcache-destination: {tmp_path / "ci-mirror"}
 ci:
   pipeline-gen:
   - submapping:
     - match:
         - archive-files
       build-job:
         tags:
           - donotcare
         image: {image_name}
"""
        )

    with working_dir(tmp_path), ev.Environment(".") as env:
        env.concretize()
        env.write()

    def fake_download_and_extract_artifacts(url, work_dir, merge_commit_test=True):
        with working_dir(tmp_path), ev.Environment(".") as env:
            if not os.path.exists(repro_dir):
                repro_dir.mkdir()

            job_spec = env.concrete_roots()[0]
            with open(repro_dir / "archivefiles.json", "w", encoding="utf-8") as f:
                f.write(job_spec.to_json(hash=ht.dag_hash))
                artifacts_root = repro_dir / "jobs_scratch_dir"
                pipeline_path = artifacts_root / "pipeline.yml"

                ci_cmd(
                    "generate",
                    "--output-file",
                    str(pipeline_path),
                    "--artifacts-root",
                    str(artifacts_root),
                )

                job_name = gitlab_generator.get_job_name(job_spec)

                with open(repro_dir / "repro.json", "w", encoding="utf-8") as f:
                    f.write(
                        json.dumps(
                            {
                                "job_name": job_name,
                                "job_spec_json": "archivefiles.json",
                                "ci_project_dir": str(repro_dir),
                            }
                        )
                    )

                with open(repro_dir / "install.sh", "w", encoding="utf-8") as f:
                    f.write("#!/bin/sh\n\n#fake install\nspack install blah\n")

                with open(repro_dir / "spack_info.txt", "w", encoding="utf-8") as f:
                    if merge_commit_test:
                        f.write(
                            f"\nMerge {last_two_git_commits[1]} into {last_two_git_commits[0]}\n\n"
                        )
                    else:
                        f.write(f"\ncommit {last_two_git_commits[1]}\n\n")

            return "jobs_scratch_dir"

    monkeypatch.setattr(ci, "download_and_extract_artifacts", fake_download_and_extract_artifacts)
    rep_out = ci_cmd(
        "reproduce-build",
        "https://example.com/api/v1/projects/1/jobs/2/artifacts",
        "--working-dir",
        str(repro_dir),
        output=str,
    )
    # Make sure the script was generated
    assert (repro_dir / "start.sh").exists()

    # Make sure we tell the user where it is when not in interactive mode
    assert f"$ {repro_dir}/start.sh" in rep_out

    # Ensure the correct commits are used
    assert f"checkout_commit: {last_two_git_commits[0]}" in rep_out
    assert f"merge_commit: {last_two_git_commits[1]}" in rep_out

    # Test re-running in dirty working dir
    with pytest.raises(SpackError, match=f"{repro_dir}"):
        rep_out = ci_cmd(
            "reproduce-build",
            "https://example.com/api/v1/projects/1/jobs/2/artifacts",
            "--working-dir",
            str(repro_dir),
            output=str,
        )

    # Cleanup between  tests
    shutil.rmtree(repro_dir)

    # Test --use-local-head
    rep_out = ci_cmd(
        "reproduce-build",
        "https://example.com/api/v1/projects/1/jobs/2/artifacts",
        "--use-local-head",
        "--working-dir",
        str(repro_dir),
        output=str,
    )

    # Make sure we are checkout out the HEAD commit without a merge commit
    assert "checkout_commit: HEAD" in rep_out
    assert "merge_commit: None" in rep_out

    # Test the case where the spack_info.txt is not a merge commit
    monkeypatch.setattr(
        ci,
        "download_and_extract_artifacts",
        lambda url, wd: fake_download_and_extract_artifacts(url, wd, False),
    )

    # Cleanup between  tests
    shutil.rmtree(repro_dir)

    rep_out = ci_cmd(
        "reproduce-build",
        "https://example.com/api/v1/projects/1/jobs/2/artifacts",
        "--working-dir",
        str(repro_dir),
        output=str,
    )
    # Make sure the script was generated
    assert (repro_dir / "start.sh").exists()

    # Make sure we tell the user where it is when not in interactive mode
    assert f"$ {repro_dir}/start.sh" in rep_out

    # Ensure the correct commit is used (different than HEAD)
    assert f"checkout_commit: {last_two_git_commits[1]}" in rep_out
    assert "merge_commit: None" in rep_out


@pytest.mark.parametrize(
    "url_in,url_out",
    [
        (
            "https://example.com/api/v4/projects/1/jobs/2/artifacts",
            "https://example.com/api/v4/projects/1/jobs/2/artifacts",
        ),
        (
            "https://example.com/spack/spack/-/jobs/123456/artifacts/download",
            "https://example.com/spack/spack/-/jobs/123456/artifacts/download",
        ),
        (
            "https://example.com/spack/spack/-/jobs/123456",
            "https://example.com/spack/spack/-/jobs/123456/artifacts/download",
        ),
        (
            "https://example.com/spack/spack/-/jobs/////123456////?x=y#z",
            "https://example.com/spack/spack/-/jobs/123456/artifacts/download",
        ),
    ],
)
def test_reproduce_build_url_validation(url_in, url_out):
    assert spack.cmd.ci._gitlab_artifacts_url(url_in) == url_out


def test_reproduce_build_url_validation_fails():
    """Wrong URLs should cause an exception"""
    with pytest.raises(SystemExit):
        ci_cmd("reproduce-build", "example.com/spack/spack/-/jobs/123456/artifacts/download")

    with pytest.raises(SystemExit):
        ci_cmd("reproduce-build", "https://example.com/spack/spack/-/issues")

    with pytest.raises(SystemExit):
        ci_cmd("reproduce-build", "https://example.com/spack/spack/-")


@pytest.mark.parametrize(
    "subcmd", [(""), ("generate"), ("rebuild-index"), ("rebuild"), ("reproduce-build")]
)
def test_ci_help(subcmd, capsys):
    """Make sure `spack ci` --help describes the (sub)command help."""
    out = spack.main.SpackCommand("ci", subprocess=True)(subcmd, "--help")

    usage = "usage: spack ci {0}{1}[".format(subcmd, " " if subcmd else "")
    assert usage in out


def test_cmd_first_line():
    """Explicitly test first_line since not picked up in test_ci_help."""
    first = "This is a test."
    doc = """{0}

    Is there more to be said?""".format(
        first
    )

    assert spack.cmd.first_line(doc) == first


@pytest.mark.skip(reason="Gitlab CI was removed from Spack")
def test_gitlab_config_scopes(ci_generate_test, tmp_path):
    """Test pipeline generation with real configs included"""
    configs_path = os.path.join(spack_paths.share_path, "gitlab", "cloud_pipelines", "configs")
    _, outputfile, _ = ci_generate_test(
        f"""\
spack:
  config:
    install_tree: {tmp_path / "opt"}
  include: [{configs_path}]
  view: false
  specs:
    - dependent-install
  mirrors:
    buildcache-destination: {tmp_path / "ci-mirror"}
  ci:
    pipeline-gen:
    - build-job:
        image: "ecpe4s/ubuntu20.04-runner-x86_64:2023-01-01"
        tags: ["some_tag"]
"""
    )

    yaml_contents = syaml.load(outputfile.read_text())

    assert "rebuild-index" in yaml_contents

    rebuild_job = yaml_contents["rebuild-index"]
    assert "tags" in rebuild_job
    assert "variables" in rebuild_job

    rebuild_tags = rebuild_job["tags"]
    rebuild_vars = rebuild_job["variables"]
    assert all([t in rebuild_tags for t in ["spack", "service"]])
    expected_vars = ["CI_JOB_SIZE", "KUBERNETES_CPU_REQUEST", "KUBERNETES_MEMORY_REQUEST"]
    assert all([v in rebuild_vars for v in expected_vars])


def test_ci_generate_mirror_config(
    tmp_path: pathlib.Path,
    mutable_mock_env_path,
    install_mockery,
    monkeypatch,
    ci_base_environment,
    mock_binary_index,
):
    """Make sure the correct mirror gets used as the buildcache destination"""
    fst, snd = (tmp_path / "first").as_uri(), (tmp_path / "second").as_uri()
    with open(tmp_path / "spack.yaml", "w", encoding="utf-8") as f:
        f.write(
            f"""\
spack:
  specs:
    - archive-files
  mirrors:
    some-mirror: {fst}
    buildcache-destination: {snd}
  ci:
    pipeline-gen:
    - submapping:
      - match:
          - archive-files
        build-job:
          tags:
            - donotcare
          image: donotcare
"""
        )

    with ev.Environment(tmp_path):
        ci_cmd("generate", "--output-file", str(tmp_path / ".gitlab-ci.yml"))

    with open(tmp_path / ".gitlab-ci.yml", encoding="utf-8") as f:
        pipeline_doc = syaml.load(f)
        assert fst not in pipeline_doc["rebuild-index"]["script"][0]
        assert snd in pipeline_doc["rebuild-index"]["script"][0]


def dynamic_mapping_setup(tmpdir):
    filename = str(tmpdir.join("spack.yaml"))
    with open(filename, "w", encoding="utf-8") as f:
        f.write(
            """\
spack:
  specs:
    - pkg-a
  mirrors:
    buildcache-destination: https://my.fake.mirror
  ci:
    pipeline-gen:
    - dynamic-mapping:
        endpoint: https://fake.spack.io/mapper
        require: ["variables"]
        ignore: ["ignored_field"]
        allow: ["variables", "retry"]
"""
        )

    spec_a = spack.concretize.concretize_one("pkg-a")

    return gitlab_generator.get_job_name(spec_a)


def test_ci_dynamic_mapping_empty(
    tmpdir,
    working_env,
    mutable_mock_env_path,
    install_mockery,
    mock_packages,
    monkeypatch,
    ci_base_environment,
):
    # The test will always return an empty dictionary
    def _urlopen(*args, **kwargs):
        return MockHTTPResponse.with_json(200, "OK", headers={}, body={})

    monkeypatch.setattr(ci.common, "_urlopen", _urlopen)

    _ = dynamic_mapping_setup(tmpdir)
    with tmpdir.as_cwd():
        env_cmd("create", "test", "./spack.yaml")
        outputfile = str(tmpdir.join(".gitlab-ci.yml"))

        with ev.read("test"):
            output = ci_cmd("generate", "--output-file", outputfile)
            assert "Response missing required keys: ['variables']" in output


def test_ci_dynamic_mapping_full(
    tmpdir,
    working_env,
    mutable_mock_env_path,
    install_mockery,
    mock_packages,
    monkeypatch,
    ci_base_environment,
):
    def _urlopen(*args, **kwargs):
        return MockHTTPResponse.with_json(
            200,
            "OK",
            headers={},
            body={"variables": {"MY_VAR": "hello"}, "ignored_field": 0, "unallowed_field": 0},
        )

    monkeypatch.setattr(ci.common, "_urlopen", _urlopen)

    label = dynamic_mapping_setup(tmpdir)
    with tmpdir.as_cwd():
        env_cmd("create", "test", "./spack.yaml")
        outputfile = str(tmpdir.join(".gitlab-ci.yml"))

        with ev.read("test"):
            ci_cmd("generate", "--output-file", outputfile)

            with open(outputfile, encoding="utf-8") as of:
                pipeline_doc = syaml.load(of.read())
                assert label in pipeline_doc
                job = pipeline_doc[label]

                assert job.get("variables", {}).get("MY_VAR") == "hello"
                assert "ignored_field" not in job
                assert "unallowed_field" not in job


def test_ci_generate_unknown_generator(
    ci_generate_test,
    tmp_path,
    mutable_mock_env_path,
    install_mockery,
    mock_packages,
    ci_base_environment,
):
    """Ensure unrecognized ci targets are detected and the user
    sees an intelligible and actionable message"""
    src_mirror_url = tmp_path / "ci-src-mirror"
    bin_mirror_url = tmp_path / "ci-bin-mirror"
    spack_yaml_contents = f"""
spack:
  specs:
    - archive-files
  mirrors:
    some-mirror: {src_mirror_url}
    buildcache-destination: {bin_mirror_url}
  ci:
    target: unknown
    pipeline-gen:
    - submapping:
      - match:
          - archive-files
        build-job:
          tags:
            - donotcare
          image: donotcare
"""
    expect = "Spack CI module cannot generate a pipeline for format unknown"
    with pytest.raises(ci.SpackCIError, match=expect):
        ci_generate_test(spack_yaml_contents)


def test_ci_generate_copy_only(
    ci_generate_test,
    tmp_path,
    monkeypatch,
    mutable_mock_env_path,
    install_mockery,
    mock_packages,
    ci_base_environment,
):
    """Ensure the correct jobs are generated for a copy-only pipeline,
    and verify that pipeline manifest is produced containing the right
    number of entries."""
    src_mirror_url = tmp_path / "ci-src-mirror"
    bin_mirror_url = tmp_path / "ci-bin-mirror"
    copy_mirror_url = tmp_path / "ci-copy-mirror"

    monkeypatch.setenv("SPACK_PIPELINE_TYPE", "spack_copy_only")
    monkeypatch.setenv("SPACK_COPY_BUILDCACHE", copy_mirror_url)

    spack_yaml_contents = f"""
spack:
  specs:
    - archive-files
  mirrors:
    buildcache-source:
      fetch: {src_mirror_url}
      push: {src_mirror_url}
      source: False
      binary: True
    buildcache-destination:
      fetch: {bin_mirror_url}
      push: {bin_mirror_url}
      source: False
      binary: True
  ci:
    target: gitlab
    pipeline-gen:
    - submapping:
      - match:
          - archive-files
        build-job:
          tags:
            - donotcare
          image: donotcare
"""
    _, output_file, _ = ci_generate_test(spack_yaml_contents)

    with open(output_file, encoding="utf-8") as of:
        pipeline_doc = syaml.load(of.read())

    expected_keys = ["copy", "rebuild-index", "stages", "variables", "workflow"]
    assert all([k in pipeline_doc for k in expected_keys])

    # Make sure there are only two jobs and two stages
    stages = pipeline_doc["stages"]
    copy_stage = "copy"
    rebuild_index_stage = "stage-rebuild-index"

    assert len(stages) == 2
    assert stages[0] == copy_stage
    assert stages[1] == rebuild_index_stage

    rebuild_index_job = pipeline_doc["rebuild-index"]
    assert rebuild_index_job["stage"] == rebuild_index_stage

    copy_job = pipeline_doc["copy"]
    assert copy_job["stage"] == copy_stage

    # Make sure a pipeline manifest was generated
    output_directory = os.path.dirname(output_file)
    assert "SPACK_ARTIFACTS_ROOT" in pipeline_doc["variables"]
    artifacts_root = pipeline_doc["variables"]["SPACK_ARTIFACTS_ROOT"]
    pipeline_manifest_path = os.path.join(
        output_directory, artifacts_root, "specs_to_copy", "copy_rebuilt_specs.json"
    )

    assert os.path.exists(pipeline_manifest_path)
    assert os.path.isfile(pipeline_manifest_path)

    with open(pipeline_manifest_path, encoding="utf-8") as fd:
        manifest_data = json.load(fd)

    with ev.read("test") as active_env:
        active_env.concretize()
        for s in active_env.all_specs():
            assert s.dag_hash() in manifest_data


@generator("unittestgenerator")
def generate_unittest_pipeline(
    pipeline: PipelineDag, spack_ci: SpackCIConfig, options: PipelineOptions
):
    """Define a custom pipeline generator for the target 'unittestgenerator'."""
    output_file = options.output_file
    assert output_file is not None
    with open(output_file, "w", encoding="utf-8") as fd:
        fd.write("unittestpipeline\n")
        for _, node in pipeline.traverse_nodes(direction="children"):
            release_spec = node.spec
            fd.write(f"  {release_spec.name}\n")


def test_ci_generate_alternate_target(
    ci_generate_test,
    tmp_path,
    mutable_mock_env_path,
    install_mockery,
    mock_packages,
    ci_base_environment,
):
    """Ensure the above pipeline generator was correctly registered and
    is used to generate a pipeline for the stack/config defined here."""
    bin_mirror_url = tmp_path / "ci-bin-mirror"

    spack_yaml_contents = f"""
spack:
  specs:
    - archive-files
    - externaltest
  mirrors:
    buildcache-destination: {bin_mirror_url}
  ci:
    target: unittestgenerator
    pipeline-gen:
    - submapping:
      - match:
          - archive-files
        build-job:
          tags:
            - donotcare
          image: donotcare
"""
    _, output_file, _ = ci_generate_test(spack_yaml_contents, "--no-prune-externals")

    with open(output_file, encoding="utf-8") as of:
        pipeline_doc = of.read()

    assert pipeline_doc.startswith("unittestpipeline")
    assert "externaltest" in pipeline_doc


@pytest.fixture
def fetch_versions_match(monkeypatch):
    """Fake successful checksums returned from downloaded tarballs."""

    def get_checksums_for_versions(url_by_version, package_name, **kwargs):
        pkg_cls = spack.repo.PATH.get_pkg_class(package_name)
        return {v: pkg_cls.versions[v]["sha256"] for v in url_by_version}

    monkeypatch.setattr(spack.stage, "get_checksums_for_versions", get_checksums_for_versions)


@pytest.fixture
def fetch_versions_invalid(monkeypatch):
    """Fake successful checksums returned from downloaded tarballs."""

    def get_checksums_for_versions(url_by_version, package_name, **kwargs):
        return {
            v: "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
            for v in url_by_version
        }

    monkeypatch.setattr(spack.stage, "get_checksums_for_versions", get_checksums_for_versions)


@pytest.mark.parametrize("versions", [["2.1.4"], ["2.1.4", "2.1.5"]])
def test_ci_validate_standard_versions_valid(capfd, mock_packages, fetch_versions_match, versions):
    spec = spack.spec.Spec("diff-test")
    pkg = spack.repo.PATH.get_pkg_class(spec.name)(spec)
    version_list = [spack.version.Version(v) for v in versions]

    assert spack.cmd.ci.validate_standard_versions(pkg, version_list)

    out, err = capfd.readouterr()
    for version in versions:
        assert f"Validated diff-test@{version}" in out


@pytest.mark.parametrize("versions", [["2.1.4"], ["2.1.4", "2.1.5"]])
def test_ci_validate_standard_versions_invalid(
    capfd, mock_packages, fetch_versions_invalid, versions
):
    spec = spack.spec.Spec("diff-test")
    pkg = spack.repo.PATH.get_pkg_class(spec.name)(spec)
    version_list = [spack.version.Version(v) for v in versions]

    assert spack.cmd.ci.validate_standard_versions(pkg, version_list) is False

    out, err = capfd.readouterr()
    for version in versions:
        assert f"Invalid checksum found diff-test@{version}" in err


@pytest.mark.parametrize("versions", [[("1.0", -2)], [("1.1", -4), ("2.0", -6)]])
def test_ci_validate_git_versions_valid(
    capfd, monkeypatch, mock_packages, mock_git_version_info, versions
):
    spec = spack.spec.Spec("diff-test")
    pkg_class = spack.repo.PATH.get_pkg_class(spec.name)
    pkg = pkg_class(spec)
    version_list = [spack.version.Version(v) for v, _ in versions]

    repo_path, filename, commits = mock_git_version_info
    version_commit_dict = {
        spack.version.Version(v): {"tag": f"v{v}", "commit": commits[c]} for v, c in versions
    }

    monkeypatch.setattr(pkg_class, "git", repo_path)
    monkeypatch.setattr(pkg_class, "versions", version_commit_dict)

    assert spack.cmd.ci.validate_git_versions(pkg, version_list)

    out, err = capfd.readouterr()
    for version in version_list:
        assert f"Validated diff-test@{version}" in out


@pytest.mark.parametrize("versions", [[("1.0", -3)], [("1.1", -5), ("2.0", -5)]])
def test_ci_validate_git_versions_bad_tag(
    capfd, monkeypatch, mock_packages, mock_git_version_info, versions
):
    spec = spack.spec.Spec("diff-test")
    pkg_class = spack.repo.PATH.get_pkg_class(spec.name)
    pkg = pkg_class(spec)
    version_list = [spack.version.Version(v) for v, _ in versions]

    repo_path, filename, commits = mock_git_version_info
    version_commit_dict = {
        spack.version.Version(v): {"tag": f"v{v}", "commit": commits[c]} for v, c in versions
    }

    monkeypatch.setattr(pkg_class, "git", repo_path)
    monkeypatch.setattr(pkg_class, "versions", version_commit_dict)

    assert spack.cmd.ci.validate_git_versions(pkg, version_list) is False

    out, err = capfd.readouterr()
    for version in version_list:
        assert f"Mismatched tag <-> commit found for diff-test@{version}" in err


@pytest.mark.parametrize("versions", [[("1.0", -2)], [("1.1", -4), ("2.0", -6), ("3.0", -6)]])
def test_ci_validate_git_versions_invalid(
    capfd, monkeypatch, mock_packages, mock_git_version_info, versions
):
    spec = spack.spec.Spec("diff-test")
    pkg_class = spack.repo.PATH.get_pkg_class(spec.name)
    pkg = pkg_class(spec)
    version_list = [spack.version.Version(v) for v, _ in versions]

    repo_path, filename, commits = mock_git_version_info
    version_commit_dict = {
        spack.version.Version(v): {
            "tag": f"v{v}",
            "commit": "abcdefabcdefabcdefabcdefabcdefabcdefabc",
        }
        for v, c in versions
    }

    monkeypatch.setattr(pkg_class, "git", repo_path)
    monkeypatch.setattr(pkg_class, "versions", version_commit_dict)

    assert spack.cmd.ci.validate_git_versions(pkg, version_list) is False

    out, err = capfd.readouterr()
    for version in version_list:
        assert f"Invalid commit for diff-test@{version}" in err


def mock_packages_path(path):
    def packages_path():
        return path

    return packages_path


@pytest.fixture
def verify_standard_versions_valid(monkeypatch):
    def validate_standard_versions(pkg, versions):
        for version in versions:
            print(f"Validated {pkg.name}@{version}")
        return True

    monkeypatch.setattr(spack.cmd.ci, "validate_standard_versions", validate_standard_versions)


@pytest.fixture
def verify_git_versions_valid(monkeypatch):
    def validate_git_versions(pkg, versions):
        for version in versions:
            print(f"Validated {pkg.name}@{version}")
        return True

    monkeypatch.setattr(spack.cmd.ci, "validate_git_versions", validate_git_versions)


@pytest.fixture
def verify_standard_versions_invalid(monkeypatch):
    def validate_standard_versions(pkg, versions):
        for version in versions:
            print(f"Invalid checksum found {pkg.name}@{version}")
        return False

    monkeypatch.setattr(spack.cmd.ci, "validate_standard_versions", validate_standard_versions)


@pytest.fixture
def verify_git_versions_invalid(monkeypatch):
    def validate_git_versions(pkg, versions):
        for version in versions:
            print(f"Invalid commit for {pkg.name}@{version}")
        return False

    monkeypatch.setattr(spack.cmd.ci, "validate_git_versions", validate_git_versions)


def test_ci_verify_versions_valid(
    monkeypatch,
    mock_packages,
    mock_git_package_changes,
    verify_standard_versions_valid,
    verify_git_versions_valid,
    tmpdir,
):
    repo, _, commits = mock_git_package_changes
    with spack.repo.use_repositories(repo):
        monkeypatch.setattr(spack.repo, "builtin_repo", lambda: repo)

        out = ci_cmd("verify-versions", commits[-1], commits[-3])
        assert "Validated diff-test@2.1.5" in out
        assert "Validated diff-test@2.1.6" in out


def test_ci_verify_versions_standard_invalid(
    monkeypatch,
    mock_packages,
    mock_git_package_changes,
    verify_standard_versions_invalid,
    verify_git_versions_invalid,
):
    repo, _, commits = mock_git_package_changes
    with spack.repo.use_repositories(repo):
        monkeypatch.setattr(spack.repo, "builtin_repo", lambda: repo)

        out = ci_cmd("verify-versions", commits[-1], commits[-3], fail_on_error=False)
        assert "Invalid checksum found diff-test@2.1.5" in out
        assert "Invalid commit for diff-test@2.1.6" in out


def test_ci_verify_versions_manual_package(monkeypatch, mock_packages, mock_git_package_changes):
    repo, _, commits = mock_git_package_changes
    with spack.repo.use_repositories(repo):
        monkeypatch.setattr(spack.repo, "builtin_repo", lambda: repo)

        pkg_class = spack.repo.PATH.get_pkg_class("diff-test")
        monkeypatch.setattr(pkg_class, "manual_download", True)

        out = ci_cmd("verify-versions", commits[-1], commits[-2])
        assert "Skipping manual download package: diff-test" in out
