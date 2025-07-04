# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class ConditionalConstrainedDependencies(Package):
    """Package that has a variant which adds a dependency forced to
    use non default values.
    """

    homepage = "https://dev.null"

    version("1.0")

    # This variant is on by default and attaches a dependency
    # with a lot of variants set at their non-default values
    variant("dep", default=True, description="nope")
    depends_on("dep-with-variants+foo+bar+baz", when="+dep")
