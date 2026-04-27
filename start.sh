#!/bin/bash
# Install Playwright browsers only (system deps should be in Dockerfile)
python -m playwright install chromium

# Start with Gunicorn (production WSGI server)
# Use PORT env var from Railway, default to 8000
gunicorn --bind 0.0.0.0:${PORT:-8000} --workers 1 --timeout 300 --access-logfile - app:app
