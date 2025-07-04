# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class Licenses1(Package):
    """Package with a licenses field."""

    homepage = "https://www.example.com"
    url = "https://www.example.com/license"

    license("MIT", when="+foo")
    license("Apache-2.0", when="~foo")

    version("1.0", md5="0123456789abcdef0123456789abcdef")
