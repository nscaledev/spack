# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class UrlOverride(Package):
    homepage = "http://www.doesnotexist.org"
    url = "http://www.doesnotexist.org/url_override-1.0.0.tar.gz"

    version("1.0.0", md5="0123456789abcdef0123456789abcdef")
    version(
        "0.9.0",
        md5="fedcba9876543210fedcba9876543210",
        url="http://www.anothersite.org/uo-0.9.0.tgz",
    )
    version("0.8.1", md5="0123456789abcdef0123456789abcdef")
