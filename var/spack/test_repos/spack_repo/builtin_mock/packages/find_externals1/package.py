# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
import os
import re

from spack_repo.builtin_mock.build_systems.autotools import AutotoolsPackage

from spack.package import *


class FindExternals1(AutotoolsPackage):
    executables = ["find-externals1-exe"]

    url = "http://www.example.com/find-externals-1.0.tar.gz"

    version("1.0", md5="abcdef1234567890abcdef1234567890")

    @classmethod
    def determine_version(cls, exe):
        return "1.0"

    @classmethod
    def determine_spec_details(cls, prefix, exes_in_prefix):
        exe_to_path = dict((os.path.basename(p), p) for p in exes_in_prefix)
        exes = [x for x in exe_to_path.keys() if "find-externals1-exe" in x]
        if not exes:
            return
        exe = Executable(exe_to_path[exes[0]])
        output = exe("--version", output=str)
        if output:
            match = re.search(r"find-externals1.*version\s+(\S+)", output)
            if match:
                version_str = match.group(1)
                return Spec.from_detection(f"find-externals1@{version_str}", external_path=prefix)
