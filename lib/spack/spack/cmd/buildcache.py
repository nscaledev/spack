# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
import argparse
import glob
import json
import sys
import tempfile
from typing import List, Optional, Tuple

import llnl.util.tty as tty
from llnl.string import plural
from llnl.util.lang import elide_list, stable_partition

import spack.binary_distribution as bindist
import spack.cmd
import spack.concretize
import spack.config
import spack.deptypes as dt
import spack.environment as ev
import spack.error
import spack.mirrors.mirror
import spack.oci.oci
import spack.spec
import spack.stage
import spack.store
import spack.util.parallel
import spack.util.web as web_util
from spack import traverse
from spack.cmd import display_specs
from spack.cmd.common import arguments
from spack.spec import Spec, save_dependency_specfiles

from ..buildcache_migrate import migrate
from ..enums import InstallRecordStatus
from ..url_buildcache import (
    BuildcacheComponent,
    BuildcacheEntryError,
    URLBuildcacheEntry,
    check_mirror_for_layout,
    get_url_buildcache_class,
)

description = "create, download and install binary packages"
section = "packaging"
level = "long"


def setup_parser(subparser: argparse.ArgumentParser):
    setattr(setup_parser, "parser", subparser)
    subparsers = subparser.add_subparsers(help="buildcache sub-commands")

    push = subparsers.add_parser("push", aliases=["create"], help=push_fn.__doc__)
    push.add_argument("-f", "--force", action="store_true", help="overwrite tarball if it exists")
    push_sign = push.add_mutually_exclusive_group(required=False)
    push_sign.add_argument(
        "--unsigned",
        "-u",
        action="store_false",
        dest="signed",
        default=None,
        help="push unsigned buildcache tarballs",
    )
    push_sign.add_argument(
        "--signed",
        action="store_true",
        dest="signed",
        default=None,
        help="push signed buildcache tarballs",
    )
    push_sign.add_argument(
        "--key", "-k", metavar="key", type=str, default=None, help="key for signing"
    )
    push.add_argument(
        "mirror", type=arguments.mirror_name_or_url, help="mirror name, path, or URL"
    )
    push.add_argument(
        "--update-index",
        "--rebuild-index",
        action="store_true",
        default=False,
        help="regenerate buildcache index after building package(s)",
    )
    push.add_argument(
        "--only",
        default="package,dependencies",
        dest="things_to_install",
        choices=["package", "dependencies"],
        help="select the buildcache mode. "
        "The default is to build a cache for the package along with all its dependencies. "
        "Alternatively, one can decide to build a cache for only the package or only the "
        "dependencies",
    )
    with_or_without_build_deps = push.add_mutually_exclusive_group()
    with_or_without_build_deps.add_argument(
        "--with-build-dependencies",
        action="store_true",
        help="include build dependencies in the buildcache",
    )
    with_or_without_build_deps.add_argument(
        "--without-build-dependencies",
        action="store_true",
        help="exclude build dependencies from the buildcache",
    )
    push.add_argument(
        "--fail-fast",
        action="store_true",
        help="stop pushing on first failure (default is best effort)",
    )
    push.add_argument(
        "--base-image", default=None, help="specify the base image for the buildcache"
    )
    push.add_argument(
        "--tag",
        "-t",
        default=None,
        help="when pushing to an OCI registry, tag an image containing all root specs and their "
        "runtime dependencies",
    )
    push.add_argument(
        "--private",
        action="store_true",
        help="for a private mirror, include non-redistributable packages",
    )
    arguments.add_common_arguments(push, ["specs", "jobs"])
    push.set_defaults(func=push_fn)

    install = subparsers.add_parser("install", help=install_fn.__doc__)
    install.add_argument(
        "-f", "--force", action="store_true", help="overwrite install directory if it exists"
    )
    install.add_argument(
        "-m", "--multiple", action="store_true", help="allow all matching packages"
    )
    install.add_argument(
        "-u",
        "--unsigned",
        action="store_true",
        help="install unsigned buildcache tarballs for testing",
    )
    install.add_argument(
        "-o",
        "--otherarch",
        action="store_true",
        help="install specs from other architectures instead of default platform and OS",
    )

    arguments.add_common_arguments(install, ["specs"])
    install.set_defaults(func=install_fn)

    listcache = subparsers.add_parser("list", help=list_fn.__doc__)
    arguments.add_common_arguments(listcache, ["long", "very_long", "namespaces"])
    listcache.add_argument(
        "-v",
        "--variants",
        action="store_true",
        dest="variants",
        help="show variants in output (can be long)",
    )
    listcache.add_argument(
        "-a",
        "--allarch",
        action="store_true",
        help="list specs for all available architectures instead of default platform and OS",
    )
    arguments.add_common_arguments(listcache, ["specs"])
    listcache.set_defaults(func=list_fn)

    keys = subparsers.add_parser("keys", help=keys_fn.__doc__)
    keys.add_argument(
        "-i", "--install", action="store_true", help="install Keys pulled from mirror"
    )
    keys.add_argument("-t", "--trust", action="store_true", help="trust all downloaded keys")
    keys.add_argument("-f", "--force", action="store_true", help="force new download of keys")
    keys.set_defaults(func=keys_fn)

    # Check if binaries need to be rebuilt on remote mirror
    check = subparsers.add_parser("check", help=check_fn.__doc__)
    check.add_argument(
        "-m",
        "--mirror-url",
        default=None,
        help="override any configured mirrors with this mirror URL",
    )

    check.add_argument(
        "-o", "--output-file", default=None, help="file where rebuild info should be written"
    )

    # used to construct scope arguments below
    check.add_argument(
        "--scope",
        action=arguments.ConfigScope,
        default=lambda: spack.config.default_modify_scope(),
        help="configuration scope containing mirrors to check",
    )

    arguments.add_common_arguments(check, ["specs"])

    check.set_defaults(func=check_fn)

    # Download tarball and specfile
    download = subparsers.add_parser("download", help=download_fn.__doc__)
    download.add_argument("-s", "--spec", help="download built tarball for spec from mirror")
    download.add_argument(
        "-p",
        "--path",
        required=True,
        default=None,
        help="path to directory where tarball should be downloaded",
    )
    download.set_defaults(func=download_fn)

    # Given the root spec, save the yaml of the dependent spec to a file
    savespecfile = subparsers.add_parser("save-specfile", help=save_specfile_fn.__doc__)
    savespecfile_spec_or_specfile = savespecfile.add_mutually_exclusive_group(required=True)
    savespecfile_spec_or_specfile.add_argument("--root-spec", help="root spec of dependent spec")
    savespecfile.add_argument(
        "-s",
        "--specs",
        required=True,
        help="list of dependent specs for which saved yaml is desired",
    )
    savespecfile.add_argument(
        "--specfile-dir", required=True, help="path to directory where spec yamls should be saved"
    )
    savespecfile.set_defaults(func=save_specfile_fn)

    # Sync buildcache entries from one mirror to another
    sync = subparsers.add_parser("sync", help=sync_fn.__doc__)

    sync_manifest_source = sync.add_argument_group(
        "Manifest Source",
        "Specify a list of build cache objects to sync using manifest file(s)."
        'This option takes the place of the "source mirror" for synchronization'
        'and optionally takes a "destination mirror" ',
    )
    sync_manifest_source.add_argument(
        "--manifest-glob", help="a quoted glob pattern identifying CI rebuild manifest files"
    )
    sync_source_mirror = sync.add_argument_group(
        "Named Source",
        "Specify a single registered source mirror to synchronize from. This option requires"
        "the specification of a destination mirror.",
    )
    sync_source_mirror.add_argument(
        "src_mirror",
        metavar="source mirror",
        nargs="?",
        type=arguments.mirror_name_or_url,
        help="source mirror name, path, or URL",
    )

    sync.add_argument(
        "dest_mirror",
        metavar="destination mirror",
        nargs="?",
        type=arguments.mirror_name_or_url,
        help="destination mirror name, path, or URL",
    )

    sync.set_defaults(func=sync_fn)

    # Update buildcache index without copying any additional packages
    update_index = subparsers.add_parser(
        "update-index", aliases=["rebuild-index"], help=update_index_fn.__doc__
    )
    update_index.add_argument(
        "mirror", type=arguments.mirror_name_or_url, help="destination mirror name, path, or URL"
    )
    update_index.add_argument(
        "-k",
        "--keys",
        default=False,
        action="store_true",
        help="if provided, key index will be updated as well as package index",
    )
    update_index.set_defaults(func=update_index_fn)

    # Migrate a buildcache from layout_version 2 to version 3
    migrate = subparsers.add_parser("migrate", help=migrate_fn.__doc__)
    migrate.add_argument("mirror", type=arguments.mirror_name, help="name of a configured mirror")
    migrate.add_argument(
        "-u",
        "--unsigned",
        default=False,
        action="store_true",
        help="Ignore signatures and do not resign, default is False",
    )
    migrate.add_argument(
        "-d",
        "--delete-existing",
        default=False,
        action="store_true",
        help="Delete the previous layout, the default is to keep it.",
    )
    arguments.add_common_arguments(migrate, ["yes_to_all"])
    # TODO: add -y argument to prompt if user really means to delete existing
    migrate.set_defaults(func=migrate_fn)


