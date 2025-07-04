# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)


from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class Bzip2(Package):
    """This packagae has the variants shared
    defaulted to True"""

    homepage = "https://example.com"
    url = "https://example.com/bzip2-1.0.8tar.gz"

    version("1.0.8", sha256="ab5a03176ee106d3f0fa90e381da478ddae405918153cca248e682cd0c4a2269")

    variant("shared", default=True, description="Enables the build of shared libraries.")
