"""``python -m pikpak_wms``: the ``wms`` command line."""

import sys

from .cli.main import main

sys.exit(main())
