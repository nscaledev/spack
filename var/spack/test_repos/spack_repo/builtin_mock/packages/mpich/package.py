# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class Mpich(Package):
    homepage = "http://www.mpich.org"
    url = "http://www.mpich.org/static/downloads/3.0.4/mpich-3.0.4.tar.gz"
    list_url = "http://www.mpich.org/static/downloads/"
    list_depth = 2

    tags = ["tag1", "tag2"]
    executables = ["^mpichversion$"]

    variant("debug", default=False, description="Compile MPICH with debug flags.")

    version("main", branch="main", git="https://github.com/pmodels/mpich")
    version("3.0.4", md5="9c5d5d4fe1e17dd12153f40bc5b6dbc0")
    version("3.0.3", md5="0123456789abcdef0123456789abcdef")
    version("3.0.2", md5="0123456789abcdef0123456789abcdef")
    version("3.0.1", md5="0123456789abcdef0123456789abcdef")
    version("3.0", md5="0123456789abcdef0123456789abcdef")
    version("1.0", md5="0123456789abcdef0123456789abcdef")

    provides("mpi@:3", when="@3:")
    provides("mpi@:1", when="@:1")

    depends_on("c", type="build")
    depends_on("cxx", type="build")
    depends_on("fortran", type="build")

    @classmethod
    def determine_version(cls, exe):
        output = Executable(exe)(output=str, error=str)
        match = re.search(r"MPICH Version:\s+(\S+)", output)
        return match.group(1) if match else None

    def install(self, spec, prefix):
        touch(prefix.mpich)

    def test_mpich(self):
        print("Testing mpich")