def _matching_specs(specs: List[Spec]) -> List[Spec]:
    """Disambiguate specs and return a list of matching specs"""
    return [
        spack.cmd.disambiguate_spec(s, ev.active_environment(), installed=InstallRecordStatus.ANY)
        for s in specs
    ]


def _format_spec(spec: Spec) -> str:
    return spec.cformat("{name}{@version}{/hash:7}")


def _skip_no_redistribute_for_public(specs):
    remaining_specs = list()
    removed_specs = list()
    for spec in specs:
        if spec.package.redistribute_binary:
            remaining_specs.append(spec)
        else:
            removed_specs.append(spec)
    if removed_specs:
        colified_output = tty.colify.colified(list(s.name for s in removed_specs), indent=4)
        tty.debug(
            "The following specs will not be added to the binary cache"
            " because they cannot be redistributed:\n"
            f"{colified_output}\n"
            "You can use `--private` to include them."
        )
    return remaining_specs


class PackagesAreNotInstalledError(spack.error.SpackError):
    """Raised when a list of specs is not installed but picked to be packaged."""

    def __init__(self, specs: List[Spec]):
        super().__init__(
            "Cannot push non-installed packages",
            ", ".join(elide_list([_format_spec(s) for s in specs], 5)),
        )


class PackageNotInstalledError(spack.error.SpackError):
    """Raised when a spec is not installed but picked to be packaged."""


