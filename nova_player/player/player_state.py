from enum import Enum, auto

class PlayerState(Enum):
    IDLE        = auto()   # no file loaded
    LOADING     = auto()   # media opening
    PLAYING     = auto()
    PAUSED      = auto()
    STOPPED     = auto()
    ERROR       = auto()
