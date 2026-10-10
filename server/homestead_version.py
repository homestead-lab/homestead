"""The release this source is (#363). scripts/bump_version.py moves it, so a
release commit no longer edits server.py; the image's HOMESTEAD_VERSION
environment variable, set at build, wins over it."""
VERSION = "2.8.322-dev.1"
