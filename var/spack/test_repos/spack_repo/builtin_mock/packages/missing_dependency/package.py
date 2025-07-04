# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class MissingDependency(Package):
    """Package with a dependency that does not exist."""

    homepage = "http://www.example.com"
    url = "http://www.example.com/missing-dependency-1.0.tar.gz"

    version("1.0", md5="0123456789abcdef0123456789abcdef")

    # intentionally missing to test possible_dependencies()
    depends_on("this-is-a-missing-dependency")

    # this one is a "real" mock dependency
    depends_on("pkg-a")
