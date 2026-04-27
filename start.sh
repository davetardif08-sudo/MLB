#!/bin/bash
# Install Playwright browsers only (system deps should be in Dockerfile)
python -m playwright install chromium

# Start the Flask app
python app.py
