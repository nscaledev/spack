# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class GitSparsepathsPkg(Package):
    """Mock package with git_sparse_paths attribute"""

    homepage = "http://www.git-fetch-example.com"
    git = "https://a/really.com/big/repo.git"

    version("1.0", tag="v1.0")

    git_sparse_paths = ["foo", "bar", "bing/bang"]
