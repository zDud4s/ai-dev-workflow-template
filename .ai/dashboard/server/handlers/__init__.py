"""HTTP route handler mixins for the dashboard server.

Each module here defines a ``*Routes`` mixin that ``serve.py`` composes onto the
request handler class. Handlers are the leaf layer: they import server engines
(``server.jobs``, ``server.analytics``, ...) and foundations (``server.paths``,
``server.storage``, ...), but never each other, and nothing in the engine layer
imports them. Naming drops the redundant ``_handlers`` suffix now that the
package carries it: ``server.jobs_handlers`` -> ``server.handlers.jobs``.
"""
