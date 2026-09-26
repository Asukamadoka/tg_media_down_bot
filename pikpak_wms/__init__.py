"""pikpak_wms: manage a PikPak drive like a warehouse.

Stocktake the drive into a local index, evaluate rules on that index, turn
the result into a plan, show the plan, and only then apply it, auditing every
change. The design and its six rules are in docs/wms/ARCHITECTURE.md.

This package never imports ``tgmd``. The bot drives it through
``pikpak_wms.ops`` and hands it an already logged-in PikPak client.
"""

__version__ = "0.2.0"
