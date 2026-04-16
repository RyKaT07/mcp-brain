"""
Pytest configuration — stub out third-party MCP and pydantic imports that
are not available in the plain-Python test environment (no venv, no Docker).

The production server requires `mcp[cli]` and `pydantic`. Tests only exercise
pure-Python logic (auth.py, knowledge helpers, inbox helpers) that has no
runtime dependency on those packages beyond import time. We inject minimal
stubs into sys.modules before any mcp_brain module is imported.
"""

import sys
import types
from unittest.mock import MagicMock


def _make_stub(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__path__ = []  # make it a package
    return mod


# ---------------------------------------------------------------------------
# Stub: mcp and its sub-modules used at import time

mcp_stub = _make_stub("mcp")
mcp_server = _make_stub("mcp.server")
mcp_server_auth = _make_stub("mcp.server.auth")
mcp_server_auth_provider = _make_stub("mcp.server.auth.provider")
mcp_server_auth_settings = _make_stub("mcp.server.auth.settings")
mcp_server_fastmcp = _make_stub("mcp.server.fastmcp")

# AccessToken and TokenVerifier stubs used in auth.py
class _AccessToken:
    def __init__(self, *, token: str, client_id: str, scopes: list):
        self.token = token
        self.client_id = client_id
        self.scopes = scopes

class _TokenVerifier:
    async def verify_token(self, token: str):
        raise NotImplementedError

class _FastMCP:
    def __init__(self, *args, **kwargs): pass
    def tool(self, *args, **kwargs):
        def decorator(fn): return fn
        return decorator

mcp_server_auth_provider.AccessToken = _AccessToken
mcp_server_auth_provider.TokenVerifier = _TokenVerifier
mcp_server_fastmcp.FastMCP = _FastMCP

# Wire up the hierarchy
mcp_server_auth.provider = mcp_server_auth_provider
mcp_server_auth.settings = mcp_server_auth_settings
mcp_server.auth = mcp_server_auth
mcp_server.fastmcp = mcp_server_fastmcp
mcp_stub.server = mcp_server

mcp_server_auth_middleware = _make_stub("mcp.server.auth.middleware")
mcp_server_auth_middleware_ctx = _make_stub("mcp.server.auth.middleware.auth_context")

# get_access_token stub: returns None (god-mode fallback in _perms)
mcp_server_auth_middleware_ctx.get_access_token = lambda: None

mcp_server_auth_middleware.auth_context = mcp_server_auth_middleware_ctx
mcp_server_auth.middleware = mcp_server_auth_middleware

for name, mod in [
    ("mcp", mcp_stub),
    ("mcp.server", mcp_server),
    ("mcp.server.auth", mcp_server_auth),
    ("mcp.server.auth.provider", mcp_server_auth_provider),
    ("mcp.server.auth.settings", mcp_server_auth_settings),
    ("mcp.server.auth.middleware", mcp_server_auth_middleware),
    ("mcp.server.auth.middleware.auth_context", mcp_server_auth_middleware_ctx),
    ("mcp.server.fastmcp", mcp_server_fastmcp),
]:
    sys.modules.setdefault(name, mod)

# ---------------------------------------------------------------------------
# Stub: pydantic  (auth.py uses BaseModel / Field)

try:
    import pydantic  # noqa: F401 — already installed, nothing to do
except ImportError:
    pydantic_stub = _make_stub("pydantic")

    class _BaseModel:
        """Minimal pydantic.BaseModel stub.

        Supports field defaults from class-level annotations and a
        model_validate classmethod that recursively instantiates nested
        _BaseModel subclasses from dicts.
        """
        _field_defaults: dict = {}
        _field_constraints: dict = {}

        def __init_subclass__(cls, **kwargs):
            super().__init_subclass__(**kwargs)
            # Collect defaults from Field() calls (which return the default)
            cls._field_defaults = {
                k: v for k, v in vars(cls).items()
                if not k.startswith("_") and not callable(v)
            }

        def __init__(self, **kwargs):
            # Gather constraints from class hierarchy
            constraints = {}
            for klass in type(self).__mro__:
                constraints.update(getattr(klass, '_field_constraints', {}))
            defaults = {}
            for klass in type(self).__mro__:
                defaults.update(getattr(klass, '_field_defaults', {}))
            for k, v in defaults.items():
                setattr(self, k, v)
            for k, v in kwargs.items():
                if k in constraints:
                    c = constraints[k]
                    if 'min_length' in c and isinstance(v, str) and len(v) < c['min_length']:
                        raise ValueError(
                            f"Field '{k}' value {v!r} is shorter than min_length={c['min_length']}"
                        )
                setattr(self, k, v)

        @classmethod
        def model_validate(cls, data: dict):
            import typing
            import sys as _sys

            # Use get_type_hints to resolve string annotations (from __future__ annotations)
            try:
                # Look up the module where the class is defined to provide correct globals
                module = _sys.modules.get(cls.__module__, None)
                globalns = vars(module) if module else {}
                hints = typing.get_type_hints(cls, globalns=globalns)
            except Exception:
                hints = {}
                for klass in cls.__mro__:
                    hints.update(getattr(klass, '__annotations__', {}))

            kwargs = {}
            for k, v in data.items():
                hint = hints.get(k)
                if hint is not None and isinstance(v, list):
                    # Unwrap Optional[list[X]] → list[X]
                    origin = getattr(hint, '__origin__', None)
                    args = getattr(hint, '__args__', ())
                    # Handle Optional[list[X]] (Union[list[X], None])
                    if origin is typing.Union and args:
                        for a in args:
                            if getattr(a, '__origin__', None) is list:
                                hint = a
                                origin = list
                                args = getattr(a, '__args__', ())
                                break
                    if origin is list and args and isinstance(args[0], type) and issubclass(args[0], _BaseModel):
                        v = [args[0].model_validate(item) if isinstance(item, dict) else item for item in v]
                elif hint is not None and isinstance(v, dict):
                    # Unwrap Optional[SomeModel] → SomeModel
                    origin = getattr(hint, '__origin__', None)
                    args = getattr(hint, '__args__', ())
                    target = None
                    if origin is typing.Union and args:
                        for a in args:
                            if isinstance(a, type) and issubclass(a, _BaseModel):
                                target = a
                                break
                    elif isinstance(hint, type) and issubclass(hint, _BaseModel):
                        target = hint
                    if target is not None:
                        v = target.model_validate(v)
                kwargs[k] = v
            return cls(**kwargs)

    def _Field(default=None, *, default_factory=None, min_length=None, **kwargs):
        if default_factory is not None:
            return default_factory()
        # Store constraints on the caller's class (injected via __set_name__ workaround)
        # We smuggle min_length by returning a _FieldDescriptor instead
        if min_length is not None:
            return _FieldDescriptor(default=default, min_length=min_length)
        return default

    class _FieldDescriptor:
        """Carries field constraints so _BaseModel.__init__ can enforce them."""

        def __init__(self, *, default, min_length=None):
            self._default = default
            self._min_length = min_length
            self._name: str | None = None

        def __set_name__(self, owner, name):
            self._name = name
            if self._min_length is not None:
                if not hasattr(owner, '_field_constraints'):
                    owner._field_constraints = {}
                owner._field_constraints[name] = {'min_length': self._min_length}
            # Replace the descriptor with its plain default so _field_defaults works
            setattr(owner, name, self._default)

        # Fallback: if __set_name__ is never called (e.g. used in older Python),
        # behave like the plain default value.
        def __repr__(self):  # pragma: no cover
            return repr(self._default)

    pydantic_stub.BaseModel = _BaseModel
    pydantic_stub.Field = _Field
    sys.modules.setdefault("pydantic", pydantic_stub)
