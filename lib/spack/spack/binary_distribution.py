# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import codecs
import collections
import concurrent.futures
import contextlib
import copy
import datetime
import hashlib
import io
import itertools
import json
import os
import pathlib
import re
import shutil
import sys
import tarfile
import tempfile
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings
from contextlib import closing
from typing import IO, Any, Callable, Dict, Iterable, List, Optional, Set, Tuple, Union

import llnl.util.filesystem as fsys
import llnl.util.lang
import llnl.util.tty as tty
from llnl.util.filesystem import mkdirp

import spack.caches
import spack.config
import spack.database as spack_db
import spack.deptypes as dt
import spack.error
import spack.hash_types as ht
import spack.hooks
import spack.hooks.sbang
import spack.mirrors.mirror
import spack.oci.image
import spack.oci.oci
import spack.oci.opener
import spack.paths
import spack.platforms
import spack.relocate as relocate
import spack.spec
import spack.stage
import spack.store
import spack.user_environment
import spack.util.archive
import spack.util.crypto
import spack.util.file_cache as file_cache
import spack.util.gpg
import spack.util.parallel
import spack.util.path
import spack.util.spack_json as sjson
import spack.util.spack_yaml as syaml
import spack.util.timer as timer
import spack.util.url as url_util
import spack.util.web as web_util
from spack import traverse
from spack.caches import misc_cache_location
from spack.oci.image import (
    Digest,
    ImageReference,
    default_config,
    default_manifest,
    ensure_valid_tag,
)
from spack.oci.oci import (
    copy_missing_layers_with_retry,
    get_manifest_and_config_with_retry,
    list_tags,
    upload_blob_with_retry,
    upload_manifest_with_retry,
)
from spack.package_prefs import get_package_dir_permissions, get_package_group
from spack.relocate_text import utf8_paths_to_single_binary_regex
from spack.stage import Stage
from spack.util.executable import which

from .enums import InstallRecordStatus
from .url_buildcache import (
    CURRENT_BUILD_CACHE_LAYOUT_VERSION,
    SUPPORTED_LAYOUT_VERSIONS,
    BlobRecord,
    BuildcacheComponent,
    BuildcacheEntryError,
    BuildcacheManifest,
    InvalidMetadataFile,
    ListMirrorSpecsError,
    MirrorForSpec,
    MirrorURLAndVersion,
    URLBuildcacheEntry,
    get_entries_from_cache,
    get_url_buildcache_class,
    get_valid_spec_file,
)


class BuildCacheDatabase(spack_db.Database):
    """A database for binary buildcaches.

    A database supports writing buildcache index files, in which case certain fields are not
    needed in each install record, and no locking is required. To use this feature, it provides
    ``lock_cfg=NO_LOCK``, and override the list of ``record_fields``.
    """

    record_fields = ("spec", "ref_count", "in_buildcache")

    def __init__(self, root):
        super().__init__(root, lock_cfg=spack_db.NO_LOCK, layout=None)
        self._write_transaction_impl = llnl.util.lang.nullcontext
        self._read_transaction_impl = llnl.util.lang.nullcontext

    def _handle_old_db_versions_read(self, check, db, *, reindex: bool):
        if not self.is_readable():
            raise spack_db.DatabaseNotReadableError(
                f"cannot read buildcache v{self.db_version} at {self.root}"
            )
        return self._handle_current_version_read(check, db)


class FetchCacheError(Exception):
    """Error thrown when fetching the cache failed, usually a composite error list."""

    def __init__(self, errors):
        if not isinstance(errors, list):
            raise TypeError("Expected a list of errors")
        self.errors = errors
        if len(errors) > 1:
            msg = "        Error {0}: {1}: {2}"
            self.message = "Multiple errors during fetching:\n"
            self.message += "\n".join(
                (
                    msg.format(i + 1, err.__class__.__name__, str(err))
                    for (i, err) in enumerate(errors)
                )
            )
        else:
            err = errors[0]
            self.message = "{0}: {1}".format(err.__class__.__name__, str(err))
        super().__init__(self.message)


class BinaryCacheIndex:
    """
    The BinaryCacheIndex tracks what specs are available on (usually remote)
    binary caches.

    This index is "best effort", in the sense that whenever we don't find
    what we're looking for here, we will attempt to fetch it directly from
    configured mirrors anyway.  Thus, it has the potential to speed things
    up, but cache misses shouldn't break any spack functionality.

    At the moment, everything in this class is initialized as lazily as
    possible, so that it avoids slowing anything in spack down until
    absolutely necessary.

    TODO: What's the cost if, e.g., we realize in the middle of a spack
    install that the cache is out of date, and we fetch directly?  Does it
    mean we should have paid the price to update the cache earlier?
    """

    def __init__(self, cache_root: Optional[str] = None):
        self._index_cache_root: str = cache_root or binary_index_location()

        # the key associated with the serialized _local_index_cache
        self._index_contents_key = "contents.json"

        # a FileCache instance storing copies of remote binary cache indices
        self._index_file_cache: file_cache.FileCache = file_cache.FileCache(self._index_cache_root)
        self._index_file_cache_initialized = False

        # stores a map of mirror URL and version layout to index hash and cache key (index path)
        self._local_index_cache: dict[str, dict] = {}

        # hashes of remote indices already ingested into the concrete spec
        # cache (_mirrors_for_spec)
        self._specs_already_associated: Set[str] = set()

        # mapping from mirror urls to the time.time() of the last index fetch and a bool indicating
        # whether the fetch succeeded or not.
        self._last_fetch_times: Dict[MirrorURLAndVersion, float] = {}

        # _mirrors_for_spec is a dictionary mapping DAG hashes to lists of
        # entries indicating mirrors where that concrete spec can be found.
        # Each entry is a MirrorURLAndVersion.
        self._mirrors_for_spec: Dict[str, List[MirrorForSpec]] = {}

    def _init_local_index_cache(self):
        # seems logical but fails bootstrapping
        # cache_key = self._index_contents_key
        # exists = self._index_file_cache.init_entry(cache_key)
        # cache_path = self._index_file_cache.cache_path(cache_key)
        # if not exists and self._index_file_cache_initialized:
        # raise FileNotFoundError(f"Missing {cache_path}")

        if not self._index_file_cache_initialized:
            cache_key = self._index_contents_key
            self._index_file_cache.init_entry(cache_key)
            cache_path = self._index_file_cache.cache_path(cache_key)

            self._local_index_cache = {}
            if os.path.isfile(cache_path):
                with self._index_file_cache.read_transaction(cache_key) as cache_file:
                    self._local_index_cache = json.load(cache_file)

            self._index_file_cache_initialized = True

    def clear(self):
        """For testing purposes we need to be able to empty the cache and
        clear associated data structures."""
        if self._index_file_cache:
            self._index_file_cache.destroy()
            self._index_file_cache_initialized = False
            self._index_file_cache = file_cache.FileCache(self._index_cache_root)
        self._local_index_cache = {}
        self._specs_already_associated = set()
        self._last_fetch_times = {}
        self._mirrors_for_spec = {}

    def _write_local_index_cache(self):
        self._init_local_index_cache()
        cache_key = self._index_contents_key
        with self._index_file_cache.write_transaction(cache_key) as (old, new):
            json.dump(self._local_index_cache, new)

    def regenerate_spec_cache(self, clear_existing=False):
        """Populate the local cache of concrete specs (``_mirrors_for_spec``)
        from the locally cached buildcache index files.  This is essentially a
        no-op if it has already been done, as we keep track of the index
        hashes for which we have already associated the built specs."""
        self._init_local_index_cache()

        if clear_existing:
            self._specs_already_associated = set()
            self._mirrors_for_spec = {}

        for url_and_version in self._local_index_cache:
            cache_entry = self._local_index_cache[url_and_version]
            cached_index_path = cache_entry["index_path"]
            cached_index_hash = cache_entry["index_hash"]
            if cached_index_hash not in self._specs_already_associated:
                self._associate_built_specs_with_mirror(
                    cached_index_path, MirrorURLAndVersion.from_string(url_and_version)
                )
                self._specs_already_associated.add(cached_index_hash)

    def _associate_built_specs_with_mirror(self, cache_key, url_and_version: MirrorURLAndVersion):
        mirror_url = url_and_version.url
        layout_version = url_and_version.version

        with tempfile.TemporaryDirectory(dir=spack.stage.get_stage_root()) as tmpdir:
            db = BuildCacheDatabase(tmpdir)

            try:
                cache_file_exists = self._index_file_cache.init_entry(cache_key)
                with self._index_file_cache.write_transaction(cache_key):
                    cache_file_path = self._index_file_cache.cache_path(cache_key)
                    if not cache_file_exists:
                        # recreate index if it is missing
                        cache_entry = self._local_index_cache[str(url_and_version)]
                        self._fetch_and_cache_index(url_and_version, cache_entry)

                    if os.path.getsize(cache_file_path) == 0:
                        lines = textwrap.wrap(
                            f"Buildcache index for the v{layout_version} mirror "
                            f"at '{mirror_url}' is empty. "
                            "If the mirror layout is deprecated and you cannot migrate "
                            "it to the new format, consider removing it using:",
                            width=72,
                            subsequent_indent="  ",
                        )
                        lines.extend(
                            [
                                "    'spack mirror list'",
                                "    'spack mirror remove <name>'",
                                "  with the <name> for the mirror url shown in the list.",
                            ]
                        )
                        tty.warn("\n".join(lines))
                        return

                    db._read_from_file(cache_file_path)
            except spack_db.InvalidDatabaseVersionError as e:
                tty.warn(
                    "you need a newer Spack version to read the buildcache index "
                    f"for the following v{layout_version} mirror: '{mirror_url}'. "
                    f"{e.database_version_message}"
                )
                return

            spec_list = [
                s
                for s in db.query_local(installed=InstallRecordStatus.ANY)
                if s.external or db.query_local_by_spec_hash(s.dag_hash()).in_buildcache
            ]

            for indexed_spec in spec_list:
                dag_hash = indexed_spec.dag_hash()

                if dag_hash not in self._mirrors_for_spec:
                    self._mirrors_for_spec[dag_hash] = []

                for entry in self._mirrors_for_spec[dag_hash]:
                    # A binary mirror can only have one spec per DAG hash, so
                    # if we already have an entry under this DAG hash for this
                    # mirror url/layout version, we're done.
                    if (
                        entry.url_and_version.url == mirror_url
                        and entry.url_and_version.version == layout_version
                    ):
                        break
                else:
                    self._mirrors_for_spec[dag_hash].append(
                        MirrorForSpec(url_and_version, indexed_spec)
                    )

    def get_all_built_specs(self):
        spec_list = []
        for dag_hash in self._mirrors_for_spec:
            # in the absence of further information, all concrete specs
            # with the same DAG hash are equivalent, so we can just
            # return the first one in the list.
            if len(self._mirrors_for_spec[dag_hash]) > 0:
                spec_list.append(self._mirrors_for_spec[dag_hash][0].spec)

        return spec_list

    def find_built_spec(self, spec, mirrors_to_check=None):
        """Look in our cache for the built spec corresponding to ``spec``.

        If the spec can be found among the configured binary mirrors, a
        list is returned that contains the concrete spec and the mirror url
        of each mirror where it can be found.  Otherwise, ``None`` is
        returned.

        This method does not trigger reading anything from remote mirrors, but
        rather just checks if the concrete spec is found within the cache.

        The cache can be updated by calling ``update()`` on the cache.

        Args:
            spec (spack.spec.Spec): Concrete spec to find
            mirrors_to_check: Optional mapping containing mirrors to check.  If
                None, just assumes all configured mirrors.

        Returns:
            An list of objects containing the found specs and mirror url where
                each can be found, e.g.:

                .. code-block:: python

                    [
                        {
                            "spec": <concrete-spec>,
                            "mirror_url": <mirror-root-url>
                        }
                    ]
        """
        return self.find_by_hash(spec.dag_hash(), mirrors_to_check=mirrors_to_check)

    def find_by_hash(self, find_hash, mirrors_to_check=None):
        """Same as find_built_spec but uses the hash of a spec.

        Args:
            find_hash (str): hash of the spec to search
            mirrors_to_check: Optional mapping containing mirrors to check.  If
                None, just assumes all configured mirrors.
        """
        if find_hash not in self._mirrors_for_spec:
            return []
        results = self._mirrors_for_spec[find_hash]
        if not mirrors_to_check:
            return results
        mirror_urls = mirrors_to_check.values()
        return [r for r in results if r.url_and_version.url in mirror_urls]

    def update_spec(self, spec: spack.spec.Spec, found_list: List[MirrorForSpec]):
        """
        Take list of {'mirror_url': m, 'spec': s} objects and update the local
        built_spec_cache
        """
        spec_dag_hash = spec.dag_hash()

        if spec_dag_hash not in self._mirrors_for_spec:
            self._mirrors_for_spec[spec_dag_hash] = found_list
        else:
            current_list = self._mirrors_for_spec[spec_dag_hash]
            for new_entry in found_list:
                for cur_entry in current_list:
                    if new_entry.url_and_version == cur_entry.url_and_version:
                        cur_entry.spec = new_entry.spec
                        break
                else:
                    current_list.append(MirrorForSpec(new_entry.url_and_version, new_entry.spec))

    def update(self, with_cooldown=False):
        """Make sure local cache of buildcache index files is up to date.
        If the same mirrors are configured as the last time this was called
        and none of the remote buildcache indices have changed, calling this
        method will only result in fetching the index hash from each mirror
        to confirm it is the same as what is stored locally.  Otherwise, the
        buildcache ``index.json`` and ``index.json.hash`` files are retrieved
        from each configured mirror and stored locally (both in memory and
        on disk under ``_index_cache_root``)."""
        self._init_local_index_cache()
        configured_mirrors = [
            MirrorURLAndVersion(m.fetch_url, layout_version)
            for layout_version in SUPPORTED_LAYOUT_VERSIONS
            for m in spack.mirrors.mirror.MirrorCollection(binary=True).values()
        ]
        items_to_remove = []
        spec_cache_clear_needed = False
        spec_cache_regenerate_needed = not self._mirrors_for_spec

        # First compare the mirror urls currently present in the cache to the
        # configured mirrors.  If we have a cached index for a mirror which is
        # no longer configured, we should remove it from the cache.  For any
        # cached indices corresponding to currently configured mirrors, we need
        # to check if the cache is still good, or needs to be updated.
        # Finally, if there are configured mirrors for which we don't have a
        # cache entry, we need to fetch and cache the indices from those
        # mirrors.

        # If, during this process, we find that any mirrors for which we
        # already have entries have either been removed, or their index
        # hash has changed, then our concrete spec cache (_mirrors_for_spec)
        # likely has entries that need to be removed, so we will clear it
        # and regenerate that data structure.

        # If, during this process, we find that there are new mirrors for
        # which do not yet have an entry in our index cache, then we simply
        # need to regenerate the concrete spec cache, but do not need to
        # clear it first.

        # Otherwise the concrete spec cache should not need to be updated at
        # all.

        fetch_errors = []
        all_methods_failed = True
        ttl = spack.config.get("config:binary_index_ttl", 600)
        now = time.time()

        for local_index_cache_key in self._local_index_cache:
            urlAndVersion = MirrorURLAndVersion.from_string(local_index_cache_key)
            cached_mirror_url = urlAndVersion.url
            cache_entry = self._local_index_cache[local_index_cache_key]
            cached_index_path = cache_entry["index_path"]
            if urlAndVersion in configured_mirrors:
                # Only do a fetch if the last fetch was longer than TTL ago
                if (
                    with_cooldown
                    and ttl > 0
                    and cached_mirror_url in self._last_fetch_times
                    and now - self._last_fetch_times[urlAndVersion][0] < ttl
                ):
                    # We're in the cooldown period, don't try to fetch again
                    # If the fetch succeeded last time, consider this update a success, otherwise
                    # re-report the error here
                    if self._last_fetch_times[urlAndVersion][1]:
                        all_methods_failed = False
                else:
                    # May need to fetch the index and update the local caches
                    try:
                        needs_regen = self._fetch_and_cache_index(
                            urlAndVersion, cache_entry=cache_entry
                        )
                        self._last_fetch_times[urlAndVersion] = (now, True)
                        all_methods_failed = False
                    except FetchIndexError as e:
                        needs_regen = False
                        fetch_errors.append(e)
                        self._last_fetch_times[urlAndVersion] = (now, False)
                    # The need to regenerate implies a need to clear as well.
                    spec_cache_clear_needed |= needs_regen
                    spec_cache_regenerate_needed |= needs_regen
            else:
                # No longer have this mirror, cached index should be removed
                items_to_remove.append(
                    {
                        "url": local_index_cache_key,
                        "cache_key": os.path.join(self._index_cache_root, cached_index_path),
                    }
                )
                if urlAndVersion in self._last_fetch_times:
                    del self._last_fetch_times[urlAndVersion]
                spec_cache_clear_needed = True
                spec_cache_regenerate_needed = True

        # Clean up items to be removed, identified above
        for item in items_to_remove:
            url = item["url"]
            cache_key = item["cache_key"]
            self._index_file_cache.remove(cache_key)
            del self._local_index_cache[url]

        # Iterate the configured mirrors now.  Any mirror urls we do not
        # already have in our cache must be fetched, stored, and represented
        # locally.
        for urlAndVersion in configured_mirrors:
            if str(urlAndVersion) in self._local_index_cache:
                continue

            # Need to fetch the index and update the local caches
            try:
                needs_regen = self._fetch_and_cache_index(urlAndVersion)
                self._last_fetch_times[urlAndVersion] = (now, True)
                all_methods_failed = False
            except FetchIndexError as e:
                fetch_errors.append(e)
                needs_regen = False
                self._last_fetch_times[urlAndVersion] = (now, False)
            # Generally speaking, a new mirror wouldn't imply the need to
            # clear the spec cache, so leave it as is.
            if needs_regen:
                spec_cache_regenerate_needed = True

        self._write_local_index_cache()

        if configured_mirrors and all_methods_failed:
            raise FetchCacheError(fetch_errors)
        if fetch_errors:
            tty.warn(
                "The following issues were ignored while updating the indices of binary caches",
                FetchCacheError(fetch_errors),
            )
        if spec_cache_regenerate_needed:
            self.regenerate_spec_cache(clear_existing=spec_cache_clear_needed)

    def _fetch_and_cache_index(self, url_and_version: MirrorURLAndVersion, cache_entry={}):
        """Fetch a buildcache index file from a remote mirror and cache it.

        If we already have a cached index from this mirror, then we first
        check if the hash has changed, and we avoid fetching it if not.

        Args:
            url_and_version: Contains mirror base url and target binary cache layout version
            cache_entry (dict): Old cache metadata with keys ``index_hash``, ``index_path``,
                ``etag``

        Returns:
            True if the local index.json was updated.

        Throws:
            FetchIndexError
        """
        mirror_url = url_and_version.url
        layout_version = url_and_version.version

        # TODO: get rid of this request, handle 404 better
        scheme = urllib.parse.urlparse(mirror_url).scheme

        if scheme != "oci":
            cache_class = get_url_buildcache_class(layout_version=layout_version)
            if not web_util.url_exists(cache_class.get_index_url(mirror_url)):
                return False

        fetcher: IndexFetcher = get_index_fetcher(scheme, url_and_version, cache_entry)
        result = fetcher.conditional_fetch()

        # Nothing to do
        if result.fresh:
            return False

        # Persist new index.json
        url_hash = compute_hash(f"{mirror_url}/v{layout_version}")
        cache_key = "{}_{}.json".format(url_hash[:10], result.hash[:10])
        self._index_file_cache.init_entry(cache_key)
        with self._index_file_cache.write_transaction(cache_key) as (old, new):
            new.write(result.data)

        self._local_index_cache[str(url_and_version)] = {
            "index_hash": result.hash,
            "index_path": cache_key,
            "etag": result.etag,
        }

        # clean up the old cache_key if necessary
        old_cache_key = cache_entry.get("index_path", None)
        if old_cache_key and old_cache_key != cache_key:
            self._index_file_cache.remove(old_cache_key)

        # We fetched an index and updated the local index cache, we should
        # regenerate the spec cache as a result.
        return True


