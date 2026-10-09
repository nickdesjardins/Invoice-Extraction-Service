"""Browser playground for the extraction service.

Everything about the demo page lives in this package: its routes, the failure
simulation behind the "what if this model fails" controls, the scenario catalogue,
the test-suite runner and the static assets. ``PLAYGROUND_ENABLED=false`` leaves a
pure API.

Two modules here are also used by the service itself when it looks after its own
model chain: the sample documents (:mod:`app.playground.catalogue`) and the code
that sends them to a model and grades the answer (:mod:`app.playground.benchmark`).
"""

from typing import Any


def install_playground(app: Any) -> None:
    """Add the home page and its support routes to ``app``.

    The routes are imported here, when the application is built, and not when this
    package is first imported. The service's own modules import the benchmark from
    this package, and the routes import those modules: loading the routes at package
    import would close that circle and fail, depending on which module came first.
    """
    from app.playground.routes import install_playground as install

    install(app)


__all__ = ["install_playground"]
