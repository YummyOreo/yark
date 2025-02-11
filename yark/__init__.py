"""
Yark
====

YouTube archiving made simple.

Commonly-used
-------------

- `Archive`
    - `Config`
- `Video`
    - `Element`
    - `Note`
    - `Comment`
    - `CommentAuthor`
    - `Image`
- `viewer()`

Exceptions
----------
- `ArchiveNotFoundException`
- `ArchiveStructureException`
- `VideoNotFoundException`
- `NoteNotFoundException`
- `TimestampException`
- `FileNotFoundException`

Beware that using Yark as a library is currently experimental and breaking changes here are not tracked!
"""

from .archive import Archive
from .config import Config
from .video import (
    Video,
    Element,
    Note,
    Comment,
    CommentAuthor,
    Image,
)
from .viewer import viewer
from .errors import (
    ArchiveNotFoundException,
    ArchiveStructureException,
    VideoNotFoundException,
    NoteNotFoundException,
    TimestampException,
    ConversionException,
)
