"""camoufox.geolocation: the GeoIP database is downloaded at launch when it
is missing or stale."""
import json
import os
from pathlib import Path


def get_mmdb_path(ip_version="ipv4", config=None):
    return Path(os.path.dirname(os.environ["FAKE_SITE_FILE"])) / "mmdb" / f"geo-{ip_version}.mmdb"


def needs_update(config=None):
    return not get_mmdb_path().exists() or json.load(open(os.environ["FAKE_SITE_FILE"])).get("stale_geoip", False)
