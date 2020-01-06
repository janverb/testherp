from typing import Dict, Union

class _configmanager:
    options: Dict[str, Union[str, bool, int]]
    def __getitem__(self, key: str) -> Union[str, bool, int]: ...
    def __setitem__(self, key: str, value: Union[str, bool, int]) -> None: ...

config: _configmanager