def _specs_to_be_packaged(
    requested: List[Spec], things_to_install: str, build_deps: bool
) -> List[Spec]:
    """Collect all non-external with or without roots and dependencies"""
    if "dependencies" not in things_to_install:
        deptype = dt.NONE
    elif build_deps:
        deptype = dt.ALL
    else:
        deptype = dt.RUN | dt.LINK | dt.TEST
    specs = [
        s
        for s in traverse.traverse_nodes(
            requested,
            root="package" in things_to_install,
            deptype=deptype,
            order="breadth",
            key=traverse.by_dag_hash,
        )
        if not s.external
    ]
    specs.reverse()
    return specs


def push_fn(args):
    """create a binary package and push it to a mirror"""
    if args.specs:
        roots = _matching_specs(spack.cmd.parse_specs(args.specs))
    else:
        roots = spack.cmd.require_active_env(cmd_name="buildcache push").concrete_roots()

    mirror = args.mirror
    assert isinstance(mirror, spack.mirrors.mirror.Mirror)

    push_url = mirror.push_url

    # When neither --signed, --unsigned nor --key are specified, use the mirror's default.
    if args.signed is None and not args.key:
        unsigned = not mirror.signed
    else:
        unsigned = not (args.key or args.signed)

    # For OCI images, we require dependencies to be pushed for now.
    if mirror.push_url.startswith("oci://") and not unsigned:
        tty.warn(
            "Code signing is currently not supported for OCI images. "
            "Use --unsigned to silence this warning."
        )
        unsigned = True

    # Select a signing key, or None if unsigned.
    signing_key = None if unsigned else (args.key or bindist.select_signing_key())

    specs = _specs_to_be_packaged(
        roots,
        things_to_install=args.things_to_install,
        build_deps=args.with_build_dependencies or not args.without_build_dependencies,
    )

    if not args.private:
        specs = _skip_no_redistribute_for_public(specs)

    if len(specs) > 1:
        tty.info(f"Selected {len(specs)} specs to push to {push_url}")

    # Pushing not installed specs is an error. Either fail fast or populate the error list and
    # push installed package in best effort mode.
    failed: List[Tuple[Spec, BaseException]] = []
    with spack.store.STORE.db.read_transaction():
        if any(not s.installed for s in specs):
            specs, not_installed = stable_partition(specs, lambda s: s.installed)
            if args.fail_fast:
                raise PackagesAreNotInstalledError(not_installed)
            else:
                failed.extend(
                    (s, PackageNotInstalledError("package not installed")) for s in not_installed
                )

    # Warn about possible old binary mirror layout
    if not mirror.push_url.startswith("oci://"):
        check_mirror_for_layout(mirror)

    with bindist.make_uploader(
        mirror=mirror,
        force=args.force,
        update_index=args.update_index,
        signing_key=signing_key,
        base_image=args.base_image,
    ) as uploader:
        skipped, upload_errors = uploader.push(specs=specs)
        failed.extend(upload_errors)
        if not upload_errors and args.tag:
            uploader.tag(args.tag, roots)

    if skipped:
        if len(specs) == 1:
            tty.info("The spec is already in the buildcache. Use --force to overwrite it.")
        elif len(skipped) == len(specs):
            tty.info("All specs are already in the buildcache. Use --force to overwrite them.")
        else:
            tty.info(
                "The following {} specs were skipped as they already exist in the buildcache:\n"
                "    {}\n"
                "    Use --force to overwrite them.".format(
                    len(skipped), ", ".join(elide_list([_format_spec(s) for s in skipped], 5))
                )
            )

    if failed:
        if len(failed) == 1:
            raise failed[0][1]

        raise spack.error.SpackError(
            f"The following {len(failed)} errors occurred while pushing specs to the buildcache",
            "\n".join(
                elide_list(
                    [
                        f"    {_format_spec(spec)}: {e.__class__.__name__}: {e}"
                        for spec, e in failed
                    ],
                    5,
                )
            ),
        )


