# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)


from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class ConditionallyPatchDependency(Package):
    """Package that conditionally requries a patched version
    of a dependency."""

    homepage = "http://www.example.com"
    url = "http://www.example.com/patch-a-dependency-1.0.tar.gz"

    version("1.0", md5="0123456789abcdef0123456789abcdef")
    variant("jasper", default=False)
    depends_on("libelf@0.8.10", patches=[patch("uuid.patch")], when="+jasper")
