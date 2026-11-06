"""Proxy traffic metering, budgets and the download gate (docs/wms/M9).

* :mod:`.meter` and :mod:`.classify` turn mihomo's counters into bytes per
  category, outbound and node; :mod:`.pricing` reads the price off the node
  name.
* :mod:`.store` keeps hourly buckets in a SQLite file of its own.
* :mod:`.service` polls, flushes, raises alerts and sends the daily summary.
* :mod:`.gate` is what Telegram downloads and uploads ask before they move
  bytes: a pause, budget pauses, and token-bucket rate limits.
* :mod:`.report` renders it for ``/traffic`` and the command line.

The bot only ever reads mihomo.
"""

from .gate import NullControl, TokenBucket, TrafficControl
from .service import TrafficService

__all__ = ["NullControl", "TokenBucket", "TrafficControl", "TrafficService"]
