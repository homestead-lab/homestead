# App Store

The App Store reads the Community Applications catalogue - the templates behind
Unraid's Apps tab - and deploys them as proper Kubernetes apps: volumes on
Longhorn, addresses on your LAN, secrets generated here.

![App Store](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-app-store.png)

## Finding an app

The front page is laid out as Community Applications lays it out: the monthly
**Spotlight**, **Recently added**, **Top trending** and **Top performing**, each
with a page of its own. Search covers the whole catalogue. Plugins and templates
the catalogue has hidden or deprecated are left out.

An app's page has its full description, maintainer, links (project, support
thread, registry), screenshots - and Homestead's own review of it: which storage,
network, dependency and hardware questions it will ask.

## Deploying one

**Configure & deploy** opens the Deploy form filled in from the template:

- **Storage.** Paths the template maps into appdata share one `<app>-appdata`
  volume, a folder each, sized for all of them. Media paths are left for you to
  point at your library - a volume you already have, or a folder in one. Cache
  paths become scratch space.
- **Passwords.** Default passwords in public templates are thrown away and new
  ones generated. Secret values are hidden in previews.
- **Network.** The template's web port becomes the container's main port. The
  address is automatic unless you pick a **Specific VIP**.
- **Privileges.** A template marked Privileged, or asking for `--cap-add` or
  `/dev/net/tun`, fills in [Privileges](Containers#privileges) - check them.
- **Hardware.** Devices the template asks for are matched to your hardware
  features.

Templates that need the Docker socket are refused: that is Docker, not
Kubernetes, and would not work here.

After **Deploy**, the job tray follows the image pull and start-up, and the app
appears on Containers.

## Another catalogue

**Settings → Apps → App Store catalogue** points the store at any feed in the
Community Applications format - a mirror, or your own list of templates.

The catalogue is read from its public feed at run time and is not part of
Homestead. Unraid is a registered trademark of Lime Technology, Inc.; Homestead
is not affiliated with or endorsed by them.
