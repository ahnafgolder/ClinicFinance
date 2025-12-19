# WSGI entry point for PythonAnywhere
# This file is used by PythonAnywhere to run your Flask app

import sys
import os

# Add your project directory to the sys.path
# Replace 'yourusername' with your actual PythonAnywhere username
project_home = '/home/yourusername/ClinicFinance'
if project_home not in sys.path:
    sys.path.insert(0, project_home)

# Set environment variables (PythonAnywhere doesn't read .env automatically)
# You'll set these in PythonAnywhere's web app configuration
os.environ.setdefault('DATABASE_URL', 'your-supabase-url-here')
os.environ.setdefault('SECRET_KEY', 'your-secret-key-here')

# Import and run the Flask app
from app import app as application