def install_fn(args):
    """install from a binary package"""
    if not args.specs:
        tty.die("a spec argument is required to install from a buildcache")

    query = bindist.BinaryCacheQuery(all_architectures=args.otherarch)
    matches = spack.store.find(args.specs, multiple=args.multiple, query_fn=query)
    for match in matches:
        bindist.install_single_spec(match, unsigned=args.unsigned, force=args.force)


def list_fn(args):
    """list binary packages available from mirrors"""
    try:
        specs = bindist.update_cache_and_get_specs()
    except bindist.FetchCacheError as e:
        tty.die(e)

    if not args.allarch:
        arch = spack.spec.Spec.default_arch()
        specs = [s for s in specs if s.intersects(arch)]

    if args.specs:
        constraints = set(args.specs)
        specs = [s for s in specs if any(s.intersects(c) for c in constraints)]
    if sys.stdout.isatty():
        builds = len(specs)
        tty.msg("%s." % plural(builds, "cached build"))
        if not builds and not args.allarch:
            tty.msg(
                "You can query all available architectures with:",
                "spack buildcache list --allarch",
            )
    display_specs(specs, args, all_headers=True)


def keys_fn(args):
    """get public keys available on mirrors"""
    bindist.get_keys(args.install, args.trust, args.force)


def check_fn(args: argparse.Namespace):
    """check specs against remote binary mirror(s) to see if any need to be rebuilt

    this command uses the process exit code to indicate its result, specifically, if the
    exit code is non-zero, then at least one of the indicated specs needs to be rebuilt
    """
    specs_arg = args.specs

    if specs_arg:
        specs = _matching_specs(spack.cmd.parse_specs(specs_arg))
    else:
        specs = spack.cmd.require_active_env("buildcache check").all_specs()

    if not specs:
        tty.msg("No specs provided, exiting.")
        return

    specs = [spack.concretize.concretize_one(s) for s in specs]

    # Next see if there are any configured binary mirrors
    configured_mirrors = spack.config.get("mirrors", scope=args.scope)

    if args.mirror_url:
        configured_mirrors = {"additionalMirrorUrl": args.mirror_url}

    if not configured_mirrors:
        tty.msg("No mirrors provided, exiting.")
        return

    if bindist.check_specs_against_mirrors(configured_mirrors, specs, args.output_file) == 1:
        sys.exit(1)


