.. Copyright Spack Project Developers. See COPYRIGHT file for details.

   SPDX-License-Identifier: (Apache-2.0 OR MIT)

.. _signing:

=====================
Spack Package Signing
=====================

The goal of package signing in Spack is to provide data integrity
assurances around official packages produced by the automated Spack CI
pipelines. These assurances directly address the security of Spack’s
software supply chain by explaining why a security-conscious user can
be reasonably justified in the belief that packages installed via Spack
have an uninterrupted auditable trail back to change management
decisions judged to be appropriate by the Spack maintainers. This is
achieved through cryptographic signing of packages built by Spack CI
pipelines based on code that has been transparently reviewed and
approved on GitHub. This document describes the signing process for
interested users.

.. _risks:

------------------------------
Risks, Impact and Threat Model
------------------------------

This document addresses the approach taken to safeguard Spack’s
reputation with regard to the integrity of the package data produced by
Spack’s CI pipelines. It does not address issues of data confidentiality
(Spack is intended to be largely open source) or availability (efforts
are described elsewhere). With that said, the main reputational risk can
be broadly categorized as a loss of faith in the data integrity due to a
breach of the private key used to sign packages. Remediation of a
private key breach would require republishing the public key with a
revocation certificate, generating a new signing key, an assessment and
potential rebuild/resigning of all packages since the key was breached,
and finally direct intervention by every spack user to update their copy
of Spack’s public keys used for local verification.

The primary threat model used in mitigating the risks of these stated
impacts is one of individual error not malicious intent or insider
threat. The primary objective is to avoid the above impacts by making a
private key breach nearly impossible due to oversight or configuration
error. Obvious and straightforward measures are taken to mitigate issues
of malicious interference in data integrity and insider threats but
these attack vectors are not systematically addressed. It should be hard
to exfiltrate the private key intentionally, and almost impossible to
leak the key by accident.

.. _overview:

-----------------
Pipeline Overview
-----------------

Spack pipelines build software through progressive stages where packages
in later stages nominally depend on packages built in earlier stages.
For both technical and design reasons these dependencies are not
implemented through the default GitLab artifacts mechanism; instead
built packages are uploaded to AWS S3 mirrors (buckets) where they are
retrieved by subsequent stages in the pipeline. Two broad categories of
pipelines exist: Pull Request (PR) pipelines and Develop/Release
pipelines.

-  PR pipelines are launched in response to pull requests made by
   trusted and untrusted users. Packages built on these pipelines upload
   code to quarantined AWS S3 locations which cache the built packages
   for the purposes of review and iteration on the changes proposed in
   the pull request. Packages built on PR pipelines can come from
   untrusted users so signing of these pipelines is not implemented.
   Jobs in these pipelines are executed via normal GitLab runners both
   within the AWS GitLab infrastructure and at affiliated institutions.
-  Develop and Release pipelines **sign** the packages they produce and carry
   strong integrity assurances that trace back to auditable change management
   decisions. These pipelines only run after members from a trusted group of
   reviewers verify that the proposed changes in a pull request are appropriate.
   Once the PR is merged, or a release is cut, a pipeline is run on protected
   GitLab runners which provide access to the required signing keys within the
   job. Intermediary keys are used to sign packages in each stage of the
   pipeline as they are built and a final job officially signs each package
   external to any specific package’s build environment. An intermediate key
   exists in the AWS infrastructure and for each affiliated institution that
   maintains protected runners. The runners that execute these pipelines
   exclusively accept jobs from protected branches meaning the intermediate keys
   are never exposed to unreviewed code and the official keys are never exposed
   to any specific build environment.

.. _key_architecture:

----------------
Key Architecture
----------------

Spack’s CI process uses public-key infrastructure (PKI) based on GNU Privacy
Guard (gpg) keypairs to sign public releases of spack package metadata, also
called specs. Two classes of GPG keys are involved in the process to reduce the
impact of an individual private key compromise, these key classes are the
*Intermediate CI Key* and *Reputational Key*. Each of these keys has signing
sub-keys that are used exclusively for signing packages. This can be confusing
so for the purpose of this explanation we will refer to Root and Signing keys.
Each key has a private and a public component as well as one or more identities
and zero or more signatures.

