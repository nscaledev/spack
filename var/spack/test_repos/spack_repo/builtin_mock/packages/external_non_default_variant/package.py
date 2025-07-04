# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class ExternalNonDefaultVariant(Package):
    """An external that is registered with a non-default value"""

    homepage = "http://www.python.org"
    url = "http://www.python.org/ftp/python/3.8.7/Python-3.8.7.tgz"

    version("3.8.7", md5="be78e48cdfc1a7ad90efff146dce6cfe")

    variant("foo", default=True, description="just a variant")
    variant("bar", default=True, description="just a variant")
