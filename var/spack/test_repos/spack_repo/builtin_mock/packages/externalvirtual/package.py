# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class Externalvirtual(Package):
    homepage = "http://somewhere.com"
    url = "http://somewhere.com/stuff-1.0.tar.gz"

    version("1.0", md5="1234567890abcdef1234567890abcdef")
    version("2.0", md5="234567890abcdef1234567890abcdef1")
    version("2.1", md5="34567890abcdef1234567890abcdef12")
    version("2.2", md5="4567890abcdef1234567890abcdef123")

    provides("stuff", when="@1.0:")