-------------------
Intermediate CI Key
-------------------

The Intermediate key class is used to sign and verify packages between stages
within a develop or release pipeline. An intermediate key exists for the AWS
infrastructure as well as each affiliated institution that maintains protected
runners. These intermediate keys are made available to the GitLab execution
environment building the package so that the package’s dependencies may be
verified by the Signing Intermediate CI Public Key and the final package may be
signed by the Signing Intermediate CI Private Key.


+---------------------------------------------------------------------------------------------------------+
| **Intermediate CI Key (GPG)**                                                                           |
+==================================================+======================================================+
| Root Intermediate CI Private Key (RSA 4096)#     |     Root Intermediate CI Public Key (RSA 4096)       |
+--------------------------------------------------+------------------------------------------------------+
|   Signing Intermediate CI Private Key (RSA 4096) |        Signing Intermediate CI Public Key (RSA 4096) |
+--------------------------------------------------+------------------------------------------------------+
| Identity: “Intermediate CI Key <maintainers@spack.io>”                                                  |
+---------------------------------------------------------------------------------------------------------+
| Signatures: None                                                                                        |
+---------------------------------------------------------------------------------------------------------+


The *Root intermediate CI Private Key*\ is stripped out of the GPG key and
stored offline completely separate from Spack’s infrastructure. This allows the
core development team to append revocation certificates to the GPG key and
issue new sub-keys for use in the pipeline. It is our expectation that this
will happen on a semi-regular basis. A corollary of this is that *this key
should not be used to verify package integrity outside the internal CI process.*

----------------
Reputational Key
----------------

The Reputational Key is the public facing key used to sign complete groups of
development and release packages. Only one key pair exists in this class of
keys. In contrast to the Intermediate CI Key the Reputational Key *should* be
used to verify package integrity. At the end of develop and release pipelines a
final pipeline job pulls down all signed package metadata built by the pipeline,
verifies they were signed with an Intermediate CI Key, then strips the
Intermediate CI Key signature from the package and re-signs them with the
Signing Reputational Private Key. The officially signed packages are then
uploaded back to the AWS S3 mirror. Please note that separating use of the
reputational key into this final job is done to prevent leakage of the key in a
spack package. Because the Signing Reputational Private Key is never exposed to
a build job it cannot accidentally end up in any built package.


