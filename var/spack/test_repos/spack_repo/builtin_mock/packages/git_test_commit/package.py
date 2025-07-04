# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class GitTestCommit(Package):
    """Mock package that tests installing specific commit"""

    homepage = "http://www.git-fetch-example.com"

    # git='to-be-filled-in-by-test'

    # ----------------------------
    # -- mock_git_repository, or mock_git_version_info
    version("main", branch="main")
    # ----------------------------
    # -- only mock_git_repository
    # (session scope)
    version("tag", tag="test-tag")
    # ----------------------------
    # -- only mock_git_version_info below
    # (function scope)
    version("1.0", tag="v1.0")
    version("1.1", tag="v1.1")
    version("1.2", tag="1.2")  # not a typo
    version("2.0", tag="v2.0")

    def install(self, spec, prefix):
        # It is assumed for the test which installs this package, that it will
        # be using the earliest commit, which is contained in the range @:0
        assert spec.satisfies("@:0")
        mkdir(prefix.bin)

        # This will only exist for some second commit
        install("file.txt", prefix.bin)
