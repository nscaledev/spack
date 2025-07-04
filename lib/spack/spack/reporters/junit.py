# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
import os

import spack.tengine

from .base import Reporter


class JUnit(Reporter):
    """Generate reports of spec installations for JUnit."""

    _jinja_template = "reports/junit.xml"

    def concretization_report(self, filename, msg):
        pass

    def build_report(self, filename, specs):
        for spec in specs:
            spec.summarize()

        if not (os.path.splitext(filename))[1]:
            # Ensure the report name will end with the proper extension;
            # otherwise, it currently defaults to the "directory" name.
            filename = filename + ".xml"

        report_data = {"specs": specs}

        with open(filename, "w", encoding="utf-8") as f:
            env = spack.tengine.make_environment()
            t = env.get_template(self._jinja_template)
            f.write(t.render(report_data))

    def test_report(self, filename, specs):
        self.build_report(filename, specs)
