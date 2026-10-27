"""camoufox.addons: uBlock Origin is fetched at launch unless it is unpacked
already or excluded."""
import enum
import json
import os
import time


class DefaultAddons(enum.Enum):
    UBO = "https://addons.mozilla.org/firefox/downloads/latest/ublock-origin/latest.xpi"


def get_addon_path(name):
    return os.path.join(os.path.dirname(os.environ["FAKE_SITE_FILE"]), "addons", name)


def ensure(exclude):
    if DefaultAddons.UBO in (exclude or []):
        return
    if os.path.exists(os.path.join(get_addon_path("UBO"), "manifest.json")):
        return
    if json.load(open(os.environ["FAKE_SITE_FILE"])).get("no_addons"):
        with open(os.environ["FAKE_LOG"], "a") as f:
            f.write(json.dumps({"download": "ubo"}) + "\n")
        time.sleep(600)
