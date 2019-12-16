from typing import Optional

import psycopg2

class Session:
    cr: psycopg2.extensions.cursor  # actually an Odoo cursor but close enough
    def open(self, db: Optional[str] = None, with_demo: bool = False) -> None: ...
