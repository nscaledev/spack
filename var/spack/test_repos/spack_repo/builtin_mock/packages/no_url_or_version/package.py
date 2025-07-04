# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

from spack_repo.builtin_mock.build_systems.generic import Package


class NoUrlOrVersion(Package):
    """Mock package that has no url and no version."""

    homepage = "https://example.com/"
