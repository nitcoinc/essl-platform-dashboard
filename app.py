from __future__ import annotations

import os

from essl_hub.web import create_app


app = create_app()


if __name__ == "__main__":
    app.run(
        host=os.getenv("ESSL_HOST", "127.0.0.1"),
        port=int(os.getenv("ESSL_PORT", "5000")),
        debug=False,
        threaded=True,
    )
