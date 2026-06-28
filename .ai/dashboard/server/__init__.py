"""Dashboard server package.

``serve.py`` is being decomposed into focused modules under this package
(leaf-first, lowest-risk first). To keep the test suite and the
``python .ai/dashboard/serve.py`` entrypoint unaffected, ``serve.py``
re-exports the names it moves here, so ``import serve; serve._x`` and
``from serve import X`` keep resolving exactly as before.
"""
