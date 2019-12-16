from typing import Optional

from . import extensions

def connect(
    *,
    host: Optional[str],
    port: Optional[str],
    user: Optional[str],
    password: Optional[str]
) -> extensions.connection: ...

class Error(Exception): ...
class DatabaseError(Error): ...
class ProgrammingError(DatabaseError): ...
