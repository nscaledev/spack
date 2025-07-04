# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import argparse
import sys

import llnl.util.tty as tty
from llnl.util.tty.colify import colify

import spack.cmd
import spack.environment as ev
import spack.store
from spack.cmd.common import arguments
from spack.solver.input_analysis import create_graph_analyzer

description = "show dependencies of a package"
section = "basic"
level = "long"


def setup_parser(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument(
        "-i",
        "--installed",
        action="store_true",
        default=False,
        help="list installed dependencies of an installed spec "
        "instead of possible dependencies of a package",
    )
    subparser.add_argument(
        "-t",
        "--transitive",
        action="store_true",
        default=False,
        help="show all transitive dependencies",
    )
    arguments.add_common_arguments(subparser, ["deptype"])
    subparser.add_argument(
        "-V",
        "--no-expand-virtuals",
        action="store_false",
        default=True,
        dest="expand_virtuals",
        help="do not expand virtual dependencies",
    )
    arguments.add_common_arguments(subparser, ["spec"])


def dependencies(parser, args):
    specs = spack.cmd.parse_specs(args.spec)
    if len(specs) != 1:
        tty.die("spack dependencies takes only one spec.")

    if args.installed:
        env = ev.active_environment()
        spec = spack.cmd.disambiguate_spec(specs[0], env)

        format_string = "{name}{@version}{/hash:7}{%compiler}"
        if sys.stdout.isatty():
            tty.msg("Dependencies of %s" % spec.format(format_string, color=True))
        deps = spack.store.STORE.db.installed_relatives(
            spec, "children", args.transitive, deptype=args.deptype
        )
        if deps:
            spack.cmd.display_specs(deps, long=True)
        else:
            print("No dependencies")

    else:
        spec = specs[0]
        dependencies, virtuals, _ = create_graph_analyzer().possible_dependencies(
            spec,
            transitive=args.transitive,
            expand_virtuals=args.expand_virtuals,
            allowed_deps=args.deptype,
        )
        if not args.expand_virtuals:
            dependencies.update(virtuals)

        if spec.name in dependencies:
            dependencies.remove(spec.name)

        if dependencies:
            colify(sorted(dependencies))
        else:
            print("No dependencies")
