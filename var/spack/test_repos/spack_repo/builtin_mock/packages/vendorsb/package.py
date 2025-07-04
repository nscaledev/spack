# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class Vendorsb(Package):
    """A package that vendors another, and thus conflicts with it"""

    homepage = "http://www.example.com"
    url = "http://www.example.com/b-1.0.tar.gz"

    version("1.1", md5="0123456789abcdef0123456789abcdef")
    version("1.0", md5="0123456789abcdef0123456789abcdef")

    # pkg-b is not a dependency
    conflicts("pkg-b", when="@=1.1")
