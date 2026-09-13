"""
Disk cache for extracted evidence deltas (Step 10).

Keys responses by SHA-256 content hash so subsequent runs cost zero API calls
and yield byte-identical results.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CACHE_DIR = _REPO_ROOT / ".cache" / "evidence"


def get_cache_dir() -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR


def compute_hash(data: Any) -> str:
    """Compute deterministic SHA-256 hash of JSON-serializable data or text."""
    if isinstance(data, (bytes, bytearray)):
        content = data
    elif isinstance(data, str):
        content = data.encode("utf-8")
    else:
        content = json.dumps(data, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def get(cache_key: str) -> Optional[Any]:
    """Retrieve cached payload by key, or None if not cached."""
    cache_path = get_cache_dir() / f"{cache_key}.json"
    if not cache_path.exists():
        return None
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def put(cache_key: str, data: Any) -> None:
    """Persist payload to disk cache."""
    cache_path = get_cache_dir() / f"{cache_key}.json"
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
    except Exception:
        pass

