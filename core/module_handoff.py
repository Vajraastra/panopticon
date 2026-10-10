"""Discover loaded consumers of the standard image-set contract."""
from core.base_module import BaseModule


def image_set_consumers(modules, source=None):
    # BaseModule supplies a no-op: hasattr alone advertises unsupported tools.
    return {
        name: module for name, module in modules.items()
        if module is not source
        and type(module).load_image_set is not BaseModule.load_image_set
    }
