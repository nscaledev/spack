# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class Maintainers2(Package):
    """A second package with a maintainers field."""

    homepage = "http://www.example.com"
    url = "http://www.example.com/maintainers2-1.0.tar.gz"

    maintainers("user2", "user3")

    version("1.0", md5="0123456789abcdef0123456789abcdef")
