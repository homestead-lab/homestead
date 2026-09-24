# Network shares

**Shares** serves volumes to Windows, macOS and Linux over SMB (Samba), the way
an Unraid share does - `\\192.0.2.245\media`.

![Shares](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-shares.png)

## The first share

The first share installs Samba. It asks which address Samba answers on - one of
[your VIPs](Networking#your-vips), or the next free one - because a share needs
an address of its own on port 445. Samba is put in place before anything else,
so a share that could not be served leaves nothing behind.

Samba shows in Containers, in the **Homestead** group, and can be switched off
(and back on) in **Settings → About**: shares stop being served, and their
volumes, settings and passwords are kept.

## Making a share

A share either:

- **creates its own volume** - a size, a class, copies; or
- **publishes a volume that exists** - including one an app is using - optionally
  just one folder inside it. That is how you reach an app's appdata from your
  desktop without copying it.

Then choose **guest** (anyone on the LAN) or **private** (a username and
password), and **read-only** or **read/write**.

Samba keeps one password per user, so a second private share for the same user
reuses its password when the field is left blank, and a new password changes it
for every share that user has - the editor says so first.

## Connecting

- **Windows**: `\\<address>\<share>` in File Explorer, or **Map network drive**.
- **macOS**: Finder → Go → Connect to Server → `smb://<address>/<share>`.
- **Linux**: `smb://<address>/<share>` in the file manager, or mount with
  `mount -t cifs`.

## Changes and safety

Growing a share happens in place. Access changes restart Samba, followed in the
job tray. One Samba pod serves every share, so a change is tested first: if the
share's volume cannot be mounted, the old shares are put back and the change is
refused with the reason, rather than taking every share down.

Removing a share keeps its volume and data. Share settings are in the
`homestead-shares` ConfigMap; passwords in the `homestead-share-credentials`
Secret, and never sent back to the browser.
