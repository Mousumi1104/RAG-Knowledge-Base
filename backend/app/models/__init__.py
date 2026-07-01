"""ORM models package.

Importing the model modules here ensures their tables register on
``Base.metadata`` so ``init_db()`` (which imports this package) can create them.
"""

from __future__ import annotations

from app.models.chunk import Chunk
from app.models.conversation import Conversation
from app.models.document import Document
from app.models.employee_conversation import EmployeeConversation
from app.models.employee_message import EmployeeMessage
from app.models.message import Message
from app.models.user import User

__all__ = [
    "Chunk",
    "Conversation",
    "Document",
    "EmployeeConversation",
    "EmployeeMessage",
    "Message",
    "User",
]
