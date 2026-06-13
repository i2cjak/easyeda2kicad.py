from __future__ import annotations

def create_app():
    from flask import Flask

    app = Flask(__name__)
    app.config.update(
        JSON_SORT_KEYS=False,
        MAX_CONTENT_LENGTH=24 * 1024 * 1024,
    )

    from .routes import bp

    app.register_blueprint(bp)
    return app
