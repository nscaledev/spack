# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class ParentFoo(Package):
    """This package has a variant "foo", which is True by default, and depends on another
    package which has the same variant defaulting to False.
    """

    homepage = "http://www.example.com"
    url = "http://www.example.com/c-1.0.tar.gz"

    version("1.0", md5="0123456789abcdef0123456789abcdef")

    variant("foo", default=True, description="")

    depends_on("client-not-foo")
