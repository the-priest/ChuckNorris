"""camoufox.pkgman as the real package (0.5.x) behaves: the browser lives in
a cache folder; asking for its path downloads it when it's missing, unless
download_if_missing=False, which raises instead. A "download" here is logged
and takes far longer than any page read may wait, as 300 MB would."""
import json
import os
import time


def _site():
    return json.load(open(os.environ["FAKE_SITE_FILE"]))


def _log(**kw):
    with open(os.environ["FAKE_LOG"], "a") as f:
        f.write(json.dumps(kw) + "\n")


class CamoufoxNotInstalled(FileNotFoundError):
    pass


def _dir():
    return os.path.join(os.path.dirname(os.environ["FAKE_SITE_FILE"]), "browser")


def camoufox_path(download_if_missing=True):
    if _site().get("not_installed"):
        if not download_if_missing:
            raise CamoufoxNotInstalled("camoufox is not installed. Please run `camoufox fetch` to install.")
        _log(download="browser")
        time.sleep(600)
    os.makedirs(_dir(), exist_ok=True)
    exe = os.path.join(_dir(), "camoufox-bin")
    if not os.path.exists(exe):
        open(exe, "w").close()
    return _dir()


def launch_path(browser_path=None):
    return os.path.join(str(browser_path or camoufox_path()), "camoufox-bin")
