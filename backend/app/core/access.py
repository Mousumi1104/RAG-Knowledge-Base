"""Server-derived retrieval scope (the single source of truth for isolation).

Employee Zone employees must only ever read the admin-curated **global** knowledge
base — never their own (legacy) private documents and never another user's. The
scope is *always* derived from the server-verified identity/role here; it is never
client-supplied (a client flag would be a privilege-escalation vector).

The returned ``(owner_id, include_global)`` tuple feeds the already-implemented
default-deny SQL ownership clause in the vector store, so routing every retrieval
caller through :func:`retrieval_scope` guarantees employees can never blend in
personal documents.
"""

from __future__ import annotations

from app.models.user import User

# owner_id=None, include_global=True -> globals only (default-deny everything else).
GLOBAL_ONLY: tuple[int | None, bool] = (None, True)


def retrieval_scope(user: User) -> tuple[int | None, bool]:
    """Return the ``(owner_id, include_global)`` retrieval scope for ``user``.

    - ``role == "admin"`` -> ``(user.id, True)``: the admin keeps own + global.
    - everyone else (``role == "user"``, i.e. employees) -> :data:`GLOBAL_ONLY`:
      global documents only, with NO personal-document blend.
    """
    if user.role == "admin":
        return (user.id, True)
    return GLOBAL_ONLY
