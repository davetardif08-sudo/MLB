#!/bin/bash
# Install Playwright browsers AND system dependencies
python -m playwright install
python -m playwright install-deps

# Start the Flask app
python app.py
