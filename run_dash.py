import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('HERMES_DASHBOARD_PROFILE', 'tecnico')
os.environ.setdefault('HERMES_DASHBOARD_PORT', '8770')
import app
app.main()
