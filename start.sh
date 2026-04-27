#!/bin/bash
# Install Playwright browsers only (system deps should be in Dockerfile)
python -m playwright install chromium

# Start with Gunicorn (production WSGI server)
gunicorn --bind 0.0.0.0:8000 --workers 1 --timeout 300 --access-logfile - app:app
