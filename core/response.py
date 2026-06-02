from typing import Generic, TypeVar, Optional

from pydantic import BaseModel

T = TypeVar("T")


class APIResponse(BaseModel, Generic[T]):
    code: int = 200
    message: str = "ok"
    detail: Optional[T] = None
