"""Verified Pickler operations; a central service and receipt are required.

Legacy unverified move/copy/unlink helpers were removed. Use transfer() for an
explicit Native-backed action and pass its result to undo(). Both run in workers.
"""
from .transfers import transfer, undo, recover, scan_folder
