# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class QuantumEspresso(Package):
    """Used to test that a few problematic concretization
    cases with the old concretizer have been solved by the
    new ones.
    """

    homepage = "http://www.example.com"
    url = "http://www.example.com/qe-1.0.tar.gz"

    version("1.0", md5="1234567890abcdef1234567890abcdef")

    variant("invino", default=True, description="?")
    variant("veritas", default=True, description="?")

    depends_on("fftw@:1.0")
    depends_on("fftw+mpi", when="+invino")

    depends_on("openblas", when="^fftw@:1")

    depends_on("libelf@0.8.10:")
    depends_on("libelf@:0.8.12", when="+veritas")