def binary_index_location():
    """Set up a BinaryCacheIndex for remote buildcache dbs in the user's homedir."""
    cache_root = os.path.join(misc_cache_location(), "indices")
    return spack.util.path.canonicalize_path(cache_root)


#: Default binary cache index instance
BINARY_INDEX: BinaryCacheIndex = llnl.util.lang.Singleton(BinaryCacheIndex)  # type: ignore


def compute_hash(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def buildinfo_file_name(prefix):
    """Filename of the binary package meta-data file"""
    return os.path.join(prefix, ".spack", "binary_distribution")


def read_buildinfo_file(prefix):
    """Read buildinfo file"""
    with open(buildinfo_file_name(prefix), "r", encoding="utf-8") as f:
        return syaml.load(f)


def file_matches(f: IO[bytes], regex: llnl.util.lang.PatternBytes) -> bool:
    try:
        return bool(regex.search(f.read()))
    finally:
        f.seek(0)


def specs_to_relocate(spec: spack.spec.Spec) -> List[spack.spec.Spec]:
    """Return the set of specs that may be referenced in the install prefix of the provided spec.
    We currently include non-external transitive link and direct run dependencies."""
    specs = [
        s
        for s in itertools.chain(
            spec.traverse(root=True, deptype="link", order="breadth", key=traverse.by_dag_hash),
            spec.dependencies(deptype="run"),
        )
        if not s.external
    ]
    return list(llnl.util.lang.dedupe(specs, key=lambda s: s.dag_hash()))


def get_buildinfo_dict(spec):
    """Create metadata for a tarball"""
    return {
        "sbang_install_path": spack.hooks.sbang.sbang_install_path(),
        "buildpath": spack.store.STORE.layout.root,
        "spackprefix": spack.paths.prefix,
        "relative_prefix": os.path.relpath(spec.prefix, spack.store.STORE.layout.root),
        # "relocate_textfiles": [],
        # "relocate_binaries": [],
        # "relocate_links": [],
        "hardlinks_deduped": True,
        "hash_to_prefix": {d.dag_hash(): str(d.prefix) for d in specs_to_relocate(spec)},
    }


def buildcache_relative_keys_path(layout_version: int = CURRENT_BUILD_CACHE_LAYOUT_VERSION):
    cache_class = get_url_buildcache_class(layout_version=layout_version)
    return os.path.join(*cache_class.get_relative_path_components(BuildcacheComponent.KEY))


def buildcache_relative_keys_url(layout_version: int = CURRENT_BUILD_CACHE_LAYOUT_VERSION):
    cache_class = get_url_buildcache_class(layout_version=layout_version)
    return url_util.join(*cache_class.get_relative_path_components(BuildcacheComponent.KEY))


def buildcache_relative_specs_path(layout_version: int = CURRENT_BUILD_CACHE_LAYOUT_VERSION):
    cache_class = get_url_buildcache_class(layout_version=layout_version)
    return os.path.join(*cache_class.get_relative_path_components(BuildcacheComponent.SPEC))


def buildcache_relative_specs_url(layout_version: int = CURRENT_BUILD_CACHE_LAYOUT_VERSION):
    cache_class = get_url_buildcache_class(layout_version=layout_version)
    return url_util.join(*cache_class.get_relative_path_components(BuildcacheComponent.SPEC))


def buildcache_relative_blobs_path(layout_version: int = CURRENT_BUILD_CACHE_LAYOUT_VERSION):
    cache_class = get_url_buildcache_class(layout_version=layout_version)
    return os.path.join(*cache_class.get_relative_path_components(BuildcacheComponent.BLOB))


def buildcache_relative_blobs_url(layout_version: int = CURRENT_BUILD_CACHE_LAYOUT_VERSION):
    cache_class = get_url_buildcache_class(layout_version=layout_version)
    return url_util.join(*cache_class.get_relative_path_components(BuildcacheComponent.BLOB))


def buildcache_relative_index_path(layout_version: int = CURRENT_BUILD_CACHE_LAYOUT_VERSION):
    cache_class = get_url_buildcache_class(layout_version=layout_version)
    return os.path.join(*cache_class.get_relative_path_components(BuildcacheComponent.INDEX))


def buildcache_relative_index_url(layout_version: int = CURRENT_BUILD_CACHE_LAYOUT_VERSION):
    cache_class = get_url_buildcache_class(layout_version=layout_version)
    return url_util.join(*cache_class.get_relative_path_components(BuildcacheComponent.INDEX))


@llnl.util.lang.memoized
def warn_v2_layout(mirror_url: str, action: str) -> bool:
    lines = textwrap.wrap(
        f"{action} from a v2 binary mirror layout, located at "
        f"{mirror_url} is deprecated. Support for this will be "
        "removed in a future version of spack. "
        "If you manage the buildcache please consider running:",
        width=72,
        subsequent_indent="  ",
    )
    lines.extend(
        [
            "    'spack buildcache migrate'",
            "  or rebuilding the specs in this mirror. Otherwise, consider running:",
            "    'spack mirror list'",
            "    'spack mirror remove <name>'",
            "  with the <name> for the mirror url shown in the list.",
        ]
    )
    tty.warn("\n".join(lines))
    return True


def select_signing_key() -> str:
    keys = spack.util.gpg.signing_keys()
    num = len(keys)
    if num > 1:
        raise PickKeyException(str(keys))
    elif num == 0:
        raise NoKeyException(
            "No default key available for signing.\n"
            "Use spack gpg init and spack gpg create"
            " to create a default key."
        )
    return keys[0]


def _push_index(db: BuildCacheDatabase, temp_dir: str, cache_prefix: str):
    """Generate the index, compute its hash, and push the files to the mirror"""
    index_json_path = os.path.join(temp_dir, spack_db.INDEX_JSON_FILE)
    with open(index_json_path, "w", encoding="utf-8") as f:
        db._write_to_file(f)

    cache_class = get_url_buildcache_class(layout_version=CURRENT_BUILD_CACHE_LAYOUT_VERSION)
    cache_class.push_local_file_as_blob(
        index_json_path, cache_prefix, "index", BuildcacheComponent.INDEX, compression="none"
    )
    cache_class.maybe_push_layout_json(cache_prefix)


def _read_specs_and_push_index(
    file_list: List[str],
    read_method: Callable[[str], URLBuildcacheEntry],
    cache_prefix: str,
    db: BuildCacheDatabase,
    temp_dir: str,
):
    """Read listed specs, generate the index, and push it to the mirror.

    Args:
        file_list: List of urls or file paths pointing at spec files to read
        read_method: A function taking a single argument, either a url or a file path,
            and which reads the spec file at that location, and returns the spec.
        cache_prefix: prefix of the build cache on s3 where index should be pushed.
        db: A spack database used for adding specs and then writing the index.
        temp_dir: Location to write index.json and hash for pushing
    """
    for file in file_list:
        cache_entry: Optional[URLBuildcacheEntry] = None
        try:
            cache_entry = read_method(file)
            spec_dict = cache_entry.fetch_metadata()
            fetched_spec = spack.spec.Spec.from_dict(spec_dict)
        except Exception as e:
            tty.warn(f"Unable to fetch spec for manifest {file} due to: {e}")
            continue
        finally:
            if cache_entry:
                cache_entry.destroy()
        db.add(fetched_spec)
        db.mark(fetched_spec, "in_buildcache", True)

    _push_index(db, temp_dir, cache_prefix)


def _url_generate_package_index(url: str, tmpdir: str):
    """Create or replace the build cache index on the given mirror.  The
    buildcache index contains an entry for each binary package under the
    cache_prefix.

    Args:
        url: Base url of binary mirror.

    Return:
        None
    """
    with tempfile.TemporaryDirectory(dir=spack.stage.get_stage_root()) as tmpspecsdir:
        try:
            file_list, read_fn = get_entries_from_cache(
                url, tmpspecsdir, component_type=BuildcacheComponent.SPEC
            )
        except ListMirrorSpecsError as e:
            raise GenerateIndexError(f"Unable to generate package index: {e}") from e

        tty.debug(f"Retrieving spec descriptor files from {url} to build index")

        db = BuildCacheDatabase(tmpdir)
        db._write()

        try:
            _read_specs_and_push_index(file_list, read_fn, url, db, str(db.database_directory))
        except Exception as e:
            raise GenerateIndexError(
                f"Encountered problem pushing package index to {url}: {e}"
            ) from e


def generate_key_index(mirror_url: str, tmpdir: str) -> None:
    """Create the key index page.

    Creates (or replaces) the "index.json" page at the location given in mirror_url.  This page
    contains an entry for each key under mirror_url.
    """

    tty.debug(f"Retrieving key.pub files from {url_util.format(mirror_url)} to build key index")

    key_prefix = url_util.join(mirror_url, buildcache_relative_keys_url())

    try:
        fingerprints = (
            entry[:-18]
            for entry in web_util.list_url(key_prefix, recursive=False)
            if entry.endswith(".key.manifest.json")
        )
    except Exception as e:
        raise CannotListKeys(f"Encountered problem listing keys at {key_prefix}: {e}") from e

    target = os.path.join(tmpdir, "index.json")

    index = {"keys": dict((fingerprint, {}) for fingerprint in sorted(set(fingerprints)))}
    with open(target, "w", encoding="utf-8") as f:
        sjson.dump(index, f)

    cache_class = get_url_buildcache_class()

    try:
        cache_class.push_local_file_as_blob(
            local_file_path=target,
            mirror_url=mirror_url,
            manifest_name="keys",
            component_type=BuildcacheComponent.KEY_INDEX,
            compression="none",
        )
        cache_class.maybe_push_layout_json(mirror_url)
    except Exception as e:
        raise GenerateIndexError(
            f"Encountered problem pushing key index to {key_prefix}: {e}"
        ) from e


class FileTypes:
    BINARY = 0
    TEXT = 1
    UNKNOWN = 2


NOT_ISO8859_1_TEXT = re.compile(b"[\x00\x7f-\x9f]")


def file_type(f: IO[bytes]) -> int:
    try:
        # first check if this is an ELF or mach-o binary.
        magic = f.read(8)
        if len(magic) < 8:
            return FileTypes.UNKNOWN
        elif relocate.is_elf_magic(magic) or relocate.is_macho_magic(magic):
            return FileTypes.BINARY

        f.seek(0)

        # Then try utf-8, which has a fast exponential decay in false positive rate with file size.
        # Use chunked reads for fast early exit.
        f_txt = io.TextIOWrapper(f, encoding="utf-8", errors="strict")
        try:
            while f_txt.read(1024):
                pass
            return FileTypes.TEXT
        except UnicodeError:
            f_txt.seek(0)
            pass
        finally:
            f_txt.detach()
        # Finally try iso-8859-1 heuristically. In Python, all possible 256 byte values are valid.
        # We classify it as text if it does not contain any control characters / null bytes.
        data = f.read(1024)
        while data:
            if NOT_ISO8859_1_TEXT.search(data):
                break
            data = f.read(1024)
        else:
            return FileTypes.TEXT
        return FileTypes.UNKNOWN
    finally:
        f.seek(0)


def tarfile_of_spec_prefix(
    tar: tarfile.TarFile, prefix: str, prefixes_to_relocate: List[str]
) -> dict:
    """Create a tarfile of an install prefix of a spec. Skips existing buildinfo file.

    Args:
        tar: tarfile object to add files to
        prefix: absolute install prefix of spec"""
    if not os.path.isabs(prefix) or not os.path.isdir(prefix):
        raise ValueError(f"prefix '{prefix}' must be an absolute path to a directory")
    stat_key = lambda stat: (stat.st_dev, stat.st_ino)

    try:  # skip buildinfo file if it exists
        files_to_skip = [stat_key(os.lstat(buildinfo_file_name(prefix)))]
        skip = lambda entry: stat_key(entry.stat(follow_symlinks=False)) in files_to_skip
    except OSError:
        skip = lambda entry: False

    binary_regex = utf8_paths_to_single_binary_regex(prefixes_to_relocate)

    relocate_binaries = []
    relocate_links = []
    relocate_textfiles = []

    # use callbacks to add files and symlinks, so we can register which files need relocation upon
    # extraction.
    def add_file(tar: tarfile.TarFile, info: tarfile.TarInfo, path: str):
        with open(path, "rb") as f:
            relpath = os.path.relpath(path, prefix)
            # no need to relocate anything in the .spack directory
            if relpath.split(os.sep, 1)[0] == ".spack":
                tar.addfile(info, f)
                return
            f_type = file_type(f)
            if f_type == FileTypes.BINARY:
                relocate_binaries.append(os.path.relpath(path, prefix))
            elif f_type == FileTypes.TEXT and file_matches(f, binary_regex):
                relocate_textfiles.append(os.path.relpath(path, prefix))
            tar.addfile(info, f)

    def add_symlink(tar: tarfile.TarFile, info: tarfile.TarInfo, path: str):
        if os.path.isabs(info.linkname) and binary_regex.match(info.linkname.encode("utf-8")):
            relocate_links.append(os.path.relpath(path, prefix))
        tar.addfile(info)

    spack.util.archive.reproducible_tarfile_from_prefix(
        tar,
        prefix,
        # Spack <= 0.21 did not include parent directories, leading to issues when tarballs are
        # used in runtimes like AWS lambda.
        include_parent_directories=True,
        skip=skip,
        add_file=add_file,
        add_symlink=add_symlink,
    )

    return {
        "relocate_binaries": relocate_binaries,
        "relocate_links": relocate_links,
        "relocate_textfiles": relocate_textfiles,
    }


def create_tarball(spec: spack.spec.Spec, tarfile_path: str) -> Tuple[str, str]:
    """Create a tarball of a spec and return the checksums of the compressed tarfile and the
    uncompressed tarfile."""
    return _do_create_tarball(
        tarfile_path,
        spec.prefix,
        buildinfo=get_buildinfo_dict(spec),
        prefixes_to_relocate=prefixes_to_relocate(spec),
    )


def _do_create_tarball(
    tarfile_path: str, prefix: str, buildinfo: dict, prefixes_to_relocate: List[str]
) -> Tuple[str, str]:
    with spack.util.archive.gzip_compressed_tarfile(tarfile_path) as (
        tar,
        tar_gz_checksum,
        tar_checksum,
    ):
        # Tarball the install prefix
        files_to_relocate = tarfile_of_spec_prefix(tar, prefix, prefixes_to_relocate)
        buildinfo.update(files_to_relocate)

        # Serialize buildinfo for the tarball
        bstring = syaml.dump(buildinfo, default_flow_style=True).encode("utf-8")
        tarinfo = tarfile.TarInfo(
            name=spack.util.archive.default_path_to_name(buildinfo_file_name(prefix))
        )
        tarinfo.type = tarfile.REGTYPE
        tarinfo.size = len(bstring)
        tarinfo.mode = 0o644
        tar.addfile(tarinfo, io.BytesIO(bstring))

    return tar_gz_checksum.hexdigest(), tar_checksum.hexdigest()


def _exists_in_buildcache(
    spec: spack.spec.Spec, out_url: str, allow_unsigned: bool = False
) -> URLBuildcacheEntry:
    """creates and returns (after checking existence) a URLBuildcacheEntry"""
    cache_type = get_url_buildcache_class(CURRENT_BUILD_CACHE_LAYOUT_VERSION)
    cache_entry = cache_type(out_url, spec, allow_unsigned=allow_unsigned)
    return cache_entry


def prefixes_to_relocate(spec):
    prefixes = [s.prefix for s in specs_to_relocate(spec)]
    prefixes.append(spack.hooks.sbang.sbang_install_path())
    prefixes.append(str(spack.store.STORE.layout.root))
    return prefixes


def _url_upload_tarball_and_specfile(
    spec: spack.spec.Spec, tmpdir: str, cache_entry: URLBuildcacheEntry, signing_key: Optional[str]
):
    tarball = os.path.join(tmpdir, f"{spec.dag_hash()}.tar.gz")
    checksum, _ = create_tarball(spec, tarball)

    cache_entry.push_binary_package(spec, tarball, "sha256", checksum, tmpdir, signing_key)


class Uploader:
    def __init__(self, mirror: spack.mirrors.mirror.Mirror, force: bool, update_index: bool):
        self.mirror = mirror
        self.force = force
        self.update_index = update_index

        self.tmpdir: str
        self.executor: concurrent.futures.Executor

        # Verify if the mirror meets the requirements to push
        self.mirror.ensure_mirror_usable("push")

    def __enter__(self):
        self._tmpdir = tempfile.TemporaryDirectory(dir=spack.stage.get_stage_root())
        self._executor = spack.util.parallel.make_concurrent_executor()

        self.tmpdir = self._tmpdir.__enter__()
        self.executor = self.executor = self._executor.__enter__()

        return self

    def __exit__(self, *args):
        self._executor.__exit__(*args)
        self._tmpdir.__exit__(*args)

    def push_or_raise(self, specs: List[spack.spec.Spec]) -> List[spack.spec.Spec]:
        skipped, errors = self.push(specs)
        if errors:
            raise PushToBuildCacheError(
                f"Failed to push {len(errors)} specs to {self.mirror.push_url}:\n"
                + "\n".join(
                    f"Failed to push {_format_spec(spec)}: {error}" for spec, error in errors
                )
            )
        return skipped

    def push(
        self, specs: List[spack.spec.Spec]
    ) -> Tuple[List[spack.spec.Spec], List[Tuple[spack.spec.Spec, BaseException]]]:
        raise NotImplementedError

    def tag(self, tag: str, roots: List[spack.spec.Spec]):
        """Make a list of selected specs together available under the given tag"""
        pass


class OCIUploader(Uploader):
    def __init__(
        self,
        mirror: spack.mirrors.mirror.Mirror,
        force: bool,
        update_index: bool,
        base_image: Optional[str],
    ) -> None:
        super().__init__(mirror, force, update_index)
        self.target_image = spack.oci.oci.image_from_mirror(mirror)
        self.base_image = ImageReference.from_string(base_image) if base_image else None

    def push(
        self, specs: List[spack.spec.Spec]
    ) -> Tuple[List[spack.spec.Spec], List[Tuple[spack.spec.Spec, BaseException]]]:
        skipped, base_images, checksums, upload_errors = _oci_push(
            target_image=self.target_image,
            base_image=self.base_image,
            installed_specs_with_deps=specs,
            force=self.force,
            tmpdir=self.tmpdir,
            executor=self.executor,
        )

        self._base_images = base_images
        self._checksums = checksums

        # only update index if any binaries were uploaded
        if self.update_index and len(skipped) + len(upload_errors) < len(specs):
            _oci_update_index(self.target_image, self.tmpdir, self.executor)

        return skipped, upload_errors

    def tag(self, tag: str, roots: List[spack.spec.Spec]):
        tagged_image = self.target_image.with_tag(tag)

        # _push_oci may not populate self._base_images if binaries were already in the registry
        for spec in roots:
            _oci_update_base_images(
                base_image=self.base_image,
                target_image=self.target_image,
                spec=spec,
                base_image_cache=self._base_images,
            )
        _oci_put_manifest(
            self._base_images, self._checksums, tagged_image, self.tmpdir, None, None, *roots
        )


class URLUploader(Uploader):
    def __init__(
        self,
        mirror: spack.mirrors.mirror.Mirror,
        force: bool,
        update_index: bool,
        signing_key: Optional[str],
    ) -> None:
        super().__init__(mirror, force, update_index)
        self.url = mirror.push_url
        self.signing_key = signing_key

    def push(
        self, specs: List[spack.spec.Spec]
    ) -> Tuple[List[spack.spec.Spec], List[Tuple[spack.spec.Spec, BaseException]]]:
        return _url_push(
            specs,
            out_url=self.url,
            force=self.force,
            update_index=self.update_index,
            signing_key=self.signing_key,
            tmpdir=self.tmpdir,
            executor=self.executor,
        )


def make_uploader(
    mirror: spack.mirrors.mirror.Mirror,
    force: bool = False,
    update_index: bool = False,
    signing_key: Optional[str] = None,
    base_image: Optional[str] = None,
) -> Uploader:
    """Builder for the appropriate uploader based on the mirror type"""
    if mirror.push_url.startswith("oci://"):
        return OCIUploader(
            mirror=mirror, force=force, update_index=update_index, base_image=base_image
        )
    else:
        return URLUploader(
            mirror=mirror, force=force, update_index=update_index, signing_key=signing_key
        )


def _format_spec(spec: spack.spec.Spec) -> str:
    return spec.cformat("{name}{@version}{/hash:7}")


class FancyProgress:
    def __init__(self, total: int):
        self.n = 0
        self.total = total
        self.running = False
        self.enable = sys.stdout.isatty()
        self.pretty_spec: str = ""
        self.pre = ""

    def _clear(self):
        if self.enable and self.running:
            sys.stdout.write("\033[F\033[K")

    def _progress(self):
        if self.total > 1:
            digits = len(str(self.total))
            return f"[{self.n:{digits}}/{self.total}] "
        return ""

    def start(self, spec: spack.spec.Spec, running: bool) -> None:
        self.n += 1
        self.running = running
        self.pre = self._progress()
        self.pretty_spec = _format_spec(spec)
        if self.enable and self.running:
            tty.info(f"{self.pre}Pushing {self.pretty_spec}...")

    def ok(self, msg: Optional[str] = None) -> None:
        self._clear()
        msg = msg or f"Pushed {self.pretty_spec}"
        tty.info(f"{self.pre}{msg}")

    def fail(self) -> None:
        self._clear()
        tty.info(f"{self.pre}Failed to push {self.pretty_spec}")


def _url_push(
    specs: List[spack.spec.Spec],
    out_url: str,
    signing_key: Optional[str],
    force: bool,
    update_index: bool,
    tmpdir: str,
    executor: concurrent.futures.Executor,
) -> Tuple[List[spack.spec.Spec], List[Tuple[spack.spec.Spec, BaseException]]]:
    """Pushes to the provided build cache, and returns a list of skipped specs that were already
    present (when force=False), and a list of errors. Does not raise on error."""
    skipped: List[spack.spec.Spec] = []
    errors: List[Tuple[spack.spec.Spec, BaseException]] = []

    exists_futures = [
        executor.submit(
            _exists_in_buildcache, spec, out_url, allow_unsigned=False if signing_key else True
        )
        for spec in specs
    ]

    cache_entries = {
        spec.dag_hash(): exists_future.result()
        for spec, exists_future in zip(specs, exists_futures)
    }

    if not force:
        specs_to_upload = []

        for spec in specs:
            if cache_entries[spec.dag_hash()].exists(
                [BuildcacheComponent.SPEC, BuildcacheComponent.TARBALL]
            ):
                skipped.append(spec)
            else:
                specs_to_upload.append(spec)
    else:
        specs_to_upload = specs

    if not specs_to_upload:
        return skipped, errors

    total = len(specs_to_upload)

    if total != len(specs):
        tty.info(f"{total} specs need to be pushed to {out_url}")

    upload_futures = [
        executor.submit(
            _url_upload_tarball_and_specfile,
            spec,
            tmpdir,
            cache_entries[spec.dag_hash()],
            signing_key,
        )
        for spec in specs_to_upload
    ]

    uploaded_any = False
    fancy_progress = FancyProgress(total)

    for spec, upload_future in zip(specs_to_upload, upload_futures):
        fancy_progress.start(spec, upload_future.running())
        error = upload_future.exception()
        if error is None:
            uploaded_any = True
            fancy_progress.ok()
        else:
            fancy_progress.fail()
            errors.append((spec, error))

    # don't bother pushing keys / index if all failed to upload
    if not uploaded_any:
        return skipped, errors

    # If the layout.json doesn't yet exist on this mirror, push it
    cache_class = get_url_buildcache_class(layout_version=CURRENT_BUILD_CACHE_LAYOUT_VERSION)
    cache_class.maybe_push_layout_json(out_url)

    if signing_key:
        keys_tmpdir = os.path.join(tmpdir, "keys")
        os.mkdir(keys_tmpdir)
        _url_push_keys(out_url, keys=[signing_key], update_index=update_index, tmpdir=keys_tmpdir)

    if update_index:
        index_tmpdir = os.path.join(tmpdir, "index")
        os.mkdir(index_tmpdir)
        _url_generate_package_index(out_url, index_tmpdir)

    return skipped, errors


def _oci_upload_success_msg(spec: spack.spec.Spec, digest: Digest, size: int, elapsed: float):
    elapsed = max(elapsed, 0.001)  # guard against division by zero
    return (
        f"Pushed {_format_spec(spec)}: {digest} ({elapsed:.2f}s, "
        f"{size / elapsed / 1024 / 1024:.2f} MB/s)"
    )


def _oci_get_blob_info(image_ref: ImageReference) -> Optional[spack.oci.oci.Blob]:
    """Get the spack tarball layer digests and size if it exists"""
    try:
        manifest, config = get_manifest_and_config_with_retry(image_ref)

        return spack.oci.oci.Blob(
            compressed_digest=Digest.from_string(manifest["layers"][-1]["digest"]),
            uncompressed_digest=Digest.from_string(config["rootfs"]["diff_ids"][-1]),
            size=manifest["layers"][-1]["size"],
        )
    except Exception:
        return None


def _oci_push_pkg_blob(
    image_ref: ImageReference, spec: spack.spec.Spec, tmpdir: str
) -> Tuple[spack.oci.oci.Blob, float]:
    """Push a package blob to the registry and return the blob info and the time taken"""
    filename = os.path.join(tmpdir, f"{spec.dag_hash()}.tar.gz")

    # Create an oci.image.layer aka tarball of the package
    tar_gz_checksum, tar_checksum = create_tarball(spec, filename)

    blob = spack.oci.oci.Blob(
        Digest.from_sha256(tar_gz_checksum),
        Digest.from_sha256(tar_checksum),
        os.path.getsize(filename),
    )

    # Upload the blob
    start = time.time()
    upload_blob_with_retry(image_ref, file=filename, digest=blob.compressed_digest)
    elapsed = time.time() - start

    # delete the file
    os.unlink(filename)

    return blob, elapsed


def _oci_retrieve_env_dict_from_config(config: dict) -> dict:
    """Retrieve the environment variables from the image config file.
    Sets a default value for PATH if it is not present.

    Args:
        config (dict): The image config file.

    Returns:
        dict: The environment variables.
    """
    env = {"PATH": "/bin:/usr/bin"}

    if "Env" in config.get("config", {}):
        for entry in config["config"]["Env"]:
            key, value = entry.split("=", 1)
            env[key] = value
    return env


def _oci_archspec_to_gooarch(spec: spack.spec.Spec) -> str:
    name = spec.target.family.name
    name_map = {"aarch64": "arm64", "x86_64": "amd64"}
    return name_map.get(name, name)


def _oci_put_manifest(
    base_images: Dict[str, Tuple[dict, dict]],
    checksums: Dict[str, spack.oci.oci.Blob],
    image_ref: ImageReference,
    tmpdir: str,
    extra_config: Optional[dict],
    annotations: Optional[dict],
    *specs: spack.spec.Spec,
):
    architecture = _oci_archspec_to_gooarch(specs[0])

    expected_blobs: List[spack.spec.Spec] = [
        s
        for s in traverse.traverse_nodes(specs, order="topo", deptype=("link", "run"), root=True)
        if not s.external
    ]
    expected_blobs.reverse()

    base_manifest, base_config = base_images[architecture]
    env = _oci_retrieve_env_dict_from_config(base_config)

    # If the base image uses `vnd.docker.distribution.manifest.v2+json`, then we use that too.
    # This is because Singularity / Apptainer is very strict about not mixing them.
    base_manifest_mediaType = base_manifest.get(
        "mediaType", "application/vnd.oci.image.manifest.v1+json"
    )
    use_docker_format = (
        base_manifest_mediaType == "application/vnd.docker.distribution.manifest.v2+json"
    )

    spack.user_environment.environment_modifications_for_specs(*specs).apply_modifications(env)

    # Create an oci.image.config file
    config = copy.deepcopy(base_config)

    # Add the diff ids of the blobs
    for s in expected_blobs:
        # If a layer for a dependency has gone missing (due to removed manifest in the registry, a
        # failed push, or a local forced uninstall), we cannot create a runnable container image.
        checksum = checksums.get(s.dag_hash())
        if checksum:
            config["rootfs"]["diff_ids"].append(str(checksum.uncompressed_digest))

    # Set the environment variables
    config["config"]["Env"] = [f"{k}={v}" for k, v in env.items()]

    if extra_config:
        # From the OCI v1.0 spec:
        # > Any extra fields in the Image JSON struct are considered implementation
        # > specific and MUST be ignored by any implementations which are unable to
        # > interpret them.
        config.update(extra_config)

    config_file = os.path.join(tmpdir, f"{specs[0].dag_hash()}.config.json")

    with open(config_file, "w", encoding="utf-8") as f:
        json.dump(config, f, separators=(",", ":"))

    config_file_checksum = Digest.from_sha256(
        spack.util.crypto.checksum(hashlib.sha256, config_file)
    )

    # Upload the config file
    upload_blob_with_retry(image_ref, file=config_file, digest=config_file_checksum)

    manifest = {
        "mediaType": base_manifest_mediaType,
        "schemaVersion": 2,
        "config": {
            "mediaType": base_manifest["config"]["mediaType"],
            "digest": str(config_file_checksum),
            "size": os.path.getsize(config_file),
        },
        "layers": [
            *(layer for layer in base_manifest["layers"]),
            *(
                {
                    "mediaType": (
                        "application/vnd.docker.image.rootfs.diff.tar.gzip"
                        if use_docker_format
                        else "application/vnd.oci.image.layer.v1.tar+gzip"
                    ),
                    "digest": str(checksums[s.dag_hash()].compressed_digest),
                    "size": checksums[s.dag_hash()].size,
                }
                for s in expected_blobs
                if s.dag_hash() in checksums
            ),
        ],
    }

    if not use_docker_format and annotations:
        manifest["annotations"] = annotations

    # Finally upload the manifest
    upload_manifest_with_retry(image_ref, manifest=manifest)

    # delete the config file
    os.unlink(config_file)


def _oci_update_base_images(
    *,
    base_image: Optional[ImageReference],
    target_image: ImageReference,
    spec: spack.spec.Spec,
    base_image_cache: Dict[str, Tuple[dict, dict]],
):
    """For a given spec and base image, copy the missing layers of the base image with matching
    arch to the registry of the target image. If no base image is specified, create a dummy
    manifest and config file."""
    architecture = _oci_archspec_to_gooarch(spec)
    if architecture in base_image_cache:
        return
    if base_image is None:
        base_image_cache[architecture] = (
            default_manifest(),
            default_config(architecture, "linux"),
        )
    else:
        base_image_cache[architecture] = copy_missing_layers_with_retry(
            base_image, target_image, architecture
        )


def _oci_default_tag(spec: spack.spec.Spec) -> str:
    """Return a valid, default image tag for a spec."""
    return ensure_valid_tag(f"{spec.name}-{spec.version}-{spec.dag_hash()}.spack")


#: Default OCI index tag
default_index_tag = "index.spack"


def tag_is_spec(tag: str) -> bool:
    """Check if a tag is likely a Spec"""
    return tag.endswith(".spack") and tag != default_index_tag


def _oci_push(
    *,
    target_image: ImageReference,
    base_image: Optional[ImageReference],
    installed_specs_with_deps: List[spack.spec.Spec],
    tmpdir: str,
    executor: concurrent.futures.Executor,
    force: bool = False,
) -> Tuple[
    List[spack.spec.Spec],
    Dict[str, Tuple[dict, dict]],
    Dict[str, spack.oci.oci.Blob],
    List[Tuple[spack.spec.Spec, BaseException]],
]:
    # Spec dag hash -> blob
    checksums: Dict[str, spack.oci.oci.Blob] = {}

    # arch -> (manifest, config)
    base_images: Dict[str, Tuple[dict, dict]] = {}

    # Specs not uploaded because they already exist
    skipped: List[spack.spec.Spec] = []

    if not force:
        tty.info("Checking for existing specs in the buildcache")
        blobs_to_upload = []

        tags_to_check = (
            target_image.with_tag(_oci_default_tag(s)) for s in installed_specs_with_deps
        )
        available_blobs = executor.map(_oci_get_blob_info, tags_to_check)

        for spec, maybe_blob in zip(installed_specs_with_deps, available_blobs):
            if maybe_blob is not None:
                checksums[spec.dag_hash()] = maybe_blob
                skipped.append(spec)
            else:
                blobs_to_upload.append(spec)
    else:
        blobs_to_upload = installed_specs_with_deps

    if not blobs_to_upload:
        return skipped, base_images, checksums, []

    if len(blobs_to_upload) != len(installed_specs_with_deps):
        tty.info(
            f"{len(blobs_to_upload)} specs need to be pushed to "
            f"{target_image.domain}/{target_image.name}"
        )

    blob_progress = FancyProgress(len(blobs_to_upload))

    # Upload blobs
    blob_futures = [
        executor.submit(_oci_push_pkg_blob, target_image, spec, tmpdir) for spec in blobs_to_upload
    ]

    manifests_to_upload: List[spack.spec.Spec] = []
    errors: List[Tuple[spack.spec.Spec, BaseException]] = []

    # And update the spec to blob mapping for successful uploads
    for spec, blob_future in zip(blobs_to_upload, blob_futures):
        blob_progress.start(spec, blob_future.running())
        error = blob_future.exception()
        if error is None:
            blob, elapsed = blob_future.result()
            blob_progress.ok(
                _oci_upload_success_msg(spec, blob.compressed_digest, blob.size, elapsed)
            )
            manifests_to_upload.append(spec)
            checksums[spec.dag_hash()] = blob
        else:
            blob_progress.fail()
            errors.append((spec, error))

    # Copy base images if necessary
    for spec in manifests_to_upload:
        _oci_update_base_images(
            base_image=base_image,
            target_image=target_image,
            spec=spec,
            base_image_cache=base_images,
        )

    def extra_config(spec: spack.spec.Spec):
        spec_dict = spec.to_dict(hash=ht.dag_hash)
        spec_dict["buildcache_layout_version"] = CURRENT_BUILD_CACHE_LAYOUT_VERSION
        spec_dict["binary_cache_checksum"] = {
            "hash_algorithm": "sha256",
            "hash": checksums[spec.dag_hash()].compressed_digest.digest,
        }
        spec_dict["archive_size"] = checksums[spec.dag_hash()].size
        spec_dict["archive_timestamp"] = datetime.datetime.now().astimezone().isoformat()
        spec_dict["archive_compression"] = "gzip"
        return spec_dict

    # Upload manifests
    tty.info("Uploading manifests")
    manifest_futures = [
        executor.submit(
            _oci_put_manifest,
            base_images,
            checksums,
            target_image.with_tag(_oci_default_tag(spec)),
            tmpdir,
            extra_config(spec),
            {"org.opencontainers.image.description": spec.format()},
            spec,
        )
        for spec in manifests_to_upload
    ]

    manifest_progress = FancyProgress(len(manifests_to_upload))

    # Print the image names of the top-level specs
    for spec, manifest_future in zip(manifests_to_upload, manifest_futures):
        error = manifest_future.exception()
        manifest_progress.start(spec, manifest_future.running())
        if error is None:
            manifest_progress.ok(
                f"Tagged {_format_spec(spec)} as {target_image.with_tag(_oci_default_tag(spec))}"
            )
        else:
            manifest_progress.fail()
            errors.append((spec, error))

    return skipped, base_images, checksums, errors


def _oci_config_from_tag(image_ref_and_tag: Tuple[ImageReference, str]) -> Optional[dict]:
    image_ref, tag = image_ref_and_tag
    # Don't allow recursion here, since Spack itself always uploads
    # vnd.oci.image.manifest.v1+json, not vnd.oci.image.index.v1+json
    _, config = get_manifest_and_config_with_retry(image_ref.with_tag(tag), tag, recurse=0)

    # Do very basic validation: if "spec" is a key in the config, it
    # must be a Spec object too.
    return config if "spec" in config else None


def _oci_update_index(
    image_ref: ImageReference, tmpdir: str, pool: concurrent.futures.Executor
) -> None:
    tags = list_tags(image_ref)

    # Fetch all image config files in parallel
    spec_dicts = pool.map(
        _oci_config_from_tag, ((image_ref, tag) for tag in tags if tag_is_spec(tag))
    )

    # Populate the database
    db_root_dir = os.path.join(tmpdir, "db_root")
    db = BuildCacheDatabase(db_root_dir)

    for spec_dict in spec_dicts:
        spec = spack.spec.Spec.from_dict(spec_dict)
        db.add(spec)
        db.mark(spec, "in_buildcache", True)

    # Create the index.json file
    index_json_path = os.path.join(tmpdir, spack_db.INDEX_JSON_FILE)
    with open(index_json_path, "w", encoding="utf-8") as f:
        db._write_to_file(f)

    # Create an empty config.json file
    empty_config_json_path = os.path.join(tmpdir, "config.json")
    with open(empty_config_json_path, "wb") as f:
        f.write(b"{}")

    # Upload the index.json file
    index_shasum = Digest.from_sha256(spack.util.crypto.checksum(hashlib.sha256, index_json_path))
    upload_blob_with_retry(image_ref, file=index_json_path, digest=index_shasum)

    # Upload the config.json file
    empty_config_digest = Digest.from_sha256(
        spack.util.crypto.checksum(hashlib.sha256, empty_config_json_path)
    )
    upload_blob_with_retry(image_ref, file=empty_config_json_path, digest=empty_config_digest)

    # Push a manifest file that references the index.json file as a layer
    # Notice that we push this as if it is an image, which it of course is not.
    # When the ORAS spec becomes official, we can use that instead of a fake image.
    # For now we just use the OCI image spec, so that we don't run into issues with
    # automatic garbage collection of blobs that are not referenced by any image manifest.
    oci_manifest = {
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "schemaVersion": 2,
        # Config is just an empty {} file for now, and irrelevant
        "config": {
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "digest": str(empty_config_digest),
            "size": os.path.getsize(empty_config_json_path),
        },
        # The buildcache index is the only layer, and is not a tarball, we lie here.
        "layers": [
            {
                "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                "digest": str(index_shasum),
                "size": os.path.getsize(index_json_path),
            }
        ],
    }

    upload_manifest_with_retry(image_ref.with_tag(default_index_tag), oci_manifest)


def try_fetch(url_to_fetch):
    """Utility function to try and fetch a file from a url, stage it
    locally, and return the path to the staged file.

    Args:
        url_to_fetch (str): Url pointing to remote resource to fetch

    Returns:
        Path to locally staged resource or ``None`` if it could not be fetched.
    """
    stage = Stage(url_to_fetch, keep=True)
    stage.create()

    try:
        stage.fetch()
    except spack.error.FetchError:
        stage.destroy()
        return None

    return stage


def download_tarball(
    spec: spack.spec.Spec, unsigned: Optional[bool] = False, mirrors_for_spec=None
) -> Optional[spack.stage.Stage]:
    """Download binary tarball for given package

    Args:
        spec: a concrete spec
        unsigned: if ``True`` or ``False`` override the mirror signature verification defaults
        mirrors_for_spec (list): Optional list of concrete specs and mirrors
            obtained by calling binary_distribution.get_mirrors_for_spec().
            These will be checked in order first before looking in other
            configured mirrors.

    Returns:
        ``None`` if the tarball could not be downloaded, the signature verified
        (if required), and its checksum validated. Otherwise, return the stage
        containing the downloaded tarball.
    """
    configured_mirrors: Iterable[spack.mirrors.mirror.Mirror] = (
        spack.mirrors.mirror.MirrorCollection(binary=True).values()
    )
    if not configured_mirrors:
        tty.die("Please add a spack mirror to allow download of pre-compiled packages.")

    # Note on try_first and try_next:
    # mirrors_for_spec mostly likely came from spack caching remote
    # mirror indices locally and adding their specs to a local data
    # structure supporting quick lookup of concrete specs.  Those
    # mirrors are likely a subset of all configured mirrors, and
    # we'll probably find what we need in one of them.  But we'll
    # look in all configured mirrors if needed, as maybe the spec
    # we need was in an un-indexed mirror.  No need to check any
    # mirror for the spec twice though.
    try_first = [i.url_and_version for i in mirrors_for_spec] if mirrors_for_spec else []

    try_next = []
    for try_layout in SUPPORTED_LAYOUT_VERSIONS:
        try_next.extend([MirrorURLAndVersion(i.fetch_url, try_layout) for i in configured_mirrors])
    urls_and_versions = try_first + [uv for uv in try_next if uv not in try_first]

    # TODO: turn `mirrors_for_spec` into a list of Mirror instances, instead of doing that here.
    def fetch_url_to_mirror(url_and_version):
        url = url_and_version.url
        layout_version = url_and_version.version
        for mirror in configured_mirrors:
            if mirror.fetch_url == url:
                return mirror, layout_version
        return spack.mirrors.mirror.Mirror(url), layout_version

    mirrors = [fetch_url_to_mirror(url_and_version) for url_and_version in urls_and_versions]

    for mirror, layout_version in mirrors:
        # Override mirror's default if
        currently_unsigned = unsigned if unsigned is not None else not mirror.signed

        # If it's an OCI index, do things differently, since we cannot compose URLs.
        fetch_url = mirror.fetch_url

        # TODO: refactor this to some "nice" place.
        if fetch_url.startswith("oci://"):
            ref = spack.oci.image.ImageReference.from_string(fetch_url[len("oci://") :]).with_tag(
                _oci_default_tag(spec)
            )

            # Fetch the manifest
            try:
                response = spack.oci.opener.urlopen(
                    urllib.request.Request(
                        url=ref.manifest_url(),
                        headers={"Accept": ", ".join(spack.oci.oci.manifest_content_type)},
                    )
                )
            except Exception:
                continue

            # Download the config = spec.json and the relevant tarball
            try:
                manifest = json.load(response)
                spec_digest = spack.oci.image.Digest.from_string(manifest["config"]["digest"])
                tarball_digest = spack.oci.image.Digest.from_string(
                    manifest["layers"][-1]["digest"]
                )
            except Exception:
                continue

            with spack.oci.oci.make_stage(
                ref.blob_url(spec_digest), spec_digest, keep=True
            ) as local_specfile_stage:
                try:
                    local_specfile_stage.fetch()
                    local_specfile_stage.check()
                    try:
                        get_valid_spec_file(
                            local_specfile_stage.save_filename, CURRENT_BUILD_CACHE_LAYOUT_VERSION
                        )
                    except InvalidMetadataFile as e:
                        tty.warn(
                            f"Ignoring binary package for {spec.name}/{spec.dag_hash()[:7]} "
                            f"from {fetch_url} due to invalid metadata file: {e}"
                        )
                        local_specfile_stage.destroy()
                        continue
                except Exception:
                    continue
                local_specfile_stage.cache_local()

            local_specfile_stage.destroy()

            with spack.oci.oci.make_stage(
                ref.blob_url(tarball_digest), tarball_digest, keep=True
            ) as tarball_stage:
                try:
                    tarball_stage.fetch()
                    tarball_stage.check()
                except Exception:
                    continue
                tarball_stage.cache_local()

            return tarball_stage
        else:
            cache_type = get_url_buildcache_class(layout_version=layout_version)
            cache_entry = cache_type(fetch_url, spec, allow_unsigned=currently_unsigned)

            try:
                cache_entry.fetch_archive()
            except Exception as e:
                tty.debug(
                    f"Encountered error attempting to fetch archive for "
                    f"{spec.name}/{spec.dag_hash()[:7]} from {fetch_url} "
                    f"(v{layout_version}) due to {e}"
                )
                cache_entry.destroy()
                continue

            if layout_version == 2:
                warn_v2_layout(fetch_url, "Installing a spec")

            return cache_entry.get_archive_stage()

    # Falling through the nested loops meeans we exhaustively searched
    # for all known kinds of spec files on all mirrors and did not find
    # an acceptable one for which we could download a tarball and (if
    # needed) verify a signature. So at this point, we will proceed to
    # install from source.
    return None


def dedupe_hardlinks_if_necessary(root, buildinfo):
    """Updates a buildinfo dict for old archives that did not dedupe hardlinks. De-duping hardlinks
    is necessary when relocating files in parallel and in-place. This means we must preserve inodes
    when relocating."""

    # New archives don't need this.
    if buildinfo.get("hardlinks_deduped", False):
        return

    # Clearly we can assume that an inode is either in the
    # textfile or binary group, but let's just stick to
    # a single set of visited nodes.
    visited = set()

    # Note: we do *not* dedupe hardlinked symlinks, since
    # it seems difficult or even impossible to relink
    # symlinks while preserving inode.
    for key in ("relocate_textfiles", "relocate_binaries"):
        if key not in buildinfo:
            continue
        new_list = []
        for rel_path in buildinfo[key]:
            stat_result = os.lstat(os.path.join(root, rel_path))
            identifier = (stat_result.st_dev, stat_result.st_ino)
            if stat_result.st_nlink > 1:
                if identifier in visited:
                    continue
                visited.add(identifier)
            new_list.append(rel_path)
        buildinfo[key] = new_list


def relocate_package(spec: spack.spec.Spec) -> None:
    """Relocate binaries and text files in the given spec prefix, based on its buildinfo file."""
    spec_prefix = str(spec.prefix)
    buildinfo = read_buildinfo_file(spec_prefix)
    old_layout_root = str(buildinfo["buildpath"])

    # Warn about old style tarballs created with the --rel flag (removed in Spack v0.20)
    if buildinfo.get("relative_rpaths", False):
        tty.warn(
            f"Tarball for {spec} uses relative rpaths, which can cause library loading issues."
        )

    # In Spack 0.19 and older prefix_to_hash was the default and externals were not dropped, so
    # prefixes were not unique.
    if "hash_to_prefix" in buildinfo:
        hash_to_old_prefix = buildinfo["hash_to_prefix"]
    elif "prefix_to_hash" in buildinfo:
        hash_to_old_prefix = {v: k for (k, v) in buildinfo["prefix_to_hash"].items()}
    else:
        raise NewLayoutException(
            "Package tarball was created from an install prefix with a different directory layout "
            "and an older buildcache create implementation. It cannot be relocated."
        )

    prefix_to_prefix: Dict[str, str] = {}

    if "sbang_install_path" in buildinfo:
        old_sbang_install_path = str(buildinfo["sbang_install_path"])
        prefix_to_prefix[old_sbang_install_path] = spack.hooks.sbang.sbang_install_path()

    # First match specific prefix paths. Possibly the *local* install prefix of some dependency is
    # in an upstream, so we cannot assume the original spack store root can be mapped uniformly to
    # the new spack store root.

    # If the spec is spliced, we need to handle the simultaneous mapping from the old install_tree
    # to the new install_tree and from the build_spec to the spliced spec. Because foo.build_spec
    # is foo for any non-spliced spec, we can simplify by checking for spliced-in nodes by checking
    # for nodes not in the build_spec without any explicit check for whether the spec is spliced.
    # An analog in this algorithm is any spec that shares a name or provides the same virtuals in
    # the context of the relevant root spec. This ensures that the analog for a spec s is the spec
    # that s replaced when we spliced.
    relocation_specs = specs_to_relocate(spec)
    build_spec_ids = set(id(s) for s in spec.build_spec.traverse(deptype=dt.ALL & ~dt.BUILD))
    for s in relocation_specs:
        analog = s
        if id(s) not in build_spec_ids:
            analogs = [
                d
                for d in spec.build_spec.traverse(deptype=dt.ALL & ~dt.BUILD)
                if s._splice_match(d, self_root=spec, other_root=spec.build_spec)
            ]
            if analogs:
                # Prefer same-name analogs and prefer higher versions
                # This matches the preferences in spack.spec.Spec.splice, so we
                # will find same node
                analog = max(analogs, key=lambda a: (a.name == s.name, a.version))

        lookup_dag_hash = analog.dag_hash()
        if lookup_dag_hash in hash_to_old_prefix:
            old_dep_prefix = hash_to_old_prefix[lookup_dag_hash]
            prefix_to_prefix[old_dep_prefix] = str(s.prefix)

    # Only then add the generic fallback of install prefix -> install prefix.
    prefix_to_prefix[old_layout_root] = str(spack.store.STORE.layout.root)

    # Delete identity mappings from prefix_to_prefix
    prefix_to_prefix = {k: v for k, v in prefix_to_prefix.items() if k != v}

    # If there's nothing to relocate, we're done.
    if not prefix_to_prefix:
        return

    for old, new in prefix_to_prefix.items():
        tty.debug(f"Relocating: {old} => {new}.")

    # Old archives may have hardlinks repeated.
    dedupe_hardlinks_if_necessary(spec_prefix, buildinfo)

    # Text files containing the prefix text
    textfiles = [os.path.join(spec_prefix, f) for f in buildinfo["relocate_textfiles"]]
    binaries = [os.path.join(spec_prefix, f) for f in buildinfo.get("relocate_binaries")]
    links = [os.path.join(spec_prefix, f) for f in buildinfo.get("relocate_links", [])]

    platform = spack.platforms.by_name(spec.platform)
    if "macho" in platform.binary_formats:
        relocate.relocate_macho_binaries(binaries, prefix_to_prefix)
    elif "elf" in platform.binary_formats:
        relocate.relocate_elf_binaries(binaries, prefix_to_prefix)

    relocate.relocate_links(links, prefix_to_prefix)
    relocate.relocate_text(textfiles, prefix_to_prefix)
    changed_files = relocate.relocate_text_bin(binaries, prefix_to_prefix)

    # Add ad-hoc signatures to patched macho files when on macOS.
    if "macho" in platform.binary_formats and sys.platform == "darwin":
        codesign = which("codesign")
        if not codesign:
            return
        for binary in changed_files:
            # preserve the original inode by running codesign on a copy
            with fsys.edit_in_place_through_temporary_file(binary) as tmp_binary:
                codesign("-fs-", tmp_binary)

    install_manifest = os.path.join(
        spec.prefix,
        spack.store.STORE.layout.metadata_dir,
        spack.store.STORE.layout.manifest_file_name,
    )
    if not os.path.exists(install_manifest):
        spec_id = spec.format("{name}/{hash:7}")
        tty.warn("No manifest file in tarball for spec %s" % spec_id)

    # overwrite old metadata with new
    if spec.spliced:
        # rewrite spec on disk
        spack.store.STORE.layout.write_spec(spec, spack.store.STORE.layout.spec_file_path(spec))

        # de-cache the install manifest
        with contextlib.suppress(FileNotFoundError):
            os.unlink(install_manifest)


def _tar_strip_component(tar: tarfile.TarFile, prefix: str):
    """Yield all members of tarfile that start with given prefix, and strip that prefix (including
    symlinks)"""
    # Including trailing /, otherwise we end up with absolute paths.
    regex = re.compile(re.escape(prefix) + "/*")

    # Only yield members in the package prefix.
    # Note: when a tarfile is created, relative in-prefix symlinks are
    # expanded to matching member names of tarfile entries. So, we have
    # to ensure that those are updated too.
    # Absolute symlinks are copied verbatim -- relocation should take care of
    # them.
    for m in tar.getmembers():
        result = regex.match(m.name)
        if not result:
            continue
        m.name = m.name[result.end() :]
        if m.linkname:
            result = regex.match(m.linkname)
            if result:
                m.linkname = m.linkname[result.end() :]
        yield m


def extract_buildcache_tarball(tarfile_path: str, destination: str) -> None:
    with closing(tarfile.open(tarfile_path, "r")) as tar:
        # Remove common prefix from tarball entries and directly extract them to the install dir.
        tar.extractall(
            path=destination, members=_tar_strip_component(tar, prefix=_ensure_common_prefix(tar))
        )


def extract_tarball(spec, tarball_stage: spack.stage.Stage, force=False, timer=timer.NULL_TIMER):
    """
    extract binary tarball for given package into install area
    """
    timer.start("extract")

    if os.path.exists(spec.prefix):
        if force:
            shutil.rmtree(spec.prefix)
        else:
            raise NoOverwriteException(str(spec.prefix))

    # Create the install prefix
    fsys.mkdirp(
        spec.prefix,
        mode=get_package_dir_permissions(spec),
        group=get_package_group(spec),
        default_perms="parents",
    )

    tarfile_path = tarball_stage.save_filename

    try:
        extract_buildcache_tarball(tarfile_path, destination=spec.prefix)
    except Exception:
        shutil.rmtree(spec.prefix, ignore_errors=True)
        tarball_stage.destroy()
        raise

    timer.stop("extract")
    timer.start("relocate")

    try:
        relocate_package(spec)
    except Exception as e:
        shutil.rmtree(spec.prefix, ignore_errors=True)
        raise e
    finally:
        tarball_stage.destroy()

    timer.stop("relocate")


def _ensure_common_prefix(tar: tarfile.TarFile) -> str:
    # Find the lowest `binary_distribution` file (hard-coded forward slash is on purpose).
    binary_distribution = min(
        (
            e.name
            for e in tar.getmembers()
            if e.isfile() and e.name.endswith(".spack/binary_distribution")
        ),
        key=len,
        default=None,
    )

    if binary_distribution is None:
        raise ValueError("Tarball is not a Spack package, missing binary_distribution file")

    pkg_path = pathlib.PurePosixPath(binary_distribution).parent.parent

    # Even the most ancient Spack version has required to list the dir of the package itself, so
    # guard against broken tarballs where `path.parent.parent` is empty.
    if pkg_path == pathlib.PurePosixPath():
        raise ValueError("Invalid tarball, missing package prefix dir")

    pkg_prefix = str(pkg_path)

    # Ensure all tar entries are in the pkg_prefix dir, and if they're not, they should be parent
    # dirs of it.
    has_prefix = False
    for member in tar.getmembers():
        stripped = member.name.rstrip("/")
        if not (
            stripped.startswith(pkg_prefix) or member.isdir() and pkg_prefix.startswith(stripped)
        ):
            raise ValueError(f"Tarball contains file {stripped} outside of prefix {pkg_prefix}")
        if member.isdir() and stripped == pkg_prefix:
            has_prefix = True

    # This is technically not required, but let's be defensive about the existence of the package
    # prefix dir.
    if not has_prefix:
        raise ValueError(f"Tarball does not contain a common prefix {pkg_prefix}")

    return pkg_prefix


def install_root_node(
    spec: spack.spec.Spec,
    unsigned=False,
    force: bool = False,
    sha256: Optional[str] = None,
    allow_missing: bool = False,
) -> None:
    """Install the root node of a concrete spec from a buildcache.

    Checking the sha256 sum of a node before installation is usually needed only
    for software installed during Spack's bootstrapping (since we might not have
    a proper signature verification mechanism available).

    Args:
        spec: spec to be installed (note that only the root node will be installed)
        unsigned: if True allows installing unsigned binaries
        force: force installation if the spec is already present in the local store
        sha256: optional sha256 of the binary package, to be checked before installation
        allow_missing: when true, allows installing a node with missing dependencies
    """
    # Early termination
    if spec.external or not spec.concrete:
        warnings.warn("Skipping external or abstract spec {0}".format(spec.format()))
        return
    elif spec.installed and not force:
        warnings.warn("Package for spec {0} already installed.".format(spec.format()))
        return

    tarball_stage = download_tarball(spec.build_spec, unsigned)
    if not tarball_stage:
        msg = 'download of binary cache file for spec "{0}" failed'
        raise RuntimeError(msg.format(spec.build_spec.format()))

    # don't print long padded paths while extracting/relocating binaries
    with spack.util.path.filter_padding():
        tty.msg('Installing "{0}" from a buildcache'.format(spec.format()))
        extract_tarball(spec, tarball_stage, force)
        spec.package.windows_establish_runtime_linkage()
        spack.hooks.post_install(spec, False)
        spack.store.STORE.db.add(spec, allow_missing=allow_missing)


def install_single_spec(spec, unsigned=False, force=False):
    """Install a single concrete spec from a buildcache.

    Args:
        spec (spack.spec.Spec): spec to be installed
        unsigned (bool): if True allows installing unsigned binaries
        force (bool): force installation if the spec is already present in the
            local store
    """
    for node in spec.traverse(root=True, order="post", deptype=("link", "run")):
        install_root_node(node, unsigned=unsigned, force=force)


def try_direct_fetch(spec, mirrors=None):
    """
    Try to find the spec directly on the configured mirrors
    """
    found_specs: List[MirrorForSpec] = []
    binary_mirrors = spack.mirrors.mirror.MirrorCollection(mirrors=mirrors, binary=True).values()

    for layout_version in SUPPORTED_LAYOUT_VERSIONS:
        for mirror in binary_mirrors:
            # layout_version could eventually come from the mirror config
            cache_class = get_url_buildcache_class(layout_version=layout_version)
            cache_entry = cache_class(mirror.fetch_url, spec)

            try:
                spec_dict = cache_entry.fetch_metadata()
            except BuildcacheEntryError:
                continue
            finally:
                cache_entry.destroy()

            # All specs in build caches are concrete (as they are built) so we need
            # to mark this spec concrete on read-in.
            fetched_spec = spack.spec.Spec.from_dict(spec_dict)
            fetched_spec._mark_concrete()

            found_specs.append(
                MirrorForSpec(MirrorURLAndVersion(mirror.fetch_url, layout_version), fetched_spec)
            )

    return found_specs


def get_mirrors_for_spec(spec=None, mirrors_to_check=None, index_only=False):
    """
    Check if concrete spec exists on mirrors and return a list
    indicating the mirrors on which it can be found

    Args:
        spec (spack.spec.Spec): The spec to look for in binary mirrors
        mirrors_to_check (dict): Optionally override the configured mirrors
            with the mirrors in this dictionary.
        index_only (bool): When ``index_only`` is set to ``True``, only the local
            cache is checked, no requests are made.

    Return:
        A list of objects, each containing a ``mirror_url`` and ``spec`` key
            indicating all mirrors where the spec can be found.
    """
    if spec is None:
        return []

    if not spack.mirrors.mirror.MirrorCollection(mirrors=mirrors_to_check, binary=True):
        tty.debug("No Spack mirrors are currently configured")
        return {}

    results = BINARY_INDEX.find_built_spec(spec, mirrors_to_check=mirrors_to_check)

    # The index may be out-of-date. If we aren't only considering indices, try
    # to fetch directly since we know where the file should be.
    if not results and not index_only:
        results = try_direct_fetch(spec, mirrors=mirrors_to_check)
        # We found a spec by the direct fetch approach, we might as well
        # add it to our mapping.
        if results:
            BINARY_INDEX.update_spec(spec, results)

    return results


def update_cache_and_get_specs():
    """
    Get all concrete specs for build caches available on configured mirrors.
    Initialization of internal cache data structures is done as lazily as
    possible, so this method will also attempt to initialize and update the
    local index cache (essentially a no-op if it has been done already and
    nothing has changed on the configured mirrors.)

    Throws:
        FetchCacheError
    """
    BINARY_INDEX.update()
    return BINARY_INDEX.get_all_built_specs()


def clear_spec_cache():
    BINARY_INDEX.clear()


def get_keys(
    install: bool = False,
    trust: bool = False,
    force: bool = False,
    mirrors: Optional[Dict[Any, spack.mirrors.mirror.Mirror]] = None,
):
    """Get pgp public keys available on mirror with suffix .pub"""
    mirror_collection = mirrors or spack.mirrors.mirror.MirrorCollection(binary=True)

    if not mirror_collection:
        tty.die("Please add a spack mirror to allow " + "download of build caches.")

    for mirror in mirror_collection.values():
        for layout_version in SUPPORTED_LAYOUT_VERSIONS:
            fetch_url = mirror.fetch_url
            # TODO: oci:// does not support signing.
            if fetch_url.startswith("oci://"):
                continue

            if layout_version == 2:
                _get_keys_v2(fetch_url, install, trust, force)
            else:
                _get_keys(fetch_url, layout_version, install, trust, force)


def _get_keys(
    mirror_url: str,
    layout_version: int = CURRENT_BUILD_CACHE_LAYOUT_VERSION,
    install: bool = False,
    trust: bool = False,
    force: bool = False,
) -> None:
    cache_class = get_url_buildcache_class(layout_version=layout_version)

    tty.debug("Finding public keys in {0}".format(url_util.format(mirror_url)))

    keys_prefix = url_util.join(
        mirror_url, *cache_class.get_relative_path_components(BuildcacheComponent.KEY)
    )
    key_index_manifest_url = url_util.join(keys_prefix, "keys.manifest.json")
    index_entry = cache_class(mirror_url, allow_unsigned=True)

    try:
        index_manifest = index_entry.read_manifest(manifest_url=key_index_manifest_url)
        index_blob_path = index_entry.fetch_blob(index_manifest.data[0])
    except BuildcacheEntryError as e:
        tty.debug(f"Failed to fetch key index due to: {e}")
        index_entry.destroy()
        return

    with open(index_blob_path, encoding="utf-8") as fd:
        json_index = json.load(fd)
    index_entry.destroy()

    for fingerprint, _ in json_index["keys"].items():
        key_manifest_url = url_util.join(keys_prefix, f"{fingerprint}.key.manifest.json")
        key_entry = cache_class(mirror_url, allow_unsigned=True)
        try:
            key_manifest = key_entry.read_manifest(manifest_url=key_manifest_url)
            key_blob_path = key_entry.fetch_blob(key_manifest.data[0])
        except BuildcacheEntryError as e:
            tty.debug(f"Failed to fetch key {fingerprint} due to: {e}")
            key_entry.destroy()
            continue

        tty.debug("Found key {0}".format(fingerprint))
        if install:
            if trust:
                spack.util.gpg.trust(key_blob_path)
                tty.debug(f"Added {fingerprint} to trusted keys.")
            else:
                tty.debug(
                    "Will not add this key to trusted keys."
                    "Use -t to install all downloaded keys"
                )

        key_entry.destroy()


def _get_keys_v2(mirror_url, install=False, trust=False, force=False):
    cache_class = get_url_buildcache_class(layout_version=2)

    keys_url = url_util.join(
        mirror_url, *cache_class.get_relative_path_components(BuildcacheComponent.KEY)
    )
    keys_index = url_util.join(keys_url, "index.json")

    tty.debug("Finding public keys in {0}".format(url_util.format(mirror_url)))

    try:
        _, _, json_file = web_util.read_from_url(keys_index)
        json_index = sjson.load(json_file)
    except (web_util.SpackWebError, OSError, ValueError) as url_err:
        # TODO: avoid repeated request
        if web_util.url_exists(keys_index):
            tty.error(
                f"Unable to find public keys in {url_util.format(mirror_url)},"
                f" caught exception attempting to read from {url_util.format(keys_index)}."
            )
            tty.error(url_err)
        return

    for fingerprint, key_attributes in json_index["keys"].items():
        link = os.path.join(keys_url, fingerprint + ".pub")

        with Stage(link, name="build_cache", keep=True) as stage:
            if os.path.exists(stage.save_filename) and force:
                os.remove(stage.save_filename)
            if not os.path.exists(stage.save_filename):
                try:
                    stage.fetch()
                except spack.error.FetchError:
                    continue

        tty.debug("Found key {0}".format(fingerprint))
        if install:
            if trust:
                spack.util.gpg.trust(stage.save_filename)
                tty.debug("Added this key to trusted keys.")
            else:
                tty.debug(
                    "Will not add this key to trusted keys."
                    "Use -t to install all downloaded keys"
                )


def _url_push_keys(
    *mirrors: Union[spack.mirrors.mirror.Mirror, str],
    keys: List[str],
    tmpdir: str,
    update_index: bool = False,
):
    """Upload pgp public keys to the given mirrors"""
    keys = spack.util.gpg.public_keys(*(keys or ()))
    files = [os.path.join(tmpdir, f"{key}.pub") for key in keys]

    for key, file in zip(keys, files):
        spack.util.gpg.export_keys(file, [key])

    cache_class = get_url_buildcache_class()

    for mirror in mirrors:
        push_url = mirror if isinstance(mirror, str) else mirror.push_url

        tty.debug(f"Pushing public keys to {url_util.format(push_url)}")
        pushed_a_key = False

        for key, file in zip(keys, files):
            cache_class.push_local_file_as_blob(
                local_file_path=file,
                mirror_url=push_url,
                manifest_name=f"{key}.key",
                component_type=BuildcacheComponent.KEY,
                compression="none",
            )
            pushed_a_key = True

        if update_index:
            generate_key_index(push_url, tmpdir=tmpdir)

        if pushed_a_key or update_index:
            cache_class.maybe_push_layout_json(push_url)


def needs_rebuild(spec, mirror_url):
    if not spec.concrete:
        raise ValueError("spec must be concrete to check against mirror")

    pkg_name = spec.name
    pkg_version = spec.version
    pkg_hash = spec.dag_hash()

    tty.debug("Checking {0}-{1}, dag_hash = {2}".format(pkg_name, pkg_version, pkg_hash))
    tty.debug(spec.tree())

    # Try to retrieve the specfile directly, based on the known
    # format of the name, in order to determine if the package
    # needs to be rebuilt.
    cache_class = get_url_buildcache_class(layout_version=CURRENT_BUILD_CACHE_LAYOUT_VERSION)
    cache_entry = cache_class(mirror_url, spec, allow_unsigned=True)
    exists = cache_entry.exists([BuildcacheComponent.SPEC, BuildcacheComponent.TARBALL])
    return not exists


def check_specs_against_mirrors(mirrors, specs, output_file=None):
    """Check all the given specs against buildcaches on the given mirrors and
    determine if any of the specs need to be rebuilt.  Specs need to be rebuilt
    when their hash doesn't exist in the mirror.

    Arguments:
        mirrors (dict): Mirrors to check against
        specs (typing.Iterable): Specs to check against mirrors
        output_file (str): Path to output file to be written.  If provided,
            mirrors with missing or out-of-date specs will be formatted as a
            JSON object and written to this file.

    Returns: 1 if any spec was out-of-date on any mirror, 0 otherwise.

    """
    rebuilds = {}
    for mirror in spack.mirrors.mirror.MirrorCollection(mirrors, binary=True).values():
        tty.debug("Checking for built specs at {0}".format(mirror.fetch_url))

        rebuild_list = []

        for spec in specs:
            if needs_rebuild(spec, mirror.fetch_url):
                rebuild_list.append({"short_spec": spec.short_spec, "hash": spec.dag_hash()})

        if rebuild_list:
            rebuilds[mirror.fetch_url] = {
                "mirrorName": mirror.name,
                "mirrorUrl": mirror.fetch_url,
                "rebuildSpecs": rebuild_list,
            }

    if output_file:
        with open(output_file, "w", encoding="utf-8") as outf:
            outf.write(json.dumps(rebuilds))

    return 1 if rebuilds else 0


def download_single_spec(
    concrete_spec,
    destination,
    mirror_url=None,
    layout_version: int = CURRENT_BUILD_CACHE_LAYOUT_VERSION,
):
    """Download the buildcache files for a single concrete spec.

    Args:
        concrete_spec: concrete spec to be downloaded
        destination (str): path where to put the downloaded buildcache
        mirror_url (str): url of the mirror from which to download
    """
    if not mirror_url and not spack.mirrors.mirror.MirrorCollection(binary=True):
        tty.die(
            "Please provide or add a spack mirror to allow " + "download of buildcache entries."
        )

    urls = (
        [mirror_url]
        if mirror_url
        else [
            mirror.fetch_url
            for mirror in spack.mirrors.mirror.MirrorCollection(binary=True).values()
        ]
    )

    mkdirp(destination)

    for url in urls:
        cache_class = get_url_buildcache_class(layout_version=layout_version)
        cache_entry = cache_class(url, concrete_spec, allow_unsigned=True)

        try:
            cache_entry.fetch_metadata()
            cache_entry.fetch_archive()
        except BuildcacheEntryError as e:
            tty.warn(f"Error downloading {concrete_spec.name}/{concrete_spec.dag_hash()[:7]}: {e}")
            cache_entry.destroy()
            continue

        shutil.move(cache_entry.get_local_spec_path(), destination)
        shutil.move(cache_entry.get_local_archive_path(), destination)
        return True

    return False


class BinaryCacheQuery:
    """Callable object to query if a spec is in a binary cache"""

    def __init__(self, all_architectures):
        """
        Args:
            all_architectures (bool): if True consider all the spec for querying,
                otherwise restrict to the current default architecture
        """
        self.all_architectures = all_architectures

        specs = update_cache_and_get_specs()

        if not self.all_architectures:
            arch = spack.spec.Spec.default_arch()
            specs = [s for s in specs if s.satisfies(arch)]

        self.possible_specs = specs

    def __call__(self, spec: spack.spec.Spec, **kwargs):
        """
        Args:
            spec: The spec being searched for
        """
        return [s for s in self.possible_specs if s.satisfies(spec)]


class FetchIndexError(Exception):
    def __str__(self):
        if len(self.args) == 1:
            return str(self.args[0])
        else:
            return "{}, due to: {}".format(self.args[0], self.args[1])


class BuildcacheIndexError(spack.error.SpackError):
    """Raised when a buildcache cannot be read for any reason"""


FetchIndexResult = collections.namedtuple("FetchIndexResult", "etag hash data fresh")


class IndexFetcher:
    def conditional_fetch(self) -> FetchIndexResult:
        raise NotImplementedError(f"{self.__class__.__name__} is abstract")

    def get_index_manifest(self, manifest_response) -> BlobRecord:
        """Read the response of the manifest request and return a BlobRecord"""
        cache_class = get_url_buildcache_class(CURRENT_BUILD_CACHE_LAYOUT_VERSION)
        try:
            result = codecs.getreader("utf-8")(manifest_response).read()
        except (ValueError, OSError) as e:
            raise FetchIndexError(f"Remote index {manifest_response.url} is invalid", e) from e

        manifest = BuildcacheManifest.from_dict(
            # Currently we do not sign buildcache index, but we could
            cache_class.verify_and_extract_manifest(result, verify=False)
        )
        blob_record = manifest.get_blob_records(
            cache_class.component_to_media_type(BuildcacheComponent.INDEX)
        )[0]
        return blob_record

    def fetch_index_blob(
        self, cache_entry: URLBuildcacheEntry, blob_record: BlobRecord
    ) -> Tuple[str, str]:
        """Fetch the index blob indicated by the BlobRecord, and return the
        (checksum, contents) of the blob"""
        try:
            staged_blob_path = cache_entry.fetch_blob(blob_record)
        except BuildcacheEntryError as e:
            cache_entry.destroy()
            raise FetchIndexError(
                f"Could not fetch index blob from {cache_entry.mirror_url}"
            ) from e

        with open(staged_blob_path, encoding="utf-8") as fd:
            blob_result = fd.read()

        computed_hash = compute_hash(blob_result)

        if computed_hash != blob_record.checksum:
            cache_entry.destroy()
            raise FetchIndexError(f"Remote index at {cache_entry.mirror_url} is invalid")

        return (computed_hash, blob_result)


class DefaultIndexFetcherV2(IndexFetcher):
    """Fetcher for index.json, using separate index.json.hash as cache invalidation strategy"""

    def __init__(self, url, local_hash, urlopen=web_util.urlopen):
        self.url = url
        self.local_hash = local_hash
        self.urlopen = urlopen
        self.headers = {"User-Agent": web_util.SPACK_USER_AGENT}

    def get_remote_hash(self):
        # Failure to fetch index.json.hash is not fatal
        url_index_hash = url_util.join(self.url, "build_cache", "index.json.hash")
        try:
            response = self.urlopen(urllib.request.Request(url_index_hash, headers=self.headers))
            remote_hash = response.read(64)
        except OSError:
            return None

        # Validate the hash
        if not re.match(rb"[a-f\d]{64}$", remote_hash):
            return None
        return remote_hash.decode("utf-8")

    def conditional_fetch(self) -> FetchIndexResult:
        # Do an intermediate fetch for the hash
        # and a conditional fetch for the contents

        # Early exit if our cache is up to date.
        if self.local_hash and self.local_hash == self.get_remote_hash():
            return FetchIndexResult(etag=None, hash=None, data=None, fresh=True)

        # Otherwise, download index.json
        url_index = url_util.join(self.url, "build_cache", spack_db.INDEX_JSON_FILE)

        try:
            response = self.urlopen(urllib.request.Request(url_index, headers=self.headers))
        except OSError as e:
            raise FetchIndexError(f"Could not fetch index from {url_index}", e) from e

        try:
            result = codecs.getreader("utf-8")(response).read()
        except (ValueError, OSError) as e:
            raise FetchIndexError(f"Remote index {url_index} is invalid") from e

        computed_hash = compute_hash(result)

        # We don't handle computed_hash != remote_hash here, which can happen
        # when remote index.json and index.json.hash are out of sync, or if
        # the hash algorithm changed.
        # The most likely scenario is that we got index.json got updated
        # while we fetched index.json.hash. Warning about an issue thus feels
        # wrong, as it's more of an issue with race conditions in the cache
        # invalidation strategy.

        # For now we only handle etags on http(s), since 304 error handling
        # in s3:// is not there yet.
        if urllib.parse.urlparse(self.url).scheme not in ("http", "https"):
            etag = None
        else:
            etag = web_util.parse_etag(
                response.headers.get("Etag", None) or response.headers.get("etag", None)
            )

        warn_v2_layout(self.url, "Fetching an index")

        return FetchIndexResult(etag=etag, hash=computed_hash, data=result, fresh=False)


class EtagIndexFetcherV2(IndexFetcher):
    """Fetcher for index.json, using ETags headers as cache invalidation strategy"""

    def __init__(self, url, etag, urlopen=web_util.urlopen):
        self.url = url
        self.etag = etag
        self.urlopen = urlopen

    def conditional_fetch(self) -> FetchIndexResult:
        # Just do a conditional fetch immediately
        url = url_util.join(self.url, "build_cache", spack_db.INDEX_JSON_FILE)
        headers = {"User-Agent": web_util.SPACK_USER_AGENT, "If-None-Match": f'"{self.etag}"'}

        try:
            response = self.urlopen(urllib.request.Request(url, headers=headers))
        except urllib.error.HTTPError as e:
            if e.getcode() == 304:
                # Not modified; that means fresh.
                return FetchIndexResult(etag=None, hash=None, data=None, fresh=True)
            raise FetchIndexError(f"Could not fetch index {url}", e) from e
        except OSError as e:  # URLError, socket.timeout, etc.
            raise FetchIndexError(f"Could not fetch index {url}", e) from e

        try:
            result = codecs.getreader("utf-8")(response).read()
        except (ValueError, OSError) as e:
            raise FetchIndexError(f"Remote index {url} is invalid", e) from e

        warn_v2_layout(self.url, "Fetching an index")

        headers = response.headers
        etag_header_value = headers.get("Etag", None) or headers.get("etag", None)
        return FetchIndexResult(
            etag=web_util.parse_etag(etag_header_value),
            hash=compute_hash(result),
            data=result,
            fresh=False,
        )


class OCIIndexFetcher(IndexFetcher):
    def __init__(self, url_and_version: MirrorURLAndVersion, local_hash, urlopen=None) -> None:
        self.local_hash = local_hash

        url = url_and_version.url

        # Remove oci:// prefix
        assert url.startswith("oci://")
        self.ref = spack.oci.image.ImageReference.from_string(url[6:])
        self.urlopen = urlopen or spack.oci.opener.urlopen

    def conditional_fetch(self) -> FetchIndexResult:
        """Download an index from an OCI registry type mirror."""
        url_manifest = self.ref.with_tag(default_index_tag).manifest_url()
        try:
            response = self.urlopen(
                urllib.request.Request(
                    url=url_manifest,
                    headers={"Accept": "application/vnd.oci.image.manifest.v1+json"},
                )
            )
        except OSError as e:
            raise FetchIndexError(f"Could not fetch manifest from {url_manifest}", e) from e

        try:
            manifest = json.load(response)
        except Exception as e:
            raise FetchIndexError(f"Remote index {url_manifest} is invalid", e) from e

        # Get first blob hash, which should be the index.json
        try:
            index_digest = spack.oci.image.Digest.from_string(manifest["layers"][0]["digest"])
        except Exception as e:
            raise FetchIndexError(f"Remote index {url_manifest} is invalid", e) from e

        # Fresh?
        if index_digest.digest == self.local_hash:
            return FetchIndexResult(etag=None, hash=None, data=None, fresh=True)

        # Otherwise fetch the blob / index.json
        try:
            response = self.urlopen(
                urllib.request.Request(
                    url=self.ref.blob_url(index_digest),
                    headers={"Accept": "application/vnd.oci.image.layer.v1.tar+gzip"},
                )
            )
            result = codecs.getreader("utf-8")(response).read()
        except (OSError, ValueError) as e:
            raise FetchIndexError(f"Remote index {url_manifest} is invalid", e) from e

        # Make sure the blob we download has the advertised hash
        if compute_hash(result) != index_digest.digest:
            raise FetchIndexError(f"Remote index {url_manifest} is invalid")

        return FetchIndexResult(etag=None, hash=index_digest.digest, data=result, fresh=False)


class DefaultIndexFetcher(IndexFetcher):
    """Fetcher for buildcache index, cache invalidation via manifest contents"""

    def __init__(self, url_and_version: MirrorURLAndVersion, local_hash, urlopen=web_util.urlopen):
        self.url = url_and_version.url
        self.layout_version = url_and_version.version
        self.local_hash = local_hash
        self.urlopen = urlopen
        self.headers = {"User-Agent": web_util.SPACK_USER_AGENT}

    def conditional_fetch(self) -> FetchIndexResult:
        cache_class = get_url_buildcache_class(layout_version=self.layout_version)
        url_index_manifest = cache_class.get_index_url(self.url)

        try:
            response = self.urlopen(
                urllib.request.Request(url_index_manifest, headers=self.headers)
            )
        except OSError as e:
            raise FetchIndexError(
                f"Could not read index manifest from {url_index_manifest}"
            ) from e

        index_blob_record = self.get_index_manifest(response)

        # Early exit if our cache is up to date.
        if self.local_hash and self.local_hash == index_blob_record.checksum:
            return FetchIndexResult(etag=None, hash=None, data=None, fresh=True)

        # Otherwise, download the index blob
        cache_entry = cache_class(self.url, allow_unsigned=True)
        computed_hash, result = self.fetch_index_blob(cache_entry, index_blob_record)
        cache_entry.destroy()

        # For now we only handle etags on http(s), since 304 error handling
        # in s3:// is not there yet.
        if urllib.parse.urlparse(self.url).scheme not in ("http", "https"):
            etag = None
        else:
            etag = web_util.parse_etag(
                response.headers.get("Etag", None) or response.headers.get("etag", None)
            )

        return FetchIndexResult(etag=etag, hash=computed_hash, data=result, fresh=False)


class EtagIndexFetcher(IndexFetcher):
    """Fetcher for buildcache index, cache invalidation via ETags headers

    This class differs from the DefaultIndexFetcher in the following ways: 1) It
    is provided with an etag value on creation, rather than an index checksum
    value. Note that since we never start out with an etag, the default fetcher
    must have been used initially and determined that the etag approach is valid.
    2) It provides this etag value in the 'If-None-Match' request header for the
    index manifest. 3) It checks for special exception type and response code
    indicating the index manifest is not modified, exiting early and returning
    'Fresh', if encountered. 4) If it needs to actually read the manfiest, it
    does not need to do any checks of the url scheme to determine whether an
    etag should be included in the return value."""

    def __init__(self, url_and_version: MirrorURLAndVersion, etag, urlopen=web_util.urlopen):
        self.url = url_and_version.url
        self.layout_version = url_and_version.version
        self.etag = etag
        self.urlopen = urlopen

    def conditional_fetch(self) -> FetchIndexResult:
        # Do a conditional fetch of the index manifest (i.e. using If-None-Match header)
        cache_class = get_url_buildcache_class(layout_version=self.layout_version)
        manifest_url = cache_class.get_index_url(self.url)
        headers = {"User-Agent": web_util.SPACK_USER_AGENT, "If-None-Match": f'"{self.etag}"'}

        try:
            response = self.urlopen(urllib.request.Request(manifest_url, headers=headers))
        except urllib.error.HTTPError as e:
            if e.getcode() == 304:
                # The remote manifest has not been modified, i.e. the index we
                # already have is the freshest there is.
                return FetchIndexResult(etag=None, hash=None, data=None, fresh=True)
            raise FetchIndexError(f"Could not fetch index manifest {manifest_url}", e) from e
        except OSError as e:  # URLError, socket.timeout, etc.
            raise FetchIndexError(f"Could not fetch index manifest {manifest_url}", e) from e

        # We need to read the index manifest and fetch the associated blob
        cache_entry = cache_class(self.url, allow_unsigned=True)
        computed_hash, result = self.fetch_index_blob(
            cache_entry, self.get_index_manifest(response)
        )
        cache_entry.destroy()

        headers = response.headers
        etag_header_value = headers.get("Etag", None) or headers.get("etag", None)

        return FetchIndexResult(
            etag=web_util.parse_etag(etag_header_value),
            hash=computed_hash,
            data=result,
            fresh=False,
        )


def get_index_fetcher(
    scheme: str, url_and_version: MirrorURLAndVersion, cache_entry: Dict[str, str]
) -> IndexFetcher:
    if scheme == "oci":
        # TODO: Actually etag and OCI are not mutually exclusive...
        return OCIIndexFetcher(url_and_version, cache_entry.get("index_hash", None))
    elif cache_entry.get("etag"):
        if url_and_version.version < 3:
            return EtagIndexFetcherV2(url_and_version.url, cache_entry["etag"])
        else:
            return EtagIndexFetcher(url_and_version, cache_entry["etag"])

    else:
        if url_and_version.version < 3:
            return DefaultIndexFetcherV2(
                url_and_version.url, local_hash=cache_entry.get("index_hash", None)
            )
        else:
            return DefaultIndexFetcher(
                url_and_version, local_hash=cache_entry.get("index_hash", None)
            )


class NoOverwriteException(spack.error.SpackError):
    """Raised when a file would be overwritten"""

    def __init__(self, file_path):
        super().__init__(f"Refusing to overwrite the following file: {file_path}")


class NoGpgException(spack.error.SpackError):
    """
    Raised when gpg2 is not in PATH
    """

    def __init__(self, msg):
        super().__init__(msg)


class NoKeyException(spack.error.SpackError):
    """
    Raised when gpg has no default key added.
    """

    def __init__(self, msg):
        super().__init__(msg)


class PickKeyException(spack.error.SpackError):
    """
    Raised when multiple keys can be used to sign.
    """

    def __init__(self, keys):
        err_msg = "Multiple keys available for signing\n%s\n" % keys
        err_msg += "Use spack buildcache create -k <key hash> to pick a key."
        super().__init__(err_msg)


class NewLayoutException(spack.error.SpackError):
    """
    Raised if directory layout is different from buildcache.
    """

    def __init__(self, msg):
        super().__init__(msg)


class UnsignedPackageException(spack.error.SpackError):
    """
    Raised if installation of unsigned package is attempted without
    the use of ``--no-check-signature``.
    """


class GenerateIndexError(spack.error.SpackError):
    """Raised when unable to generate key or package index for mirror"""


class CannotListKeys(GenerateIndexError):
    """Raised when unable to list keys when generating key index"""


class PushToBuildCacheError(spack.error.SpackError):
    """Raised when unable to push objects to binary mirror"""
