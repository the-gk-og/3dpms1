import os

from app import create_app

app = create_app()

if __name__ == '__main__':
    # Debug mode enables the Werkzeug interactive debugger, which allows arbitrary
    # code execution from the browser if this ever becomes reachable from outside
    # localhost. Off by default; opt in explicitly for local development only.
    app.run(debug=os.environ.get('FLASK_DEBUG') == '1')