def download_fn(args):
    """download buildcache entry from a remote mirror to local folder

    this command uses the process exit code to indicate its result, specifically, a non-zero exit
    code indicates that the command failed to download at least one of the required buildcache
    components
    """
    specs = _matching_specs(spack.cmd.parse_specs(args.spec))

    if len(specs) != 1:
        tty.die("a single spec argument is required to download from a buildcache")

    bindist.download_single_spec(specs[0], args.path)


def save_specfile_fn(args):
    """get full spec for dependencies and write them to files in the specified output directory

    uses exit code to signal success or failure. an exit code of zero means the command was likely
    successful. if any errors or exceptions are encountered, or if expected command-line arguments
    are not provided, then the exit code will be non-zero
    """
    specs = spack.cmd.parse_specs(args.root_spec)

    if len(specs) != 1:
        tty.die("a single spec argument is required to save specfile")

    root = specs[0]

    if not root.concrete:
        root = spack.concretize.concretize_one(root)

    save_dependency_specfiles(
        root, args.specfile_dir, dependencies=spack.cmd.parse_specs(args.specs)
    )


def copy_buildcache_entry(cache_entry: URLBuildcacheEntry, destination_url: str):
    """Download buildcache entry and copy it to the destination_url"""
    try:
        spec_dict = cache_entry.fetch_metadata()
        cache_entry.fetch_archive()
    except bindist.BuildcacheEntryError as e:
        tty.warn(f"Failed to retrieve buildcache for copying due to {e}")
        cache_entry.destroy()
        return

    spec_blob_record = cache_entry.get_blob_record(BuildcacheComponent.SPEC)
    local_spec_path = cache_entry.get_local_spec_path()
    tarball_blob_record = cache_entry.get_blob_record(BuildcacheComponent.TARBALL)
    local_tarball_path = cache_entry.get_local_archive_path()

    target_spec = spack.spec.Spec.from_dict(spec_dict)
    spec_label = f"{target_spec.name}/{target_spec.dag_hash()[:7]}"

    if not tarball_blob_record:
        cache_entry.destroy()
        raise BuildcacheEntryError(f"No source tarball blob record, failed to sync {spec_label}")

    # Try to push the tarball
    tarball_dest_url = cache_entry.get_blob_url(destination_url, tarball_blob_record)

    try:
        web_util.push_to_url(local_tarball_path, tarball_dest_url, keep_original=True)
    except Exception as e:
        tty.warn(f"Failed to push {local_tarball_path} to {tarball_dest_url} due to {e}")
        cache_entry.destroy()
        return

    if not spec_blob_record:
        cache_entry.destroy()
        raise BuildcacheEntryError(f"No source spec blob record, failed to sync {spec_label}")

    # Try to push the spec file
    spec_dest_url = cache_entry.get_blob_url(destination_url, spec_blob_record)

    try:
        web_util.push_to_url(local_spec_path, spec_dest_url, keep_original=True)
    except Exception as e:
        tty.warn(f"Failed to push {local_spec_path} to {spec_dest_url} due to {e}")
        cache_entry.destroy()
        return

    # Stage the manifest locally, since if it's signed, we don't want to try to
    # to reproduce that here. Instead just push the locally staged manifest to
    # the expected path at the destination url.
    manifest_src_url = cache_entry.remote_manifest_url
    manifest_dest_url = cache_entry.get_manifest_url(target_spec, destination_url)

    manifest_stage = spack.stage.Stage(manifest_src_url)

    try:
        manifest_stage.create()
        manifest_stage.fetch()
    except Exception as e:
        tty.warn(f"Failed to fetch manifest from {manifest_src_url} due to {e}")
        manifest_stage.destroy()
        cache_entry.destroy()
        return

    local_manifest_path = manifest_stage.save_filename

    try:
        web_util.push_to_url(local_manifest_path, manifest_dest_url, keep_original=True)
    except Exception as e:
        tty.warn(f"Failed to push manifest to {manifest_dest_url} due to {e}")

    manifest_stage.destroy()
    cache_entry.destroy()


