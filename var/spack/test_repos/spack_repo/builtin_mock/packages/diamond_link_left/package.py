# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class DiamondLinkLeft(Package):
    """Part of diamond-link-{top,left,right,bottom} group"""

    homepage = "http://www.example.com"
    url = "http://www.example.com/diamond-link-left-1.0.tar.gz"

    version("1.0", md5="0123456789abcdef0123456789abcdef")

    depends_on("diamond-link-bottom", type="link")
