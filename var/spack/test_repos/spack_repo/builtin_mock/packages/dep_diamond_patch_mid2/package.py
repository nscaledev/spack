# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class DepDiamondPatchMid2(Package):
    r"""Package that requires a patch on a dependency

  W
 / \
X   Y
 \ /
  Z

    This is package Y
    """

    homepage = "http://www.example.com"
    url = "http://www.example.com/patch-a-dependency-1.0.tar.gz"

    version("1.0", md5="0123456789abcdef0123456789abcdef")

    # single patch file in repo
    depends_on(
        "patch",
        patches=[
            patch(
                "http://example.com/urlpatch.patch",
                sha256="mid21234abcd1234abcd1234abcd1234abcd1234abcd1234abcd1234abcd1234",
            )
        ],
    )