def sync_fn(args):
    """sync binaries (and associated metadata) from one mirror to another

    requires an active environment in order to know which specs to sync
    """
    if args.manifest_glob:
        # Passing the args.src_mirror here because it is not possible to
        # have the destination be required when specifying a named source
        # mirror and optional for the --manifest-glob argument. In the case
        # of manifest glob sync, the source mirror positional argument is the
        # destination mirror if it is specified. If there are two mirrors
        # specified, the second is ignored and the first is the override
        # destination.
        if args.dest_mirror:
            tty.warn(f"Ignoring unused arguemnt: {args.dest_mirror.name}")

        manifest_copy(glob.glob(args.manifest_glob), args.src_mirror)
        return 0

    if args.src_mirror is None or args.dest_mirror is None:
        tty.die("Provide mirrors to sync from and to.")

    src_mirror = args.src_mirror
    dest_mirror = args.dest_mirror

    src_mirror_url = src_mirror.fetch_url
    dest_mirror_url = dest_mirror.push_url

    # Get the active environment
    env = spack.cmd.require_active_env(cmd_name="buildcache sync")

    tty.msg(
        "Syncing environment buildcache files from {0} to {1}".format(
            src_mirror_url, dest_mirror_url
        )
    )

    tty.debug("Syncing the following specs:")
    specs_to_sync = [s for s in env.all_specs() if not s.external]
    for s in specs_to_sync:
        tty.debug("  {0}{1}: {2}".format("* " if s in env.roots() else "  ", s.name, s.dag_hash()))
        cache_class = get_url_buildcache_class(
            layout_version=bindist.CURRENT_BUILD_CACHE_LAYOUT_VERSION
        )
        src_cache_entry = cache_class(src_mirror_url, s, allow_unsigned=True)
        src_cache_entry.read_manifest()
        copy_buildcache_entry(src_cache_entry, dest_mirror_url)


def manifest_copy(
    manifest_file_list: List[str], dest_mirror: Optional[spack.mirrors.mirror.Mirror] = None
):
    """Read manifest files containing information about specific specs to copy
    from source to destination, remove duplicates since any binary packge for
    a given hash should be the same as any other, and copy all files specified
    in the manifest files."""
    deduped_manifest = {}

    for manifest_path in manifest_file_list:
        with open(manifest_path, encoding="utf-8") as fd:
            manifest = json.loads(fd.read())
            for spec_hash, copy_obj in manifest.items():
                # Last duplicate hash wins
                deduped_manifest[spec_hash] = copy_obj

    for spec_hash, copy_obj in deduped_manifest.items():
        cache_class = get_url_buildcache_class(
            layout_version=bindist.CURRENT_BUILD_CACHE_LAYOUT_VERSION
        )
        src_cache_entry = cache_class(
            cache_class.get_base_url(copy_obj["src"]), allow_unsigned=True
        )
        src_cache_entry.read_manifest(manifest_url=copy_obj["src"])
        if dest_mirror:
            destination_url = dest_mirror.push_url
        else:
            destination_url = cache_class.get_base_url(copy_obj["dest"])
        tty.debug("copying {0} to {1}".format(copy_obj["src"], destination_url))
        copy_buildcache_entry(src_cache_entry, destination_url)


