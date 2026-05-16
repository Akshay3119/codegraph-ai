"""
Sample Codebase — Authentication Module
Used for testing the ingestion pipeline.
"""

from typing import Optional
from dataclasses import dataclass


@dataclass
class User:
    """Represents an authenticated user in the system."""
    user_id: str
    username: str
    email: str
    role: str = "viewer"

    def is_admin(self) -> bool:
        """Check if the user has admin privileges."""
        return self.role == "admin"

    def has_permission(self, permission: str) -> bool:
        """Check if the user has a specific permission based on their role."""
        role_permissions = {
            "admin": {"read", "write", "delete", "manage"},
            "editor": {"read", "write"},
            "viewer": {"read"},
        }
        return permission in role_permissions.get(self.role, set())


class AuthService:
    """
    Handles user authentication and token management.
    Uses in-memory storage for demo purposes.
    """

    def __init__(self):
        self._users: dict[str, User] = {}
        self._tokens: dict[str, str] = {}  # token → user_id

    def register_user(self, username: str, email: str, role: str = "viewer") -> User:
        """Register a new user and return the User object."""
        import uuid
        user_id = str(uuid.uuid4())[:8]
        user = User(user_id=user_id, username=username, email=email, role=role)
        self._users[user_id] = user
        return user

    def authenticate(self, username: str) -> Optional[str]:
        """
        Authenticate a user by username and return a session token.
        Returns None if the user is not found.
        """
        import secrets
        for user in self._users.values():
            if user.username == username:
                token = secrets.token_hex(16)
                self._tokens[token] = user.user_id
                return token
        return None

    def get_user_from_token(self, token: str) -> Optional[User]:
        """Resolve a session token to a User object."""
        user_id = self._tokens.get(token)
        if user_id:
            return self._users.get(user_id)
        return None

    def revoke_token(self, token: str) -> bool:
        """Revoke a session token. Returns True if the token existed."""
        return self._tokens.pop(token, None) is not None
