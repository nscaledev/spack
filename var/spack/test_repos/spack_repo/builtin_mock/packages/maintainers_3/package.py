# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
from spack.package import *

from ..maintainers_1.package import Maintainers1


class Maintainers3(Maintainers1):
    """A second package with a maintainers field."""

    homepage = "http://www.example.com"
    url = "http://www.example.com/maintainers2-1.0.tar.gz"

    maintainers("user0", "user3")

    version("1.0", md5="0123456789abcdef0123456789abcdef")
