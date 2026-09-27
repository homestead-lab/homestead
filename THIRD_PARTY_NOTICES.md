# Third-party notices

Homestead is released under the [MIT License](LICENSE). It is written against
Python's standard library and plain browser JavaScript, with an optional
node-allocation collector using gRPC and Protocol Buffers. What ships, what it runs, and what it reads
are listed here with their terms.

## Bundled in this repository and the Homestead image

| Component | Where | Licence |
|---|---|---|
| Kubernetes PodResources v1 schema (adapted from v1.32.0) and generated Python messages | `server/probe/podresources.proto`, `server/probe/podresources_pb2.py` | Apache-2.0; [full licence](licenses/KUBERNETES-PODRESOURCES.txt). Go-specific options and the unused Get RPC were removed; field numbers are unchanged. |
| [Monaco Editor](https://github.com/microsoft/monaco-editor) 0.52.2 (a trimmed subset) | `web/vendor/monaco/` | MIT - © Microsoft Corporation; the full text is in [`web/vendor/monaco/LICENSE`](web/vendor/monaco/LICENSE) |
| Codicon icon font, shipped with Monaco | `web/vendor/monaco/vs/base/browser/ui/codicons/codicon/codicon.ttf` | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) - © Microsoft Corporation, from [vscode-codicons](https://github.com/microsoft/vscode-codicons) |
| [noVNC](https://github.com/novnc/noVNC) 1.7.0 (`core` and `vendor` from the npm package) | `web/vendor/novnc/` | MPL-2.0 - © the noVNC authors (see [`AUTHORS`](web/vendor/novnc/AUTHORS)); the full text is in [`web/vendor/novnc/LICENSE.txt`](web/vendor/novnc/LICENSE.txt). The files are unmodified, so their source is the upstream release. |
| [xterm.js](https://github.com/xtermjs/xterm.js) 6.0.0 and its fit add-on 0.11.0 (`lib` and `css` from the npm packages) | `web/vendor/xterm/` | MIT - © The xterm.js authors; the full text is in [`web/vendor/xterm/LICENSE`](web/vendor/xterm/LICENSE). Unmodified. |
| pako, shipped with noVNC | `web/vendor/novnc/vendor/pako/` | MIT - © Vitaly Puzrin; see [`web/vendor/novnc/vendor/pako/LICENSE`](web/vendor/novnc/vendor/pako/LICENSE) |

## In the Homestead container image

The image is built on the official [`python:3.12-alpine`](https://hub.docker.com/_/python)
image: the Python interpreter and standard library (PSF License) on Alpine
Linux, whose packages carry their own licences. The image adds these packages;
Python wheel versions and hashes are pinned in `requirements-topology.txt`.
Their distribution licence files remain installed with the packages.

| Package | Why | Licence |
|---|---|---|
| [grpcio](https://github.com/grpc/grpc) | bounded local PodResources RPCs in the optional allocation collector | Apache-2.0 |
| [protobuf](https://github.com/protocolbuffers/protobuf) | PodResources message decoding | BSD-3-Clause |
| [typing-extensions](https://github.com/python/typing_extensions) | gRPC runtime dependency | PSF-2.0 |
| [smartmontools](https://www.smartmontools.org/) | reading drive health in the node probe's SMART sidecar | GPL-2.0-or-later; unmodified, installed from Alpine's package repository, whose [source](https://gitlab.alpinelinux.org/alpine/aports) is published |
| [rsync](https://rsync.samba.org/) | verified copies of Homestead's persistent data | GPL-3.0-or-later; [full licence](licenses/RSYNC-GPL-3.0.txt); unmodified Alpine package; [upstream source](https://rsync.samba.org/ftp/rsync/src/) and [Alpine packaging](https://gitlab.alpinelinux.org/alpine/aports/-/tree/master/main/rsync) |

## Images Homestead starts in your cluster

Homestead pulls these from their publishers when a feature needs them; none is
bundled or modified.

| Image | Used for | Licence |
|---|---|---|
| `alpine:3.20` | file browsing, import copies, ownership changes | MIT (Alpine base) and package licences |
| `python:3.12-alpine` | the node probe, image cache cleanup | PSF License and package licences |
| `busybox` | the node power helper, and a node's terminal | GPL-2.0 |
| `registry.k8s.io/pause:3.9` | image pre-pulls | Apache-2.0 |
| `quay.io/minio/minio` | optional backup storage | AGPL-3.0 - run unmodified as a separate service |

## Loaded at runtime

- **Fonts** - [Inter Tight](https://github.com/rsms/inter) and
  [JetBrains Mono](https://github.com/JetBrains/JetBrainsMono), both under the
  [SIL Open Font License 1.1](https://openfontlicense.org), served by Google Fonts.
- **App Store listings** - read on demand from the public
  [Community Applications feed](https://github.com/Squidly271/AppFeed) (or a feed
  set in Settings) and never bundled. Templates, descriptions and icons belong to
  their authors; the feed declares no licence of its own, so Homestead only
  displays it and links back to each template's project and support pages.
  Anyone redistributing or hosting the catalogue should get permission from its
  maintainers.
- **App icons** set on a workload are fetched from the address given and cached
  by content hash; they belong to their projects.

## Trademarks

Homestead is an independent project and is not affiliated with, endorsed or
sponsored by any of the following. Names are used only to say what Homestead
works with.

- Unraid® is a registered trademark of Lime Technology, Inc.
- Harvester, Rancher and Longhorn are trademarks of SUSE LLC. Longhorn is a
  Cloud Native Computing Foundation project.
- Kubernetes® is a registered trademark of The Linux Foundation.
- Docker® is a registered trademark of Docker, Inc.
- Cloudflare® is a registered trademark of Cloudflare, Inc.
