"""AWS API Gateway adapter for the existing FastAPI application."""

from mangum import Mangum

from app.main import app


handler = Mangum(app)
