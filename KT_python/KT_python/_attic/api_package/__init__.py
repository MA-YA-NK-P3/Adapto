"""FastAPI HTTP layer — port of the *.api.* controllers, common.web.ApiKeyFilter and
common.error.GlobalExceptionHandler (docs/api-flow.md).

    from knowledge_tracer.api import create_app
    app = create_app()                      # sqlite kt_python.db next to the package
    app = create_app(database_url="sqlite://")   # in-memory, for tests
"""
from .app import create_app

__all__ = ["create_app"]
