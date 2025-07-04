# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import argparse
import collections
import warnings

import _vendoring.archspec.cpu

import llnl.util.tty.colify as colify
import llnl.util.tty.color as color

import spack.platforms
import spack.spec

description = "print architecture information about this machine"
section = "system"
level = "short"


def setup_parser(subparser: argparse.ArgumentParser) -> None:
    # DEPRECATED: equivalent to --generic --target
    subparser.add_argument(
        "-g",
        "--generic-target",
        action="store_true",
        help="show the best generic target (deprecated)",
    )
    subparser.add_argument(
        "--known-targets", action="store_true", help="show a list of all known targets and exit"
    )
    target_type = subparser.add_mutually_exclusive_group()
    target_type.add_argument(
        "--family", action="store_true", help="print generic ISA (x86_64, aarch64, ppc64le, ...)"
    )
    target_type.add_argument(
        "--generic", action="store_true", help="print feature level (x86_64_v3, armv8.4a, ...)"
    )
    parts = subparser.add_mutually_exclusive_group()
    parts2 = subparser.add_mutually_exclusive_group()
    parts.add_argument(
        "-p", "--platform", action="store_true", default=False, help="print only the platform"
    )
    parts.add_argument(
        "-o",
        "--operating-system",
        action="store_true",
        default=False,
        help="print only the operating system",
    )
    parts.add_argument(
        "-t", "--target", action="store_true", default=False, help="print only the target"
    )
    parts2.add_argument(
        "-f", "--frontend", action="store_true", default=False, help="print frontend (DEPRECATED)"
    )
    parts2.add_argument(
        "-b", "--backend", action="store_true", default=False, help="print backend (DEPRECATED)"
    )


def display_targets(targets):
    """Prints a human readable list of the targets passed as argument."""
    by_vendor = collections.defaultdict(list)
    for _, target in targets.items():
        by_vendor[target.vendor].append(target)

    def display_target_group(header, target_group):
        print(header)
        colify.colify(target_group, indent=4)
        print("")

    generic_architectures = by_vendor.pop("generic", None)
    if generic_architectures:
        header = color.colorize(r"@*B{Generic architectures (families)}")
        group = sorted(generic_architectures, key=lambda x: str(x))
        display_target_group(header, group)

    for vendor, vendor_targets in by_vendor.items():
        by_family = collections.defaultdict(list)
        for t in vendor_targets:
            by_family[str(t.family)].append(t)

        for family, group in by_family.items():
            vendor = color.colorize(r"@*B{" + vendor + r"}")
            family = color.colorize(r"@*B{" + family + r"}")
            header = " - ".join([vendor, family])
            group = sorted(group, key=lambda x: len(x.ancestors))
            display_target_group(header, group)


def arch(parser, args):
    if args.generic_target:
        # TODO: add deprecation warning in 0.24
        print(_vendoring.archspec.cpu.host().generic)
        return

    if args.known_targets:
        display_targets(_vendoring.archspec.cpu.TARGETS)
        return

    if args.frontend:
        warnings.warn("the argument --frontend is deprecated, and will be removed in Spack v1.0")
    elif args.backend:
        warnings.warn("the argument --backend is deprecated, and will be removed in Spack v1.0")

    host_platform = spack.platforms.host()
    host_os = host_platform.default_operating_system()
    host_target = host_platform.default_target()
    if args.family:
        host_target = host_target.family
    elif args.generic:
        host_target = host_target.generic
    architecture = spack.spec.ArchSpec((str(host_platform), str(host_os), str(host_target)))

    if args.platform:
        print(architecture.platform)
    elif args.operating_system:
        print(architecture.os)
    elif args.target:
        print(architecture.target)
    else:
        print(architecture)
