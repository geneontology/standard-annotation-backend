"""FastAPI application entry point."""

from fastapi import FastAPI

from standard_annotation_backend.logging import configure_logging

configure_logging()

app = FastAPI(title="Standard Annotation Backend")


@app.get("/health")
def health() -> dict[str, str]:
    """Return the process health status without requiring dependencies."""
    return {"status": "ok"}