def update_index(mirror: spack.mirrors.mirror.Mirror, update_keys=False):
    # Special case OCI images for now.
    try:
        image_ref = spack.oci.oci.image_from_mirror(mirror)
    except ValueError:
        image_ref = None

    if image_ref:
        with tempfile.TemporaryDirectory(
            dir=spack.stage.get_stage_root()
        ) as tmpdir, spack.util.parallel.make_concurrent_executor() as executor:
            bindist._oci_update_index(image_ref, tmpdir, executor)
        return

    # Otherwise, assume a normal mirror.
    url = mirror.push_url

    with tempfile.TemporaryDirectory(dir=spack.stage.get_stage_root()) as tmpdir:
        bindist._url_generate_package_index(url, tmpdir)

    if update_keys:
        try:
            with tempfile.TemporaryDirectory(dir=spack.stage.get_stage_root()) as tmpdir:
                bindist.generate_key_index(url, tmpdir)
        except bindist.CannotListKeys as e:
            # Do not error out if listing keys went wrong. This usually means that the _gpg path
            # does not exist. TODO: distinguish between this and other errors.
            tty.warn(f"did not update the key index: {e}")


def update_index_fn(args):
    """update a buildcache index"""
    return update_index(args.mirror, update_keys=args.keys)


def migrate_fn(args):
    """perform in-place binary mirror migration (2 to 3)

    A mirror can contain both layout version 2 and version 3 simultaneously without
    interference. This command performs in-place migration of a binary mirror laid
    out according to version 2, to a binary mirror laid out according to layout
    version 3.  Only indexed specs will be migrated, so consider updating the mirror
    index before running this command.  Re-run the command to migrate any missing
    items.

    The default mode of operation is to perform a signed migration, that is, spack
    will attempt to verify the signatures on specs, and then re-sign them before
    migration, using whatever keys are already installed in your key ring.  You can
    migrate a mirror of unsigned binaries (or convert a mirror of signed binaries
    to unsigned) by providing the --unsigned argument.

    By default spack will leave the original mirror contents (in the old layout) in
    place after migration. You can have spack remove the old contents by providing
    the --delete-existing argument.  Because migrating a mostly-already-migrated
    mirror should be fast, consider a workflow where you perform a default migration,
    (i.e. preserve the existing layout rather than deleting it) then evaluate the
    state of the migrated mirror by attempting to install from it, and finally
    running the migration again with --delete-existing."""
    target_mirror = args.mirror
    unsigned = args.unsigned
    assert isinstance(target_mirror, spack.mirrors.mirror.Mirror)
    delete_existing = args.delete_existing

    proceed = True
    if delete_existing and not args.yes_to_all:
        msg = (
            "Using --delete-existing will delete the entire contents \n"
            "    of the old layout within the mirror. Because migrating a mirror \n"
            "    that has already been migrated should be fast, consider a workflow \n"
            "    where you perform a default migration (i.e. preserve the existing \n"
            "    layout rather than deleting it), then evaluate the state of the \n"
            "    migrated mirror by attempting to install from it, and finally, \n"
            "    run the migration again with --delete-existing."
        )
        tty.warn(msg)
        proceed = tty.get_yes_or_no("Do you want to proceed?", default=False)

    if not proceed:
        tty.die("Migration aborted.")

    migrate(target_mirror, unsigned=unsigned, delete_existing=delete_existing)


def buildcache(parser, args):
    return args.func(args)
