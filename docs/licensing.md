# Homestead licensing

Homestead's original code, documentation and assets are licensed under **Apache
License 2.0 with Commons Clause License Condition v1.0**. The complete binding
terms are in [LICENSE](../LICENSE); this guide explains their scope.

## Use and contributions

You may use Homestead at home or internally in a business, study and modify its
source, contribute improvements, and redistribute it subject to the licence.
You may use it to run your own business applications. The restriction concerns
selling Homestead's functionality, rather than all commercial activity.

The Commons Clause excludes the right to provide third parties, for a fee or
other consideration, a product or service whose value derives entirely or
substantially from Homestead's functionality. This can include a rebranded copy,
paid hosted Homestead, or related consulting/support services meeting that test.
For permission outside these terms, contact the maintainer through the
[repository](https://github.com/homestead-lab/homestead). Adding a small feature or
changing the name does not automatically avoid the restriction.

The project is **source-available**, not OSI-approved open source. Community
contributions remain welcome under the same terms; see [CONTRIBUTING.md](../CONTRIBUTING.md).
The [Commons Clause FAQ](https://commonsclause.com/) explains the distinction.

## Transition

The change was made on 3 October 2026, after the `v2.8.300` release at
`e2302feb363ef55a0f3296eec36b9def6e106023`. That release and earlier MIT-licensed
versions retain their original permissions, including commercial reuse. No tag
or published release is retroactively relicensed. The historic notice is kept
in [HOMESTEAD-MIT-LEGACY.txt](../licenses/HOMESTEAD-MIT-LEGACY.txt); it is not an
alternative licence for later changes.

Machine-readable metadata uses `LicenseRef-Apache-2.0-Commons-Clause` for the
combined terms in LICENSE. Plain `Apache-2.0` would omit the restriction. The
Helm chart carries the complete licence rather than an inaccurate standard
Artifact Hub licence identifier.

## Dependency compatibility audit — 3 October 2026

The reviewed baseline is `e2302feb363ef55a0f3296eec36b9def6e106023`. Main and dev
were at that commit when the audit began. Git author history on main identified
wjcloudy as the author; no separate co-author trailers were found. This is
repository evidence, not an independent determination of copyright ownership.

| Component | Finding and required treatment |
|---|---|
| Monaco 0.52.2, xterm.js 6.0.0, fit add-on 0.11.0, pako | Permissive licences allow inclusion with Homestead. Preserve upstream notices and rights. Monaco's full third-party notice and the fit add-on's separate MIT notice are now included. |
| noVNC 1.7.0 | MPL applies to its covered files, including modifications. Unmodified source is distributed in `web/vendor/novnc/core/`, with the full MPL and upstream notices. Homestead's separate files may use the new terms without applying the Commons Clause to noVNC. Embedded DES code retains its permissive attribution headers. |
| Codicon font | CC BY 4.0 attribution and terms are retained in Monaco's upstream third-party notice. No Commons Clause restriction applies to the font. |
| PodResources schema and generated Python | Apache-2.0; existing modification notice and full upstream licence retained. These files are excluded from Homestead's Commons Clause. |
| grpcio, protobuf, typing-extensions | Apache-2.0, BSD-3-Clause and PSF-2.0 respectively; permitted alongside the new terms, with package notices retained. Versions and wheel hashes remain in `requirements-topology.txt`. |
| Python / Alpine and rsync / smartmontools | Independently licensed runtime and executables. rsync and smartctl are invoked as subprocesses, not linked or copied into Homestead. Their GPL rights and corresponding-source obligations remain intact. The root licence expressly excludes these components. |
| Kubernetes, KubeVirt, Longhorn, Harvester and other cluster services | Interoperate as separately installed services. Installing or managing them does not place them under the Commons Clause. |
| Fonts, app catalogue, workload icons and optional service images | External content retains its original terms. This change supplies no permission to redistribute the catalogue or relicense third-party images. |

Published npm archives were compared with the bundled executable/source files
(normalising CRLF only); Monaco, noVNC, xterm and its fit add-on matched.
The review restored missing noVNC licence texts, Monaco's third-party notice,
and the fit add-on's licence. No third-party executable code was changed.

A local Linux image build passed. Offline inspection confirmed the full project
licence and restored notices are present, and that installed grpcio 1.84.0,
protobuf 7.36.2 and typing-extensions 4.16.0 retain their upstream licence files.
Chart generation and vendored-asset tests passed. This validation did not
publish a container or modify existing release artefacts.

The reviewed integration boundaries support applying the new licence to
Homestead's original work. They do **not** permit relicensing all dependencies
under it. Mozilla explains [MPL file-level copyleft and larger works](https://www.mozilla.org/en-US/MPL/2.0/FAQ/);
GNU explains [separate programs and aggregation](https://www.gnu.org/licenses/gpl-faq.en.html#MereAggregation).

This is a source and packaging review, not a legal opinion or a complete audit
of every transitive package in every published image. Before publishing a new
container release, provide the exact third-party notices and corresponding
sources required by its packages; moving upstream source links alone do not
establish compliance. Mutable base images and separately pulled services must
be reviewed for the release being distributed. Full component notices are in
[THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).
