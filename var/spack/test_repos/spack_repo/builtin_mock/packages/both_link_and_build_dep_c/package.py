# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class BothLinkAndBuildDepC(Package):
    """
    Structure where c occurs as a build dep down the line and as a direct
    link dep. Useful for testing situations where you copy the parent spec
    just with link deps, and you want to make sure b is not part of that.
    a <--build-- b <-link-- c
    a <--link--- c
    """

    homepage = "http://www.example.com"
    url = "http://www.example.com/1.0.tar.gz"

    version("1.0", md5="0123456789abcdef0123456789abcdef")
