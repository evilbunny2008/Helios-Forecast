"""Small JSON files that survive a restart: what Home Assistant's helpers.storage.Store does for the integration."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

_LOGGER = logging.getLogger(__name__)


class JsonStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> Optional[Any]:
        try:
            return json.loads(self._path.read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as err:
            # Not something a user repairs by hand: start over rather than fail every refresh.
            _LOGGER.warning("Ignored unreadable %s: %s", self._path, err)
            return None

    def save(self, data: Any) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(self._path)