+---------------------------------------------------------------------------------------------------------+
| **Reputational Key (GPG)**                                                                              |
+==================================================+======================================================+
| Root Reputational Private Key (RSA 4096)#        |          Root Reputational Public Key (RSA 4096)     |
+--------------------------------------------------+------------------------------------------------------+
| Signing Reputational Private Key (RSA 4096)      |          Signing Reputational Public Key (RSA 4096)  |
+--------------------------------------------------+------------------------------------------------------+
| Identity: “Spack Project <maintainers@spack.io>”                                                        |
+---------------------------------------------------------------------------------------------------------+
| Signatures: Signed by core development team [#f1]_                                                      |
+---------------------------------------------------------------------------------------------------------+

The Root Reputational Private Key is stripped out of the GPG key and stored
offline completely separate from Spack’s infrastructure. This allows the core
development team to append revocation certificates to the GPG key in the
unlikely event that the Signing Reputation Private Key is compromised. In
general it is the expectation that rotating this key will happen infrequently if
at all. This should allow relatively transparent verification for the end-user
community without needing deep familiarity with GnuPG or Public Key
Infrastructure.


.. _build_cache_signing:

-------------------
Build Cache Signing
-------------------

For an in-depth description of the layout of a binary mirror, see
the :ref:`documentation<build_cache_layout>` covering binary caches. The
key takeaway from that discussion that applies here is that the entry point
to a binary package is its manifest.  The manifest refers unambiguously to the
spec metadata and compressed archive, which are stored as content-addressed
blobs.

The manifest files can either be signed or unsigned, but are always given
a name ending with ``.spec.manifest.json`` regardless. The difference between
signed and unsigned manifests is simply that the signed version is wrapped in
a gpg cleartext signature, as illustrated below::

  -----BEGIN PGP SIGNED MESSAGE-----
  Hash: SHA512

  {
    "version": 3,
    "data": [
      {
        "contentLength": 10731083,
        "mediaType": "application/vnd.spack.install.v2.tar+gzip",
        "compression": "gzip",
        "checksumAlgorithm": "sha256",
        "checksum": "0f24aa6b5dd7150067349865217acd3f6a383083f9eca111d2d2fed726c88210"
      },
      {
        "contentLength": 1000,
        "mediaType": "application/vnd.spack.spec.v5+json",
        "compression": "gzip",
        "checksumAlgorithm": "sha256",
        "checksum": "fba751c4796536737c9acbb718dad7429be1fa485f5585d450ab8b25d12ae041"
      }
    ]
  }
  -----BEGIN PGP SIGNATURE-----

  iQGzBAEBCgAdFiEEdbwFKBFJCcB24mB0GAEP+tc8mwcFAmf2rr4ACgkQGAEP+tc8
  mwfefwv+KJs8MsQ5ovFaBdmyx5H/3k4rO4QHBzuSPOB6UaxErA9IyOB31iP6vNTU
  HzYpxz6F5dJCJWmmNEMN/0+vjhMHEOkqd7M1l5reVcxduTF2yc4tBZUO2gienEHL
  W0e+SnUznl1yc/aVpChUiahO2zToCsI8HZRNT4tu6iCnE/OpghqjsSdBOZHmSNDD
  5wuuCxfDUyWI6ZlLclaaB7RdbCUUJf/iqi711J+wubvnDFhc6Ynwm1xai5laJ1bD
  ev3NrSb2AAroeNFVo4iECA0fZC1OZQYzaRmAEhBXtCideGJ5Zf2Cp9hmCwNK8Hq6
  bNt94JP9LqC3FCCJJOMsPyOOhMSA5MU44zyyzloRwEQpHHLuFzVdbTHA3dmTc18n
  HxNLkZoEMYRc8zNr40g0yb2lCbc+P11TtL1E+5NlE34MX15mPewRCiIFTMwhCnE3
  gFSKtW1MKustZE35/RUwd2mpJRf+mSRVCl1f1RiFjktLjz7vWQq7imIUSam0fPDr
  XD4aDogm
  =RrFX
  -----END PGP SIGNATURE-----

If a user has trusted the public key associated with the private key
used to sign the above manifest file, the signature can be verified with
gpg, as follows::

  $ gpg --verify gcc-runtime-12.3.0-s2nqujezsce4x6uhtvxscu7jhewqzztx.spec.manifest.json

When attempting to install a binary package that has been signed, spack will
attempt to verify the signature with one of the trusted keys in its keyring,
and will fail if unable to do so.  While not recommended, it is possible to
force installation of a signed package without verification by providing the
``--no-check-signature`` argument to ``spack install ...``.

.. _internal_implementation:

-----------------------
Internal Implementation
-----------------------

The technical implementation of the pipeline signing process includes components
defined in Amazon Web Services, the Kubernetes cluster, at affiliated
institutions, and the GitLab/GitLab Runner deployment. We present the technical
implementation in two interdependent sections. The first addresses how secrets
are managed through the lifecycle of a develop or release pipeline. The second
section describes how Gitlab Runner and pipelines are configured and managed to
support secure automated signing.

Secrets Management
^^^^^^^^^^^^^^^^^^

As stated above the Root Private Keys (intermediate and reputational)
are stripped from the GPG keys and stored outside Spack’s
infrastructure.

.. warning::
  **TODO**
    - Explanation here about where and how access is handled for these keys.
    - Both Root private keys are protected with strong passwords
    - Who has access to these and how?

**Intermediate CI Key**
-----------------------

Multiple intermediate CI signing keys exist, one Intermediate CI Key for jobs
run in AWS, and one key for each affiliated institution (e.g. University of
Oregon). Here we describe how the Intermediate CI Key is managed in AWS:

The Intermediate CI Key (including the Signing Intermediate CI Private Key) is
exported as an ASCII armored file and stored in a Kubernetes secret called
``spack-intermediate-ci-signing-key``. For convenience sake, this same secret
contains an ASCII-armored export of just the *public* components of the
Reputational Key. This secret also contains the *public* components of each of
the affiliated institutions' Intermediate CI Key. These are potentially needed
to verify dependent packages which may have been found in the public mirror or
built by a protected job running on an affiliated institution's infrastructure
in an earlier stage of the pipeline.

Procedurally the ``spack-intermediate-ci-signing-key`` secret is used in
the following way:

1. A ``large-arm-prot`` or ``large-x86-prot`` protected runner picks up
   a job tagged ``protected`` from a protected GitLab branch. (See
   `Protected Runners and Reserved Tags <#_8bawjmgykv0b>`__).
2. Based on its configuration, the runner creates a job Pod in the
   pipeline namespace and mounts the spack-intermediate-ci-signing-key
   Kubernetes secret into the build container
3. The Intermediate CI Key, affiliated institutions' public key and the
   Reputational Public Key are imported into a keyring by the ``spack gpg …``
   sub-command. This is initiated by the job’s build script which is created by
   the generate job at the beginning of the pipeline.
4. Assuming the package has dependencies those spec manifests are verified using
   the keyring.
5. The package is built and the spec manifest is generated
6. The spec manifest is signed by the keyring and uploaded to the mirror’s
   build cache.

**Reputational Key**
--------------------

Because of the increased impact to end users in the case of a private
key breach, the Reputational Key is managed separately from the
Intermediate CI Keys and has additional controls. First, the Reputational
Key was generated outside of Spack’s infrastructure and has been signed
by the core development team. The Reputational Key (along with the
Signing Reputational Private Key) was then ASCII armor exported to a
file. Unlike the Intermediate CI Key this exported file is not stored as
a base64 encoded secret in Kubernetes. Instead\ *the key file
itself*\ is encrypted and stored in Kubernetes as the
``spack-signing-key-encrypted`` secret in the pipeline namespace.

The encryption of the exported Reputational Key (including the Signing
Reputational Private Key) is handled by `AWS Key Management Store (KMS) data
keys
<https://docs.aws.amazon.com/kms/latest/developerguide/concepts.html#data-keys>`__.
The private key material is decrypted and imported at the time of signing into a
memory mounted temporary directory holding the keychain. The signing job uses
the `AWS Encryption SDK
<https://docs.aws.amazon.com/encryption-sdk/latest/developer-guide/crypto-cli.html>`__
(i.e. ``aws-encryption-cli``) to decrypt the Reputational Key. Permission to
decrypt the key is granted to the job Pod through a Kubernetes service account
specifically used for this, and only this, function. Finally, for convenience
sake, this same secret contains an ASCII-armored export of the *public*
components of the Intermediate CI Keys and the Reputational Key. This allows the
signing script to verify that packages were built by the pipeline (both on AWS
or at affiliated institutions), or signed previously as a part of a different
pipeline. This is done *before* importing decrypting and importing the
Signing Reputational Private Key material and officially signing the packages.

Procedurally the ``spack-signing-key-encrypted`` secret is used in the
following way:

1.  The ``spack-package-signing-gitlab-runner`` protected runner picks
    up a job tagged ``notary`` from a protected GitLab branch (See
    `Protected Runners and Reserved Tags <#_8bawjmgykv0b>`__).
2.  Based on its configuration, the runner creates a job pod in the
    pipeline namespace. The job is run in a stripped down purpose-built
    image ``ghcr.io/spack/notary:latest`` Docker image. The runner is
    configured to only allow running jobs with this image.
3.  The runner also mounts the ``spack-signing-key-encrypted`` secret to
    a path on disk. Note that this becomes several files on disk, the
    public components of the Intermediate CI Keys, the public components
    of the Reputational CI, and an AWS KMS encrypted file containing the
    Signing Reputational Private Key.
4.  In addition to the secret, the runner creates a tmpfs memory mounted
    directory where the GnuPG keyring will be created to verify, and
    then resign the package specs.
5.  The job script syncs all spec manifest files from the build cache to
    a working directory in the job’s execution environment.
6.  The job script then runs the ``sign.sh`` script built into the
    Notary Docker image.
7.  The ``sign.sh`` script imports the public components of the
    Reputational and Intermediate CI Keys and uses them to verify good
    signatures on the spec.manifest.json files. If any signed manifest
    does not verify, the job immediately fails.
8.  Assuming all manifests are verified, the ``sign.sh`` script then unpacks
    the manifest json data from the signed file in preparation for being
    re-signed with the Reputational Key.
9.  The private components of the Reputational Key are decrypted to
    standard out using ``aws-encryption-cli`` directly into a ``gpg
    –import …`` statement which imports the key into the
    keyring mounted in-memory.
10. The private key is then used to sign each of the manifests and the
    keyring is removed from disk.
11. The re-signed manifests are resynced to the AWS S3 Mirror and the
    public signing of the packages for the develop or release pipeline
    that created them is complete.

Non service-account access to the private components of the Reputational
Key that are managed through access to the symmetric secret in KMS used
to encrypt the data key (which in turn is used to encrypt the GnuPG key
- See:\ `Encryption SDK
Documentation <https://docs.aws.amazon.com/encryption-sdk/latest/developer-guide/crypto-cli-examples.html#cli-example-encrypt-file>`__).
A small trusted subset of the core development team are the only
individuals with access to this symmetric key.

.. _protected_runners:

Protected Runners and Reserved Tags
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Spack has a large number of Gitlab Runners operating in its build farm.
These include runners deployed in the AWS Kubernetes cluster as well as
runners deployed at affiliated institutions. The majority of runners are
shared runners that operate across projects in gitlab.spack.io. These
runners pick up jobs primarily from the spack/spack project and execute
them in PR pipelines.

A small number of runners operating on AWS and at affiliated institutions are
registered as specific *protected* runners on the spack/spack project. In
addition to protected runners there are protected branches on the spack/spack
project. These are the ``develop`` branch, any release branch (i.e. managed with
the ``releases/v*`` wildcard) and any tag branch (managed with the ``v*``
wildcard) Finally, Spack’s pipeline generation code reserves certain tags to make
sure jobs are routed to the correct runners; these tags are ``public``,
``protected``, and ``notary``. Understanding how all this works together to
protect secrets and provide integrity assurances can be a little confusing so
lets break these down:

-  **Protected Branches**- Protected branches in Spack prevent anyone
   other than Maintainers in GitLab from pushing code. In the case of
   Spack, the only Maintainer level entity pushing code to protected
   branches is Spack bot. Protecting branches also marks them in such a
   way that Protected Runners will only run jobs from those branches
- **Protected Runners**- Protected Runners only run jobs from protected
   branches. Because protected runners have access to secrets, it's critical
   that they not run jobs from untrusted code (i.e. PR branches). If they did, it
   would be possible for a PR branch to tag a job in such a way that a protected
   runner executed that job and mounted secrets into a code execution
   environment that had not been reviewed by Spack maintainers. Note however
   that in the absence of tagging used to route jobs, public runners *could* run
   jobs from protected branches. No secrets would be at risk of being breached
   because non-protected runners do not have access to those secrets; lack of
   secrets would, however, cause the jobs to fail.
- **Reserved Tags**- To mitigate the issue of public runners picking up
   protected jobs Spack uses a small set of “reserved” job tags (Note that these
   are *job* tags not git tags). These tags are “public”, “private”, and
   “notary.” The majority of jobs executed in Spack’s GitLab instance are
   executed via a ``generate`` job. The generate job code systematically ensures
   that no user defined configuration sets these tags. Instead, the ``generate``
   job sets these tags based on rules related to the branch where this pipeline
   originated. If the job is a part of a pipeline on a PR branch it sets the
   ``public`` tag. If the job is part of a pipeline on a protected branch it
   sets the ``protected`` tag. Finally if the job is the package signing job and
   it is running on a pipeline that is part of a protected branch then it sets
   the ``notary`` tag.

Protected Runners are configured to only run jobs from protected branches. Only
jobs running in pipelines on protected branches are tagged with ``protected`` or
``notary`` tags. This tightly couples jobs on protected branches to protected
runners that provide access to the secrets required to sign the built packages.
The secrets can **only** be accessed via:

1. Runners under direct control of the core development team.
2. Runners under direct control of trusted maintainers at affiliated institutions.
3. By code running the automated pipeline that has been reviewed by the
   Spack maintainers and judged to be appropriate.

Other attempts (either through malicious intent or incompetence) can at
worst grab jobs intended for protected runners which will cause those
jobs to fail alerting both Spack maintainers and the core development
team.

.. [#f1]
   The Reputational Key has also cross signed core development team
   keys.
