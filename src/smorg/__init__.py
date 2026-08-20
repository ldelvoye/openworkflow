import importlib.metadata
import json
from importlib.metadata import version

__version__ = version("smorg")


def is_dev_build() -> bool:
    try:
        distribution = importlib.metadata.distribution("smorg")
        raw_direct_url = distribution.read_text("direct_url.json")
        if raw_direct_url is None:
            return False
        direct_url = json.loads(raw_direct_url)
        dir_info = direct_url.get("dir_info", {})
        editable = dir_info.get("editable", False)
        return bool(editable)
    except Exception:
        return False
