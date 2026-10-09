"""The model chain the service chose for itself, and the small file that remembers it.

With ``OPENROUTER_MODELS`` left blank the service may replace a model of its chain
(:mod:`app.model_chooser`). The chain it is using then lives here, in this process's
memory: :attr:`app.config.Settings.model_chain` asks :func:`chain` for it.

``model_cache.json``, next to this package, is only how that choice survives a
restart on a machine that keeps its disk. The service works the same without it. A
host that forgets its files at every restart simply starts again from the tested
default (and from the published list, see :mod:`app.published_chain`), and a folder
that cannot be written costs one warning in the log and nothing else.

A stand-in is kept apart from all that. While a repair tests the free models, a
model that has read one document correctly may already answer. That chain lies on
top of the chosen one, in memory only, and is never written to the file: a model
that was put to use after one document must not be what a restart finds.

Delete the file and restart to go back to the tested default.

The one module of the service this one imports is :mod:`app.model_catalogue`, which
imports none itself, so the settings can use this module without a circular import.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional, Sequence

from app.model_catalogue import is_free_model

logger = logging.getLogger("app.model_cache")

PATH = Path(__file__).resolve().parents[1] / "model_cache.json"

#: Raised when the meaning of the file changes. A file with another number is ignored.
VERSION = 1
#: A real file is a few kilobytes. Nothing larger is looked at.
MAX_BYTES = 256_000
#: The longest chain the service builds (``app.model_rules.PLACES``, repeated here so
#: that this module needs nothing from the modules that import it; a test holds the
#: two numbers together).
MAX_CHAIN = 3

_loaded = False  #: whether the file has been looked at in this process
_state: dict[str, Any] = {}  #: {"chain": [...], "origin": {...}, "last": {...}}, or empty
_standing_in: Optional[dict[str, Any]] = None  #: {"chain": [...], "origin": {...}} with stand-ins
_warned = False  #: "could not save" is logged once, not at every change


def forget() -> None:
    """Drop what this process remembers, without touching the file (tests use this)."""
    global _loaded, _state, _standing_in, _warned
    _loaded, _state, _standing_in, _warned = False, {}, None, False


def _default() -> str:
    from app.config import DEFAULT_MODEL_CHAIN  # imported late: app.config imports this module

    return DEFAULT_MODEL_CHAIN


def read() -> dict[str, Any]:
    """The file's content when it may be used, else ``{}``. Never raises.

    The file can be edited by hand, so it is checked like any other input. It is
    used only when it is small, is a version 1 object, was written under the same
    built-in default chain (a new default means somebody measured again, which is
    newer evidence than anything saved here), and holds a chain of at most three
    different free model ids (none at all means "the tested default"). One id that is
    not free discards the whole file, so nothing in it can ever make the service call
    a model that costs money.
    """
    try:
        if PATH.stat().st_size > MAX_BYTES:
            return {}
        saved = json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return {}
    if not isinstance(saved, dict) or saved.get("version") != VERSION:
        return {}
    models = saved.get("chain")
    usable = (
        saved.get("default") == _default()
        and isinstance(models, list)
        and len(models) <= MAX_CHAIN
        and all(is_free_model(model) for model in models)
        and len(set(models)) == len(models)
        and (isinstance(saved.get("origin"), dict) or not models)
    )
    return saved if usable else {}


def _load() -> None:
    """Look at the file once per process, the first time anything is asked."""
    global _loaded, _state
    if _loaded:
        return
    _loaded = True
    saved = read()
    if saved:
        last = saved.get("last")
        _state = {
            "chain": list(saved["chain"]),
            "origin": dict(saved["origin"]) if saved["chain"] else None,
            "last": last if isinstance(last, dict) else None,
        }
    elif PATH.exists():
        logger.info(
            "%s is there and was not used: it is not what this version of the service saves, "
            "or it was saved under another DEFAULT_MODEL_CHAIN. The service starts from the "
            "published list or the tested default, and the next change of the chain replaces "
            "the file.",
            PATH,
        )


def chain() -> list[str]:
    """The chain in use, stand-ins included, or ``[]`` for the tested default."""
    _load()
    return list((_standing_in or _state).get("chain", []))


def settled() -> list[str]:
    """The chain the service chose, without stand-ins: what a restart would find.

    ``[]`` when that is the tested default.
    """
    _load()
    return list(_state.get("chain", []))


def origin() -> Optional[dict[str, Any]]:
    """Where the chain in use came from, or None when the tested default is in use."""
    _load()
    current = _standing_in or _state
    return dict(current["origin"]) if current.get("chain") else None


def last() -> Optional[dict[str, Any]]:
    """The record of the last test this service ran or adopted, if it has one."""
    _load()
    return _state.get("last")


def use(models: Sequence[str], origin: dict[str, Any], save: bool = True) -> None:
    """Use ``models`` from now on, and save that if the disk allows.

    Raises ``ValueError`` for an empty chain, a chain that is too long, an id that
    is named twice or an id that is not a free model id: what :func:`read` would not
    take back from the file is not put there. This is the last door a paid model
    could come through, and it stays shut. With ``save=False`` the chain holds a
    stand-in: it is used from now on, lies on top of the chosen chain in memory only,
    and goes with :func:`stand_down` or with the next chain that is chosen.
    """
    global _state, _standing_in
    if (
        not 0 < len(models) <= MAX_CHAIN
        or len(set(models)) != len(models)
        or not all(is_free_model(model) for model in models)
    ):
        raise ValueError(
            f"only a chain of 1 to {MAX_CHAIN} different ids ending in ':free' is ever chosen"
        )
    _load()
    if not save:
        _standing_in = {"chain": list(models), "origin": dict(origin)}
        return
    _state = {"chain": list(models), "origin": dict(origin), "last": _state.get("last")}
    _standing_in = None
    _save()


def stand_down() -> None:
    """No stand-in answers any more: the chosen chain is in use again, exactly as it was."""
    global _standing_in
    _standing_in = None


def remember(record: dict[str, Any]) -> None:
    """Keep the record of a finished test for the page, whatever it changed."""
    global _state
    _load()
    _state = {"chain": _state.get("chain", []), "origin": _state.get("origin"), "last": record}
    _save()


def _save() -> None:
    """Replace the file in one step, so a reader never sees half of it. Never raises."""
    global _warned
    document = {"version": VERSION, "default": _default(), **_state}
    scratch = PATH.with_name(f"{PATH.name}.{os.getpid()}.tmp")
    try:
        scratch.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        try:
            os.replace(scratch, PATH)
        except PermissionError:
            # Windows refuses while another program (a virus scanner, a second
            # process) holds the file open for an instant. Once more, then give up.
            time.sleep(0.1)
            os.replace(scratch, PATH)
    except OSError as exc:
        if not _warned:
            _warned = True
            logger.warning(
                "Could not save the chosen model chain in %s (%s). The service goes on using "
                "it from memory; after a restart it starts again from the published list or "
                "the tested default.",
                PATH,
                type(exc).__name__,
            )
