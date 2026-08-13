# EIRVEN Licensing

## Short version

**EIRVEN-owned source code: GNU AGPL v3 or later (`AGPL-3.0-or-later`).**

You may use, study, modify, fork, redistribute, and commercially use EIRVEN, subject to the GNU AGPL. In particular, modified covered versions that are conveyed must keep the required source available under the same license, and a modified version used to provide interaction over a computer network must offer its Corresponding Source to those remote users as required by AGPL section 13.

This is intentionally a **copyleft open-source** license. It is not a non-commercial license and it does not prohibit paid support, donations, paid hosting, paid distribution, or other commercial activity permitted by the AGPL.

## What this license applies to

Unless a file or directory says otherwise, source code authored for EIRVEN and owned/licensable by the EIRVEN project in this repository is licensed under:

`SPDX-License-Identifier: AGPL-3.0-or-later`

The full license text is in [`LICENSE`](LICENSE).

## Earlier MIT releases

Some earlier EIRVEN releases were distributed under the MIT License. This repository-wide licensing change does **not** purport to revoke rights already granted to recipients of those earlier copies. The AGPL applies to the current/future EIRVEN-owned code released under this licensing notice, subject to the rights of all relevant copyright holders.

If a contribution was supplied under different terms, those terms remain applicable to that contribution unless the relevant copyright holder has validly agreed to the relicensing.

## Third-party code and dependencies

The EIRVEN license does not replace third-party licenses. Third-party packages, downloaded tools, embedded libraries, and other separately licensed components remain under their upstream terms. See [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

## Qwen3 / model assets

The upstream Qwen3 model materials used by the Education pipeline are distributed under the upstream Apache License 2.0 terms. When EIRVEN redistributes model artifacts derived from or containing Qwen3 material, the applicable upstream license and attribution notices must be retained. See:

`release_assets/QWEN3_MODEL_LICENSE.txt`

The AGPL software license must not be read as deleting or replacing rights/conditions that apply to upstream model material.

## Education / RAG data

The Education RAG contains or indexes source texts and dataset material that are **not relicensed as EIRVEN software**.

Current Education sources include, among others:

- `maxzt/RuHeritage-Corpus`: dataset compilation/curation is identified by its upstream dataset card as CC BY 4.0; underlying literary works are identified there as public-domain material. Preserve attribution and the source notice.
- `PleIAs/Russian-PD`: its upstream dataset card identifies the collection as public domain. Preserve provenance information in distributable Education releases.

See [`release_assets/EDUCATION_DATA_NOTICE.md`](release_assets/EDUCATION_DATA_NOTICE.md).

## Forks and project identity

The AGPL permits forks. Modified versions should clearly state that they were modified and should not misrepresent themselves as an official, unmodified EIRVEN release. The software license grants code rights; it does not grant anyone a right to falsely imply endorsement, authorship, or official project status.

A truthful description such as “fork of EIRVEN” is encouraged when applicable.

## Contributions

Unless a contributor explicitly states otherwise in a contribution agreement accepted by the project, contributions intentionally submitted for inclusion in the AGPL-licensed EIRVEN codebase are expected to be compatible with `AGPL-3.0-or-later`.

Do not submit code you do not have the right to license.

## No warranty

EIRVEN is provided without warranty to the extent stated in the GNU AGPL and applicable law.
